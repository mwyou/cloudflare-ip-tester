"""Persistent Linux CFST releases, explicit upgrades, and rollback."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
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
RELEASE_ID = rf"{TAG}-(?:amd64|arm64)-[a-f0-9]{{64}}"


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
        raise InstallError("CFST 归档 SHA-256 不匹配；未切换版本")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        members = [m for m in bundle.getmembers() if m.isfile() and Path(m.name).name == "cfst"]
        if len(members) != 1 or members[0].size > 64_000_000:
            raise InstallError("CFST 归档内容无效")
        with bundle.extractfile(members[0]) as stream:
            binary = stream.read(64_000_001)
    validate_elf(binary, arch)
    return binary, {"version": version, "arch": arch, "archive_sha256": checksum.lower(), "binary_sha256": digest(binary)}


def atomic_json(path, data):
    handle, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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
                raise InstallError("CFST 兼容性检查未通过；未切换版本")
            return result.stdout.decode("utf-8", errors="replace") + result.stderr.decode("utf-8", errors="replace")

        help_text = run(["-h"])
        if (not re.search(r"CloudflareSpeedTest\s+" + re.escape(version) + r"(?=\s|$)", help_text) or
                not all(re.search(r"(?m)^\s*" + re.escape(flag) + r"(?=\s|$)", help_text)
                        for flag in ("-f", "-o", "-url", "-n", "-t", "-tp", "-dn", "-dt", "-tl", "-sl", "-p", "-debug"))):
            raise InstallError("CFST 版本或命令行参数不兼容；未切换版本")

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
                raise InstallError("CFST 下载 / CSV 格式不兼容；未切换版本")
            output = run([*base, "-url", f"http://fixture.invalid:{port}/limited", "-o", str(Path(folder)/"limited.csv")])
            if not re.search(r"(?:状态码|status(?:\s+code)?)[：:\s]+429\b", output, re.I):
                raise InstallError("CFST 429 限流输出不兼容；未切换版本")
        finally:
            server.shutdown()
            server.server_close()


class Store:
    def __init__(self, home, arch=None):
        self.home = Path(home)
        self.arch = arch or native_arch()
        self.state_file = self.home/"current.json"

    def state(self):
        if not self.state_file.exists():
            return {"active": None, "previous": None}
        data = json.loads(self.state_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != {"active", "previous"} or not data["active"]:
            raise InstallError("CFST 版本状态无效")
        for value in data.values():
            if value is not None and (not isinstance(value, str) or not re.fullmatch(RELEASE_ID, value)):
                raise InstallError("CFST 版本路径无效")
        return data

    def release(self, release_id):
        if not isinstance(release_id, str) or not re.fullmatch(RELEASE_ID, release_id):
            raise InstallError("CFST 版本路径无效")
        folder = self.home/"releases"/release_id
        metadata = json.loads((folder/"manifest.json").read_text(encoding="utf-8"))
        validate_release(metadata["version"], metadata["arch"], metadata["archive_sha256"])
        binary = (folder/"cfst").read_bytes()
        if (metadata["arch"] != self.arch or digest(binary) != metadata["binary_sha256"] or
                release_id != self.release_id(metadata)):
            raise InstallError("CFST 二进制校验失败或架构不匹配")
        validate_elf(binary, self.arch)
        return folder/"cfst", metadata

    @staticmethod
    def release_id(metadata):
        return f"{metadata['version']}-{metadata['arch']}-{metadata['binary_sha256']}"

    def stage(self, binary, metadata, smoke=smoke_test):
        validate_release(metadata["version"], metadata["arch"], metadata["archive_sha256"])
        if metadata["arch"] != self.arch or metadata["binary_sha256"] != digest(binary):
            raise InstallError("CFST 安装文件校验失败")
        validate_elf(binary, self.arch)
        release_id = self.release_id(metadata)
        releases = self.home/"releases"
        releases.mkdir(parents=True, exist_ok=True)
        target = releases/release_id
        with tempfile.TemporaryDirectory(prefix=".install-", dir=releases) as temporary:
            folder = Path(temporary)
            executable = folder/"cfst"
            executable.write_bytes(binary)
            executable.chmod(0o555)
            atomic_json(folder/"manifest.json", metadata | {"installed_at": time.time()})
            smoke(executable, metadata["version"])
            if target.exists():
                self.release(release_id)
            else:
                # Rename a complete directory; an interrupted download cannot become active.
                folder.rename(target)
        return release_id

    def activate(self, release_id):
        self.release(release_id)
        current = self.state()
        if current["active"] == release_id:
            return
        atomic_json(self.state_file, {"active": release_id, "previous": current["active"]})

    def initialize(self, seed, smoke=smoke_test):
        current = self.state()
        if not current["active"]:
            seed = Path(seed)
            metadata = json.loads((seed/"manifest.json").read_text(encoding="utf-8"))
            self.activate(self.stage((seed/"cfst").read_bytes(), metadata, smoke))
        return self.release(self.state()["active"])

    def upgrade(self, version, checksum, smoke=smoke_test):
        if not self.state()["active"]:
            raise InstallError("请先启动服务，完成 CFST 数据卷初始化")
        # Verify the old release too, before preserving it as the rollback target.
        self.release(self.state()["active"])
        binary, metadata = download_release(version, self.arch, checksum)
        self.activate(self.stage(binary, metadata, smoke))

    def rollback(self, smoke=smoke_test):
        current = self.state()
        if not current["previous"]:
            raise InstallError("没有可回滚的 CFST 版本")
        binary, metadata = self.release(current["previous"])
        smoke(binary, metadata["version"])
        atomic_json(self.state_file, {"active": current["previous"], "previous": current["active"]})

    def describe(self):
        data = {}
        for name, release_id in self.state().items():
            if release_id:
                path, metadata = self.release(release_id)
                data[name] = metadata | {"path": str(path)}
            else:
                data[name] = None
        return data


def main(argv=None):
    from automation import acquire_lock
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"status", "upgrade", "rollback", "init"}
    command = argv[0] if argv and argv[0] in commands else "run"
    if command != "run":
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("command", choices=sorted(commands))
        parser.add_argument("--version")
        parser.add_argument("--sha256")
        args = parser.parse_args(argv)
        if command == "upgrade" and (not args.version or not args.sha256):
            parser.error("upgrade 必须提供 --version 和 --sha256（官方 Linux 归档的 SHA-256）")
    home = Path(os.environ.get("CFST_HOME", "/opt/cfst"))
    data = Path(os.environ.get("DATA_DIR", "/data"))
    store = Store(home)
    if command == "status":
        print(json.dumps(store.describe(), ensure_ascii=False, indent=2))
        return
    data.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    lock = acquire_lock(data/"worker.lock")
    try:
        binary_lock = acquire_lock(home/"install.lock")
        try:
            if command == "upgrade":
                store.upgrade(args.version, args.sha256)
            elif command == "rollback":
                store.rollback()
            else:
                binary, metadata = store.initialize(Path(os.environ.get("CFST_SEED", "/usr/local/lib/cfst")))
        finally:
            binary_lock.close()
    finally:
        lock.close()
    if command == "run":
        env = os.environ.copy()
        env["CFST_BIN"], env["CFST_VERSION"] = str(binary), metadata["version"]
        os.execve(sys.executable, [sys.executable, str(Path(__file__).with_name("automation.py")), *argv], env)
    else:
        print(json.dumps(store.describe(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # OS errors and external responses may carry local paths or unexpected text.
        from automation import ServiceError
        print(str(error) if isinstance(error, (InstallError, ServiceError)) else
              "CFST 管理失败；请检查文件权限、网络和版本数据卷，并用 status 核对当前版本", file=sys.stderr)
        raise SystemExit(1)
