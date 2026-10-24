"""Pinned-IP HTTP downloads with verified TLS and an overall deadline."""
from __future__ import annotations

import http.client
import io
import ipaddress
import socket
import ssl
import threading
import time
from urllib.parse import quote, urlsplit, urlunsplit

DEFAULT_URL = "https://speed.cloudflare.com/__down?bytes=10000000"
MAX_BYTES = 100_000_000
MIN_BYTES = 65_536


class DeadlineReader(io.RawIOBase):
    """Retry short socket waits without poisoning a buffered socket.makefile."""
    def __init__(self, sock, cancel, deadline, expired, on_close):
        super().__init__()
        self.sock, self.cancel, self.deadline, self.expired = sock, cancel, deadline, expired
        self.on_close = on_close

    def close(self):
        if not self.closed:
            super().close()
            self.on_close()

    def readable(self):
        return True

    def readinto(self, buffer):
        while True:
            if self.cancel.is_set():
                raise TimeoutError("cancelled")
            if time.perf_counter() >= self.deadline:
                self.expired.set()
                raise TimeoutError("deadline")
            try:
                return self.sock.recv_into(buffer)
            except socket.timeout:
                continue


class SocketView:
    def __init__(self, sock, cancel, deadline, expired):
        self.raw = sock
        self.cancel, self.deadline, self.expired = cancel, deadline, expired
        self.files = 0
        self.close_requested = False

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def makefile(self, *args, **kwargs):
        self.files += 1
        return io.BufferedReader(DeadlineReader(self.raw, self.cancel, self.deadline, self.expired,
                                                self.file_closed))

    def file_closed(self):
        self.files -= 1
        if self.close_requested and not self.files:
            self.raw.close()

    def close(self):
        # HTTPConnection releases ownership on 'Connection: close'; the response
        # stream must keep the socket alive until its own close, like socket.makefile.
        self.close_requested = True
        if not self.files:
            self.raw.close()


def validate_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 or c.isspace() for c in value):
        raise ValueError("测速 URL 格式不正确")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
        if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
            raise ValueError()
        if port not in (None, 80, 443) or parsed.fragment:
            raise ValueError()
        # A hostname is needed for certificate validation and Host routing.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError()
        host = host.encode("idna").decode("ascii")
        if not all(label and len(label) <= 63 and all(c.isalnum() or c == "-" for c in label)
                   for label in host.split(".")):
            raise ValueError()
        authority = host + (f":{port}" if port else "")
        return urlunsplit((parsed.scheme, authority, quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                           quote(parsed.query, safe="%=&?/:;+,$@!()*'-._~"), ""))
    except (ValueError, UnicodeError):
        raise ValueError("测速地址需为 HTTP(S) 下载 URL，端口仅支持 80 / 443") from None


class PinnedConnection(http.client.HTTPConnection):
    def __init__(self, ip: str, url: str, timeout: int, context: ssl.SSLContext):
        parsed = urlsplit(url)
        self.secure = parsed.scheme == "https"
        super().__init__(parsed.hostname, parsed.port or (443 if self.secure else 80), timeout=timeout)
        self.ip = ip
        self.context = context
        self.pinned_socket = None
        self.tcp_ms = None
        self.tls_ms = None
        self.cancel = None
        self.deadline = None
        self.expired = None

    def connect(self) -> None:
        started = time.perf_counter()
        family = socket.AF_INET6 if ":" in self.ip else socket.AF_INET
        self.sock = socket.socket(family, socket.SOCK_STREAM)
        self.pinned_socket = self.sock
        self.sock.settimeout(self.timeout)
        self.sock.connect((self.ip, self.port))
        self.tcp_ms = (time.perf_counter() - started) * 1000
        if self.secure:
            tls_started = time.perf_counter()
            self.sock = self.context.wrap_socket(self.sock, server_hostname=self.host,
                                                do_handshake_on_connect=False)
            self.pinned_socket = self.sock
            self.sock.do_handshake()
            self.tls_ms = (time.perf_counter() - tls_started) * 1000
        self.sock.settimeout(.25)
        self.sock = SocketView(self.sock, self.cancel, self.deadline, self.expired)

    def abort(self) -> None:
        sock = self.pinned_socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


