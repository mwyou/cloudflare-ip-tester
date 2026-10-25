# Third-party software

The container bundles the unmodified Linux binary of CloudflareSpeedTest (CFST) by XIU2.

- Bundled seed version: v2.3.5
- Project: https://github.com/XIU2/CloudflareSpeedTest
- Corresponding source: https://github.com/XIU2/CloudflareSpeedTest/tree/v2.3.5
- Source archive: https://github.com/XIU2/CloudflareSpeedTest/archive/refs/tags/v2.3.5.tar.gz
- Release archives: https://github.com/XIU2/CloudflareSpeedTest/releases/tag/v2.3.5
- License: GNU GPL version 3; full text is included in `LICENSE-CFST`.

The bundled archive SHA-256 values are pinned in `app/cfst_bundle.py`. The image includes the unmodified official binary and a manifest recording its version, architecture and checksums. CFST is updated together with the application image.
