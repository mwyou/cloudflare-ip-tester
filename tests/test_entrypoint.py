import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "docker/entrypoint.py"
spec = importlib.util.spec_from_file_location("cfip_entrypoint", MODULE_PATH)
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)


class EntryPointTests(unittest.TestCase):
    def test_root_prepares_mounted_data_directory_for_service_user(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "data"
            with patch.object(entrypoint, "DATA_DIR", directory), \
                    patch.object(entrypoint.os, "geteuid", return_value=0, create=True), \
                    patch.object(entrypoint.os, "chown", create=True) as chown:
                entrypoint.prepare_data_dir()
            self.assertTrue(directory.is_dir())
            chown.assert_called_once_with(directory, entrypoint.UID, entrypoint.GID,
                                           follow_symlinks=False)

    def test_non_root_start_does_not_attempt_chown(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "data"
            with patch.object(entrypoint, "DATA_DIR", directory), \
                    patch.object(entrypoint.os, "geteuid", return_value=entrypoint.UID, create=True), \
                    patch.object(entrypoint.os, "chown", create=True) as chown:
                entrypoint.prepare_data_dir()
            self.assertTrue(directory.is_dir())
            chown.assert_not_called()


if __name__ == "__main__":
    unittest.main()
