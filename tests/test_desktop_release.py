"""Release invariants for Synchri's self-updating macOS application."""

from __future__ import annotations

import json
import re
from pathlib import Path

import tomllib


ROOT = Path(__file__).parents[1]


def _cargo_version() -> str:
    source = (ROOT / "desktop" / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', source, flags=re.MULTILINE)
    assert match, "desktop Cargo package must declare a version"
    return match.group(1)


def test_desktop_release_versions_are_kept_in_lockstep():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    package = json.loads((ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "desktop" / "package-lock.json").read_text(encoding="utf-8"))
    tauri = json.loads((ROOT / "desktop" / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    init = (ROOT / "synchri" / "__init__.py").read_text(encoding="utf-8")

    assert package["version"] == version
    assert lock["version"] == version
    assert lock["packages"][""]["version"] == version
    assert _cargo_version() == version
    assert tauri["version"] == version
    assert f'__version__ = "{version}"' in init


def test_release_build_installs_the_declared_runtime_dependencies():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    sidecar = (ROOT / "scripts" / "build_tauri_sidecar.sh").read_text(encoding="utf-8")
    desktop_build = (ROOT / "scripts" / "build_tauri_macos.sh").read_text(encoding="utf-8")

    assert "python -m pip install --upgrade pyinstaller ." in workflow
    assert "--collect-data certifi" in sidecar
    assert "make_tauri_update_manifest.py" in workflow
    assert 'tar --no-xattrs --no-acls --no-fflags -czf "$UPDATE" -C "$FINAL_DIR" Synchri.app' in desktop_build
    assert 'ditto -c -k --sequesterRsrc --keepParent "$FINAL_APP" "$UPDATE"' not in desktop_build
    assert 'tr -d \'[:space:]\')" = "1f8b"' in workflow
    assert "Updater archive must not contain macOS metadata records" in workflow


def test_authenticated_loopback_ui_has_only_its_explicit_native_actions():
    """The native window loads the UI from a token-protected loopback server.

    Tauri treats that as a remote origin, so each native action has to be
    deliberately granted.  Keep the authority tiny: GitHub's external browser
    launch and the two updater actions, nothing else.
    """
    capability = json.loads(
        (ROOT / "desktop" / "src-tauri" / "capabilities" / "loopback-ui.json").read_text(
            encoding="utf-8"
        )
    )

    assert capability["remote"]["urls"] == ["http://127.0.0.1:*"]
    assert capability["permissions"] == [
        "allow-check-for-update",
        "allow-install-update",
        "allow-open-github-url",
    ]
