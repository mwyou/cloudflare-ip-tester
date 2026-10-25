"""CFST based DNS maintenance. No credentials are written to status or CFST processes."""
from __future__ import annotations

import argparse
import csv
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import queue
import random
import re
import secrets
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from cloudflare import API, CF_NETWORKS, NoRedirect, is_cloudflare_ip, validate_domain, validate_url
from cfst_bundle import BUNDLED_DIR, VERSION, InstallError, bundled_release

LOG = logging.getLogger("cfst-dns")
ROOT = Path(__file__).resolve().parent
MIN_INTERVAL_SECONDS = 60
DEFAULT_RECORD_COUNT = 3
MIN_RECORD_COUNT = 1
MAX_RECORD_COUNT = 10
DEFAULT_HISTORY_COUNT = 2
MIN_HISTORY_COUNT = 0
MAX_HISTORY_COUNT = 10
MIN_JITTER_SECONDS = 1
MAX_JITTER_SECONDS = 10000
ACTIONS = ("read", "check", "select", "compare", "write")
SETTING_FIELDS = {"interval_seconds", "jitter_min_seconds", "jitter_max_seconds",
                  "max_latency_ms", "min_bandwidth_mbps", "dns_record_count", "dns_history_count"}
STATUS_SECTIONS = {
    "settings": SETTING_FIELDS,
    "dns": {"dns_ips", "dns_history", "dns_history_count", "last_dns_update"},
    "check": {"check_results", "check_time", "single_results", "max_latency_ms", "min_bandwidth_mbps"},
    "selection": {"selection_results", "selection_existing_ips", "selection_candidate_count", "selection_time",
                  "single_results", "max_latency_ms", "min_bandwidth_mbps"},
    "ranked": {"ranked_results", "ranked_time", "proposed_ips", "proposal_applied", "single_results", "max_latency_ms", "min_bandwidth_mbps"},
    "events": {"events"},
}
SUMMARY_EXCLUDED = {"results", "check_results", "selection_results", "ranked_results", "single_results",
                    "dns_ips", "dns_history", "selection_existing_ips", "proposed_ips", "events"}


def valid_jitter(minimum, maximum):
    return (type(minimum) is int and type(maximum) is int and
            ((minimum == maximum == 0) or
             MIN_JITTER_SECONDS <= minimum <= maximum <= MAX_JITTER_SECONDS))


def valid_thresholds(latency_ms, min_mbps):
    return (type(latency_ms) in (int, float) and type(min_mbps) in (int, float) and
            1 <= latency_ms <= 2000 and 0.1 <= min_mbps <= 10000)


def secret_value(name: str) -> str:
    value, filename = os.environ.get(name, "").strip(), os.environ.get(name + "_FILE", "").strip()
    if value and filename:
        raise ValueError(f"{name} 与 {name}_FILE 只能设置一个")
    return Path(filename).read_text(encoding="utf-8").strip() if filename else value


def number(name, default, lower, upper, integer=False):
    try:
        value = float(os.environ.get(name, default))
        if not math.isfinite(value) or not lower <= value <= upper or (integer and int(value) != value):
            raise ValueError()
        return int(value) if integer else value
    except ValueError:
        raise ValueError(f"{name} 必须在 {lower} 到 {upper} 之间") from None


@dataclass(frozen=True)
class Settings:
    domain: str
    token: str = field(default="", repr=False)
    api_key: str = field(default="", repr=False)
    email: str = field(default="", repr=False)
    zone_id: str = ""
    kind: str = "A"
    latency_ms: float = 100
    min_mbps: float = 30
    interval: int = 3600
    # IPv6 sampling only. IPv4 CIDRs are expanded by CFST itself.
    candidates: int = 128
    download_count: int = 10
    download_seconds: int = 10
    threads: int = 200
    test_url: str = "https://cf.xiu2.xyz/url"
    binary: str = str(BUNDLED_DIR / "cfst")
    candidate_file: str = ""
    data_dir: Path = Path("/data")
    ttl: int = 60
    cfst_version: str = VERSION

    @classmethod
    def from_env(cls):
        domain = validate_domain(os.environ.get("CF_DOMAIN", ""))
        token, key, email = secret_value("CF_API_TOKEN"), secret_value("CF_API_KEY"), os.environ.get("CF_EMAIL", "").strip()
        if bool(token) == bool(key) or (key and not email):
            raise ValueError("请设置 CF_API_TOKEN；或仅设置 CF_API_KEY 和 CF_EMAIL")
        if any(ord(c) < 32 for c in token + key + email):
            raise ValueError("Cloudflare 凭据包含无效字符")
        zone = os.environ.get("CF_ZONE_ID", "").strip()
        if zone and not re.fullmatch(r"[a-fA-F0-9]{32}", zone):
            raise ValueError("CF_ZONE_ID 应为 32 位十六进制字符")
        kind = os.environ.get("RECORD_TYPE", "A").upper()
        if kind not in ("A", "AAAA"):
            raise ValueError("RECORD_TYPE 仅支持 A 或 AAAA")
        return cls(domain, token, key, email, zone, kind,
                   number("MAX_LATENCY_MS", 100, 1, 2000), number("MIN_BANDWIDTH_MBPS", 30, 0.1, 10000),
                   number("CHECK_INTERVAL_SECONDS", 3600, MIN_INTERVAL_SECONDS, 86400, True),
                   number("CANDIDATE_COUNT", 128, 3, 512, True),
                   number("DOWNLOAD_COUNT", 10, 3, 50, True),
                   number("DOWNLOAD_SECONDS", 10, 5, 30, True), number("CFST_THREADS", 200, 1, 1000, True),
                   validate_url(os.environ.get("CFST_URL", cls.test_url)),
                   cls.binary, os.environ.get("CANDIDATE_FILE", ""),
                   Path(os.environ.get("DATA_DIR", "/data")), number("DNS_TTL", 60, 60, 86400, True),
                   VERSION)


class ServiceError(RuntimeError):
    pass


class RateLimited(ServiceError):
    pass


