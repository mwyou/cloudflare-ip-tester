"""Persistent binary upgrades and rollback, without external DNS operations."""
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest.mock import Mock, patch

import automation as auto
import cfst_manager as manager


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


class PersistentCFSTTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.seed = self.root/"seed"
        self.seed.mkdir()
        binary, metadata = artifact()
        (self.seed/"cfst").write_bytes(binary)
        (self.seed/"manifest.json").write_text(json.dumps(metadata))
        self.store = manager.Store(self.root/"volume", "amd64")
        self.smoke = Mock()

    def initialize(self):
        return self.store.initialize(self.seed, smoke=self.smoke)

    def upgrade(self, version="v2.3.6"):
        binary, metadata = artifact(version)
        with patch.object(manager, "download_release", return_value=(binary, metadata)):
            self.store.upgrade(version, "a"*64, smoke=self.smoke)

    def test_first_start_initializes_volume_then_image_rebuild_keeps_active(self):
        binary, metadata = self.initialize()
        self.assertEqual(binary.read_bytes(), artifact()[0])
        self.assertEqual(metadata["version"], "v2.3.5")
        self.upgrade()
        original_state = self.store.state()
        # A recreated image brings a seed, which must never overwrite the chosen release.
        (self.seed/"cfst").write_bytes(b"a different image seed")
        active, info = manager.Store(self.store.home, "amd64").initialize(self.seed, smoke=self.smoke)
        self.assertEqual(info["version"], "v2.3.6")
        self.assertEqual(self.store.state(), original_state)
        self.assertEqual(active.read_bytes(), artifact("v2.3.6")[0])

    def test_upgrade_retains_previous_and_rollback_swaps_versions(self):
        old, _ = self.initialize()
        self.upgrade()
        new, _ = self.store.release(self.store.state()["active"])
        self.assertTrue(old.is_file())
        self.assertNotEqual(old, new)
        self.store.rollback(smoke=self.smoke)
        self.assertEqual(self.store.describe()["active"]["version"], "v2.3.5")
        self.assertEqual(self.store.describe()["previous"]["version"], "v2.3.6")
        self.store.rollback(smoke=self.smoke)
        self.assertEqual(self.store.describe()["active"]["version"], "v2.3.6")

    def test_failed_compatibility_never_switches_current(self):
        self.initialize()
        before = self.store.state_file.read_bytes()
        binary, metadata = artifact("v2.3.6")
        self.smoke.side_effect = manager.InstallError("incompatible")
        with patch.object(manager, "download_release", return_value=(binary, metadata)), self.assertRaises(manager.InstallError):
            self.store.upgrade("v2.3.6", "a"*64, smoke=self.smoke)
        self.assertEqual(self.store.state_file.read_bytes(), before)
        self.assertFalse(list((self.store.home/"releases").glob(".install-*")))

    def test_download_error_never_switches_current(self):
        self.initialize()
        before = self.store.state_file.read_bytes()
        with patch.object(manager, "download_release", side_effect=manager.InstallError("checksum")), self.assertRaises(manager.InstallError):
            self.store.upgrade("v2.3.6", "a"*64, smoke=self.smoke)
        self.assertEqual(self.store.state_file.read_bytes(), before)

    def test_initialization_failure_does_not_publish_partial_release(self):
        self.smoke.side_effect = manager.InstallError("incompatible")
        with self.assertRaises(manager.InstallError):
            self.initialize()
        self.assertFalse(self.store.state_file.exists())

    def test_repeat_upgrade_does_not_lose_rollback_target(self):
        self.initialize()
        self.upgrade()
        previous = self.store.state()["previous"]
        self.upgrade()
        self.assertEqual(self.store.state()["previous"], previous)

    def test_hash_and_linux_arch_are_checked_before_execution(self):
        binary, metadata = artifact(arch="arm64")
        with self.assertRaises(manager.InstallError):
            self.store.stage(binary, metadata, smoke=self.smoke)
        self.smoke.assert_not_called()
        binary, metadata = artifact()
        with self.assertRaises(manager.InstallError):
            self.store.stage(binary+b"changed", metadata, smoke=self.smoke)
        self.smoke.assert_not_called()

    def test_corrupt_active_binary_refuses_start_instead_of_silent_downgrade(self):
        binary, _ = self.initialize()
        binary.chmod(0o644)
        binary.write_bytes(b"corrupt")
        with self.assertRaises(manager.InstallError):
            self.initialize()

    def test_rollback_rechecks_previous_before_switch(self):
        self.initialize()
        self.upgrade()
        previous, _ = self.store.release(self.store.state()["previous"])
        previous.chmod(0o644)
        previous.write_bytes(b"corrupt")
        before = self.store.state_file.read_bytes()
        with self.assertRaises(manager.InstallError):
            self.store.rollback(smoke=self.smoke)
        self.assertEqual(self.store.state_file.read_bytes(), before)

    def test_no_previous_release_refuses_rollback(self):
        self.initialize()
        with self.assertRaises(manager.InstallError):
            self.store.rollback(smoke=self.smoke)

    def test_pointer_cannot_escape_version_directory(self):
        self.store.home.mkdir()
        for value in ("../elsewhere", "/tmp/cfst", "v2.3.5-amd64-abc"):
            with self.subTest(value=value):
                manager.atomic_json(self.store.state_file, {"active": value, "previous": None})
                with self.assertRaises(manager.InstallError):
                    self.store.state()

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

    def test_maintenance_refuses_to_change_binary_while_service_holds_lock(self):
        data = self.root/"data"
        data.mkdir()
        lock = auto.acquire_lock(data/"worker.lock")
        try:
            with patch.dict(os.environ, {"DATA_DIR": str(data), "CFST_HOME": str(self.store.home)}), patch.object(manager.Store, "upgrade") as upgrade, self.assertRaises(auto.ServiceError):
                manager.main(["upgrade", "--version", "v2.3.6", "--sha256", "a"*64])
            upgrade.assert_not_called()
        finally:
            lock.close()

    def test_entrypoint_exports_active_version_and_path_to_service(self):
        self.initialize()
        data = self.root/"data"
        with patch.dict(os.environ, {"DATA_DIR": str(data), "CFST_HOME": str(self.store.home), "CFST_BIN": "/wrong"}), patch.object(manager.os, "execve") as execute:
            manager.main(["--once", "--dry-run"])
        args = execute.call_args.args
        self.assertEqual(args[1][-2:], ["--once", "--dry-run"])
        self.assertEqual(args[2]["CFST_VERSION"], "v2.3.5")
        self.assertEqual(Path(args[2]["CFST_BIN"]), self.store.release(self.store.state()["active"])[0])
        cfg = replace(auto.Settings("cf.example.com"), cfst_version="v2.3.6")
        self.assertEqual(auto.Status(None, cfg).snapshot()["engine"], "CFST v2.3.6")

    def test_compatibility_child_does_not_receive_credentials_or_proxy(self):
        with patch.dict(os.environ, {"CF_API_TOKEN": "secret", "HTTPS_PROXY": "http://secret-proxy"}):
            env = manager.child_environment()
        self.assertNotIn("CF_API_TOKEN", env)
        self.assertNotIn("HTTPS_PROXY", env)

    @unittest.skipUnless(os.environ.get("CFST_TEST_BIN"), "Set CFST_TEST_BIN to verify the upgrade compatibility probe")
    def test_upgrade_probe_with_official_cfst(self):
        manager.smoke_test(Path(os.environ["CFST_TEST_BIN"]).resolve(), "v2.3.5")


if __name__ == "__main__":
    unittest.main(verbosity=2)
