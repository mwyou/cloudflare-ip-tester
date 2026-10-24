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
from cfst_manager import InstallError, ReleaseCatalog, Store, TAG, RELEASE_ID

LOG = logging.getLogger("cfst-dns")
ROOT = Path(__file__).resolve().parent
STEP_TTL = 1800
MIN_INTERVAL_SECONDS = 60
ACTIONS = ("read", "check", "select", "compare", "write")


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
    candidates: int = 128
    download_count: int = 20
    download_seconds: int = 10
    threads: int = 8
    test_url: str = "https://cf.xiu2.xyz/url"
    binary: str = "/usr/local/bin/cfst"
    candidate_file: str = ""
    data_dir: Path = Path("/data")
    ttl: int = 60
    initial_select: bool = True
    cfst_version: str = "v2.3.5"

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
                   number("DOWNLOAD_COUNT", 20, 3, 50, True),
                   number("DOWNLOAD_SECONDS", 10, 5, 30, True), number("CFST_THREADS", 8, 1, 16, True),
                   validate_url(os.environ.get("CFST_URL", cls.test_url)),
                   os.environ.get("CFST_BIN", cls.binary), os.environ.get("CANDIDATE_FILE", ""),
                   Path(os.environ.get("DATA_DIR", "/data")), number("DNS_TTL", 60, 60, 86400, True),
                   os.environ.get("INITIAL_SELECT", "true").lower() in ("true", "1", "yes"),
                   os.environ.get("CFST_VERSION", "v2.3.5"))


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
            raise ServiceError("Cloudflare API 连接失败或响应无效；本次不更新 DNS") from None
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
            raise ServiceError("测速期间 DNS 被其他程序修改；放弃覆盖，等待下个周期")
        plan = dns_plan(self.settings, current, ips)
        if any(plan.values()):
            self.request(f"/zones/{self.zone_id()}/dns_records/batch", body=plan)
        # A write timeout is ambiguous: never retry a mutation blindly.
        after = self.records()
        managed = [r for r in after if r.get("type") == self.settings.kind]
        if sorted(r.get("content") for r in managed) != sorted(ips) or any(r.get("proxied") for r in managed):
            raise ServiceError("写入后核验未通过；请检查 DNS，下个周期将重新读取")
        return after


def fingerprint(records):
    return json.dumps(sorted(records, key=lambda r: r.get("id", "")), sort_keys=True)


def dns_plan(settings, records, ips):
    ips = [str(ipaddress.ip_address(ip)) for ip in ips]
    if len(set(ips)) != 3 or len(ips) != 3 or not all(
            is_cloudflare_ip(ip) and ipaddress.ip_address(ip).version == (4 if settings.kind == "A" else 6) for ip in ips):
        raise ServiceError("只有三个去重且属于 Cloudflare 的合格 IP 才能写入")
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


