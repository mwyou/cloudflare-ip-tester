"""Verify deletion scope without contacting Docker Hub."""
import importlib.util
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "cleanup_versions", Path(__file__).resolve().parents[1] / ".github/scripts/cleanup_versions.py")
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class CleanupTests(unittest.TestCase):
    def run_cleanup(self, apply=False, matching=True):
        tags = {name: {"name": name, "digest": "sha256:new"} for name in
                ("latest", "3.2.19", "3.2.18", "3.2.9", "3.2.20", "dev")}
        if not matching:
            tags["latest"]["digest"] = "sha256:other"
        calls = []

        def open_request(request, timeout):
            calls.append((request.method, request.full_url))
            if request.full_url.endswith("/auth/token"):
                data = {"access_token": "temporary-test-token"}
            elif request.method == "DELETE":
                del tags[request.full_url.rstrip("/").rsplit("/", 1)[1]]
                data = None
            else:
                data = {"results": list(tags.values()), "next": None}
            return io.BytesIO(json.dumps(data).encode() if data is not None else b"")

        with patch.dict(os.environ, {"KEEP_VERSION": "3.2.19", "APPLY": str(apply).lower(),
                                    "DOCKERHUB_USERNAME": "cotelforce", "DOCKERHUB_TOKEN": "test"}), \
                patch.object(cleanup, "urlopen", side_effect=open_request), patch("sys.stdout", new=io.StringIO()):
            cleanup.main()
        return calls, tags

    def test_preview_does_not_delete(self):
        calls, tags = self.run_cleanup()
        self.assertFalse(any(method == "DELETE" for method, _ in calls))
        self.assertIn("3.2.18", tags)

    def test_apply_only_deletes_older_numeric_version_tags(self):
        calls, tags = self.run_cleanup(apply=True)
        deleted = [url for method, url in calls if method == "DELETE"]
        self.assertEqual(set(tags), {"latest", "3.2.19", "3.2.20", "dev"})
        self.assertEqual(len(deleted), 2)
        self.assertTrue(all(url.startswith(
            "https://hub.docker.com/v2/repositories/cotelforce/cloudflare-ip-tester/tags/") for url in deleted))

    def test_refuses_cleanup_if_latest_is_not_the_kept_image(self):
        with self.assertRaisesRegex(ValueError, "same published image"):
            self.run_cleanup(apply=True, matching=False)

    def test_version_boundary_and_non_version_tags(self):
        self.assertEqual(cleanup.older_tags(
            ["3.2.9", "3.2.19", "3.2.20", "4.0.0", "latest", "3.2.18-dev", "v3.2.18"], "3.2.19"), ["3.2.9"])
        for value in ("latest", "../3.2.19", "3.2.19\n", "v3.2.19"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cleanup.version(value)
