# Third-party software

The container bundles the unmodified Linux binary of CloudflareSpeedTest (CFST) by XIU2.

- Bundled seed version: v2.3.5
- Project: https://github.com/XIU2/CloudflareSpeedTest
- Corresponding source: https://github.com/XIU2/CloudflareSpeedTest/tree/v2.3.5
- Source archive: https://github.com/XIU2/CloudflareSpeedTest/archive/refs/tags/v2.3.5.tar.gz
- Release archives: https://github.com/XIU2/CloudflareSpeedTest/releases/tag/v2.3.5
- License: GNU GPL version 3; full text is included in `LICENSE-CFST`.

The seed archive SHA-256 values are pinned in `cfst_manager.py`. Independent upgrades retain the installed release version and checksums in the CFST volume. Source for an independently installed version is available under its corresponding upstream release tag. No modifications to the upstream CFST binary are made by this project.