def measure_once(ip: str, url: str, timeout: int, cancel: threading.Event,
                 context: ssl.SSLContext | None = None) -> dict:
    # The context is built before the timer so local trust-store loading isn't network latency.
    conn = PinnedConnection(ip, url, timeout, context or ssl.create_default_context())
    ended = threading.Event()
    expired = threading.Event()
    started = time.perf_counter()
    deadline = started + timeout
    conn.cancel, conn.deadline, conn.expired = cancel, deadline, expired
    response = None
    status = None
    received = 0
    first_byte_ms = None
    body_started = None
    body_ended = None
    partial = False
    error = None
    colo = ""

    def watchdog() -> None:
        while not ended.wait(.05):
            if cancel.is_set() or time.perf_counter() >= deadline:
                if not cancel.is_set():
                    expired.set()
                conn.abort()
                return

    watcher = threading.Thread(target=watchdog, daemon=True)
    watcher.start()
    try:
        if cancel.is_set():
            return {"ok": False, "kind": "cancelled", "error": "已停止", "bytes": 0}
        parsed = urlsplit(url)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        conn.request("GET", path, headers={"User-Agent": "CFIPDesk/2.0",
                                          "Accept-Encoding": "identity", "Connection": "close"})
        response = conn.getresponse()
        first_byte_ms = (time.perf_counter() - started) * 1000
        status = response.status
        ray = response.getheader("CF-Ray", "")
        colo = ray.rsplit("-", 1)[-1] if "-" in ray else ""
        if status != 200:
            messages = {429: "测速源限流（429）；请稍后重试或手动换用自有测速源",
                        403: "测速源拒绝访问（403）"}
            message = messages.get(status, f"测速源返回 HTTP {status}")
            if 300 <= status < 400:
                message += "，请填写最终下载 URL"
            return {"ok": False, "kind": "rate_limited" if status == 429 else "http_error",
                    "status": status, "error": message, "bytes": 0, "colo": colo,
                    "retry_after": response.getheader("Retry-After")}
        if response.getheader("Content-Encoding", "identity").lower() not in ("identity", ""):
            return {"ok": False, "kind": "invalid_source", "status": status,
                    "error": "测速源返回压缩内容，请换用不压缩的二进制下载地址", "bytes": 0}
        mime = response.getheader("Content-Type", "").split(";", 1)[0].lower()
        if mime in ("text/html", "application/json", "application/problem+json"):
            return {"ok": False, "kind": "invalid_source", "status": status,
                    "error": "测速源返回网页或 JSON，请填写直接下载文件的 URL", "bytes": 0}
        body_started = time.perf_counter()
        # HTTPResponse decodes chunked transfer framing; only actual payload bytes count.
        while received < MAX_BYTES:
            if cancel.is_set():
                break
            if time.perf_counter() >= deadline:
                expired.set()
                break
            data = response.read1(min(65_536, MAX_BYTES - received))
            if not data:
                break
            received += len(data)
            body_ended = time.perf_counter()
        partial = expired.is_set() or received >= MAX_BYTES
        if not partial and response.length not in (None, 0):
            error = "下载内容不完整"
    except ssl.SSLCertVerificationError:
        error = "TLS 证书校验失败"
    except (OSError, http.client.HTTPException) as exc:
        partial = expired.is_set() or isinstance(exc, TimeoutError)
        if not partial:
            error = "连接失败或响应中断"
    finally:
        ended.set()
        if response is not None:
            response.close()
        conn.close()
        watcher.join(timeout=.2)

    base = {"status": status, "bytes": received, "colo": colo,
            "tcp_ms": round(conn.tcp_ms, 1) if conn.tcp_ms is not None else None,
            "tls_ms": round(conn.tls_ms, 1) if conn.tls_ms is not None else None,
            "first_byte_ms": round(first_byte_ms, 1) if first_byte_ms is not None else None,
            "total_ms": round((time.perf_counter() - started) * 1000, 1)}
    if cancel.is_set():
        return {**base, "ok": False, "kind": "cancelled", "error": "已停止"}
    if partial and received >= MIN_BYTES and body_started is not None:
        body_ended = time.perf_counter()
    if error or received < MIN_BYTES or body_started is None or body_ended is None:
        message = error or ("连接或下载超时" if partial or expired.is_set() else "文件过小，不能作为有效下载样本")
        return {**base, "ok": False, "kind": "timeout" if partial else "error", "error": message}
    seconds = max(.001, body_ended - body_started)
    return {**base, "ok": True, "kind": "sampled" if partial else "complete",
            "speed_MBps": round(received / seconds / 1_000_000, 3),
            "body_seconds": round(seconds, 4), "partial": partial,
            "small_sample": received < 5_000_000 or seconds < 2}


def aggregate(record: dict, runs: list[dict], total_runs: int) -> dict:
    good = [run for run in runs if run.get("ok")]
    good.sort(key=lambda run: run["speed_MBps"])
    statuses = [str(run["status"]) for run in runs if run.get("status") is not None]
    return {**record, "runs": runs, "completed_runs": len(runs), "successful_runs": len(good),
            "expected_runs": total_runs, "ok": bool(good),
            "avg_speed_MBps": round(sum(run["speed_MBps"] for run in good) / len(good), 3) if good else None,
            "min_speed_MBps": good[0]["speed_MBps"] if good else None,
            "max_speed_MBps": good[-1]["speed_MBps"] if good else None,
            "avg_first_byte_ms": round(sum(run["first_byte_ms"] for run in good) / len(good), 1) if good else None,
            "statuses": ", ".join(dict.fromkeys(statuses)),
            "colo": ", ".join(dict.fromkeys(run.get("colo", "") for run in runs if run.get("colo"))),
            "error": next((run["error"] for run in reversed(runs) if run.get("error")), None)}