def parse_csv(path, allowed):
    rows = []
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
                if (ip not in allowed or ip in seen or not 0 < received <= sent or
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
        if not ips or not all(is_cloudflare_ip(ip) for ip in ips):
            raise ServiceError("测速候选必须为 Cloudflare 公网 IP")
        cfg = self.settings
        with tempfile.TemporaryDirectory(prefix="cfst-", dir=cfg.data_dir) as folder:
            source, result = Path(folder) / "ips.txt", Path(folder) / "result.csv"
            source.write_text("\n".join(ips) + "\n", encoding="ascii")
            args = [cfg.binary, "-f", str(source), "-o", str(result), "-url", cfg.test_url,
                    "-n", str(cfg.threads), "-t", "4", "-tp", str(urlsplit(cfg.test_url).port or (443 if cfg.test_url.startswith("https:") else 80)),
                    "-dn", str(min(download_count, len(ips))), "-dt", str(cfg.download_seconds),
                    "-tl", str(math.ceil(cfg.latency_ms) + 1), "-sl", "0", "-p", "0", "-debug"]
            # CFST is a child process without API credentials or environment proxies.
            env = {k: v for k, v in os.environ.items() if k.upper() in
                   ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "SSL_CERT_FILE", "SSL_CERT_DIR", "LANG", "LC_ALL")}
            try:
                process = subprocess.Popen(args, cwd=folder, env=env, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            except OSError:
                raise ServiceError("CFST 无法启动；请检查 CFST 运行目录或 CFST_BIN") from None
            lines = queue.Queue()

            def collect():
                try:
                    for line in process.stdout:
                        lines.put(line)
                finally:
                    lines.put(None)

            reader = threading.Thread(target=collect, daemon=True)
            reader.start()
            deadline = time.monotonic() + 30 + math.ceil(len(ips) / cfg.threads) * 6 + min(download_count, len(ips)) * (cfg.download_seconds + 3)
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
                code = process.wait(timeout=3)
                if code != 0 or not result.is_file():
                    raise ServiceError("CFST 未产生有效结果；保留原 DNS")
                return parse_csv(result, set(ips))
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


def candidate_pool(settings, existing):
    version = 4 if settings.kind == "A" else 6
    valid = lambda ip: is_cloudflare_ip(ip) and ipaddress.ip_address(ip).version == version
    current = list(dict.fromkeys(ip for ip in existing if valid(ip)))[:settings.candidates]
    if settings.candidate_file:
        pool = []
        for line in Path(settings.candidate_file).read_text(encoding="utf-8-sig").splitlines():
            value = line.split("#", 1)[0].strip()
            if not value:
                continue
            try:
                value = str(ipaddress.ip_address(value))
            except ValueError:
                raise ServiceError("候选文件每行必须是一个完整 IP，不支持网段") from None
            if not valid(value):
                raise ServiceError("候选文件包含不属于所选 Cloudflare 地址族的 IP")
            pool.append(value)
        pool = list(dict.fromkeys(pool))
        if len(pool) > 512:
            raise ServiceError("候选文件最多支持 512 个 IP")
        # A user-specified file is authoritative, including removal of old IPs.
        if len(pool) < 3:
            raise ServiceError("候选文件至少需要三个去重 IP")
        return pool
    networks = [net for net in CF_NETWORKS if net.version == version]
    pool = set(current)
    while len(pool) < settings.candidates:
        net = secrets.choice(networks)
        ip = str(net.network_address + 1 + secrets.randbelow(net.num_addresses - 2))
        if valid(ip):
            pool.add(ip)
    return current + sorted(pool.difference(current))


class Status:
    def __init__(self, path, settings):
        self.path, self.lock = path, threading.RLock()
        self.data = {"domain": settings.domain, "record_type": settings.kind, "engine": "CFST " + settings.cfst_version,
                     "max_latency_ms": settings.latency_ms, "min_bandwidth_mbps": settings.min_mbps,
                     "interval_seconds": settings.interval, "phase": "starting", "message": "等待首次检查",
                     "last_run": None, "next_run": None, "last_dns_update": None,
                     "dns_ips": [], "previous_dns_ips": [], "dns_change": None,
                     "results": [], "check_results": [], "selection_results": [], "ranked_results": [],
                     "events": [], "steps": {}, "proposed_ips": [],
                     "manual_enabled": False, "busy": False, "active_action": None}
        if path and path.is_file():
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
                if previous.get("domain") == settings.domain and previous.get("record_type") == settings.kind:
                    for key in ("events", "last_dns_update", "results", "check_results", "selection_results",
                                "ranked_results", "dns_ips", "previous_dns_ips", "dns_change"):
                        self.data[key] = previous.get(key, self.data[key])
            except (ValueError, OSError):
                pass

    def update(self, **values):
        with self.lock:
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

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data))


