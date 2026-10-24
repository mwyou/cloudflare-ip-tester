"""Web-managed CFST versions: locking, activation, failure recovery and HTTP boundaries."""
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import automation as auto
import cfst_manager as manager
from test_cfst_manager import artifact


class CatalogTests(unittest.TestCase):
    def test_only_official_matching_assets_with_checksums_are_selectable(self):
        def release(tag, digest='sha256:'+'a'*64, **extra):
            return {'tag_name': tag, 'assets': [{'name':'cfst_linux_amd64.tar.gz', 'digest':digest}], **extra}
        response = [release('v2.3.6'), release('v2.3.4', None), release('v2.3.7', prerelease=True),
                    release('../../payload'), release(manager.VERSION, None)]
        catalog = manager.ReleaseCatalog('amd64')
        with patch.object(manager, 'read_url', return_value=json.dumps(response).encode()) as read:
            catalog.refresh()
            self.assertEqual(catalog.resolve('v2.3.6'), 'a'*64)
            self.assertEqual(catalog.resolve(manager.VERSION), manager.HASHES['amd64'])
            with self.assertRaises(manager.InstallError):
                catalog.resolve('v2.3.4')
            read.assert_called_once()
            self.assertEqual(read.call_args.args[0], 'https://api.github.com/repos/XIU2/CloudflareSpeedTest/releases?per_page=20')
        self.assertEqual([item['version'] for item in catalog.snapshot()['releases']], ['v2.3.6', manager.VERSION])
        self.assertNotIn('checksum', json.dumps(catalog.snapshot()))

    def test_status_snapshot_does_not_wait_for_github(self):
        catalog = manager.ReleaseCatalog('amd64')
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            release.wait(4)
            return b'[]'
        def fetch():
            try:
                catalog.refresh()
            except manager.InstallError:
                pass
        with patch.object(manager, 'read_url', side_effect=blocked):
            worker = threading.Thread(target=fetch)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                snapshot = threading.Thread(target=lambda: (catalog.snapshot(), done.set()))
                snapshot.start()
                self.assertTrue(done.wait(1), 'Status must remain responsive during an upstream request')
            finally:
                release.set()
                worker.join(5)
                snapshot.join(2)


class WebCFSTTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = manager.Store(self.root/'cfst', 'amd64')
        self.smoke = Mock()
        binary, metadata = artifact()
        self.original = self.store.stage(binary, metadata, self.smoke)
        self.store.activate(self.original)
        binary, metadata = artifact('v2.3.6')
        self.target = self.store.stage(binary, metadata, self.smoke)
        self.control = auto.CFSTControl(self.store, self.smoke)
        self.cfg = auto.Settings('cf.example.com', token='private-cf-token', data_dir=self.root,
                                 binary=str(self.store.release(self.original)[0]))
        self.status = auto.Status(None, self.cfg)
        self.client, self.runner, self.stop = Mock(), Mock(), threading.Event()
        self.client.records.return_value = []
        self.runner.run.return_value = []
        self.service = auto.Maintainer(self.cfg, self.client, self.runner, self.status, self.stop)
        self.controller = auto.TaskController(self.service, self.control)

    def run_change(self, action, target=None):
        self.controller.start_cfst(action, target)
        self.controller.worker.join(4)
        self.assertFalse(self.controller.worker.is_alive())
        self.assertFalse(self.controller.lock.locked())

    def test_switch_rebinds_runner_clears_old_proposals_and_persists(self):
        self.service.records, self.service.new_top, self.service.final = [], [], [{'ip':'104.18.13.1'}]*3
        self.status.update(proposed_ips=['104.18.13.1']*3, last_run=123)
        self.run_change('switch', self.target)
        self.assertEqual(self.store.state()['active'], self.target)
        self.assertEqual(manager.Store(self.store.home, 'amd64').state()['active'], self.target)
        self.assertEqual(self.service.cfg.cfst_version, 'v2.3.6')
        self.assertEqual(self.service.runner.settings, self.service.cfg)
        self.assertEqual(self.service.cfg.binary, str(self.store.release(self.target)[0]))
        self.assertEqual(self.status.snapshot()['engine'], 'CFST v2.3.6')
        self.assertEqual(self.status.snapshot()['last_run'], 123)
        self.assertEqual(self.status.snapshot()['proposed_ips'], [])
        self.assertIsNone(self.service.records)
        self.assertIsNone(self.service.new_top)
        self.assertEqual(self.service.final, [])
        self.client.replace.assert_not_called()

    def test_rollback_switches_runtime_back_without_restarting(self):
        self.run_change('switch', self.target)
        self.run_change('rollback')
        self.assertEqual(self.store.state()['active'], self.original)
        self.assertEqual(self.store.state()['previous'], self.target)
        self.assertEqual(self.service.cfg.cfst_version, manager.VERSION)
        self.client.replace.assert_not_called()

    def test_install_uses_catalog_checksum_and_updates_runner(self):
        binary, metadata = artifact('v2.3.7')
        self.control.catalog.resolve = Mock(return_value='b'*64)
        with patch.object(manager, 'download_release', return_value=(binary, metadata)) as download:
            self.run_change('install', 'v2.3.7')
        download.assert_called_once_with('v2.3.7', 'amd64', 'b'*64)
        self.assertEqual(self.service.cfg.cfst_version, 'v2.3.7')
        self.assertEqual(self.store.state()['previous'], self.original)

    def test_failed_compatibility_keeps_current_engine_and_dns(self):
        self.smoke.side_effect = manager.InstallError('版本不兼容')
        self.run_change('switch', self.target)
        self.assertEqual(self.store.state()['active'], self.original)
        self.assertEqual(self.service.cfg, self.cfg)
        self.assertIs(self.service.runner, self.runner)
        self.assertEqual(self.status.snapshot()['engine'], 'CFST '+manager.VERSION)
        self.assertEqual(self.status.snapshot()['cfst_message'], '版本不兼容')
        self.client.replace.assert_not_called()

    def test_version_changes_share_the_manual_and_scheduled_task_lock(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            release.wait(4)
        self.smoke.side_effect = blocked
        try:
            self.controller.start_cfst('switch', self.target)
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.controller.snapshot()['busy'])
            self.assertFalse(self.controller.automatic())
            with self.assertRaises(auto.ServiceError):
                self.controller.start('read')
            with self.assertRaises(auto.ServiceError):
                self.controller.start_cfst('rollback')
        finally:
            release.set()
            self.controller.worker.join(5)
        self.controller.reserve('automatic')
        try:
            with self.assertRaises(auto.ServiceError):
                self.controller.start_cfst('rollback')
        finally:
            self.controller.run_reserved('automatic')

    def test_http_management_is_protected_and_accepts_only_version_identifiers(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), auto.make_dashboard(self.status, self.controller))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        headers = {'Content-Type':'application/json', 'X-Local-Client':'cfst-dashboard'}
        def request(payload, extra=None):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            try:
                conn.request('POST', '/api/cfst', json.dumps(payload), headers=headers | (extra or {}))
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()
        for extra in ({'Origin':'https://evil.example'}, {'Host':'bad/host'}, {'X-Local-Client':''}):
            self.assertEqual(request({'action':'rollback'}, extra)[0], 403)
        for payload in ({'action':'install','version':'../../bad'}, {'action':'switch','release_id':'/bin/sh'},
                        {'action':'install','version':'v2.3.6','url':'https://evil.example'},
                        {'action':'rollback','extra':1}, {'action':['rollback']}):
            self.assertEqual(request(payload)[0], 400)
        self.assertEqual(request({'action':'switch','release_id':self.target},
                                 {'Host':'tester.example.com:18789','Origin':'http://tester.example.com:18789'})[0], 202)
        self.controller.worker.join(4)
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
        conn.request('GET','/api/status')
        response = conn.getresponse()
        data = response.read()
        conn.close()
        self.assertNotIn(self.cfg.token.encode(), data)
        self.assertEqual(json.loads(data)['cfst']['current'], self.target)


if __name__ == '__main__':
    unittest.main()
