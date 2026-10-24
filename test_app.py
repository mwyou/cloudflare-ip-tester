"""Offline regression tests: no real token or public download traffic."""
import http.client
import json
import ssl
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import app
import benchmark
import cloudflare


class DownloadFixture(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Host")))
        body = b"x" * 131_072
        if self.path == "/429":
            self.send_response(429)
            self.send_header("Content-Length", "0")
            self.send_header("Retry-After", "60")
            self.end_headers()
            return
        if self.path == "/403":
            self.send_response(403)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        if self.path == "/html":
            self.send_header("Content-Type", "text/html")
        else:
            self.send_header("Content-Type", "application/octet-stream")
        self.send_header("CF-Ray", "fixture-SIN")
        if self.path == "/chunked":
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for part in (body[:65_536], body[65_536:]):
                self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        elif self.path in ("/trickle", "/stall"):
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            try:
                if self.path == "/trickle":
                    for _ in range(50):
                        self.wfile.write(b"x" * 32_768)
                        self.wfile.flush()
                        time.sleep(.05)
                else:
                    time.sleep(1)
            except OSError:
                pass
        elif self.path == "/truncated":
            self.send_header("Content-Length", "200000")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
        else:
            if self.path == "/small":
                body = b"tiny"
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass


class QuietApp(app.AppHandler):
    def log_message(self, *args):
        pass


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = ThreadingHTTPServer(("127.0.0.1", 0), DownloadFixture)
        cls.fixture.daemon_threads = True
        cls.fixture.seen = []
        cls.web = ThreadingHTTPServer(("127.0.0.1", 0), QuietApp)
        cls.web.daemon_threads = True
        for server in (cls.fixture, cls.web):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.fixture_port = cls.fixture.server_address[1]
        cls.context = ssl.create_default_context()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.fixture, cls.web):
            server.shutdown()
            server.server_close()

    def measure(self, path, timeout=2, cancel=None):
        return benchmark.measure_once("127.0.0.1", f"http://fixture.example:{self.fixture_port}{path}",
                                      timeout, cancel or threading.Event(), self.context)

    def request(self, path, data=None, cookie=None, origin=None, local_header=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.web.server_address[1], timeout=5)
        headers = {}
        if cookie:
            headers["Cookie"] = cookie
        if origin:
            headers["Origin"] = origin
        if data is not None:
            headers["Content-Type"] = "application/json"
            if local_header:
                headers["X-Local-Client"] = "cfip-desk"
        connection.request("GET" if data is None else "POST", path,
                           None if data is None else json.dumps(data), headers)
        response = connection.getresponse()
        body = response.read()
        result = (response.status, dict(response.getheaders()), body)
        connection.close()
        return result

    def test_chunked_counts_payload_only_and_pins_host(self):
        result = self.measure("/chunked")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["bytes"], 131_072)
        self.assertEqual(result["colo"], "SIN")
        self.assertEqual(self.fixture.seen[-1][1], f"fixture.example:{self.fixture_port}")

    def test_connection_close_keeps_response_socket_alive(self):
        # The custom file wrapper must retain the socket after HTTPConnection
        # releases its own handle on a Connection: close response.
        result = self.measure("/complete")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["bytes"], 131_072)

    def test_error_bodies_are_never_speed_results(self):
        for path in ("/429", "/403", "/html", "/small", "/truncated"):
            with self.subTest(path=path):
                result = self.measure(path)
                self.assertFalse(result["ok"], result)
                self.assertNotIn("speed_MBps", result)

    def test_deadline_stops_trickle_and_retains_valid_sample(self):
        start = time.perf_counter()
        result = self.measure("/trickle", timeout=.45)
        self.assertLess(time.perf_counter() - start, 1.5)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["partial"])

    def test_cancel_interrupts_stalled_download(self):
        stop = threading.Event()
        timer = threading.Timer(.15, stop.set)
        timer.start()
        start = time.perf_counter()
        result = self.measure("/stall", cancel=stop)
        self.assertLess(time.perf_counter() - start, 1)
        self.assertEqual(result["kind"], "cancelled")

    def test_zone_parent_lookup(self):
        def fake_api(path, token, params=None):
            return {"result": [{"name": "example.com", "id": "a" * 32}]} if params["name"] == "example.com" else {"result": []}
        with patch.object(cloudflare, "cf_api", side_effect=fake_api) as mock:
            self.assertEqual(cloudflare.get_zone_id("cf.sub.example.com", "fake"), "a" * 32)
            self.assertEqual(mock.call_count, 3)

    def test_dns_pagination_dedup_and_exact_filter(self):
        def fake_api(path, token, params=None):
            if params["type"] == "AAAA":
                return {"result": [{"name": "cf.example.com", "content": "2606:4700::1111"}]}
            if params["page"] == 1:
                return {"result_info": {"total_pages": 2}, "result": [
                    {"name": "cf.example.com", "content": "104.18.13.1"},
                    {"name": "wrong.example.com", "content": "104.18.13.2"}]}
            return {"result_info": {"total_pages": 2}, "result": [
                {"name": "cf.example.com", "content": "104.18.13.1"},
                {"name": "cf.example.com", "content": "192.168.1.1"}]}
        with patch.object(cloudflare, "cf_api", side_effect=fake_api):
            data = cloudflare.fetch_records("cf.example.com", "fake", "a" * 32)
        self.assertEqual(len(data["records"]), 3)
        self.assertFalse(next(r for r in data["records"] if r["ip"] == "192.168.1.1")["eligible"])

    def test_validation(self):
        for url in ("http://localhost:8080/x", "file:///test", "https://x.example/a\r\nX:yes", "https://u:p@x.example/", "https://127.0.0.1/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                benchmark.validate_url(url)
        self.assertEqual(cloudflare.validate_domain("BestCF.DPDNS.org."), "bestcf.dpdns.org")
        self.assertFalse(cloudflare.is_cloudflare_ip("127.0.0.1"))
        self.assertTrue(cloudflare.is_cloudflare_ip("104.18.13.1"))
        with self.assertRaises(ValueError):
            cloudflare.validate_domain("bad..example.com")

    def test_aggregate_excludes_errors_and_keeps_success_count(self):
        good = {"ok": True, "speed_MBps": 8, "first_byte_ms": 100, "status": 200}
        failed = {"ok": False, "status": 429, "error": "rate limited"}
        result = benchmark.aggregate({"ip": "104.18.13.1"}, [good, failed], 2)
        self.assertEqual(result["avg_speed_MBps"], 8)
        self.assertEqual(result["successful_runs"], 1)

    def test_local_origin_and_header_required(self):
        payload = {"token": "fixture-token", "domain": "cf.example.com"}
        self.assertEqual(self.request("/api/records", payload, origin="https://evil.example")[0], 403)
        self.assertEqual(self.request("/api/records", payload, local_header=False)[0], 403)

    def test_records_job_progress_and_session_isolation(self):
        data = {"domain": "cf.example.com", "zone_id": "a" * 32,
                "records": [{"ip": "104.18.13.1", "type": "A", "eligible": True, "proxied": False}], "truncated": False}
        with patch.object(app, "fetch_records", return_value=data):
            status, headers, body = self.request("/api/records", {"token": "fixture-token", "domain": "cf.example.com"})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertNotIn(b"fixture-token", body)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        args = {"snapshot_id": result["snapshot_id"], "ips": ["104.18.13.1"], "rounds": 1, "timeout": 2}
        rate = {"ok": False, "kind": "rate_limited", "status": 429, "error": "限流", "bytes": 0}
        with patch.object(app, "measure_once", return_value=rate):
            status, _, body = self.request("/api/jobs", args, cookie=cookie)
            self.assertEqual(status, 202)
            job_id = json.loads(body)["id"]
            for _ in range(40):
                _, _, body = self.request(f"/api/jobs/{job_id}", cookie=cookie)
                view = json.loads(body)
                if view["state"] == "rate_limited":
                    break
                time.sleep(.05)
        self.assertEqual(view["state"], "rate_limited")
        self.assertIsNone(view["results"][0]["avg_speed_MBps"])
        self.assertEqual(self.request(f"/api/jobs/{job_id}")[0], 404)
        args["ips"] = ["127.0.0.1"]
        self.assertEqual(self.request("/api/jobs", args, cookie=cookie)[0], 400)


def preview_server():
    """Opt-in isolated UI fixture; never used by app.py or the normal launcher."""
    def records(domain, token, zone=""):
        if token != "ui-fixture-token":
            raise ValueError("界面测试专用：请填写模拟 Token ui-fixture-token")
        return {"domain": domain, "zone_id": "a" * 32, "truncated": False, "records": [
            {"ip": ip, "type": "A", "eligible": True, "proxied": False}
            for ip in ("104.18.13.1", "104.18.13.251", "104.18.13.39")]}

    def measure(ip, url, timeout, cancel, context):
        if cancel.wait(1):
            return {"ok": False, "kind": "cancelled", "error": "已停止", "bytes": 0}
        if "/429" in url:
            return {"ok": False, "kind": "rate_limited", "status": 429, "error": "模拟 429 限流", "bytes": 0}
        return {"ok": True, "status": 200, "kind": "complete", "bytes": 10_000_000,
                "speed_MBps": {"104.18.13.1": 10.2, "104.18.13.251": 12.1, "104.18.13.39": 7.3}[ip],
                "first_byte_ms": 180, "colo": "TEST", "small_sample": True, "partial": False}

    app.fetch_records = records
    app.measure_once = measure
    print("UI fixture only, synthetic results: http://127.0.0.1:8788", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", 8788), QuietApp)
    server.serve_forever()


if __name__ == "__main__":
    if "--preview" in sys.argv:
        preview_server()
    else:
        unittest.main(verbosity=2)