class Maintainer:
    def __init__(self, settings, client, runner, status, stop, dry_run=False):
        self.cfg, self.client, self.runner, self.status, self.stop = settings, client, runner, status, stop
        self.dry_run = dry_run
        # Execution inputs live only in memory. Persisted display results cannot
        # authorize a write after restart.
        self.records = None
        self.read_at = self.selected_at = self.compared_at = 0
        self.new_top = None
        self.final = []
        self.check_rows = None
        self.selection_rows = None
        self.ranked = []

    def fresh(self, timestamp):
        return bool(timestamp) and 0 <= time.time() - timestamp < STEP_TTL

    def valid_ips(self):
        return list(dict.fromkeys(str(ipaddress.ip_address(r["content"])) for r in (self.records or []) if
            r.get("type") == self.cfg.kind and is_cloudflare_ip(r.get("content", "")) and
            ipaddress.ip_address(r["content"]).version == (4 if self.cfg.kind == "A" else 6)))

    def actions(self):
        reasons = dict.fromkeys(ACTIONS)
        dns_reason = "先读取 DNS" if self.records is None else (None if self.fresh(self.read_at) else "DNS 快照已过期，请重新读取")
        selection_reason = "先优选新 IP" if self.new_top is None else (None if self.fresh(self.selected_at) else "优选结果已过期，请重新优选")
        current_reason = None
        if not dns_reason and len(self.valid_ips()) == 3 and self.check_rows is None:
            current_reason = "先检测当前 IP"
        reasons["check"] = dns_reason or (None if self.valid_ips() else "DNS 中没有可测的 Cloudflare IP")
        # A comparison can rank a standalone selection before DNS is read;
        # writing still requires the DNS snapshot for its optimistic check.
        compare_dns_reason = dns_reason if self.records is not None else None
        reasons["compare"] = compare_dns_reason or selection_reason or current_reason
        reasons["write"] = dns_reason or selection_reason or (
            "先完成六个结果排序，取得三个达标 IP" if len(self.final) != 3 else
            (None if self.fresh(self.compared_at) else "排序结果已过期，请重新排序"))
        if self.stop.is_set():
            return dict.fromkeys(ACTIONS, "服务已停止")
        return reasons

    def require_action(self, action):
        if action not in ACTIONS:
            raise ServiceError("未知步骤")
        if reason := self.actions()[action]:
            raise ServiceError(reason)

    def invalidate_final(self):
        self.final, self.compared_at, self.ranked = [], 0, []
        steps = self.status.snapshot()["steps"]
        for key in ("compare", "write"):
            steps.pop(key, None)
        self.status.update(proposed_ips=[], ranked_results=[], results=[], steps=steps)

    def complete(self, action, count):
        steps = self.status.snapshot()["steps"]
        steps[action] = {"time": time.time(), "count": count}
        self.status.update(steps=steps)

    def read(self):
        self.records, self.read_at = None, 0
        self.check_rows, self.selection_rows, self.new_top = None, None, None
        self.invalidate_final()
        steps = self.status.snapshot()["steps"]
        for key in ("read", "check"):
            steps.pop(key, None)
        self.status.update(phase="reading", steps=steps, message="正在读取 DNS")
        records = self.client.records()
        if any(r.get("type") in ("CNAME", "NS") for r in records):
            raise ServiceError("目标域名存在 CNAME / NS 冲突；未修改 DNS")
        self.records, self.read_at = records, time.time()
        ips = list(dict.fromkeys(r["content"] for r in records if r.get("type") == self.cfg.kind))
        self.complete("read", len(ips))
        self.status.update(phase="read_done", dns_ips=ips, check_results=[], selection_results=[],
                           ranked_results=[], results=[], message=f"已读取 {len(ips)} 个 DNS IP")
        return ips

    def check(self):
        self.require_action("check")
        self.invalidate_final()
        steps = self.status.snapshot()["steps"]
        steps.pop("check", None)
        self.status.update(phase="checking", steps=steps, message="正在检测当前 IP")
        ips = self.valid_ips()
        rows = self.runner.run(ips, len(ips))
        self.check_rows = rows
        good = sum(qualifies(self.cfg, row) for row in rows)
        managed = [r for r in self.records if r.get("type") == self.cfg.kind]
        healthy = (len(managed) == len(ips) == len(rows) == good == 3 and
                   {r["ip"] for r in rows} == set(ips) and not any(r.get("proxied") for r in managed))
        self.complete("check", good)
        self.status.update(results=rows, check_results=rows, ranked_results=[], phase="healthy" if healthy else "check_done",
                           message="三个 IP 均达标，保持现有 DNS" if healthy else f"检测完成：{good} / {len(ips)} 个 IP 达标")
        return healthy

    def select(self):
        self.require_action("select")
        self.new_top, self.selected_at = None, 0
        self.selection_rows = None
        self.invalidate_final()
        steps = self.status.snapshot()["steps"]
        steps.pop("select", None)
        self.status.update(phase="selecting", steps=steps, message="正在优选新 IP")
        pool = candidate_pool(self.cfg, self.valid_ips())
        rows = self.runner.run(pool, self.cfg.download_count)
        self.selection_rows = rows
        self.new_top, self.selected_at = self.top(rows), time.time()
        self.complete("select", len(self.new_top))
        self.status.update(results=rows, selection_results=rows, ranked_results=[], phase="select_done",
                           message=f"优选完成：取得 {len(self.new_top)} 个达标候选")
        return rows

    def top(self, rows):
        return sorted((r for r in rows if qualifies(self.cfg, r)),
                      key=lambda r: (-r["speed_mibps"], r["latency_ms"]))[:3]

    def compare(self, bootstrap_rows=None):
        self.require_action("compare")
        self.final, self.compared_at, self.ranked = [], 0, []
        old = [{**row, "source": "当前检测"} for row in (self.check_rows or [])]
        new = [{**row, "source": "新 IP 优选"} for row in (self.new_top or [])]
        ranked = sorted(old + new, key=lambda r: (-r["speed_mibps"], r["latency_ms"]))
        self.ranked = ranked
        seen = set()
        good = []
        for row in ranked:
            if qualifies(self.cfg, row) and row["ip"] not in seen:
                seen.add(row["ip"])
                good.append(row)
        self.complete("compare", len(good))
        if len(good) < 3:
            self.status.update(results=ranked, ranked_results=ranked, phase="degraded",
                               message=f"六个结果排序后仅找到 {len(good)} 个达标 IP；保留现有 DNS")
            return False
        self.final, self.compared_at = good[:3], time.time()
        self.status.update(results=ranked, ranked_results=ranked, proposed_ips=[r["ip"] for r in self.final],
                           phase="compare_done", message="六个结果已排序，前三名待写入")
        return True

    def write(self):
        self.require_action("write")
        selected = [r["ip"] for r in self.final]
        if self.dry_run:
            self.status.update(phase="dry_run", message=f"演练完成：拟写入 {', '.join(selected)}；未写入 DNS")
            return
        before = self.records
        # Consume the proposal before making a request. A failed/ambiguous API
        # write must not leave a clickable retry carrying the old snapshot.
        self.records, self.read_at = None, 0
        self.final, self.compared_at = [], 0
        self.status.update(proposed_ips=[], steps={key: value for key, value in self.status.snapshot()["steps"].items()
                                                   if key not in ("compare", "write")})
        self.status.update(phase="updating", message="正在写入 DNS")
        self.client.replace(before, selected)
        self.complete("write", 3)
        now = time.time()
        old_ips = list(dict.fromkeys(r.get("content") for r in before if r.get("type") == self.cfg.kind))
        self.status.update(phase="updated", previous_dns_ips=old_ips,
                           dns_change={"before": old_ips, "after": selected, "time": now},
                           dns_ips=selected, last_dns_update=now, message="已写入并核验三个 IP")

    def cycle(self, initial=False):
        self.new_top, self.selected_at = None, 0
        self.status.update(last_run=time.time(), next_run=None, steps={})
        ips = self.read()
        managed = [r for r in self.records if r.get("type") == self.cfg.kind]
        current_dns_ready = (len(managed) == len(ips) == 3 and all(
                is_cloudflare_ip(ip) for ip in ips) and not any(r.get("proxied") for r in managed))
        if current_dns_ready:
            healthy = self.check()
            if healthy and not (initial and self.cfg.initial_select):
                return
        rows = self.select()
        if self.compare():
            self.write()


