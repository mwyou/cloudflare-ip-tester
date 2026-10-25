"""Run inside each Linux image. No Cloudflare credentials or external DNS writes."""
import os
import json
import socket
from pathlib import Path
import sys
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request, ProxyHandler, build_opener
from urllib.error import HTTPError

sys.path.insert(0, '/app')
from cfst_bundle import VERSION, native_arch, bundled_release, smoke_test
from automation import Settings, Status, Maintainer, CFSTRunner, TaskController, make_dashboard, SETTING_FIELDS

expected_arch = sys.argv[1]
assert os.getuid() == 10001
assert native_arch() == expected_arch
binary, metadata = bundled_release()
assert metadata['version'] == VERSION and metadata['arch'] == expected_arch
assert os.access(binary, os.X_OK) and not os.access(binary, os.W_OK)
smoke_test(binary, VERSION)

settings = Settings('ci.example.com', token='ci-placeholder', binary=str(binary), cfst_version=metadata['version'],
                    data_dir=Path('/tmp'))
status = Status(None, settings)
stop = threading.Event()
service = Maintainer(settings, None, CFSTRunner(settings, stop), status, stop)
controller = TaskController(service)
server = ThreadingHTTPServer(('0.0.0.0', 0), make_dashboard(status, controller))
threading.Thread(target=server.serve_forever, daemon=True).start()
opener = build_opener(ProxyHandler({}))
dashboard_url = f'http://{socket.gethostbyname(socket.gethostname())}:{server.server_port}'
try:
    for path in ('/health', '/', '/auto.js', '/auto.css', '/logo.svg', '/api/status', '/api/status/summary'):
        with opener.open(dashboard_url+path, timeout=5) as response:
            assert response.status == 200 and response.read(), path
    for payload in ({'dns_history_count': 5}, {'jitter_min_seconds': 10, 'jitter_max_seconds': 300},
                    {'max_latency_ms': 80, 'min_bandwidth_mbps': 50}):
        request = Request(dashboard_url+'/api/settings', json.dumps(payload).encode(),
                          {'Content-Type':'application/json','X-Local-Client':'cfst-dashboard','Origin':dashboard_url})
        with opener.open(request, timeout=5) as response:
            assert response.status == 202
            response.read()
    assert status.snapshot()['dns_history_count'] == 5
    assert status.snapshot()['jitter_max_seconds'] == 300
    assert service.cfg.latency_ms == 80 and service.cfg.min_mbps == 50
    payload = {key: status.snapshot()[key] for key in SETTING_FIELDS}
    payload.update(interval_seconds=120, dns_record_count=4)
    request = Request(dashboard_url+'/api/settings', json.dumps(payload).encode(),
                      {'Content-Type':'application/json','X-Local-Client':'cfst-dashboard','Origin':dashboard_url})
    with opener.open(request, timeout=5) as response:
        assert response.status == 202
        response.read()
    assert service.cfg.interval == 120 and service.record_count == 4
    for path in ('/api/cfst', '/api/cfst/releases'):
        try:
            opener.open(dashboard_url+path, timeout=5)
            raise AssertionError('Version management should be removed')
        except HTTPError as error:
            assert error.code == 404
    assert service.runner.settings.binary == str(binary)

finally:
    server.shutdown()
    server.server_close()
print(f'Checked bundled Linux/{expected_arch}, CFST {metadata["version"]}, UID 10001')
