"""Run inside each Linux image. No Cloudflare credentials or external DNS writes."""
import os
from pathlib import Path
import sys
import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

sys.path.insert(0, '/app')
from cfst_manager import Store, VERSION, native_arch
from automation import Settings, Status, make_dashboard

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

server = ThreadingHTTPServer(('127.0.0.1', 0), make_dashboard(Status(None, Settings(''))))
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    for path in ('/health', '/', '/auto.js', '/auto.css', '/api/status'):
        with urlopen(f'http://127.0.0.1:{server.server_port}{path}', timeout=5) as response:
            assert response.status == 200 and response.read(), path
finally:
    server.shutdown()
    server.server_close()
print(f'Checked {mode}: Linux/{expected_arch}, CFST {metadata["version"]}, UID 10001')
