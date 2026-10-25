"""Offline state-machine, DNS transaction and CFST subprocess contract tests."""
import csv
from dataclasses import replace
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import automation as auto

IPS = ["104.18.13.1", "104.18.13.2", "104.18.13.3"]
NEW = ["104.18.13.4", "104.18.13.5", "104.18.13.6"]
HEADER = ["IP 地址", "已发送", "已接收", "丢包率", "平均延迟", "下载速度(MB/s)", "地区码"]


def records(ips=IPS, proxied=False):
    return [{"id": str(i), "name": "cf.example.com", "type": "A", "content": ip,
             "proxied": proxied, "ttl": 60} for i, ip in enumerate(ips)]


def row(ip, speed=6, latency=80):
    return {"ip": ip, "speed_mibps": speed, "bandwidth_mbps": speed*1048576*8/1e6,
            "latency_ms": latency, "sent": 4, "received": 4, "loss": 0, "colo": "TEST"}


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.cfg = auto.Settings("cf.example.com", token="very-secret-token", data_dir=Path(self.folder.name))
        self.stop = threading.Event()
        self.status = auto.Status(None, self.cfg)
        self.client = Mock()
        self.client.records.return_value = records()
        self.runner = Mock()
        self.service = auto.Maintainer(self.cfg, self.client, self.runner, self.status, self.stop)

    def change_dns(self, before, after):
        self.client.records.return_value = records(before)
        self.service.read()
        if before:
            self.runner.run.return_value = [row(ip) for ip in before]
            self.service.check()
        self.runner.run.return_value = [row(ip, speed=10) for ip in after]
        with patch.object(auto, "candidate_pool", return_value=after):
            self.service.select()
        self.service.compare()
        self.service.write()

    def test_dns_history_keeps_two_previous_snapshots_newest_first(self):
        second = ["104.18.34.1", "104.18.34.2", "104.18.34.3"]
        self.change_dns(IPS, NEW)
        first_time = self.status.snapshot()["dns_history"][0]["time"]
        self.change_dns(NEW, second)
        self.change_dns(second, IPS)
        history = self.status.snapshot()["dns_history"]
        self.assertEqual([entry["ips"] for entry in history], [second, NEW])
        self.assertGreaterEqual(history[0]["time"], history[1]["time"])
        self.assertGreaterEqual(history[1]["time"], first_time)
        self.assertEqual(self.status.snapshot()["dns_ips"], IPS)
        self.assertNotIn("dns_change", self.status.snapshot())

    def test_unchanged_or_failed_write_preserves_dns_history(self):
        self.change_dns(IPS, NEW)
        history = self.status.snapshot()["dns_history"]
        self.change_dns(NEW, list(reversed(NEW)))
        self.assertEqual(self.status.snapshot()["dns_history"], history)
        self.client.replace.side_effect = auto.ServiceError("write verification failed")
        with self.assertRaises(auto.ServiceError):
            self.change_dns(NEW, IPS)
        self.assertEqual(self.status.snapshot()["dns_history"], history)
        self.assertEqual(set(self.status.snapshot()["dns_ips"]), set(NEW))

    def test_empty_dns_snapshot_is_recorded_on_first_write(self):
        self.change_dns([], NEW)
        self.assertEqual(self.status.snapshot()["dns_history"][0]["ips"], [])

    def test_history_limit_changes_trim_existing_and_limit_future_writes(self):
        controller = auto.TaskController(self.service)
        controller.set_history_count(3)
        for before, after in ((IPS, NEW), (NEW, IPS), (IPS, NEW), (NEW, IPS)):
            self.change_dns(before, after)
        self.assertEqual([h["ips"] for h in self.status.snapshot()["dns_history"]], [NEW, IPS, NEW])
        controller.set_history_count(1)
        self.assertEqual(len(self.status.snapshot()["dns_history"]), 1)
        controller.set_history_count(0)
        self.change_dns(IPS, NEW)
        self.assertEqual(self.status.snapshot()["dns_history"], [])
        for value in (True, -1, 11, 1.5):
            with self.subTest(value=value), self.assertRaises(auto.ServiceError):
                controller.set_history_count(value)

    def test_history_limit_and_jitter_restore_from_status_without_restoring_writes(self):
        path = Path(self.folder.name) / "status.json"
        state = auto.Status(path, self.cfg)
        state.update(dns_history_count=4, dns_history=[{"ips": IPS, "time": i} for i in range(4)],
                     jitter_min_seconds=10, jitter_max_seconds=300, proposed_ips=NEW)
        restored = auto.Status(path, self.cfg).snapshot()
        self.assertEqual(len(restored["dns_history"]), 4)
        self.assertEqual((restored["jitter_min_seconds"], restored["jitter_max_seconds"]), (10, 300))
        self.assertEqual(restored["proposed_ips"], [])
        state.update(dns_history_count=True, jitter_min_seconds=100, jitter_max_seconds=10)
        restored = auto.Status(path, self.cfg).snapshot()
        self.assertEqual(restored["dns_history_count"], 2)
        self.assertEqual(restored["jitter_max_seconds"], 0)

    def test_dns_history_reloads_and_migrates_old_status(self):
        path = Path(self.folder.name)/"status.json"
        path.write_text(json.dumps({"domain": self.cfg.domain, "record_type": "A",
            "dns_change": {"before": IPS, "after": NEW, "time": 123}}))
        state = auto.Status(path, self.cfg)
        self.assertEqual(state.snapshot()["dns_history"], [{"ips": IPS, "time": 123}])
        state.update(dns_history=[{"ips": NEW, "time": 456}, {"ips": IPS, "time": 123}])
        self.assertEqual(auto.Status(path, self.cfg).snapshot()["dns_history"], state.snapshot()["dns_history"])
        self.assertNotIn("dns_change", json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(auto.Status(path, replace(self.cfg, domain="other.example.com")).snapshot()["dns_history"], [])

    def test_required_configuration_and_secret_never_in_repr(self):
        with patch.dict(os.environ, {"CF_DOMAIN": "bestcf.dpdns.org", "CF_API_TOKEN": "secret"}, clear=True):
            settings = auto.Settings.from_env()
            self.assertEqual(settings.min_mbps, 30)
            self.assertNotIn("secret", repr(settings))
        for env in ({"CF_DOMAIN": "cf.example.com"}, {"CF_DOMAIN": "cf.example.com", "CF_API_TOKEN": "t", "CF_API_KEY": "k"},
                    {"CF_DOMAIN": "cf.example.com", "CF_API_TOKEN": "t", "MIN_BANDWIDTH_MBPS": "nan"},
                    {"CF_DOMAIN": "cf.example.com", "CF_API_TOKEN": "t", "CHECK_INTERVAL_SECONDS": "0"}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
                auto.Settings.from_env()
        with patch.dict(os.environ, {"CF_DOMAIN": "cf.example.com", "CF_API_TOKEN": "t", "CHECK_INTERVAL_SECONDS": "60"}, clear=True):
            self.assertEqual(auto.Settings.from_env().interval, 60)

    def test_secret_file_and_global_key(self):
        secret = Path(self.folder.name)/"secret"
        secret.write_text("file-token\n")
        with patch.dict(os.environ, {"CF_DOMAIN": "cf.example.com", "CF_API_TOKEN_FILE": str(secret)}, clear=True):
            self.assertEqual(auto.Settings.from_env().token, "file-token")
        with patch.dict(os.environ, {"CF_DOMAIN": "cf.example.com", "CF_API_KEY": "global", "CF_EMAIL": "a@example.com"}, clear=True):
            self.assertEqual(auto.Settings.from_env().api_key, "global")

    def test_input_validation(self):
        for url in ("http://localhost:8080/x", "file:///test", "https://x.example/a\r\nX:yes",
                    "https://u:p@x.example/", "https://127.0.0.1/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                auto.validate_url(url)
        self.assertEqual(auto.validate_domain("BestCF.DPDNS.org."), "bestcf.dpdns.org")
        self.assertFalse(auto.is_cloudflare_ip("127.0.0.1"))
        self.assertTrue(auto.is_cloudflare_ip("104.18.13.1"))
        with self.assertRaises(ValueError):
            auto.validate_domain("bad..example.com")

    def test_thresholds_use_mbps_and_strict_bandwidth(self):
        exact = 30e6/8/1048576
        self.assertFalse(auto.qualifies(self.cfg, row(IPS[0], speed=exact)))
        self.assertTrue(auto.qualifies(self.cfg, row(IPS[0], speed=exact+.01, latency=100)))
        self.assertFalse(auto.qualifies(self.cfg, row(IPS[0], speed=10, latency=100.01)))
        self.assertTrue(auto.qualifies(self.cfg, row(IPS[0], speed=3.58)))

    def test_healthy_hourly_check_makes_no_dns_write_or_selection(self):
        self.runner.run.return_value = [row(ip) for ip in IPS]
        self.service.cycle()
        self.runner.run.assert_called_once_with(IPS, 3)
        self.client.replace.assert_not_called()
        self.assertEqual(self.status.snapshot()["phase"], "healthy")

    def test_first_read_adopts_existing_dns_record_count(self):
        existing = IPS + [NEW[0]]
        self.client.records.return_value = records(existing)
        self.service.read()
        self.assertEqual(self.service.record_count, 4)
        self.assertEqual(self.status.snapshot()["dns_record_count"], 4)

    def test_first_read_without_dns_defaults_to_three_records(self):
        self.client.records.return_value = []
        self.service.read()
        self.assertEqual(self.service.record_count, 3)
        self.assertEqual(self.status.snapshot()["dns_record_count"], 3)

    def test_reselection_combines_old_and_new_then_writes_final_three(self):
        self.runner.run.side_effect = [[row(ip, latency=101 if i == 0 else 70) for i, ip in enumerate(IPS)],
                                       [row(NEW[0], speed=4), row(NEW[1], speed=8), row(NEW[2], speed=6), row(IPS[0], speed=3)],
                                       ]
        with patch.object(auto, "candidate_pool", return_value=IPS+NEW):
            self.service.cycle()
        self.assertEqual([call.args for call in self.runner.run.call_args_list], [(IPS, 3), (IPS+NEW, 10)])
        self.client.replace.assert_called_once_with(records(), [NEW[1], IPS[1], IPS[2]])
        snapshot = self.status.snapshot()
        self.assertEqual(snapshot["dns_ips"], [NEW[1], IPS[1], IPS[2]])
        self.assertEqual(len(snapshot["check_results"]), 3)
        self.assertEqual(len(snapshot["selection_results"]), 4)
        self.assertEqual([row["ip"] for row in snapshot["ranked_results"][:3]], [NEW[1], IPS[1], IPS[2]])
        self.assertEqual(snapshot["dns_history"][0]["ips"], IPS)
        self.assertIsInstance(snapshot["dns_history"][0]["time"], float)

    def test_initial_start_selects_even_if_old_dns_is_healthy(self):
        self.runner.run.return_value = [row(ip) for ip in IPS]
        with patch.object(auto, "candidate_pool", return_value=NEW):
            self.service.cycle(initial=True)
        self.runner.run.assert_called_once_with(IPS, 3)
        self.client.replace.assert_not_called()

    def test_start_without_records_bootstraps(self):
        self.client.records.return_value = []
        self.runner.run.return_value = [row(ip, speed=8) for ip in NEW]
        with patch.object(auto, "candidate_pool", return_value=NEW):
            self.service.cycle()
        self.client.replace.assert_called_once_with([], NEW)

    def test_healthy_old_results_are_usable_when_new_candidates_are_insufficient(self):
        self.runner.run.side_effect = [[row(ip) for ip in IPS], [row(NEW[0]), row(NEW[1]), row(NEW[2], speed=1)]]
        with patch.object(auto, "candidate_pool", return_value=NEW):
            self.service.cycle(initial=True)
        self.runner.run.assert_called_once_with(IPS, 3)
        self.client.replace.assert_not_called()
        self.assertEqual(self.status.snapshot()["phase"], "healthy")

    def test_overlapping_ips_keep_both_samples_but_write_unique_ips(self):
        initial = [row(IPS[0], speed=10), row(IPS[1], speed=8), row(IPS[2], speed=6)]
        selected = [row(NEW[0], speed=8), row(NEW[1], speed=6), row(NEW[2], speed=4)]
        self.runner.run.side_effect = [initial, selected]
        with patch.object(auto, "candidate_pool", return_value=IPS+NEW):
            self.service.cycle(initial=True)
        self.runner.run.assert_called_once_with(IPS, 3)
        self.client.replace.assert_not_called()

    def test_fewer_than_three_new_qualifiers_can_combine_with_old_ips(self):
        self.runner.run.side_effect = [[row(ip, speed=6) for ip in IPS],
                                      [row(NEW[0], speed=8), row(NEW[1], speed=1), row(NEW[2], speed=1)]]
        with patch.object(auto, "candidate_pool", return_value=NEW):
            self.service.cycle(initial=True)
        self.runner.run.assert_called_once_with(IPS, 3)
        self.client.replace.assert_not_called()

    def test_low_speed_selection_results_can_still_be_ranked(self):
        self.runner.run.side_effect = [[row(IPS[0], latency=110), row(IPS[1]), row(IPS[2])],
                                      [row(ip, speed=1) for ip in NEW]]
        with patch.object(auto, "candidate_pool", return_value=NEW):
            self.service.cycle(initial=True)
        self.client.replace.assert_called_once_with(records(), [IPS[1], IPS[2], IPS[0]])
        self.assertEqual(self.status.snapshot()["phase"], "updated")
        self.assertTrue(all(r["speed_mibps"] == 1 for r in self.status.snapshot()["selection_results"]))

    def test_rate_limit_during_selection_preserves_dns(self):
        self.runner.run.side_effect = [[row(IPS[0], latency=110), row(IPS[1]), row(IPS[2])], auto.RateLimited("429")]
        with patch.object(auto, "candidate_pool", return_value=NEW), self.assertRaises(auto.RateLimited):
            self.service.cycle(initial=True)
        self.client.replace.assert_not_called()

    def test_equal_final_speeds_prefer_lower_latency(self):
        current = [row(IPS[0], speed=6, latency=101), row(IPS[1], speed=6, latency=70), row(IPS[2], speed=6, latency=80)]
        selected = [row(NEW[0], speed=6, latency=60)]
        self.runner.run.side_effect = [current, selected]
        with patch.object(auto, "candidate_pool", return_value=NEW):
            self.service.cycle(initial=True)
        self.client.replace.assert_called_once_with(records(), [NEW[0], IPS[1], IPS[2]])

    def test_read_error_does_not_start_speedtest(self):
        self.client.records.side_effect = auto.ServiceError("API unavailable")
        with self.assertRaises(auto.ServiceError):
            self.service.cycle()
        self.runner.run.assert_not_called()
        self.client.replace.assert_not_called()

    def test_rate_limit_in_check_never_triggers_selection_or_dns_write(self):
        self.runner.run.side_effect = auto.RateLimited("429")
        with patch.object(auto, "candidate_pool") as pool, self.assertRaises(auto.RateLimited):
            self.service.cycle()
        pool.assert_not_called()
        self.client.replace.assert_not_called()

    def test_dry_run_proposes_without_write(self):
        self.service.dry_run = True
        self.runner.run.return_value = [row(ip) for ip in NEW]
        with patch.object(auto, "candidate_pool", return_value=NEW):
            self.service.cycle(initial=True)
        self.client.replace.assert_not_called()
        self.assertEqual(self.status.snapshot()["proposed_ips"], NEW)

    def test_dns_plan_removes_extra_and_duplicate_records_preserves_other_types(self):
        before = records(IPS+[IPS[0]]) + [{"id": "txt", "type": "TXT", "content": "keep"},
                                         {"id": "v6", "type": "AAAA", "content": "2606:4700::1111"}]
        before[0]["proxied"] = True
        plan = auto.dns_plan(self.cfg, before, [IPS[0], NEW[0], NEW[1]])
        self.assertEqual(plan["deletes"], [{"id": "1"}, {"id": "2"}, {"id": "3"}])
        self.assertEqual(plan["patches"], [{"id": "0", "proxied": False, "ttl": 60}])
        self.assertEqual([r["content"] for r in plan["posts"]], NEW[:2])
        self.assertTrue(all(not r["proxied"] for r in plan["posts"]))

    def test_dns_plan_rejects_conflict_and_incomplete_set(self):
        for before, ips in (([{"type": "CNAME"}], NEW), (records(), NEW[:2]), (records(), [*NEW[:2], "127.0.0.1"])):
            with self.subTest(ips=ips), self.assertRaises(auto.ServiceError):
                auto.dns_plan(self.cfg, before, ips)

    def test_dns_changed_while_measuring_does_not_overwrite(self):
        client = auto.CloudflareClient(self.cfg)
        client.records = Mock(return_value=records(NEW))
        client.request = Mock()
        with self.assertRaises(auto.ServiceError):
            client.replace(records(), NEW)
        client.request.assert_not_called()

    def test_batch_write_is_one_request_and_verified_afterward(self):
        client = auto.CloudflareClient(replace(self.cfg, zone_id="a"*32))
        client.records = Mock(side_effect=[records(), records(NEW)])
        client.request = Mock()
        client.replace(records(), NEW)
        self.assertEqual(client.request.call_count, 1)
        self.assertEqual(client.request.call_args.args[0], f"/zones/{'a'*32}/dns_records/batch")
        self.assertEqual(client.records.call_count, 2)

    def test_write_error_not_retried(self):
        client = auto.CloudflareClient(replace(self.cfg, zone_id="a"*32))
        client.records = Mock(return_value=records())
        client.request = Mock(side_effect=auto.ServiceError("timeout"))
        with self.assertRaises(auto.ServiceError):
            client.replace(records(), NEW)
        self.assertEqual(client.request.call_count, 1)

    def test_cfst_network_sources_and_authoritative_file(self):
        pool = auto.candidate_pool(self.cfg, IPS)
        self.assertEqual(pool, [str(net) for net in auto.CF_NETWORKS if net.version == 4])
        self.assertEqual(len(set(pool)), 15)
        self.assertEqual(auto.candidate_count(pool), 5956)
        self.assertTrue(all('/' in value for value in pool))
        path = Path(self.folder.name)/"ips.txt"
        path.write_text("104.16.0.0/13\n" + "\n".join(NEW))
        custom = auto.candidate_pool(replace(self.cfg, candidate_file=str(path)), IPS)
        self.assertEqual(custom, ["104.16.0.0/13", *NEW])
        path.write_text("127.0.0.1\n"+"\n".join(NEW))
        with self.assertRaises(auto.ServiceError):
            auto.candidate_pool(replace(self.cfg, candidate_file=str(path)), [])

    def test_csv_validation_and_unit_conversion(self):
        path = Path(self.folder.name)/"result.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            csv.writer(stream).writerows([HEADER, [IPS[0],4,4,0,80,4,"TEST"], [IPS[1],4,4,0,75,6,"TEST"]])
        parsed = auto.parse_csv(path, set(IPS))
        self.assertEqual(parsed[0]["ip"], IPS[1])
        self.assertAlmostEqual(parsed[0]["bandwidth_mbps"], 50.332, places=3)
        with self.assertRaises(auto.ServiceError):
            auto.parse_csv(path, set(NEW))
        self.assertEqual(len(auto.parse_csv(path, ["104.18.13.0/24"])), 2)
        with self.assertRaises(auto.ServiceError):
            auto.parse_csv(path, ["104.18.14.0/24"])

    def test_cidr_validation_and_small_ranges(self):
        self.assertTrue(auto._valid_source("104.18.0.0/16", "A"))
        self.assertFalse(auto._valid_source("0.0.0.0/0", "A"))
        self.assertFalse(auto._valid_source("104.16.0.0/12", "A"))
        self.assertFalse(auto._valid_source("2606:4700::/32", "AAAA"))
        self.assertFalse(auto._valid_source("104.18.0.0/16", "AAAA"))
        self.assertEqual(auto.candidate_count(["104.18.13.0/24", "104.18.14.0/23", IPS[0]]), 4)
        path = Path(self.folder.name)/"cidr.txt"
        path.write_text("104.18.0.0/16")
        self.assertEqual(auto.candidate_pool(replace(self.cfg, candidate_file=str(path)), IPS), ["104.18.0.0/16"])
        ipv6 = auto.candidate_pool(replace(self.cfg, kind="AAAA"), [])
        self.assertEqual(len(ipv6), 128)
        self.assertTrue(all(auto._valid_source(ip, "AAAA") for ip in ipv6))

    def test_cidr_runner_passes_networks_without_reducing_download_count(self):
        script = Path(self.folder.name)/"cidr-cfst.py"
        script.write_text('''import csv,pathlib,sys
a=sys.argv[1:]
assert pathlib.Path(a[a.index('-f')+1]).read_text().strip()=='104.18.0.0/16'
assert a[a.index('-dn')+1]=='10'
assert a[a.index('-p')+1]=='10'
with open(a[a.index('-o')+1], 'w', encoding='utf-8', newline='') as f:
 w=csv.writer(f)
 w.writerow(['IP 地址','已发送','已接收','丢包率','平均延迟','下载速度(MB/s)','地区码'])
 for i in range(12): w.writerow(['104.18.13.'+str(i+1),4,4,0,70,5 if i<10 else 0,'TEST'])
''', encoding="utf-8")
        original = subprocess.Popen
        def launch(args, **kwargs):
            return original([sys.executable, str(script), *args[1:]], **kwargs)
        with patch.object(auto.subprocess, "Popen", side_effect=launch):
            rows = auto.CFSTRunner(self.cfg, self.stop).run(["104.18.0.0/16"], 10)
        self.assertEqual(len(rows), 12)
        self.assertEqual(sum(r['speed_mibps'] > 0 for r in rows), 10)

    def test_persisted_status_excludes_credentials(self):
        path = Path(self.folder.name)/"status.json"
        state = auto.Status(path, self.cfg)
        state.update(message="ok", results=[row(IPS[0])])
        self.assertNotIn(self.cfg.token, path.read_text())
        self.assertNotIn("token", path.read_text())

    def test_dashboard_preview_has_no_credentials_and_rejects_unprotected_post(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), auto.make_dashboard(self.status))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
            conn.request("GET", "/api/status")
            response = conn.getresponse()
            body = response.read()
            self.assertEqual(response.status, 200)
            self.assertNotIn(self.cfg.token.encode(), body)
            conn.request("POST", "/api/status", body=b"{}")
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 403)
            conn.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_cfst_command_contract_and_credentials_removed(self):
        # Exercise the real orchestration with a process fixture, not mocked subprocess output.
        script = Path(self.folder.name)/"fake-cfst.py"
        script.write_text('''import csv,json,os,pathlib,sys
a=sys.argv[1:]
assert 'CF_API_TOKEN' not in os.environ
assert a[a.index('-dt')+1]=='10'
assert a[a.index('-n')+1]=='200'
assert a[a.index('-t')+1]=='4'
assert a[a.index('-p')+1]=='3'
assert a[a.index('-tl')+1]=='9999'
assert a[a.index('-sl')+1]=='0'
ips=pathlib.Path(a[a.index('-f')+1]).read_text().splitlines()
with open(a[a.index('-o')+1], 'w', encoding='utf-8', newline='') as f:
 w=csv.writer(f)
 w.writerow(['IP 地址','已发送','已接收','丢包率','平均延迟','下载速度(MB/s)','地区码'])
 for ip in ips:w.writerow([ip,4,4,0,70,5,'TEST'])
''', encoding="utf-8")
        original = subprocess.Popen

        def launch(args, **kwargs):
            return original([sys.executable, str(script), *args[1:]], **kwargs)

        with patch.object(auto.subprocess, "Popen", side_effect=launch), patch.dict(os.environ, {"CF_API_TOKEN": "never-in-child"}):
            result = auto.CFSTRunner(self.cfg, self.stop).run(IPS, 3)
        self.assertEqual(len(result), 3)

    def test_cfst_429_aborts_subprocess_promptly(self):
        script = Path(self.folder.name)/"limited.py"
        script.write_text("import time\nprint('[调试] 下载测速终止，HTTP 状态码: 429',flush=True)\ntime.sleep(10)\n", encoding="utf-8")
        original = subprocess.Popen

        def launch(args, **kwargs):
            return original([sys.executable, '-X', 'utf8', str(script)], **kwargs)

        start = time.monotonic()
        with patch.object(auto.subprocess, "Popen", side_effect=launch), self.assertRaises(auto.RateLimited):
            auto.CFSTRunner(self.cfg, self.stop).run(IPS, 3)
        self.assertLess(time.monotonic()-start, 3)


class ManualSteps(unittest.TestCase):
    def setUp(self):
        self.cfg = auto.Settings("cf.example.com", token="secret-token")
        self.status = auto.Status(None, self.cfg)
        self.client = Mock()
        self.client.records.return_value = records()
        self.runner = Mock()
        self.runner.run.return_value = [row(ip, speed=8) for ip in NEW]
        self.stop = threading.Event()
        self.service = auto.Maintainer(self.cfg, self.client, self.runner, self.status, self.stop)
        self.controller = auto.TaskController(self.service)

    def run_step(self, action):
        self.controller.start(action)
        self.controller.worker.join(timeout=3)
        self.assertFalse(self.controller.worker.is_alive())
        self.assertFalse(self.status.snapshot()["busy"])

    def prepare_final(self):
        self.run_step("read")
        if self.service.valid_ips():
            previous = self.runner.run.return_value
            self.runner.run.return_value = [row(ip) for ip in IPS]
            self.run_step("check")
            self.runner.run.return_value = previous
        with patch.object(auto, "candidate_pool", return_value=NEW):
            self.run_step("select")
        self.run_step("compare")

    def test_each_step_runs_only_itself_and_write_uses_existing_results(self):
        self.run_step("read")
        self.client.records.assert_called_once()
        self.runner.run.assert_not_called()
        self.client.replace.assert_not_called()
        self.runner.run.return_value = [row(IPS[0], latency=110), row(IPS[1]), row(IPS[2])]
        self.run_step("check")
        self.runner.run.assert_called_once_with(IPS, 3)
        self.assertEqual(self.status.snapshot()["phase"], "check_done")
        with self.assertRaises(auto.ServiceError):
            self.controller.start("write")
        self.runner.run.return_value = [row(ip, speed=8) for ip in NEW]
        with patch.object(auto, "candidate_pool", return_value=NEW):
            self.run_step("select")
        self.assertEqual(self.runner.run.call_count, 2)
        self.assertEqual(self.status.snapshot()["proposed_ips"], [])
        self.client.records.assert_called_once()
        self.client.replace.assert_not_called()
        self.run_step("compare")
        self.assertEqual(self.runner.run.call_count, 2)
        self.client.replace.assert_not_called()
        self.assertEqual(self.status.snapshot()["proposed_ips"], NEW)
        self.run_step("write")
        self.assertEqual(self.runner.run.call_count, 2)
        self.client.replace.assert_called_once_with(records(), NEW)
        self.assertEqual(self.status.snapshot()["proposed_ips"], [])
        with self.assertRaises(auto.ServiceError):
            self.controller.start("write")

    def test_selection_can_start_without_reading_dns(self):
        with patch.object(auto, "candidate_pool", return_value=NEW):
            self.run_step("select")
        self.client.records.assert_not_called()
        self.client.replace.assert_not_called()
        self.assertIsNone(self.controller.snapshot()["actions"]["compare"])
        self.run_step("compare")
        self.assertEqual(self.status.snapshot()["proposed_ips"], NEW)

    def test_interval_can_be_changed_at_runtime(self):
        wakeup = threading.Event()
        controller = auto.TaskController(self.service, scheduler_wakeup=wakeup)
        controller.set_interval(120)
        self.assertEqual(self.service.cfg.interval, 120)
        self.assertEqual(self.service.runner.settings.interval, 120)
        self.assertEqual(self.status.snapshot()["interval_seconds"], 120)
        self.assertTrue(wakeup.is_set())
        for value in (True, 59, 86401):
            with self.subTest(value=value), self.assertRaises(auto.ServiceError):
                controller.set_interval(value)

    def test_global_settings_validate_before_any_change_and_commit_together(self):
        self.prepare_final()
        payload = dict(interval_seconds=120, jitter_min_seconds=10, jitter_max_seconds=30,
                       max_latency_ms=70, min_bandwidth_mbps=60, dns_record_count=2, dns_history_count=1)
        self.status.update(dns_history=[{"ips": IPS, "time": 10}, {"ips": NEW, "time": 5}])
        before = self.status.snapshot()
        cfg = self.service.cfg
        with patch.object(auto.random, "randint", return_value=20) as sample:
            with self.assertRaises(auto.ServiceError):
                self.controller.set_settings(**(payload | {"dns_history_count": 11}))
            self.assertEqual(self.status.snapshot(), before)
            self.assertEqual(self.service.cfg, cfg)
            self.assertEqual(self.service.record_count, 3)
            self.assertIsNone(self.controller.next_check)
            sample.assert_not_called()
            self.controller.set_settings(**payload)
            sample.assert_called_once_with(10, 30)
        state = self.status.snapshot()
        self.assertEqual({key: state[key] for key in payload}, payload)
        self.assertEqual(len(state["dns_history"]), 1)
        self.assertEqual(state["steps"]["check"]["count"], 0)
        self.assertAlmostEqual(state["next_run"] - time.time(), 140, delta=1)
        self.assertEqual(self.service.runner.settings, self.service.cfg)
        self.assertEqual(self.service.record_count, 2)
        self.client.replace.assert_not_called()

    def test_global_save_without_schedule_change_preserves_deadline(self):
        self.controller.set_schedule(120, 10, 20)
        deadline, timestamp = self.controller.next_check, self.status.snapshot()["next_run"]
        payload = {key: self.status.snapshot()[key] for key in auto.SETTING_FIELDS}
        payload.update(max_latency_ms=80, dns_history_count=4)
        with patch.object(auto.random, "randint") as sample:
            self.controller.set_settings(**payload)
            self.controller.set_settings(**payload)
            sample.assert_not_called()
        self.assertEqual(self.controller.next_check, deadline)
        self.assertEqual(self.status.snapshot()["next_run"], timestamp)
        self.controller.reserve("automatic")
        try:
            with self.assertRaises(auto.ServiceError):
                self.controller.set_settings(**(payload | {"dns_record_count": 8}))
            self.assertEqual(self.service.record_count, payload["dns_record_count"])
        finally:
            self.controller.lock.release()

    def test_summary_revisions_follow_relevant_changes_and_server_restart(self):
        self.prepare_final()
        before = self.controller.snapshot(summary=True)
        self.assertTrue(auto.SUMMARY_EXCLUDED.isdisjoint(before))
        self.assertEqual(before["revisions"], self.controller.snapshot()["revisions"])
        self.status.update(phase="checking", busy=True)
        self.assertEqual(before["revisions"], self.controller.snapshot(summary=True)["revisions"])
        self.status.update(dns_ips=NEW)
        changed = self.controller.snapshot(summary=True)["revisions"]
        self.assertEqual([key for key in changed if before["revisions"][key] != changed[key]], ["dns"])
        self.status.update(dns_ips=list(NEW))
        self.assertEqual(changed, self.status.snapshot(summary=True)["revisions"])
        self.status.update(message="Testing", check_results=[row(IPS[0], latency=50)])
        latest = self.status.snapshot()["revisions"]
        self.assertEqual({key for key in latest if latest[key] != changed[key]}, {"check", "events"})
        restarted = auto.Status(None, self.cfg).snapshot(summary=True)
        self.assertTrue(all(latest[key] != restarted["revisions"][key] for key in latest))

    def test_dns_record_count_can_be_changed_at_runtime(self):
        controller = auto.TaskController(self.service)
        controller.set_record_count(6)
        self.assertEqual(self.service.record_count, 6)
        self.assertEqual(self.status.snapshot()["dns_record_count"], 6)
        for value in (True, 0, 11):
            with self.subTest(value=value), self.assertRaises(auto.ServiceError):
                controller.set_record_count(value)

    def test_next_time_is_generated_at_start_and_not_recomputed_after_work(self):
        with patch.object(auto.random, "randint", side_effect=[10, 300, 20]) as sample, \
                patch.object(auto.time, "time", return_value=1000), \
                patch.object(auto.time, "monotonic", return_value=500):
            self.controller.set_jitter(10, 300)
            self.assertEqual(self.status.snapshot()["next_run"], 4610)
            self.assertEqual(self.controller.next_check, 4110)
            observed = []
            def read_dns():
                observed.append(self.status.snapshot()["next_run"])
                return records()
            self.client.records.side_effect = read_dns
            self.runner.run.return_value = [row(ip) for ip in IPS]
            self.controller.automatic(initial=True)
            self.assertEqual(observed, [4900])
            self.assertEqual(self.status.snapshot()["next_run"], 4900)
            self.assertEqual(self.controller.next_check, 4400)
            # An overdue deadline does not draw a new random delay after a slow job.
            with patch.object(auto.time, "monotonic", return_value=5000):
                self.controller.wait_next()
            self.controller.snapshot()
            self.assertEqual(sample.call_count, 2)
            self.assertEqual(self.status.snapshot()["next_run"], 4900)
            self.controller.set_interval(120)
            self.assertEqual(self.status.snapshot()["next_run"], 1140)
            self.assertEqual(self.controller.next_check, 640)
        self.controller.set_jitter(0, 0)
        self.assertAlmostEqual(self.status.snapshot()["next_run"] - time.time(), 120, delta=1)
        self.controller.set_jitter(1, 1)
        self.assertAlmostEqual(self.status.snapshot()["next_run"] - time.time(), 121, delta=1)
        for minimum, maximum in ((0, 10), (10, 0), (-1, 5), (5, 4), (1, 10001), (True, 5), (1, 1.5)):
            with self.subTest(bounds=(minimum, maximum)), self.assertRaises(auto.ServiceError):
                self.controller.set_jitter(minimum, maximum)

    def test_failed_automatic_check_keeps_the_already_generated_next_time(self):
        self.client.records.side_effect = auto.ServiceError("network unavailable")
        with patch.object(auto.random, "randint", return_value=25) as sample, \
                patch.object(auto.time, "time", return_value=1000):
            self.controller.automatic(initial=True)
        self.assertEqual(self.status.snapshot()["next_run"], 4625)
        self.assertEqual(self.status.snapshot()["phase"], "error")
        self.assertEqual(sample.call_count, 1)

    def test_scheduler_wakes_for_new_settings_and_keeps_displayed_deadline(self):
        entered, release = threading.Event(), threading.Event()
        original_wait = self.controller.scheduler_wakeup.wait
        def wait(timeout):
            entered.set()
            release.wait(2)
            return original_wait(timeout)
        with patch.object(self.controller.scheduler_wakeup, "wait", side_effect=wait):
            thread = threading.Thread(target=self.controller.wait_next)
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                self.controller.set_jitter(10000, 10000)
                deadline = self.controller.next_check
                self.assertAlmostEqual(deadline - time.monotonic(), 13600, delta=1)
                self.assertAlmostEqual(self.status.snapshot()["next_run"] - time.time(), 13600, delta=1)
                self.stop.set()
            finally:
                release.set()
                self.controller.scheduler_wakeup.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(self.controller.next_check, deadline)

    def test_settings_endpoint_and_removed_cfst_endpoints(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), auto.make_dashboard(self.status, self.controller))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(payload, path="/api/settings", method="POST", extra=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                conn.request(method, path, json.dumps(payload) if method == "POST" else None, headers={"Content-Type": "application/json",
                             "X-Local-Client": "cfst-dashboard"} | (extra or {}))
                response = conn.getresponse()
                response.read()
                return response.status
            finally:
                conn.close()
        for payload in ({"dns_history_count": 4}, {"jitter_min_seconds": 1, "jitter_max_seconds": 10000}):
            self.assertEqual(request(payload), 202)
        full_settings = {key: self.status.snapshot()[key] for key in auto.SETTING_FIELDS}
        self.assertEqual(request(full_settings | {"interval_seconds": 180}), 202)
        self.assertEqual(self.service.cfg.interval, 180)
        self.assertEqual(request(full_settings | {"dns_record_count": True}), 400)
        self.assertEqual(request(full_settings | {"dns_history_count": -1}), 409)
        self.assertEqual(self.service.cfg.interval, 180)
        self.assertEqual(request({}, "/api/status/summary", "GET"), 200)
        self.assertEqual(self.status.snapshot()["dns_history_count"], 4)
        self.assertEqual(self.status.snapshot()["jitter_max_seconds"], 10000)
        for payload in ({"jitter_min_seconds": 1}, {"dns_history_count": True},
                        {"jitter_min_seconds": 1, "jitter_max_seconds": 2, "extra": 1}):
            self.assertEqual(request(payload), 400)
        self.assertEqual(request({"jitter_min_seconds": 100, "jitter_max_seconds": 1}), 409)
        self.controller.reserve("automatic")
        try:
            for payload in ({"dns_history_count": 5}, {"jitter_min_seconds": 5, "jitter_max_seconds": 6}):
                self.assertEqual(request(payload), 409)
        finally:
            self.controller.lock.release()
        self.assertEqual(self.status.snapshot()["dns_history_count"], 4)
        self.assertEqual(request({"dns_history_count": 5}, extra={"Origin": "https://other.example"}), 403)
        self.assertEqual(request({"action": "rollback"}, "/api/cfst"), 404)
        self.assertEqual(request({}, "/api/cfst/releases", "GET"), 404)
        self.assertEqual(request({"max_latency_ms": 75.5, "min_bandwidth_mbps": 40}), 202)
        self.assertEqual(self.service.cfg.latency_ms, 75.5)
        self.assertEqual(request({"max_latency_ms": 100, "min_bandwidth_mbps": 0}), 409)
        self.assertEqual(request({"max_latency_ms": True, "min_bandwidth_mbps": 30}), 400)
        self.assertEqual(request({"ip": "127.0.0.1"}, "/api/test-ip"), 409)
        self.assertEqual(request({"ip": IPS}, "/api/test-ip"), 400)
        self.service.read()
        self.runner.run.return_value = [row(IPS[0])]
        self.assertEqual(request({"ip": IPS[0]}, "/api/test-ip"), 202)
        self.controller.worker.join(3)
        self.runner.run.assert_called_once_with([IPS[0]], 1)
        self.client.replace.assert_not_called()


    def test_threshold_changes_update_health_rule_and_restore_from_state(self):
        self.run_step("read")
        self.runner.run.return_value = [row(ip) for ip in IPS]
        self.run_step("check")
        self.controller.set_thresholds(70.5, 60)
        self.assertEqual(self.service.cfg.latency_ms, 70.5)
        self.assertEqual(self.service.runner.settings.min_mbps, 60)
        self.assertEqual(self.status.snapshot()["steps"]["check"]["count"], 0)
        self.assertFalse(auto.qualifies(self.service.cfg, row(IPS[0])))
        for latency, speed in ((True, 30), (0, 30), (2001, 30), (100, .01), (100, 10001), (float("nan"), 30)):
            with self.subTest(thresholds=(latency, speed)), self.assertRaises(auto.ServiceError):
                self.controller.set_thresholds(latency, speed)
        self.client.replace.assert_not_called()
        with tempfile.TemporaryDirectory() as folder:
            status = auto.Status(Path(folder)/"status.json", self.cfg)
            status.update(interval_seconds=120, max_latency_ms=75, min_bandwidth_mbps=45)
            restored = auto.Status(status.path, self.cfg)
            service = auto.Maintainer(self.cfg, self.client, self.runner, restored, self.stop)
            auto.TaskController(service)
            self.assertEqual((service.cfg.interval, service.cfg.latency_ms, service.cfg.min_mbps), (120, 75, 45))

    def test_single_ip_only_measures_one_and_updates_existing_rows_without_writing(self):
        self.prepare_final()
        before = self.status.snapshot()
        self.runner.run.reset_mock()
        self.runner.run.return_value = [row(IPS[0], speed=10, latency=25)]
        self.controller.start_test_ip(IPS[0])
        self.controller.worker.join(3)
        self.runner.run.assert_called_once_with([IPS[0]], 1)
        after = self.status.snapshot()
        self.assertEqual(after["check_results"][0]["latency_ms"], 25)
        self.assertEqual(after["check_results"][1:], before["check_results"][1:])
        self.assertEqual(after["selection_results"], before["selection_results"])
        self.assertEqual(after["ranked_results"][0]["ip"], IPS[0])
        self.assertEqual(after["proposed_ips"], [])
        self.assertFalse(any(r.get("selected") for r in after["ranked_results"]))
        self.assertEqual(after["dns_ips"], before["dns_ips"])
        self.assertEqual(after["single_results"][IPS[0]]["result"]["latency_ms"], 25)
        self.client.replace.assert_not_called()

    def test_result_timestamps_follow_measurements_through_ranking_and_retests(self):
        self.run_step("read")
        self.runner.run.return_value = [row(ip) for ip in IPS]
        with patch.object(auto.time, "time", return_value=time.time() + 10):
            self.run_step("check")
        check_time = self.status.snapshot()["check_time"]
        self.runner.run.return_value = [row(ip) for ip in NEW]
        with patch.object(auto.time, "time", return_value=check_time + 10), \
                patch.object(auto, "candidate_pool", return_value=NEW):
            self.run_step("select")
            self.run_step("compare")
        state = self.status.snapshot()
        expected = {ip: check_time for ip in IPS} | {ip: check_time + 10 for ip in NEW}
        self.assertEqual({r["ip"]: r["tested_at"] for r in state["ranked_results"]}, expected)
        self.assertTrue(all(r["tested_at"] == state["selection_time"] for r in state["selection_results"]))
        self.runner.run.return_value = [row(IPS[0], speed=10)]
        with patch.object(auto.time, "time", return_value=check_time + 20):
            self.controller.start_test_ip(IPS[0])
            self.controller.worker.join(3)
        expected[IPS[0]] = check_time + 20
        self.assertEqual({r["ip"]: r["tested_at"] for r in self.status.snapshot()["ranked_results"]}, expected)

    def test_historical_ip_retest_does_not_pollute_current_or_new_ip_results(self):
        self.prepare_final()
        old_ip = "104.18.34.99"
        self.status.update(dns_history=[{"ips": [old_ip], "time": 100}])
        before = self.status.snapshot()
        self.runner.run.return_value = [row(old_ip, speed=7)]
        self.controller.start_test_ip(old_ip)
        self.controller.worker.join(3)
        after = self.status.snapshot()
        self.assertEqual(after["check_results"], before["check_results"])
        self.assertEqual(after["selection_results"], before["selection_results"])
        self.assertEqual(after["dns_history"], before["dns_history"])
        self.assertIn(old_ip, after["single_results"])
        self.client.replace.assert_not_called()

    def test_single_ip_requires_visible_ip_and_shares_execution_lock(self):
        self.run_step("read")
        self.runner.run.reset_mock()
        for ip in ("127.0.0.1", "104.18.13.0/24", "not-an-ip", "104.18.34.99"):
            with self.subTest(ip=ip), self.assertRaises(auto.ServiceError):
                self.controller.start_test_ip(ip)
        self.runner.run.assert_not_called()
        self.controller.reserve("automatic")
        try:
            with self.assertRaises(auto.ServiceError):
                self.controller.start_test_ip(IPS[0])
        finally:
            self.controller.lock.release()
        entered, release = threading.Event(), threading.Event()
        def probe(*args):
            entered.set()
            release.wait(3)
            return [row(IPS[0])]
        self.runner.run.side_effect = probe
        self.controller.start_test_ip(IPS[0])
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(self.controller.snapshot()["active_ip"], IPS[0])
            self.assertFalse(self.controller.automatic())
            with self.assertRaises(auto.ServiceError):
                self.controller.start_test_ip(IPS[1])
        finally:
            release.set()
            self.controller.worker.join(4)
        self.assertFalse(self.controller.lock.locked())
        self.assertIsNone(self.status.snapshot()["active_ip"])

    def test_single_ip_failed_or_empty_result_is_not_reported_as_successful_sample(self):
        self.prepare_final()
        self.runner.run.side_effect = auto.RateLimited("429")
        self.controller.start_test_ip(IPS[0])
        self.controller.worker.join(3)
        self.assertEqual(self.status.snapshot()["single_results"][IPS[0]]["error"], "429")
        self.assertEqual(self.status.snapshot()["proposed_ips"], [])
        self.runner.run.side_effect = None
        self.runner.run.return_value = []
        self.controller.start_test_ip(IPS[0])
        self.controller.worker.join(3)
        self.assertFalse(self.status.snapshot()["single_results"][IPS[0]]["result"]["measured"])
        # A later successful measurement must clear the old measured=False flag.
        self.runner.run.return_value = [row(IPS[0])]
        self.controller.start_test_ip(IPS[0])
        self.controller.worker.join(3)
        self.assertTrue(self.status.snapshot()["check_results"][0]["measured"])
        self.client.replace.assert_not_called()

    def test_manual_bootstrap_comparison_uses_selection_results(self):
        self.client.records.return_value = []
        self.prepare_final()
        self.assertEqual(self.runner.run.call_count, 1)
        self.runner.run.assert_called_with(NEW, 10)
        self.client.replace.assert_not_called()

    def test_missing_preconditions_do_not_run_other_steps(self):
        for action in ("check", "compare", "write", "invalid"):
            with self.subTest(action=action), self.assertRaises(auto.ServiceError):
                self.controller.start(action)
        self.assertFalse(self.controller.lock.locked())
        self.client.records.assert_not_called()
        self.runner.run.assert_not_called()

    def test_expired_dns_selection_and_final_results_refuse_write(self):
        self.prepare_final()
        for field in ("read_at", "selected_at", "compared_at"):
            previous = getattr(self.service, field)
            setattr(self.service, field, time.time() - auto.STEP_TTL - 1)
            with self.subTest(field=field), self.assertRaises(auto.ServiceError):
                self.controller.start("write")
            setattr(self.service, field, previous)
        self.client.replace.assert_not_called()

    def test_new_read_check_or_selection_invalidates_proposal(self):
        for action in ("read", "check", "select"):
            with self.subTest(action=action):
                self.prepare_final()
                self.assertEqual(len(self.status.snapshot()["proposed_ips"]), 3)
                self.run_step(action)
                self.assertEqual(self.status.snapshot()["proposed_ips"], [])
                self.assertIsNotNone(self.controller.snapshot()["actions"]["write"])
        self.client.replace.assert_not_called()

    def test_sorting_does_not_run_a_third_speedtest(self):
        self.prepare_final()
        self.runner.run.side_effect = auto.RateLimited("429")
        self.run_step("compare")
        self.assertEqual(self.status.snapshot()["phase"], "compare_done")
        self.assertEqual(self.status.snapshot()["proposed_ips"], NEW)
        self.client.replace.assert_not_called()

    def test_failed_api_write_consumes_proposal_and_cannot_retry_blindly(self):
        self.prepare_final()
        self.client.replace.side_effect = auto.ServiceError("timeout")
        self.run_step("write")
        self.assertEqual(self.status.snapshot()["phase"], "error")
        with self.assertRaises(auto.ServiceError):
            self.controller.start("write")
        self.assertEqual(self.client.replace.call_count, 1)

    def test_changed_dns_between_steps_refuses_write(self):
        self.prepare_final()
        real_client = auto.CloudflareClient(self.cfg)
        real_client.records = Mock(return_value=records(NEW))
        real_client.request = Mock()
        self.service.client = real_client
        self.run_step("write")
        self.assertEqual(self.status.snapshot()["phase"], "error")
        real_client.request.assert_not_called()

    def test_persisted_results_do_not_authorize_steps_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"status.json"
            self.service.status = auto.Status(path, self.cfg)
            self.prepare_final()
            state = auto.Status(path, self.cfg)
            service = auto.Maintainer(self.cfg, self.client, self.runner, state, self.stop)
            self.assertEqual(state.snapshot()["proposed_ips"], [])
            for action in ("check", "compare", "write"):
                with self.subTest(action=action), self.assertRaises(auto.ServiceError):
                    service.require_action(action)

    def test_manual_and_automatic_tasks_share_one_reservation(self):
        entered, release = threading.Event(), threading.Event()
        def blocked_records():
            entered.set()
            release.wait(3)
            return records()
        self.client.records.side_effect = blocked_records
        try:
            self.controller.start("read")
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.controller.snapshot()["busy"])
            with self.assertRaises(auto.ServiceError):
                self.controller.start("select")
            self.assertFalse(self.controller.automatic())
            self.assertEqual(self.client.records.call_count, 1)
            self.runner.run.assert_not_called()
        finally:
            release.set()
            self.controller.worker.join(3)
        self.assertFalse(self.controller.lock.locked())
        self.assertTrue(self.controller.automatic(initial=True))

    def test_automatic_reservation_blocks_manual_start(self):
        self.controller.reserve("automatic")
        try:
            with self.assertRaises(auto.ServiceError):
                self.controller.start("read")
        finally:
            self.controller.run_reserved("automatic", initial=True)
        self.assertFalse(self.controller.lock.locked())

    def test_insufficient_manual_finalists_cannot_write(self):
        self.prepare_final()
        self.service.new_top = [row(NEW[0])]
        self.service.selection_rows = [row(NEW[0])]
        self.service.check_rows = [row(IPS[0], speed=1)]
        self.run_step("compare")
        self.assertEqual(self.status.snapshot()["phase"], "degraded")
        with self.assertRaises(auto.ServiceError):
            self.controller.start("write")
        self.client.replace.assert_not_called()

    def test_manual_dry_run_never_writes(self):
        self.service.dry_run = True
        self.prepare_final()
        self.run_step("write")
        self.assertEqual(self.status.snapshot()["phase"], "dry_run")
        self.client.replace.assert_not_called()

    def test_dashboard_api_protection_payload_and_async_execution(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), auto.make_dashboard(self.status, self.controller))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        def request(payload, headers=None, path="/api/actions"):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                conn.request("POST", path, json.dumps(payload), headers=headers or {})
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()
        host = f"127.0.0.1:{server.server_port}"
        base = {"Content-Type": "application/json", "X-Local-Client": "cfst-dashboard", "Origin": "http://"+host}
        try:
            for extra in ({"Origin": "https://attacker.example"}, {"Host": "attacker.example"},
                          {"X-Local-Client": ""}, {"Sec-Fetch-Site": "cross-site"}):
                with self.subTest(extra=extra):
                    self.assertEqual(request({"action": "read"}, base|extra)[0], 403)
            self.assertEqual(request({"action": "read"}, base|{"Content-Type": "text/plain"})[0], 415)
            for payload in ({"action": "write", "ips": NEW}, {"action": "automatic"}, {"action": []}):
                self.assertEqual(request(payload, base)[0], 400)
            self.assertEqual(request({"action": "read"}, base, path="/api/status")[0], 404)
            self.assertEqual(request({"action": "compare"}, base)[0], 409)
            self.assertEqual(request({"action": "read"}, base)[0], 202)
            self.controller.worker.join(3)
            self.client.records.assert_called_once()
            self.runner.run.assert_not_called()
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
            conn.request("GET", "/api/status")
            response = conn.getresponse()
            data = json.loads(response.read())
            conn.close()
            self.assertTrue(data["manual_enabled"])
            self.assertIsNone(data["actions"]["check"])
            self.assertNotIn(self.cfg.token, json.dumps(data))
        finally:
            server.shutdown()
            server.server_close()

    def test_remote_dashboard_hosts_and_same_origin_actions(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), auto.make_dashboard(self.status, self.controller))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(host, method="GET", origin=None, extra=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            headers = {"Host": host, "Content-Type": "application/json", "X-Local-Client": "cfst-dashboard"}
            if origin is not None:
                headers["Origin"] = origin
            try:
                conn.request(method, "/api/actions" if method == "POST" else "/api/status",
                             '{"action":"read"}' if method == "POST" else None, headers | (extra or {}))
                response = conn.getresponse()
                response.read()
                return response.status
            finally:
                conn.close()

        for host in ("192.168.1.10:18789", "203.0.113.10:18789", "[2001:db8::10]:18789", "tester.example.com", "nas:18789"):
            with self.subTest(host=host):
                self.assertEqual(request(host), 200)
                for scheme in ("http://", "https://"):
                    self.assertEqual(request(host, "POST", scheme + host), 202)
                    self.controller.worker.join(3)
                self.assertEqual(request(host, "POST", "http://other.example.com"), 403)
                self.assertEqual(request(host, "POST", "http://" + host,
                                         {"Sec-Fetch-Site": "cross-site"}), 403)
        for host in ("", "user@server:18789", "server/path", "server:0", "server:65536", "server:",
                     "[::1]extra", "bad host", "server?query", "server#fragment", "bad%20host"):
            with self.subTest(invalid_host=host):
                self.assertEqual(request(host), 403)

    def test_preview_rejects_even_protected_action(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), auto.make_dashboard(self.status))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
            conn.request("POST", "/api/actions", '{"action":"read"}',
                         {"Content-Type": "application/json", "X-Local-Client": "cfst-dashboard"})
            response = conn.getresponse()
            self.assertEqual(response.status, 503)
            response.read()
            conn.close()
        finally:
            server.shutdown()
            server.server_close()


