"""Pinned bundle integrity and official CFST compatibility checks."""
import io
import json
import os
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest.mock import patch

import automation as auto
import cfst_bundle as manager


def artifact(version="v2.3.5", arch="amd64"):
    binary = bytearray(64)
    binary[:6] = b"\x7fELF\x02\x01"
    binary[16:18] = (2).to_bytes(2, "little")
    binary[18:20] = (62 if arch == "amd64" else 183).to_bytes(2, "little")
    binary = bytes(binary) + version.encode()
    return binary, {"version": version, "arch": arch, "archive_sha256": "a"*64, "binary_sha256": manager.digest(binary)}


def archive(binary, name="cfst"):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as bundle:
        member = tarfile.TarInfo(name)
        member.size = len(binary)
        bundle.addfile(member, io.BytesIO(binary))
    return data.getvalue()


class BundledCFSTTests(unittest.TestCase):
    def test_bundle_startup_verifies_version_arch_and_content(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(manager, "native_arch", return_value="amd64"):
            root = Path(folder)
            binary, metadata = artifact()
            metadata["archive_sha256"] = manager.HASHES["amd64"]
            (root / "cfst").write_bytes(binary)
            (root / "manifest.json").write_text(json.dumps(metadata))
            self.assertEqual(manager.bundled_release(root), (root / "cfst", metadata))
            for change in ({"version": "v2.3.6"}, {"arch": "arm64"}, {"archive_sha256": "0"*64}):
                (root / "manifest.json").write_text(json.dumps(metadata | change))
                with self.subTest(change=change), self.assertRaises(manager.InstallError):
                    manager.bundled_release(root)
            (root / "manifest.json").write_text(json.dumps(metadata))
            (root / "cfst").write_bytes(binary+b"changed")
            with self.assertRaises(manager.InstallError):
                manager.bundled_release(root)

    def test_runtime_always_uses_bundled_cfst(self):
        with patch.dict(os.environ, {"CF_DOMAIN": "cf.example.com", "CF_API_TOKEN": "test-token",
                                     "CFST_BIN": "/custom/cfst", "CFST_VERSION": "v9.0.0"}, clear=True):
            settings = auto.Settings.from_env()
        self.assertEqual(settings.binary, str(manager.BUNDLED_DIR / "cfst"))
        self.assertEqual(settings.cfst_version, manager.VERSION)

    def test_download_verifies_archive_and_only_reads_cfst_member(self):
        for arch in ("amd64", "arm64"):
            binary, _ = artifact(arch=arch)
            data = archive(binary, "nested/cfst")
            checksum = manager.digest(data)
            with patch.object(manager, "read_url", return_value=data) as read:
                actual, metadata = manager.download_release("v2.3.5", arch, checksum)
            self.assertEqual(actual, binary)
            self.assertEqual(metadata["archive_sha256"], checksum)
            self.assertIn(f"/v2.3.5/cfst_linux_{arch}.tar.gz", read.call_args.args[0])
        with patch.object(manager, "read_url", return_value=data), self.assertRaises(manager.InstallError):
            manager.download_release("v2.3.5", "arm64", "0"*64)

    def test_invalid_version_and_checksum_do_not_download(self):
        with patch.object(manager, "read_url") as read:
            for version, arch, checksum in (("../../tag", "amd64", "a"*64), ("v2.3.5", "windows", "a"*64), ("v2.3.5", "amd64", "abc")):
                with self.subTest(version=version), self.assertRaises(manager.InstallError):
                    manager.download_release(version, arch, checksum)
            read.assert_not_called()

    def test_compatibility_child_does_not_receive_credentials_or_proxy(self):
        with patch.dict(os.environ, {"CF_API_TOKEN": "secret", "HTTPS_PROXY": "http://secret-proxy"}):
            env = manager.child_environment()
        self.assertNotIn("CF_API_TOKEN", env)
        self.assertNotIn("HTTPS_PROXY", env)

    @unittest.skipUnless(os.environ.get("CFST_TEST_BIN"), "Set CFST_TEST_BIN to verify the bundled binary compatibility probe")
    def test_bundled_binary_probe_with_official_cfst(self):
        manager.smoke_test(Path(os.environ["CFST_TEST_BIN"]).resolve(), "v2.3.5")
