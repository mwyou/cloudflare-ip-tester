"""Run inside each Linux image. No Cloudflare credentials or external DNS writes."""
import os
from pathlib import Path
import sys

sys.path.insert(0, '/app')
from cfst_manager import Store, VERSION, native_arch

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
print(f'Checked {mode}: Linux/{expected_arch}, CFST {metadata["version"]}, UID 10001')
