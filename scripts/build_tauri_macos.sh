#!/bin/sh
# Build the signed, native Apple Silicon installer. The caller supplies signing
# and notarization environment variables when producing a public release.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON=${PYTHON:-python3}
TARGET=${SYNCHRI_TARGET_TRIPLE:-$(rustc --print host-tuple)}
ARCH=${SYNCHRI_ARCH:-arm64}
# Do not assemble a signable .app inside Desktop, iCloud Drive, or another
# Finder-managed folder. Those providers may attach Finder metadata *while
# Tauri is signing*, which invalidates the bundle. The installer and updater
# assets are copied back to release/ after the native build is complete.
TAURI_TARGET_DIR=${SYNCHRI_TAURI_TARGET_DIR:-"${TMPDIR:-/tmp}/synchri-tauri-${UID:-$(id -u)}"}

case "$TARGET" in
  aarch64-apple-darwin) ;;
  *) echo "Synchri's public macOS build currently targets Apple Silicon, got $TARGET" >&2; exit 1 ;;
esac

PYTHON="$PYTHON" \
  SYNCHRI_TARGET_TRIPLE="$TARGET" \
  PYINSTALLER_CODESIGN_IDENTITY="${APPLE_SIGNING_IDENTITY:-}" \
  "$ROOT/scripts/build_tauri_sidecar.sh"
# Tauri's bundle step signs every nested executable. CI checkouts do not carry
# Finder metadata, unlike a local Desktop folder, so it is the authoritative
# signing test. ``CI=true`` also keeps the DMG helper non-interactive.
mkdir -p "$TAURI_TARGET_DIR"
xattr -crs "$TAURI_TARGET_DIR" 2>/dev/null || true
(cd "$ROOT/desktop" && CARGO_TARGET_DIR="$TAURI_TARGET_DIR" CI=true npm exec tauri build -- --bundles dmg,app)

DMG=$(find "$TAURI_TARGET_DIR/release/bundle/dmg" -maxdepth 1 -name '*.dmg' -type f | head -n 1)
[ -n "$DMG" ] && [ -f "$DMG" ] || { echo "Tauri did not produce a DMG" >&2; exit 1; }

# Tauri deliberately signs a tarball of the app bundle on macOS. Updating from
# a DMG is not supported because a DMG is an installation medium, not an app.
# The DMG is the first-download asset; this tarball is the verified updater
# payload users never need to handle themselves.
UPDATE=$(find "$TAURI_TARGET_DIR/release/bundle/macos" -maxdepth 1 -name '*.app.tar.gz' -type f | head -n 1)
[ -n "$UPDATE" ] && [ -f "$UPDATE" ] || { echo "Tauri did not produce a macOS update payload" >&2; exit 1; }
SIG="$UPDATE.sig"
[ -f "$SIG" ] || { echo "Tauri did not produce an updater signature" >&2; exit 1; }

OUT="$ROOT/release/tauri"
mkdir -p "$OUT"
install -m 644 "$DMG" "$OUT/Synchri-macos-$ARCH.dmg"
install -m 644 "$UPDATE" "$OUT/Synchri-macos-$ARCH.app.tar.gz"
install -m 644 "$SIG" "$OUT/Synchri-macos-$ARCH.app.tar.gz.sig"
shasum -a 256 "$OUT/Synchri-macos-$ARCH.dmg" > "$OUT/Synchri-macos-$ARCH.dmg.sha256"
printf 'Built %s\n' "$OUT/Synchri-macos-$ARCH.dmg"
