"""Pinned official Linux CFST bundle, verified at build and startup."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tarfile
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.request import Request, urlopen

VERSION = "v2.3.5"
HASHES = {
    "amd64": "c4c8fc76b4e1bf2bdb5ced8b765956d82dda7bc4eb59df5c04053f0f7db98d90",
    "arm64": "0ac992fcf24d4684caed33620deb9b83ce82f32d2418dc1f90be490ce5900300",
}
TAG = r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?"
BUNDLED_DIR = Path("/usr/local/lib/cfst")


class InstallError(RuntimeError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def native_arch():
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if arch is None:
        raise InstallError("只支持 Linux amd64 / arm64")
    return arch


def validate_release(version, arch, checksum):
    if not re.fullmatch(TAG, version) or arch not in HASHES or not re.fullmatch(r"[a-fA-F0-9]{64}", checksum):
        raise InstallError("版本、架构或 SHA-256 格式无效")


def validate_elf(binary, arch):
    machine = {"amd64": 62, "arm64": 183}[arch]
    if (len(binary) < 64 or binary[:6] != b"\x7fELF\x02\x01" or
            int.from_bytes(binary[18:20], "little") != machine or int.from_bytes(binary[16:18], "little") not in (2, 3)):
        raise InstallError("CFST 必须是与容器架构匹配的 Linux ELF 二进制")


def read_url(url, limit, headers=None, timeout=30):
    with urlopen(Request(url, headers=headers or {"User-Agent": "CFIPDesk-CFST"}), timeout=timeout) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise InstallError("CFST 下载文件过大")
    return data


def download_release(version, arch, checksum):
    validate_release(version, arch, checksum)
    asset_name = f"cfst_linux_{arch}.tar.gz"
    base = "https://github.com/XIU2/CloudflareSpeedTest/releases/download"
    try:
        archive = read_url(f"{base}/{version}/{asset_name}", 30_000_000)
    except (URLError, TimeoutError):
        release = json.loads(read_url(f"https://api.github.com/repos/XIU2/CloudflareSpeedTest/releases/tags/{version}", 4_000_000))
        asset = next((item for item in release.get("assets", []) if item.get("name") == asset_name), None)
        # Construct the official API URL from a numeric ID, never an arbitrary URL in a response.
        if asset is None or not isinstance(asset.get("id"), int):
            raise InstallError("官方发布中没有对应架构的 CFST")
        archive = read_url(f"https://api.github.com/repos/XIU2/CloudflareSpeedTest/releases/assets/{asset['id']}?download=1",
                           30_000_000, {"Accept": "application/octet-stream", "User-Agent": "CFIPDesk-CFST"}, 90)
    if digest(archive) != checksum.lower():
        raise InstallError("CFST 归档 SHA-256 不匹配")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        members = [m for m in bundle.getmembers() if m.isfile() and Path(m.name).name == "cfst"]
        if len(members) != 1 or members[0].size > 64_000_000:
            raise InstallError("CFST 归档内容无效")
        with bundle.extractfile(members[0]) as stream:
            binary = stream.read(64_000_001)
    validate_elf(binary, arch)
    return binary, {"version": version, "arch": arch, "archive_sha256": checksum.lower(), "binary_sha256": digest(binary)}


def child_environment():
    return {k: v for k, v in os.environ.items() if k.upper() in
            ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL")}


def smoke_test(binary, version):
    """Only -h and a loopback HTTP fixture; no public speed test or DNS write."""
    from automation import parse_csv
    with tempfile.TemporaryDirectory(prefix="cfst-compat-") as folder:
        def run(args):
            result = subprocess.run([str(binary), *args], cwd=folder, env=child_environment(),
                                    capture_output=True, timeout=20)
            if result.returncode != 0:
                raise InstallError("CFST 兼容性检查未通过")
            return result.stdout.decode("utf-8", errors="replace") + result.stderr.decode("utf-8", errors="replace")

        help_text = run(["-h"])
        if (not re.search(r"CloudflareSpeedTest\s+" + re.escape(version) + r"(?=\s|$)", help_text) or
                not all(re.search(r"(?m)^\s*" + re.escape(flag) + r"(?=\s|$)", help_text)
                        for flag in ("-f", "-o", "-url", "-n", "-t", "-tp", "-dn", "-dt", "-tl", "-sl", "-p", "-debug"))):
            raise InstallError("CFST 版本或命令行参数不兼容")

        class Fixture(BaseHTTPRequestHandler):
            def do_GET(self):
                limited = self.path == "/limited"
                self.send_response(429 if limited else 200)
                self.send_header("Content-Length", "0" if limited else str(8*1024*1024))
                self.end_headers()
                if not limited:
                    try:
                        for _ in range(128):
                            self.wfile.write(b"x"*65536)
                            self.wfile.flush()
                            time.sleep(.02)
                    except OSError:
                        pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_port
            base = ["-ip", "127.0.0.1", "-tp", str(port), "-n", "1", "-t", "4", "-dn", "1", "-dt", "5", "-p", "0", "-debug"]
            csv_path = Path(folder)/"result.csv"
            run([*base, "-url", f"http://fixture.invalid:{port}/file", "-o", str(csv_path)])
            rows = parse_csv(csv_path, {"127.0.0.1"})
            if len(rows) != 1 or rows[0]["speed_mibps"] <= 0:
                raise InstallError("CFST 下载 / CSV 格式不兼容")
            output = run([*base, "-url", f"http://fixture.invalid:{port}/limited", "-o", str(Path(folder)/"limited.csv")])
            if not re.search(r"(?:状态码|status(?:\s+code)?)[：:\s]+429\b", output, re.I):
                raise InstallError("CFST 429 限流输出不兼容")
        finally:
            server.shutdown()
            server.server_close()


def bundled_release(directory=BUNDLED_DIR):
    directory = Path(directory)
    binary = directory / "cfst"
    metadata = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    arch = native_arch()
    if (metadata.get("version") != VERSION or metadata.get("arch") != arch or
            metadata.get("archive_sha256") != HASHES[arch]):
        raise InstallError("内置 CFST 版本或架构不匹配，请重新拉取镜像")
    content = binary.read_bytes()
    if digest(content) != metadata.get("binary_sha256"):
        raise InstallError("内置 CFST 校验失败，请重新拉取镜像")
    validate_elf(content, arch)
    return binary, metadata
