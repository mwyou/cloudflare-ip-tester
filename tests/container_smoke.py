"""Run inside each Linux image. No Cloudflare credentials or external DNS writes."""
import os
import json
import socket
from pathlib import Path
import sys
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request, ProxyHandler, build_opener

sys.path.insert(0, '/app')
from cfst_manager import Store, VERSION, native_arch
from automation import Settings, Status, Maintainer, CFSTRunner, CFSTControl, TaskController, make_dashboard

mode, expected_arch = sys.argv[1:]
assert os.getuid() == 10001
assert native_arch() == expected_arch
store = Store('/opt/cfst')
marker = store.home/'ci-persistence.txt'
if mode == 'init':
    binary, metadata = store.initialize('/usr/local/lib/cfst')
    assert metadata['version'] == VERSION and metadata['arch'] == expected_arch
    assert os.access(binary, os.X_OK)
    marker.write_text(store.state()['active'], encoding='ascii')
elif mode == 'existing':
    # A second container must use the volume, even if its bundled seed is unavailable.
    binary, metadata = store.initialize('/does-not-exist')
    assert marker.read_text(encoding='ascii') == store.state()['active']
    assert metadata['version'] == VERSION and metadata['arch'] == expected_arch
    assert os.access(binary, os.X_OK)
else:
    raise AssertionError('Unknown test mode')

settings = Settings('ci.example.com', token='ci-placeholder', binary=str(binary), cfst_version=metadata['version'],
                    data_dir=Path('/tmp'))
status = Status(None, settings)
stop = threading.Event()
service = Maintainer(settings, None, CFSTRunner(settings, stop), status, stop)
controller = TaskController(service, CFSTControl(store))
server = ThreadingHTTPServer(('0.0.0.0', 0), make_dashboard(status, controller))
threading.Thread(target=server.serve_forever, daemon=True).start()
opener = build_opener(ProxyHandler({}))
dashboard_url = f'http://{socket.gethostbyname(socket.gethostname())}:{server.server_port}'
try:
    for path in ('/health', '/', '/auto.js', '/auto.css', '/logo.svg', '/api/status'):
        with opener.open(dashboard_url+path, timeout=5) as response:
            assert response.status == 200 and response.read(), path
    request = Request(dashboard_url+'/api/cfst',
                      json.dumps({'action':'switch','release_id':store.state()['active']}).encode(),
                      {'Content-Type':'application/json','X-Local-Client':'cfst-dashboard','Origin':dashboard_url})
    with opener.open(request, timeout=5) as response:
        assert response.status == 202
        response.read()
    controller.worker.join(60)
    assert not controller.worker.is_alive()
    assert status.snapshot()['phase'] == 'cfst_ready', status.snapshot()['message']
    assert service.runner.settings.binary == str(binary)
    assert service.runner.settings.cfst_version == metadata['version']
finally:
    server.shutdown()
    server.server_close()
print(f'Checked {mode}: Linux/{expected_arch}, CFST {metadata["version"]}, UID 10001')
