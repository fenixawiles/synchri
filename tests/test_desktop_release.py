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
    assert (
        'env COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs --no-acls --no-fflags'
        in desktop_build
    )
    assert 'ditto -c -k --sequesterRsrc --keepParent "$FINAL_APP" "$UPDATE"' not in desktop_build
    assert 'tr -d \'[:space:]\')" = "1f8b"' in workflow
    assert "Updater archive must not contain macOS metadata records" in workflow
    assert "--example verify_update_archive" in workflow
    assert 'codesign --verify --deep --strict --verbose=2 "$VERIFY_DIR"' in workflow
    assert "-perm -111" not in desktop_build
    assert "file -b \"$candidate\" | grep -q 'Mach-O'" in desktop_build


def test_update_payload_is_notarized_assessed_and_published_safely():
    """The update path must satisfy Gatekeeper end to end, not only codesign.

    Each assertion pins one hard-won invariant: the app itself carries a
    stapled ticket before either container is built; every Mach-O verifies
    regardless of permission bits; Gatekeeper's own assessment gates the
    release; the manifest uploads last so a mid-publish update check can
    never pair it with missing or replaced bytes; and the engine never runs
    with a working directory inside the sealed bundle.
    """
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    desktop_build = (ROOT / "scripts" / "build_tauri_macos.sh").read_text(encoding="utf-8")
    shell = (ROOT / "desktop" / "src-tauri" / "src" / "main.rs").read_text(encoding="utf-8")

    assert "verify_final_app" in desktop_build
    assert 'xcrun stapler staple "$APP"' in desktop_build
    assert desktop_build.index('xcrun stapler staple "$APP"') < desktop_build.index("hdiutil create")
    assert desktop_build.index('xcrun stapler staple "$APP"') < desktop_build.index(
        "env COPYFILE_DISABLE=1 tar"
    )
    assert "SYNCHRI_NOTARY_APPLE_ID" in workflow and "SYNCHRI_NOTARY_APPLE_ID" in desktop_build

    assert 'xcrun stapler validate "$VERIFY_DIR"' in workflow
    assert "spctl --assess --type exec" in workflow
    assert "spctl --assess --type open" in workflow

    assert 'gh release upload "$TAG" $payload --clobber' in workflow
    assert workflow.index('gh release upload "$TAG" $payload --clobber') < workflow.index(
        'gh release upload "$TAG" "$manifest" --clobber'
    )

    assert "current_dir(sidecar_working_directory())" in shell
    assert "current_dir(engine_dir)" not in shell


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
