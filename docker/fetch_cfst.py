"""Download and verify the bundled Linux CFST seed (cross-build safe)."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cfst_bundle import HASHES, VERSION, download_release


def main():
    arch = os.environ["TARGETARCH"]
    if arch not in HASHES:
        raise SystemExit("Only linux/amd64 and linux/arm64 are supported")
    binary, metadata = download_release(VERSION, arch, HASHES[arch])
    target = Path("/out")
    target.mkdir(exist_ok=True)
    (target / "cfst").write_bytes(binary)
    (target / "cfst").chmod(0o755)
    (target / "manifest.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(f"Verified CFST {VERSION} linux/{arch}")


if __name__ == "__main__":
    main()
