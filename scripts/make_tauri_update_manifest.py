#!/usr/bin/env python3
"""Write Tauri's static, signed-update manifest for a GitHub release."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("usage: make_tauri_update_manifest.py VERSION TAG OUTPUT")
    version, tag, output = sys.argv[1:]
    signature_path = Path("release/tauri/Synchri-macos-arm64.app.tar.gz.sig")
    signature = signature_path.read_text(encoding="utf-8").strip()
    if not signature:
        raise SystemExit("the Tauri update signature is empty")
    repository = os.environ.get("GITHUB_REPOSITORY", "fenixawiles/synchri")
    asset = "Synchri-macos-arm64.app.tar.gz"
    manifest = {
        "version": version,
        "notes": f"Synchri {version}",
        "pub_date": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "platforms": {
            "darwin-aarch64": {
                "url": f"https://github.com/{repository}/releases/download/{tag}/{asset}",
                "signature": signature,
            }
        },
    }
    Path(output).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