class RealCFSTContract(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("CFST_TEST_BIN"), "Set CFST_TEST_BIN for an official binary 429 check")
    def test_official_binary_rate_limit_stops_wrapper(self):
        class Limited(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(429)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Limited)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                settings = auto.Settings("cf.example.com", data_dir=Path(folder),
                    binary=str(Path(os.environ['CFST_TEST_BIN']).resolve()),
                    test_url=f'http://fixture.example:{server.server_port}/file')
                with patch.object(auto, "is_cloudflare_ip", return_value=True), self.assertRaises(auto.RateLimited):
                    auto.CFSTRunner(settings, threading.Event()).run(["127.0.0.1"], 1)
        finally:
            server.shutdown()
            server.server_close()

    @unittest.skipUnless(os.environ.get("CFST_TEST_BIN"), "Set CFST_TEST_BIN for a real binary / loopback fixture check")
    def test_official_binary_csv_and_download(self):
        class Download(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(8*1024*1024))
                self.end_headers()
                try:
                    for _ in range(128):
                        self.wfile.write(b'x'*65536)
                        self.wfile.flush()
                        time.sleep(.025)
                except OSError:
                    pass

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Download)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                result = Path(folder)/"result.csv"
                proc = subprocess.run([str(Path(os.environ['CFST_TEST_BIN']).resolve()), '-ip','127.0.0.1',
                    '-tp',str(server.server_port),'-url',f'http://fixture.example:{server.server_port}/file',
                    '-o',str(result),'-t','4','-n','1','-dn','1','-dt','5','-p','0'],
                    cwd=folder, capture_output=True, timeout=20)
                self.assertEqual(proc.returncode, 0, proc.stdout.decode('utf-8', errors='replace'))
                parsed = auto.parse_csv(result, {'127.0.0.1'})
                self.assertEqual(len(parsed), 1)
                self.assertGreater(parsed[0]['speed_mibps'], 0)
                self.assertEqual(parsed[0]['sent'], 4)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
