from __future__ import annotations

import argparse
import copy
import json
import re
import secrets
import ssl
import threading
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from benchmark import DEFAULT_URL, aggregate, measure_once, validate_url
from cloudflare import fetch_records, validate_domain

ROOT = Path(__file__).resolve().parent
STATE_LOCK = threading.RLock()
SNAPSHOTS: dict[str, dict] = {}
JOBS: dict[str, dict] = {}
TTL = 1800


class ApiError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def prune() -> None:
    now = time.time()
    with STATE_LOCK:
        for key in list(SNAPSHOTS):
            if now - SNAPSHOTS[key]["created"] > TTL:
                del SNAPSHOTS[key]
        for key in list(JOBS):
            if JOBS[key]["state"] not in ("running", "stopping") and now - JOBS[key]["created"] > TTL:
                del JOBS[key]


def integer(value, lower: int, upper: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise ApiError(f"{label}必须是 {lower} 到 {upper} 之间的整数")
    return value


def job_view(job: dict) -> dict:
    with STATE_LOCK:
        data = copy.deepcopy({key: value for key, value in job.items() if key not in ("cancel", "owner")})
    data["results"].sort(key=lambda row: row.get("avg_speed_MBps") or -1, reverse=True)
    return {"ok": True, **data}


def execute_job(job: dict, records: list[dict]) -> None:
    run_map = {record["ip"]: [] for record in records}
    state = "completed"
    try:
        context = ssl.create_default_context()
        for round_index in range(job["rounds"]):
            # Sequential downloads: every candidate gets the full connection bandwidth.
            for record in records:
                if job["cancel"].is_set():
                    state = "cancelled"
                    return
                with STATE_LOCK:
                    job["current_ip"] = record["ip"]
                    job["current_round"] = round_index + 1
                run = measure_once(record["ip"], job["test_url"], job["timeout"], job["cancel"], context)
                run_map[record["ip"]].append(run)
                with STATE_LOCK:
                    job["completed"] += 1
                    job["bytes"] += run.get("bytes", 0)
                    job["results"] = [aggregate(item, run_map[item["ip"]], job["rounds"])
                                      for item in records if run_map[item["ip"]]]
                if run.get("kind") == "rate_limited":
                    state = "rate_limited"
                    with STATE_LOCK:
                        job["message"] = "测速源返回 429，已停止本轮任务；未测试的 IP 不会被记为慢速或失败。"
                    return
                if job["cancel"].wait(.5):
                    state = "cancelled"
                    return
    except Exception:
        state = "failed"
        with STATE_LOCK:
            job["message"] = "测速任务异常结束，请查看本地终端。"
        traceback.print_exc()
    finally:
        with STATE_LOCK:
            job["state"] = "cancelled" if job["cancel"].is_set() else state
            job["current_ip"] = None
            job["finished"] = time.time()


def create_job(owner: str, payload: dict) -> dict:
    snapshot_id = str(payload.get("snapshot_id", ""))
    url = validate_url(payload.get("test_url", DEFAULT_URL))
    rounds = integer(payload.get("rounds", 2), 1, 3, "测试轮数")
    timeout = integer(payload.get("timeout", 10), 2, 30, "单次时限")
    selected = payload.get("ips")
    if not isinstance(selected, list) or not selected or not all(isinstance(ip, str) for ip in selected):
        raise ApiError("请至少选择一个候选 IP")
    with STATE_LOCK:
        snapshot = SNAPSHOTS.get(snapshot_id)
        if not snapshot or snapshot["owner"] != owner:
            raise ApiError("DNS 记录已过期，请重新获取", 404)
        allowed = {record["ip"]: record for record in snapshot["records"] if record["eligible"]}
        if any(ip not in allowed for ip in selected):
            raise ApiError("只能测试本次获取到的 Cloudflare 公网 IP")
        if any(job["state"] in ("running", "stopping") for job in JOBS.values()):
            raise ApiError("当前已有测速任务运行，请等待完成或停止后重试", 409)
        records = [allowed[ip] for ip in dict.fromkeys(selected)]
        key = secrets.token_hex(16)
        job = {"id": key, "owner": owner, "domain": snapshot["domain"], "state": "running",
               "created": time.time(), "test_url": url, "rounds": rounds, "timeout": timeout,
               "total": len(records) * rounds, "completed": 0, "bytes": 0,
               "record_count": len(records), "current_ip": None, "current_round": 1,
               "results": [], "message": "", "cancel": threading.Event()}
        JOBS[key] = job
        while len(JOBS) > 20:
            old_key = next((item for item in JOBS if JOBS[item]["state"] not in ("running", "stopping")), None)
            if old_key is None:
                break
            del JOBS[old_key]
    threading.Thread(target=execute_job, args=(job, records), daemon=True).start()
    return job_view(job)


class AppHandler(BaseHTTPRequestHandler):
    server_version = "CFIPDesk/2.0"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(15)
        self.new_cookie = False

    def owner(self) -> str:
        if hasattr(self, "session_id"):
            return self.session_id
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            pass
        value = cookie["cfip_session"].value if "cfip_session" in cookie else ""
        if not re.fullmatch(r"[0-9a-f]{48}", value):
            value = secrets.token_hex(24)
            self.new_cookie = True
        self.session_id = value
        return value

    def local_request(self, mutation: bool = False) -> None:
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in allowed:
            raise ApiError("仅允许本机访问", 403)
        if mutation:
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + host for host in allowed}:
                raise ApiError("请求来源不匹配", 403)
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise ApiError("需要 application/json 请求", 415)
            if self.headers.get("X-Local-Client") != "cfip-desk":
                raise ApiError("请从本地页面提交请求", 403)

    def send(self, body: bytes, status: int, mime: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if self.new_cookie:
            self.send_header("Set-Cookie", f"cfip_session={self.session_id}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json(self, payload: dict, status: int = 200) -> None:
        self.send(json.dumps(payload, ensure_ascii=False).encode("utf-8"), status,
                  "application/json; charset=utf-8")

    def do_GET(self) -> None:
        try:
            self.local_request()
            owner = self.owner()
            prune()
            path = urlsplit(self.path).path
            files = {"/": ("index.html", "text/html"), "/style.css": ("style.css", "text/css"),
                     "/app.js": ("app.js", "text/javascript")}
            if path in files:
                name, mime = files[path]
                self.send((ROOT / "static" / name).read_bytes(), 200, mime + "; charset=utf-8")
            elif path == "/health":
                self.json({"ok": True, "version": "2.0", "location": "本机后端"})
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}", path):
                with STATE_LOCK:
                    job = JOBS.get(path.rsplit("/", 1)[-1])
                    if not job or job["owner"] != owner:
                        raise ApiError("找不到这个测速任务", 404)
                    view = job_view(job)
                self.json(view)
            else:
                raise ApiError("页面不存在", 404)
        except ApiError as exc:
            self.json({"ok": False, "error": str(exc)}, exc.status)

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 32_768:
                raise ApiError("请求大小不正确")
            body = self.rfile.read(length)
            self.local_request(mutation=True)
            owner = self.owner()
            prune()
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ApiError("请求需要是 JSON 对象")
            if self.path == "/api/records":
                token = str(payload.pop("token", "")).strip()
                if not re.fullmatch(r"[A-Za-z0-9_.-]{10,300}", token):
                    raise ApiError("请输入有效的 Cloudflare API Token")
                domain = validate_domain(str(payload.get("domain", "")))
                # Only this request uses the token. Snapshots and jobs never contain it.
                try:
                    result = fetch_records(domain, token, str(payload.get("zone_id", "")))
                finally:
                    token = ""
                key = secrets.token_hex(16)
                with STATE_LOCK:
                    SNAPSHOTS[key] = {**result, "owner": owner, "created": time.time()}
                    owner_keys = [item for item in SNAPSHOTS if SNAPSHOTS[item]["owner"] == owner]
                    for old_key in owner_keys[:-5]:
                        del SNAPSHOTS[old_key]
                self.json({"ok": True, "snapshot_id": key, **result})
            elif self.path == "/api/jobs":
                self.json(create_job(owner, payload), 202)
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}/cancel", self.path):
                key = self.path.split("/")[3]
                with STATE_LOCK:
                    job = JOBS.get(key)
                    if not job or job["owner"] != owner:
                        raise ApiError("找不到这个测速任务", 404)
                    if job["state"] == "running":
                        job["state"] = "stopping"
                        job["cancel"].set()
                    view = job_view(job)
                self.json(view)
            else:
                raise ApiError("接口不存在", 404)
        except (ValueError, TypeError, UnicodeError) as exc:
            # Parsing failures have stable messages and never echo the submitted body.
            message = "JSON 请求格式不正确" if isinstance(exc, (json.JSONDecodeError, UnicodeError)) else str(exc)
            self.json({"ok": False, "error": message}, getattr(exc, "status", 400))
        except Exception:
            self.json({"ok": False, "error": "本地服务出错，请检查终端"}, 500)
            traceback.print_exc()

    def log_message(self, fmt: str, *args) -> None:
        # Log method/status only; do not log credentials, request bodies or query strings.
        if args and len(args) > 1:
            print(f"[{self.log_date_time_string()}] {self.command} {args[1]}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Cloudflare preferred-IP benchmark")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), AppHandler)
    server.daemon_threads = True
    print(f"Cloudflare IP Tester: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with STATE_LOCK:
            for job in JOBS.values():
                job["cancel"].set()
        server.server_close()


if __name__ == "__main__":
    main()
