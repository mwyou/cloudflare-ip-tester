"""Prepare a bind-mounted data directory, then run the service as cfip."""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import sys


UID = 10001
GID = 10001
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))


def prepare_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if os.geteuid() != 0:
        return
    # A bind mount hides the ownership prepared in the image.  Repair the
    # directory (and the small state files it contains) before dropping uid.
    paths = [DATA_DIR, *DATA_DIR.rglob("*")]
    for path in paths:
        try:
            os.chown(path, UID, GID, follow_symlinks=False)
        except OSError:
            # Root-squashed NAS mounts may refuse chown; the service will then
            # report the actual mount permission problem instead of masking it.
            pass


def main() -> None:
    prepare_data_dir()
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(GID)
        os.setuid(UID)
    probe = DATA_DIR / (".write-check-" + secrets.token_hex(8))
    try:
        descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        probe.unlink()
    except OSError as error:
        raise SystemExit(
            f"Cannot write to {DATA_DIR} as UID/GID {UID}:{GID}; "
            "check the NAS shared-folder permissions and ACL settings."
        ) from error
    os.execv(sys.executable, [sys.executable, "/app/automation.py", *sys.argv[1:]])


if __name__ == "__main__":
    main()