class CFSTControl:
    def __init__(self, store, smoke=None):
        self.store = store
        self.catalog = ReleaseCatalog(store.arch)
        self.smoke = smoke

    def snapshot(self):
        data = self.catalog.snapshot()
        try:
            return data | self.store.inventory()
        except (InstallError, OSError, ValueError, KeyError):
            return data | {"installed": [], "current": None, "previous": None, "arch": self.store.arch,
                           "error": "CFST 版本信息读取失败，请检查运行目录"}

    def change(self, action, target):
        lock = acquire_lock(self.store.home/"install.lock")
        try:
            kwargs = {"smoke": self.smoke} if self.smoke is not None else {}
            if action == "install":
                self.store.upgrade(target, self.catalog.resolve(target), **kwargs)
            elif action == "switch":
                self.store.select(target, **kwargs)
            elif action == "rollback":
                self.store.rollback(**kwargs)
            else:
                raise ServiceError("未知 CFST 操作")
            return self.store.release(self.store.state()["active"])
        finally:
            lock.close()


class TaskController:
    """Reserve execution before acknowledging HTTP; share one lock with cron."""
    def __init__(self, service, cfst=None, scheduler_wakeup=None):
        self.service = service
        self.cfst = cfst
        self.scheduler_wakeup = scheduler_wakeup
        self.lock = threading.Lock()
        self.worker = None
        service.status.update(manual_enabled=True)

    def snapshot(self):
        data = self.service.status.snapshot()
        data["cfst"] = self.cfst.snapshot() if self.cfst else None
        data["actions"] = self.service.actions()
        if data["busy"]:
            data["actions"] = dict.fromkeys(ACTIONS, "任务运行中")
        return data

    def run_reserved(self, action, initial=False, target=None):
        try:
            if action.startswith("cfst_"):
                self.service.status.update(phase="cfst_changing", message="正在校验并切换 CFST", cfst_message="正在校验并切换 CFST")
                binary, metadata = self.cfst.change(action[5:], target)
                cfg = replace(self.service.cfg, binary=str(binary), cfst_version=metadata["version"])
                self.service.cfg, self.service.runner = cfg, CFSTRunner(cfg, self.service.stop)
                self.service.records, self.service.read_at = None, 0
                self.service.new_top, self.service.selected_at = None, 0
                self.service.final, self.service.compared_at = [], 0
                self.service.status.update(engine="CFST " + metadata["version"], phase="cfst_ready",
                                           results=[], steps={}, proposed_ips=[],
                                           cfst_message=f"已启用 {metadata['version']}",
                                           message=f"CFST 已切换到 {metadata['version']}")
            elif action == "automatic":
                self.service.cycle(initial=initial)
            else:
                getattr(self.service, action)()
        except Exception as error:
            message = str(error) if isinstance(error, (ServiceError, InstallError)) else "任务失败；请检查容器日志和文件配置"
            values = {"cfst_message": message} if action.startswith("cfst_") else {}
            self.service.status.update(phase="error", message=message, **values)
            if not isinstance(error, (ServiceError, InstallError, OSError, ValueError)):
                LOG.error("任务异常：%s", type(error).__name__)
        finally:
            self.service.status.update(busy=False, active_action=None)
            self.lock.release()

    def reserve(self, action):
        if not self.lock.acquire(blocking=False):
            raise ServiceError("已有任务运行中，请稍后再试")
        try:
            if action.startswith("cfst_"):
                if self.cfst is None or self.service.stop.is_set():
                    raise ServiceError("CFST 管理不可用")
            elif action != "automatic":
                self.service.require_action(action)
            elif self.service.stop.is_set():
                raise ServiceError("服务已停止")
            values = {} if action.startswith("cfst_") else {"last_run": time.time()}
            self.service.status.update(busy=True, active_action=action, **values)
        except Exception:
            self.lock.release()
            raise

    def start(self, action):
        self.reserve(action)
        try:
            self.worker = threading.Thread(target=self.run_reserved, args=(action,), daemon=True)
            self.worker.start()
        except Exception:
            self.service.status.update(busy=False, active_action=None)
            self.lock.release()
            raise

    def automatic(self, initial=False):
        try:
            self.reserve("automatic")
        except ServiceError:
            return False
        self.run_reserved("automatic", initial)
        return True

    def start_cfst(self, action, target=None):
        if (action not in ("install", "switch", "rollback") or
                (action == "install" and (not isinstance(target, str) or not re.fullmatch(TAG, target))) or
                (action == "switch" and (not isinstance(target, str) or not re.fullmatch(RELEASE_ID, target))) or
                (action == "rollback" and target is not None)):
            raise ServiceError("CFST 操作参数无效")
        self.reserve("cfst_" + action)
        try:
            self.worker = threading.Thread(target=self.run_reserved,
                                           args=("cfst_" + action,), kwargs={"target": target}, daemon=True)
            self.worker.start()
        except Exception:
            self.service.status.update(busy=False, active_action=None)
            self.lock.release()
            raise

    def set_interval(self, seconds):
        if type(seconds) is not int or not MIN_INTERVAL_SECONDS <= seconds <= 86400:
            raise ServiceError(f"检查间隔必须在 {MIN_INTERVAL_SECONDS} 到 86400 秒之间")
        if not self.lock.acquire(blocking=False):
            raise ServiceError("已有任务运行中，请稍后再试")
        try:
            self.service.cfg = replace(self.service.cfg, interval=seconds)
            self.service.runner.settings = self.service.cfg
            self.service.status.update(interval_seconds=seconds, next_run=time.time() + seconds,
                                       message=f"检查间隔已改为 {seconds // 60} 分钟")
            if self.scheduler_wakeup:
                self.scheduler_wakeup.set()
        finally:
            self.lock.release()


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
            if self.path not in ("/api/actions", "/api/cfst", "/api/settings"):
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
                elif self.path == "/api/settings":
                    if set(data) != {"interval_seconds"} or type(data["interval_seconds"]) is not int:
                        raise ValueError()
                elif not ((data.get("action") == "rollback" and set(data) == {"action"}) or
                          (data.get("action") == "install" and set(data) == {"action", "version"}
                           and isinstance(data["version"], str) and re.fullmatch(TAG, data["version"])) or
                          (data.get("action") == "switch" and set(data) == {"action", "release_id"}
                           and isinstance(data["release_id"], str) and re.fullmatch(RELEASE_ID, data["release_id"]))):
                    raise ValueError()
            except (ValueError, TypeError, OSError):
                self.send_json(400, {"error": "步骤参数无效"})
                return
            if controller is None:
                self.send_json(503, {"error": "预览模式，请先配置部署"})
                return
            try:
                if self.path == "/api/settings":
                    controller.set_interval(data["interval_seconds"])
                elif self.path == "/api/cfst":
                    controller.start_cfst(data["action"], data.get("version", data.get("release_id")))
                else:
                    controller.start(data["action"])
            except ServiceError as exc:
                self.send_json(409, {"error": str(exc)})
                return
            self.send_json(202, {"accepted": data.get("action", "settings")})

        def do_GET(self):
            if not self.valid_request():
                return
            if self.path == "/api/status":
                self.send_json(200, controller.snapshot() if controller else status.snapshot())
                return
            elif self.path == "/api/cfst/releases":
                if controller is None or controller.cfst is None:
                    self.send_json(503, {"error": "请先配置部署"})
                    return
                try:
                    controller.cfst.catalog.refresh()
                    self.send_json(200, controller.cfst.catalog.snapshot())
                except InstallError as error:
                    self.send_json(502, {"error": str(error)})
                return
            elif self.path == "/health":
                body, mime = b'{"ok":true}', "application/json"
            elif self.path in ("/", "/auto.js", "/auto.css"):
                name = {"/": "auto.html", "/auto.js": "auto.js", "/auto.css": "auto.css"}[self.path]
                body = (ROOT / "static" / name).read_bytes()
                mime = {"/": "text/html; charset=utf-8", "/auto.js": "text/javascript", "/auto.css": "text/css"}[self.path]
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
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        lock = acquire_lock(cfg.data_dir / "worker.lock")
    except (ValueError, ServiceError, OSError) as exc:
        LOG.error("启动失败：%s", exc)
        raise SystemExit(2)
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    status = Status(cfg.data_dir / "status.json", cfg)
    service = Maintainer(cfg, CloudflareClient(cfg), CFSTRunner(cfg, stop), status, stop, args.dry_run)
    scheduler_wakeup = threading.Event()
    controller = TaskController(service, CFSTControl(Store(os.environ.get("CFST_HOME", "/opt/cfst"))), scheduler_wakeup)
    server = None
    if not args.once:
        server = ThreadingHTTPServer(("0.0.0.0", args.port), make_dashboard(status, controller))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        initial = True
        exit_code = 0
        while not stop.is_set():
            start = time.monotonic()
            if not controller.automatic(initial=initial):
                stop.wait(0.5)
                continue
            initial = False
            if args.once:
                exit_code = 1 if status.snapshot()["phase"] in ("error", "degraded") else 0
                break
            elapsed = time.monotonic() - start
            delay = service.cfg.interval - elapsed if elapsed < service.cfg.interval else service.cfg.interval
            status.update(next_run=time.time() + delay)
            scheduler_wakeup.wait(delay)
            scheduler_wakeup.clear()
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