class CloudflareClient:
    def __init__(self, settings):
        self.settings = settings
        self.zone = settings.zone_id
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path, params=None, body=None):
        headers = {"Accept": "application/json", "User-Agent": "CFIPDesk-Auto/3.0"}
        if self.settings.token:
            headers["Authorization"] = "Bearer " + self.settings.token
        else:
            headers.update({"X-Auth-Key": self.settings.api_key, "X-Auth-Email": self.settings.email})
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(API + path + ("?" + urlencode(params) if params else ""),
                          data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with self.opener.open(request, timeout=20) as response:
                data = json.loads(response.read(4_000_000))
        except HTTPError as exc:
            if exc.code == 429:
                raise RateLimited("Cloudflare API 限流；保留 DNS，等待下个检查周期") from None
            raise ServiceError(f"Cloudflare API HTTP {exc.code}；请检查 Zone 和 DNS 读写权限") from None
        except (URLError, OSError, ValueError):
            raise ServiceError("Cloudflare API 连接失败或响应无效；请求结果未确认") from None
        if not isinstance(data, dict) or not data.get("success"):
            raise ServiceError("Cloudflare API 拒绝请求；请检查凭据及 DNS Edit 权限")
        return data

    def zone_id(self):
        if self.zone:
            return self.zone
        labels = self.settings.domain.split(".")
        for offset in range(len(labels) - 1):
            name = ".".join(labels[offset:])
            for zone in self.request("/zones", {"name": name, "per_page": 50}).get("result", []):
                if zone.get("name") == name:
                    self.zone = zone["id"]
                    return self.zone
        raise ServiceError("没有找到域名所属 Zone；请填写 CF_ZONE_ID 或授予 Zone Read")

    def records(self):
        records = []
        for page in range(1, 12):
            data = self.request(f"/zones/{self.zone_id()}/dns_records",
                                {"name": self.settings.domain, "per_page": 100, "page": page})
            records.extend(r for r in data.get("result", [])
                           if r.get("name", "").lower().rstrip(".") == self.settings.domain)
            if page >= data.get("result_info", {}).get("total_pages", 1):
                return records
        raise ServiceError("目标域名记录过多；未取得完整快照，不修改 DNS")

    def replace(self, before, ips):
        # A last read catches edits made while a selection run was downloading.
        current = self.records()
        if fingerprint(current) != fingerprint(before):
            raise ServiceError("写入前 DNS 再次发生变化；本次未覆盖，可重试写入")
        plan = dns_plan(self.settings, current, ips, len(ips))
        if any(plan.values()):
            self.request(f"/zones/{self.zone_id()}/dns_records/batch", body=plan)
        # A write timeout is ambiguous: never retry a mutation blindly.
        after = self.records()
        managed = [r for r in after if r.get("type") == self.settings.kind]
        if sorted(r.get("content") for r in managed) != sorted(ips) or any(r.get("proxied") or r.get("ttl") != self.settings.ttl for r in managed):
            raise ServiceError("写入后核验未通过；保留排序结果，重试前会重新读取 DNS")
        return after


def fingerprint(records):
    return json.dumps(sorted(records, key=lambda r: r.get("id", "")), sort_keys=True)


def dns_plan(settings, records, ips, record_count=DEFAULT_RECORD_COUNT):
    ips = [str(ipaddress.ip_address(ip)) for ip in ips]
    if len(set(ips)) != record_count or len(ips) != record_count or not all(
            is_cloudflare_ip(ip) and ipaddress.ip_address(ip).version == (4 if settings.kind == "A" else 6) for ip in ips):
        raise ServiceError(f"只能写入 {record_count} 个去重且属于 Cloudflare 的 IP")
    if any(r.get("type") in ("CNAME", "NS") for r in records):
        raise ServiceError("目标域名存在 CNAME / NS 冲突；请先使用独立的优选域名")
    pending = set(ips)
    plan = {"deletes": [], "patches": [], "posts": []}
    for record in records:
        if record.get("type") != settings.kind:
            continue
        if record.get("content") in pending:
            pending.remove(record["content"])
            if record.get("proxied") or record.get("ttl") != settings.ttl:
                plan["patches"].append({"id": record["id"], "proxied": False, "ttl": settings.ttl})
        else:
            plan["deletes"].append({"id": record["id"]})
    for ip in ips:
        if ip in pending:
            plan["posts"].append({"name": settings.domain, "type": settings.kind, "content": ip,
                                  "ttl": settings.ttl, "proxied": False, "comment": "Managed by CFIPDesk CFST"})
    return plan


def _allowed_matcher(allowed):
    addresses, networks = set(), []
    for value in allowed:
        try:
            addresses.add(str(ipaddress.ip_address(value)))
            continue
        except ValueError:
            pass
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            raise ServiceError("CFST 候选来源含无效 IP 或网段") from None
    return lambda ip: ip in addresses or any(ipaddress.ip_address(ip) in network for network in networks)


def parse_csv(path, allowed):
    rows = []
    is_allowed = _allowed_matcher(allowed)
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        header = next(reader, [])
        if header[:6] != ["IP 地址", "已发送", "已接收", "丢包率", "平均延迟", "下载速度(MB/s)"]:
            raise ServiceError("CFST CSV 格式与测速接口不兼容")
        seen = set()
        for cells in reader:
            try:
                ip = str(ipaddress.ip_address(cells[0]))
                sent, received = int(cells[1]), int(cells[2])
                loss, latency, speed = map(float, cells[3:6])
                if (not is_allowed(ip) or ip in seen or not 0 < received <= sent or
                        not all(math.isfinite(n) for n in (loss, latency, speed)) or
                        not 0 <= loss <= 1 or latency < 0 or speed < 0):
                    raise ValueError()
            except (ValueError, IndexError):
                raise ServiceError("CFST 输出含无效结果；本次不修改 DNS") from None
            seen.add(ip)
            rows.append({"ip": ip, "sent": sent, "received": received, "loss": loss,
                         "latency_ms": latency, "speed_mibps": speed,
                         "bandwidth_mbps": round(speed * 1048576 * 8 / 1_000_000, 3),
                         "colo": cells[6] if len(cells) > 6 else ""})
    return sorted(rows, key=lambda r: (-r["speed_mibps"], r["latency_ms"]))


def qualifies(settings, row):
    return row["latency_ms"] <= settings.latency_ms and row["speed_mibps"] * 1048576 * 8 / 1_000_000 > settings.min_mbps


class CFSTRunner:
    def __init__(self, settings, stop):
        self.settings, self.stop = settings, stop

    def run(self, ips, download_count):
        if not ips or not all(_valid_source(source, self.settings.kind) for source in ips):
            raise ServiceError("测速候选必须为 Cloudflare IP 或网段")
        cfg = self.settings
        with tempfile.TemporaryDirectory(prefix="cfst-", dir=cfg.data_dir) as folder:
            source, result = Path(folder) / "ips.txt", Path(folder) / "result.csv"
            source.write_text("\n".join(ips) + "\n", encoding="ascii")
            args = [cfg.binary, "-f", str(source), "-o", str(result), "-url", cfg.test_url,
                    "-n", str(cfg.threads), "-t", "4", "-tp", str(urlsplit(cfg.test_url).port or (443 if cfg.test_url.startswith("https:") else 80)),
                    "-dn", str(min(download_count, candidate_count(ips))), "-dt", str(cfg.download_seconds),
                    # Match CFST's normal selection: test all CIDR-derived
                    # candidates, then return the requested download results.
                    # The service applies its 100 ms / 30 Mbps monitor rule
                    # separately; it must not narrow CFST's output here.
                    "-tl", "9999", "-sl", "0", "-p", str(min(download_count, candidate_count(ips))), "-debug"]
            # CFST is a child process without API credentials or environment proxies.
            env = {k: v for k, v in os.environ.items() if k.upper() in
                   ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "SSL_CERT_FILE", "SSL_CERT_DIR", "LANG", "LC_ALL")}
            try:
                process = subprocess.Popen(args, cwd=folder, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            except OSError:
                raise ServiceError("内置 CFST 无法启动；请检查容器日志或重新拉取镜像") from None
            lines = queue.Queue()

            def collect():
                try:
                    for line in process.stdout:
                        lines.put(line)
                finally:
                    lines.put(None)

            reader = threading.Thread(target=collect, daemon=True)
            reader.start()
            deadline = time.monotonic() + 60 + math.ceil(candidate_count(ips) / cfg.threads) * 6 + min(download_count, candidate_count(ips)) * (cfg.download_seconds + 3)
            try:
                ended = False
                while not ended:
                    if self.stop.is_set():
                        raise ServiceError("服务正在停止，取消本次测速")
                    if time.monotonic() > deadline:
                        raise ServiceError("CFST 运行超时；保留原 DNS")
                    try:
                        line = lines.get(timeout=.1)
                    except queue.Empty:
                        continue
                    if line is None:
                        ended = True
                    elif re.search(r"(?:状态码|status(?:\s+code)?)[：:\s]+429\b", line, re.I):
                        raise RateLimited("测速源返回 429；已停止 CFST，保留 DNS，等待下个周期")
                    else:
                        for marker in ("CloudflareSpeedTest", "开始延迟测速", "开始下载测速", "[调试]", "[信息]"):
                            if marker in line:
                                # CFST redraws progress with backspaces on the
                                # same line; keep only the diagnostic suffix.
                                LOG.info("CFST: %s", line[line.rfind(marker):].strip()[:2000])
                                break
                code = process.wait(timeout=3)
                if code != 0 or not result.is_file():
                    raise ServiceError("CFST 未产生有效结果；保留原 DNS")
                return parse_csv(result, ips)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                reader.join(timeout=2)
                process.stdout.close()


def _valid_source(value, kind):
    version = 4 if kind == "A" else 6
    try:
        address = ipaddress.ip_address(value)
        return address.version == version and is_cloudflare_ip(value)
    except ValueError:
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError:
            return False
        if network.version != version:
            return False
        # CFST's IPv6 expansion is random and unbounded for large prefixes.
        if version == 6 and network.prefixlen < 112:
            return False
        end, cursor = int(network.broadcast_address), int(network.network_address)
        for allowed in sorted((net for net in CF_NETWORKS if net.version == version), key=lambda net: int(net.network_address)):
            left, right = int(allowed.network_address), int(allowed.broadcast_address)
            if right < cursor:
                continue
            if left > cursor:
                break
            cursor = max(cursor, right + 1)
            if cursor > end:
                return True
        return False


def candidate_count(sources):
    count = 0
    for source in sources:
        try:
            ipaddress.ip_address(source)
        except ValueError:
            network = ipaddress.ip_network(source, strict=False)
            count += max(1, network.num_addresses // (256 if network.version == 4 else 65536))
        else:
            count += 1
    return count


def candidate_pool(settings, existing):
    version = 4 if settings.kind == "A" else 6
    if settings.candidate_file:
        pool = []
        for line in Path(settings.candidate_file).read_text(encoding="utf-8-sig").splitlines():
            value = line.split("#", 1)[0].strip()
            if not value:
                continue
            if not _valid_source(value, settings.kind):
                raise ServiceError("候选文件包含不属于所选 Cloudflare 地址族的 IP 或网段")
            try:
                value = str(ipaddress.ip_address(value))
            except ValueError:
                value = str(ipaddress.ip_network(value, strict=False))
            pool.append(value)
        pool = list(dict.fromkeys(pool))
        if len(pool) > 512:
            raise ServiceError("候选文件最多支持 512 个 IP 或网段")
        # A user-specified file is authoritative, including removal of old IPs.
        if not pool or candidate_count(pool) < 3:
            raise ServiceError("候选文件至少需要三个可测速候选 IP")
        return pool
    if version == 6:
        # Preserve the bounded IPv6 selection; large IPv6 CIDRs cannot be
        # passed directly to CFST's address expansion.
        networks = [net for net in CF_NETWORKS if net.version == 6]
        pool = set()
        while len(pool) < settings.candidates:
            net = secrets.choice(networks)
            pool.add(str(net.network_address + 1 + secrets.randbelow(net.num_addresses - 2)))
        return sorted(pool)
    # Pass the complete official CIDR list to CFST. CFST itself expands each
    # range to one candidate per /24 and performs the latency/download stages.
    # Sampling concrete addresses here changes CFST's selection algorithm.
    return [str(net) for net in CF_NETWORKS if net.version == version]


class Status:
    def __init__(self, path, settings):
        self.path, self.lock = path, threading.RLock()
        self.instance = secrets.token_hex(8)
        self.revisions = dict.fromkeys(STATUS_SECTIONS, 0)
        self.data = {"domain": settings.domain, "record_type": settings.kind, "engine": "CFST " + settings.cfst_version,
                     "max_latency_ms": settings.latency_ms, "min_bandwidth_mbps": settings.min_mbps,
                     "interval_seconds": settings.interval, "phase": "starting", "message": "等待首次检查",
                     "dns_record_count": DEFAULT_RECORD_COUNT,
                     "dns_history_count": DEFAULT_HISTORY_COUNT,
                     "jitter_min_seconds": 0, "jitter_max_seconds": 0,
                     "last_run": None, "next_run": None, "last_dns_update": None,
                     "dns_ips": [], "dns_history": [],
                     "results": [], "check_results": [], "selection_results": [], "ranked_results": [],
                     "check_time": None, "selection_time": None, "ranked_time": None,
                     "selection_candidate_count": None, "selection_existing_ips": [],
                     "events": [], "steps": {}, "proposed_ips": [], "proposal_applied": False,
                     "manual_enabled": False, "busy": False, "active_action": None, "active_ip": None,
                     "single_results": {}}
        if path and path.is_file():
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
                if previous.get("domain") == settings.domain and previous.get("record_type") == settings.kind:
                    for key in ("events", "last_dns_update", "results", "check_results", "selection_results",
                                "ranked_results", "check_time", "selection_time", "ranked_time",
                                "selection_candidate_count", "selection_existing_ips",
                                "dns_ips", "dns_history", "dns_history_count", "dns_record_count", "interval_seconds", "single_results",
                                "max_latency_ms", "min_bandwidth_mbps",
                                "jitter_min_seconds", "jitter_max_seconds"):
                        self.data[key] = previous.get(key, self.data[key])
                    # Migrate the previous single-change display once. Only
                    # snapshots before a successful change belong in history.
                    if "dns_history" not in previous and previous.get("dns_change"):
                        change = previous["dns_change"]
                        self.data["dns_history"] = [{"ips": change["before"], "time": change["time"]}]
                    count = self.data["dns_record_count"]
                    if type(count) is not int or not MIN_RECORD_COUNT <= count <= MAX_RECORD_COUNT:
                        self.data["dns_record_count"] = DEFAULT_RECORD_COUNT
                    history_count = self.data["dns_history_count"]
                    if type(history_count) is not int or not MIN_HISTORY_COUNT <= history_count <= MAX_HISTORY_COUNT:
                        history_count = DEFAULT_HISTORY_COUNT
                    self.data["dns_history_count"] = history_count
                    self.data["dns_history"] = self.data["dns_history"][:self.data["dns_history_count"]]
                    jitter_min, jitter_max = self.data["jitter_min_seconds"], self.data["jitter_max_seconds"]
                    if not valid_jitter(jitter_min, jitter_max):
                        jitter_min = jitter_max = 0
                    self.data["jitter_min_seconds"] = jitter_min
                    self.data["jitter_max_seconds"] = jitter_max
                    interval = self.data["interval_seconds"]
                    if type(interval) is not int or not MIN_INTERVAL_SECONDS <= interval <= 86400:
                        self.data["interval_seconds"] = settings.interval
                    if not valid_thresholds(self.data["max_latency_ms"], self.data["min_bandwidth_mbps"]):
                        self.data.update(max_latency_ms=settings.latency_ms, min_bandwidth_mbps=settings.min_mbps)
            except (ValueError, OSError):
                pass

    def update(self, **values):
        with self.lock:
            changed = {key for key, value in values.items() if self.data.get(key) != value}
            if "message" in values:
                changed.add("events")
            for section, fields in STATUS_SECTIONS.items():
                if changed & fields:
                    self.revisions[section] += 1
            self.data.update(values)
            if "message" in values:
                event = {"time": time.time(), "message": values["message"]}
                self.data["events"] = (self.data["events"] + [event])[-30:]
                LOG.info(values["message"])
            self.data["updated"] = time.time()
            if self.path:
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(self.path)

    def snapshot(self, summary=False):
        with self.lock:
            data = {key: value for key, value in self.data.items() if not summary or key not in SUMMARY_EXCLUDED}
            data["revisions"] = {key: f"{self.instance}:{value}" for key, value in self.revisions.items()}
            return json.loads(json.dumps(data))


class Maintainer:
    def __init__(self, settings, client, runner, status, stop, dry_run=False):
        self.cfg, self.client, self.runner, self.status, self.stop = settings, client, runner, status, stop
        self.dry_run = dry_run
        self.records = None
        # Displayed samples and execution inputs survive reads and failed tasks.
        state = status.snapshot()
        self.check_rows = self.restore_rows(state["check_results"])
        self.selection_rows = self.restore_rows(state["selection_results"])
        self.ranked, self.final = [], []
        self.ranking_started = state["ranked_time"] is not None
        self.record_count = None  # The first DNS read still adopts its real count.
        status.update(check_results=self.check_rows, selection_results=self.selection_rows,
                      ranked_results=[], results=[], proposed_ips=[], proposal_applied=False)
        if self.ranking_started:
            self.rebuild_ranking()

    def restore_rows(self, rows):
        restored = {}
        for row in rows if isinstance(rows, list) else []:
            try:
                ip = str(ipaddress.ip_address(row["ip"]))
                latency, speed = row["latency_ms"], row["speed_mibps"]
                if not _valid_source(ip, self.cfg.kind) or not all(
                        type(n) in (int, float) and math.isfinite(n) and n >= 0 for n in (latency, speed)):
                    continue
                restored[ip] = {**row, "ip": ip, "bandwidth_mbps": round(speed * 1048576 * 8 / 1_000_000, 3)}
            except (TypeError, KeyError, ValueError):
                continue
        return list(restored.values())

    def target_count(self):
        return self.record_count or self.status.snapshot()["dns_record_count"]

    def proposal_matches(self, records):
        managed = [r for r in records if r.get("type") == self.cfg.kind]
        return bool(self.final) and sorted(r.get("content", "") for r in managed) == sorted(r["ip"] for r in self.final) and all(
            not r.get("proxied") and r.get("ttl") == self.cfg.ttl for r in managed)

    def valid_ips(self):
        return list(dict.fromkeys(str(ipaddress.ip_address(r["content"])) for r in (self.records or []) if
            r.get("type") == self.cfg.kind and is_cloudflare_ip(r.get("content", "")) and
            ipaddress.ip_address(r["content"]).version == (4 if self.cfg.kind == "A" else 6)))

    def actions(self):
        reasons = dict.fromkeys(ACTIONS)
        reasons["check"] = "先读取 DNS" if self.records is None else (None if self.valid_ips() else "DNS 中没有可测的 Cloudflare IP")
        reasons["compare"] = None if self.check_rows or self.selection_rows else "暂无测速结果，请先检测或优选 IP"
        target = self.target_count()
        reasons["write"] = ("先完成综合排序" if not self.ranking_started else
                            f"排序结果不足 {target} 个唯一 IP" if len(self.final) != target else None)
        if self.stop.is_set():
            return dict.fromkeys(ACTIONS, "服务已停止")
        return reasons

    def require_action(self, action):
        if action not in ACTIONS:
            raise ServiceError("未知步骤")
        if reason := self.actions()[action]:
            raise ServiceError(reason)

    def refresh_ranking(self):
        if self.ranking_started:
            self.rebuild_ranking()

    def complete(self, action, count):
        steps = self.status.snapshot()["steps"]
        steps[action] = {"time": time.time(), "count": count}
        self.status.update(steps=steps)

    def read(self):
        self.status.update(phase="reading", message="正在读取 DNS")
        records = self.client.records()
        if any(r.get("type") in ("CNAME", "NS") for r in records):
            raise ServiceError("目标域名存在 CNAME / NS 冲突；未修改 DNS")
        self.records = records
        ips = list(dict.fromkeys(r["content"] for r in records if r.get("type") == self.cfg.kind))
        if self.record_count is None:
            self.record_count = len(ips) or DEFAULT_RECORD_COUNT
        self.refresh_ranking()
        self.complete("read", len(ips))
        self.status.update(phase="read_done", dns_ips=ips, dns_record_count=self.record_count,
                           proposal_applied=self.proposal_matches(records),
                           message=f"已读取 {len(ips)} 个 DNS IP；目标保留 {self.record_count} 条")
        return ips

    def check(self):
        self.require_action("check")
        self.status.update(phase="checking", message="正在检测当前 IP")
        ips = self.valid_ips()
        rows = self.runner.run(ips, len(ips))
        measured = {row["ip"] for row in rows}
        rows += [{"ip": ip, "speed_mibps": 0, "bandwidth_mbps": 0, "latency_ms": self.cfg.latency_ms + 1,
                  "sent": 0, "received": 0, "loss": 1, "colo": "", "measured": False}
                 for ip in ips if ip not in measured]
        tested_at = time.time()
        rows = [{**row, "tested_at": tested_at} for row in rows]
        self.check_rows = rows
        good = sum(qualifies(self.cfg, row) for row in rows)
        managed = [r for r in self.records if r.get("type") == self.cfg.kind]
        target = self.record_count or DEFAULT_RECORD_COUNT
        healthy = (len(managed) == len(ips) == len(rows) == good == target and
                   {r["ip"] for r in rows} == set(ips) and not any(r.get("proxied") for r in managed))
        self.refresh_ranking()
        self.complete("check", good)
        self.status.update(results=rows, check_results=rows, check_time=tested_at, phase="healthy" if healthy else "check_done",
                           single_results={ip: value for ip, value in self.status.snapshot()["single_results"].items() if ip not in measured},
                           message=f"{target} 个 IP 均达标，保持现有 DNS" if healthy else f"检测完成：{good} / {len(ips)} 个 IP 达标")
        return healthy

    def require_test_ip(self, value):
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise ServiceError("IP 地址无效") from None
        version = 4 if self.cfg.kind == "A" else 6
        if address.version != version or not is_cloudflare_ip(str(address)):
            raise ServiceError("只能测速与 DNS 类型匹配的 Cloudflare IP")
        state = self.status.snapshot()
        known = set(state["dns_ips"])
        for snapshot in state["dns_history"]:
            known.update(snapshot["ips"])
        for field in ("check_results", "selection_results", "ranked_results"):
            known.update(row["ip"] for row in state[field])
        ip = str(address)
        if ip not in known:
            raise ServiceError("该 IP 已不在当前记录或测速结果中，请刷新页面")
        return ip

    def test_ip(self, value):
        ip = self.require_test_ip(value)
        state = self.status.snapshot()
        singles = state["single_results"]
        singles.pop(ip, None)
        self.status.update(phase="testing_ip", message=f"正在测速 {ip}")
        try:
            measured = self.runner.run([ip], 1)
        except Exception as error:
            singles[ip] = {"time": time.time(), "error": str(error) if isinstance(error, ServiceError) else "测速失败"}
            self.status.update(single_results=dict(list(singles.items())[-100:]))
            raise
        result = next((row for row in measured if row.get("ip") == ip), None)
        if result is None:
            result = {"ip": ip, "speed_mibps": 0, "bandwidth_mbps": 0,
                      "latency_ms": self.cfg.latency_ms + 1, "sent": 0, "received": 0,
                      "loss": 1, "colo": "", "measured": False}
        result = {**result, "measured": result.get("measured", True), "tested_at": time.time()}
        singles[ip] = {"time": result["tested_at"], "result": result}

        def updated(rows, sort=False):
            if rows is None:
                return None
            output = [{**row, **result} if row["ip"] == ip else dict(row) for row in rows]
            for row in output:
                row.pop("selected", None)
            if sort:
                output.sort(key=lambda row: (-row["speed_mibps"], row["latency_ms"]))
            return output

        # Update only rows already in each result set. A retest of a historical
        # IP must not insert it into the current DNS check or new-IP selection.
        self.check_rows = updated(self.check_rows)
        self.selection_rows = updated(self.selection_rows, True)
        values = {field: updated(state[field], field != "check_results")
                  for field in ("check_results", "selection_results", "ranked_results", "results")}
        steps = state["steps"]
        if "check" in steps:
            steps["check"]["count"] = sum(qualifies(self.cfg, row) for row in values["check_results"])
        self.status.update(**values, single_results=dict(list(singles.items())[-100:]), steps=steps,
                           phase="ip_test_done", message=(f"单 IP 测速完成：{ip}" if result["measured"]
                                   else f"单 IP 测速未取得有效结果：{ip}"))
        self.refresh_ranking()
        return result

    def select(self):
        self.require_action("select")
        self.status.update(phase="selecting", message="正在优选新 IP")
        pool = candidate_pool(self.cfg, self.valid_ips())
        LOG.info("CFST 优选来源：%s 行，IPv4 / 具体 IP 候选计数 %s，延迟线程 %s，下载测试上限 %s",
                 len(pool), candidate_count(pool), self.cfg.threads, self.cfg.download_count)
        rows = self.runner.run(pool, self.cfg.download_count)
        tested_at = time.time()
        rows = [{**row, "tested_at": tested_at} for row in rows]
        self.selection_rows = rows
        measured = {row["ip"] for row in rows}
        self.refresh_ranking()
        self.complete("select", len(rows))
        self.status.update(results=rows, selection_results=rows, selection_time=tested_at,
                           selection_existing_ips=self.valid_ips(),
                           selection_candidate_count=candidate_count(pool), phase="select_done",
                           single_results={ip: value for ip, value in self.status.snapshot()["single_results"].items() if ip not in measured},
                           message=f"优选完成：CFST 输出 {len(rows)} 条结果")
        return rows

    def rebuild_ranking(self):
        merged = {}
        for row in self.check_rows:
            merged[row["ip"]] = {**row, "source": "当前检测"}
        for row in self.selection_rows:
            merged.setdefault(row["ip"], {**row, "source": "新 IP 优选"})
        ranked = sorted(merged.values(), key=lambda r: (-r["speed_mibps"], r["latency_ms"]))
        target = self.target_count()
        self.ranked = ranked
        self.final = ranked[:target] if len(ranked) >= target else []
        for row in ranked:
            row["selected"] = row in self.final
        state = self.status.snapshot()
        steps = state["steps"]
        steps["compare"] = {"time": time.time(), "count": len(self.final)}
        proposed = [r["ip"] for r in self.final]
        if proposed != state["proposed_ips"]:
            steps.pop("write", None)
        self.status.update(results=ranked, ranked_results=ranked, ranked_time=time.time(),
                           proposed_ips=proposed, steps=steps,
                           proposal_applied=self.proposal_matches(self.records or []))
        return bool(self.final)

    def compare(self):
        self.require_action("compare")
        self.ranking_started = True
        ready = self.rebuild_ranking()
        self.status.update(phase="compare_done" if ready else "degraded", message=(
            f"综合排序完成，前 {self.target_count()} 名可写入" if ready else
            f"综合排序后只有 {len(self.ranked)} 个结果；保留现有 DNS"))
        return ready

    def write(self):
        self.require_action("write")
        selected = [r["ip"] for r in self.final]
        if self.dry_run:
            self.status.update(phase="dry_run", message=f"演练完成：拟写入 {', '.join(selected)}；未写入 DNS")
            return
        self.status.update(phase="updating", proposal_applied=False, message="正在读取最新 DNS 并同步排序结果")
        # Every attempt plans against a new snapshot, including retries after an
        # ambiguous timeout. replace() checks again before mutation and verifies
        # afterwards; an already-applied plan becomes a no-op, not duplicate posts.
        before = self.client.records()
        after = self.client.replace(before, selected)
        self.records = after
        self.complete("write", len(selected))
        now = time.time()
        old_ips = list(dict.fromkeys(r.get("content") for r in before if r.get("type") == self.cfg.kind))
        changes = {}
        if set(old_ips) != set(selected):
            state = self.status.snapshot()
            history_count = state.get("dns_history_count", DEFAULT_HISTORY_COUNT)
            changes["dns_history"] = ([{"ips": old_ips, "time": now}]
                                      + state["dns_history"])[:history_count]
        self.status.update(phase="updated", dns_ips=selected, last_dns_update=now, proposal_applied=True,
                           message=f"已写入并核验 {len(selected)} 个 IP", **changes)

    def cycle(self, initial=False):
        ips = self.read()
        managed = [r for r in self.records if r.get("type") == self.cfg.kind]
        target = self.record_count or DEFAULT_RECORD_COUNT
        current_dns_ready = (len(managed) == len(ips) == target and all(
                is_cloudflare_ip(ip) for ip in ips) and not any(r.get("proxied") for r in managed))
        if current_dns_ready:
            healthy = self.check()
            if healthy:
                return
        self.select()
        if self.compare():
            self.write()


class TaskController:
    """Reserve execution before acknowledging HTTP; share one lock with cron."""
    def __init__(self, service, scheduler_wakeup=None):
        self.service = service
        self.scheduler_wakeup = scheduler_wakeup if scheduler_wakeup is not None else threading.Event()
        self.next_check = None
        self.lock = threading.Lock()
        self.worker = None
        service.status.update(manual_enabled=True)
        state = service.status.snapshot()
        self.jitter_min_seconds = state["jitter_min_seconds"]
        self.jitter_max_seconds = state["jitter_max_seconds"]
        self.service.cfg = replace(service.cfg, interval=state["interval_seconds"],
                                   latency_ms=state["max_latency_ms"], min_mbps=state["min_bandwidth_mbps"])
        self.service.runner.settings = self.service.cfg

    def snapshot(self, summary=False):
        data = self.service.status.snapshot(summary=summary)
        data["actions"] = self.service.actions()
        if data["busy"]:
            data["actions"] = dict.fromkeys(ACTIONS, "任务运行中")
        return data

    def run_reserved(self, action, initial=False, ip=None):
        try:
            if action == "automatic":
                self.schedule_next()
                self.service.cycle(initial=initial)
            elif action == "test_ip":
                self.service.test_ip(ip)
            else:
                getattr(self.service, action)()
        except Exception as error:
            message = str(error) if isinstance(error, ServiceError) else "任务失败；请检查容器日志和文件配置"
            self.service.status.update(phase="error", message=message)
            if not isinstance(error, (ServiceError, OSError, ValueError)):
                LOG.error("任务异常：%s", type(error).__name__)
        finally:
            self.service.status.update(busy=False, active_action=None, active_ip=None)
            self.lock.release()

    def reserve(self, action, ip=None):
        if not self.lock.acquire(blocking=False):
            raise ServiceError("已有任务运行中，请稍后再试")
        try:
            if action == "test_ip":
                if self.service.stop.is_set():
                    raise ServiceError("服务已停止")
                ip = self.service.require_test_ip(ip)
            elif action != "automatic":
                self.service.require_action(action)
            elif self.service.stop.is_set():
                raise ServiceError("服务已停止")
            self.service.status.update(busy=True, active_action=action, active_ip=ip,
                                       **({"last_run": time.time()} if action != "test_ip" else {}))
        except Exception:
            self.lock.release()
            raise

    def start(self, action):
        self.reserve(action)
        try:
            self.worker = threading.Thread(target=self.run_reserved, args=(action,), daemon=True)
            self.worker.start()
        except Exception:
            self.service.status.update(busy=False, active_action=None, active_ip=None)
            self.lock.release()
            raise

    def automatic(self, initial=False):
        try:
            self.reserve("automatic")
        except ServiceError:
            return False
        self.run_reserved("automatic", initial)
        return True

    def schedule_next(self):
        now = time.time()
        delay = self.service.cfg.interval + self.jitter()
        self.next_check = time.monotonic() + delay
        self.service.status.update(last_run=now, next_run=now + delay)

    def start_test_ip(self, value):
        self.reserve("test_ip", value)
        try:
            self.worker = threading.Thread(target=self.run_reserved, args=("test_ip",),
                                           kwargs={"ip": value}, daemon=True)
            self.worker.start()
        except Exception:
            self.service.status.update(busy=False, active_action=None, active_ip=None)
            self.lock.release()
            raise

    def set_settings(self, **values):
        if not values or not values.keys() <= SETTING_FIELDS:
            raise ServiceError("运行设置参数无效")
        if not self.lock.acquire(blocking=False):
            raise ServiceError("已有任务运行中，请稍后再试")
        try:
            state = self.service.status.snapshot()
            settings = {key: state[key] for key in SETTING_FIELDS} | values
            interval = settings["interval_seconds"]
            minimum, maximum = settings["jitter_min_seconds"], settings["jitter_max_seconds"]
            latency, bandwidth = settings["max_latency_ms"], settings["min_bandwidth_mbps"]
            count, history_count = settings["dns_record_count"], settings["dns_history_count"]
            # Validate the whole form before mutating any runtime setting.
            if type(interval) is not int or not MIN_INTERVAL_SECONDS <= interval <= 86400:
                raise ServiceError("检查间隔必须在 60 到 86400 秒之间")
            if not valid_jitter(minimum, maximum):
                raise ServiceError("随机波动区间必须在 1 到 10000 秒之间，下限不能大于上限；关闭时两项均为 0")
            if not valid_thresholds(latency, bandwidth):
                raise ServiceError("延迟需为 1～2000 ms，带宽需为 0.1～10000 Mbps")
            if type(count) is not int or not MIN_RECORD_COUNT <= count <= MAX_RECORD_COUNT:
                raise ServiceError(f"DNS 记录数量必须在 {MIN_RECORD_COUNT} 到 {MAX_RECORD_COUNT} 条之间")
            if type(history_count) is not int or not MIN_HISTORY_COUNT <= history_count <= MAX_HISTORY_COUNT:
                raise ServiceError(f"旧记录份数必须在 {MIN_HISTORY_COUNT} 到 {MAX_HISTORY_COUNT} 份之间")
            schedule_changed = any(settings[key] != state[key] for key in
                                   ("interval_seconds", "jitter_min_seconds", "jitter_max_seconds"))
            cfg = replace(self.service.cfg, interval=interval, latency_ms=latency, min_mbps=bandwidth)
            steps = state["steps"]
            if "check" in steps:
                steps["check"]["count"] = sum(qualifies(cfg, row) for row in state["check_results"])
            update = settings | {"steps": steps, "dns_history": state["dns_history"][:history_count],
                                 "phase": "settings_saved", "message": "运行设置已保存"}
            deadline = self.next_check
            if schedule_changed:
                delay = interval + random.randint(minimum, maximum)
                deadline = time.monotonic() + delay
                update["next_run"] = time.time() + delay
            self.service.status.update(**update)
            self.service.cfg = cfg
            self.service.runner.settings = cfg
            # Unrelated setting saves must not prevent first-read count adoption.
            if "dns_record_count" in values:
                self.service.record_count = count
                if count != state["dns_record_count"]:
                    self.service.refresh_ranking()
            self.jitter_min_seconds, self.jitter_max_seconds = minimum, maximum
            self.next_check = deadline
            if schedule_changed:
                self.scheduler_wakeup.set()
        finally:
            self.lock.release()

    def set_interval(self, seconds):
        self.set_settings(interval_seconds=seconds)

    def set_schedule(self, interval, minimum, maximum):
        self.set_settings(interval_seconds=interval, jitter_min_seconds=minimum, jitter_max_seconds=maximum)

    def set_record_count(self, count):
        self.set_settings(dns_record_count=count)

    def set_history_count(self, count):
        self.set_settings(dns_history_count=count)

    def jitter(self):
        return random.randint(self.jitter_min_seconds, self.jitter_max_seconds)

    def set_jitter(self, minimum, maximum):
        self.set_settings(jitter_min_seconds=minimum, jitter_max_seconds=maximum)

    def set_thresholds(self, latency_ms, min_mbps):
        self.set_settings(max_latency_ms=latency_ms, min_bandwidth_mbps=min_mbps)

    def wait_next(self):
        with self.lock:
            if self.next_check is None:
                self.schedule_next()
        while not self.service.stop.is_set():
            with self.lock:
                self.scheduler_wakeup.clear()
                remaining = self.next_check - time.monotonic()
            if remaining <= 0:
                return
            self.scheduler_wakeup.wait(min(remaining, 0.5))


def make_dashboard(status, controller=None):
    class Dashboard(BaseHTTPRequestHandler):
        def valid_request(self, mutation=False):
            host = self.headers.get("Host", "")
            try:
                parsed = urlsplit("http://" + host)
                hostname = parsed.hostname or ""
                try:
                    ipaddress.ip_address(hostname)
                    valid_hostname = True
                except ValueError:
                    valid_hostname = (len(hostname) <= 253 and all(re.fullmatch(
                        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                        for label in hostname.rstrip(".").split(".")))
                valid = (len(self.headers.get_all("Host", [])) == 1 and valid_hostname
                         and re.fullmatch(r"(?:\[[0-9a-fA-F:.]+\]|[a-zA-Z0-9.-]+)(?::[0-9]{1,5})?", host)
                         and not parsed.username and not parsed.password and not parsed.path and not parsed.query
                         and not parsed.fragment and (parsed.port is None or 1 <= parsed.port <= 65535))
            except ValueError:
                valid = False
            if not valid:
                self.send_json(403, {"error": "访问地址格式无效"})
                return False
            origin = self.headers.get("Origin")
            if mutation and ((origin is not None and origin.lower() not in ("http://" + host.lower(), "https://" + host.lower())) or
                             len(self.headers.get_all("Origin", [])) > 1 or
                             self.headers.get("Sec-Fetch-Site") == "cross-site" or
                             self.headers.get("X-Local-Client") != "cfst-dashboard"):
                self.send_json(403, {"error": "请从管理页面启动步骤"})
                return False
            return True

        def send_body(self, code, body, mime):
            # Consume small rejected POST bodies before closing, so clients receive
            # the HTTP error instead of a TCP reset on systems such as Windows.
            if code >= 400 and self.command == "POST" and not getattr(self, "body_read", False):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if 0 < length <= 1024 and not self.headers.get("Transfer-Encoding"):
                        self.connection.settimeout(0.5)
                        self.rfile.read(length)
                except (ValueError, OSError):
                    pass
            self.send_response(code)
            for name, value in {"Content-Type": mime, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                                "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}.items():
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def send_json(self, code, data):
            self.send_body(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def do_POST(self):
            if not self.valid_request(mutation=True):
                return
            if self.path not in ("/api/actions", "/api/settings", "/api/test-ip"):
                self.send_json(404, {"error": "接口不存在"})
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                self.send_json(415, {"error": "需要 JSON 请求"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 or self.headers.get("Transfer-Encoding"):
                    raise ValueError()
                self.connection.settimeout(5)
                body = self.rfile.read(length)
                self.body_read = True
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError()
                if self.path == "/api/actions":
                    if set(data) != {"action"} or data["action"] not in ACTIONS:
                        raise ValueError()
                elif self.path == "/api/test-ip":
                    if set(data) != {"ip"} or not isinstance(data["ip"], str):
                        raise ValueError()
                elif self.path == "/api/settings":
                    groups = ({"interval_seconds"}, {"dns_record_count"}, {"dns_history_count"},
                              {"max_latency_ms", "min_bandwidth_mbps"},
                              {"jitter_min_seconds", "jitter_max_seconds"}, SETTING_FIELDS)
                    if set(data) not in groups or any(type(value) not in
                            ((int, float) if key in {"max_latency_ms", "min_bandwidth_mbps"} else (int,))
                            for key, value in data.items()):
                        raise ValueError()
            except (ValueError, TypeError, OSError):
                self.send_json(400, {"error": "步骤参数无效"})
                return
            if controller is None:
                self.send_json(503, {"error": "预览模式，请先配置部署"})
                return
            try:
                if self.path == "/api/settings":
                    controller.set_settings(**data)
                elif self.path == "/api/test-ip":
                    controller.start_test_ip(data["ip"])
                else:
                    controller.start(data["action"])
            except ServiceError as exc:
                self.send_json(409, {"error": str(exc)})
                return
            self.send_json(202, {"accepted": "test_ip" if self.path == "/api/test-ip" else data.get("action", "settings")})

        def do_GET(self):
            if not self.valid_request():
                return
            if self.path in ("/api/status", "/api/status/summary"):
                summary = self.path.endswith("/summary")
                self.send_json(200, controller.snapshot(summary=summary) if controller else status.snapshot(summary=summary))
                return
            elif self.path == "/health":
                body, mime = b'{"ok":true}', "application/json"
            elif self.path in ("/", "/auto.js", "/auto.css", "/logo.svg"):
                name = {"/": "auto.html", "/auto.js": "auto.js", "/auto.css": "auto.css", "/logo.svg": "logo.svg"}[self.path]
                body = (ROOT / "static" / name).read_bytes()
                mime = {"/": "text/html; charset=utf-8", "/auto.js": "text/javascript", "/auto.css": "text/css", "/logo.svg": "image/svg+xml"}[self.path]
            else:
                self.send_error(404)
                return
            self.send_body(200, body, mime)

        def log_message(self, *args):
            pass
    return Dashboard


def acquire_lock(path):
    stream = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise ServiceError("该数据目录已有自动优选服务运行") from None
    return stream


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="只执行一个周期")
    parser.add_argument("--dry-run", action="store_true", help="读取和测速，但不写 DNS")
    parser.add_argument("--dashboard-only", action="store_true", help="只预览未配置状态页面，不连接 Cloudflare")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.dashboard_only:
        domain = os.environ.get("CF_DOMAIN", "").strip()
        try:
            cfg = Settings(domain=validate_domain(domain) if domain else "")
        except ValueError as error:
            parser.error(str(error))
        status = Status(None, cfg)
        status.update(phase="unconfigured", message="界面预览：自动任务未启动，请在 Docker 部署配置中填写域名和 Token")
        ThreadingHTTPServer(("127.0.0.1", args.port), make_dashboard(status)).serve_forever()
        return
    try:
        cfg = Settings.from_env()
        bundled_release()
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        lock = acquire_lock(cfg.data_dir / "worker.lock")
    except (ValueError, ServiceError, InstallError, OSError) as exc:
        LOG.error("启动失败：%s", exc)
        raise SystemExit(2)
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    status = Status(cfg.data_dir / "status.json", cfg)
    service = Maintainer(cfg, CloudflareClient(cfg), CFSTRunner(cfg, stop), status, stop, args.dry_run)
    scheduler_wakeup = threading.Event()
    controller = TaskController(service, scheduler_wakeup=scheduler_wakeup)
    server = None
    if not args.once:
        server = ThreadingHTTPServer(("0.0.0.0", args.port), make_dashboard(status, controller))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        initial = True
        exit_code = 0
        while not stop.is_set():
            if not controller.automatic(initial=initial):
                stop.wait(0.5)
                continue
            initial = False
            if args.once:
                exit_code = 1 if status.snapshot()["phase"] in ("error", "degraded") else 0
                break
            controller.wait_next()
    finally:
        stop.set()
        if server:
            server.shutdown()
            server.server_close()
        if controller.worker:
            controller.worker.join(timeout=25)
        if args.once:
            status.update(next_run=None)
        else:
            status.update(phase="stopped", next_run=None, message="自动优选服务已停止")
        lock.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
