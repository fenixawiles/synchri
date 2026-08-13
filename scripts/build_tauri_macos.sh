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
(cd "$ROOT/desktop" && CARGO_TARGET_DIR="$TAURI_TARGET_DIR" CI=true npm exec tauri build -- --bundles app --skip-stapling)

sign_final_app() {
  APP="$1"
  ENGINE="$APP/Contents/Resources/engine"
  [ -x "$ENGINE/synchri-core" ] || { echo "Synchri engine is missing from app resources" >&2; exit 1; }
  # Sign concrete nested code first. The engine itself retains the narrow
  # hardened-runtime entitlements Python needs to load extension modules.
  find "$ENGINE" -type f -perm -111 ! -name synchri-core -print0 | xargs -0 -n 1 \
    codesign --force --options runtime --timestamp --sign "$APPLE_SIGNING_IDENTITY"
  codesign --force --options runtime --entitlements "$ROOT/desktop/src-tauri/EngineEntitlements.plist" \
    --timestamp --sign "$APPLE_SIGNING_IDENTITY" "$ENGINE/synchri-core"
  # Do not use `--deep`: every child is deliberately sealed above, and deep
  # signing can discard the engine's explicit entitlements.
  codesign --force --options runtime --timestamp --sign "$APPLE_SIGNING_IDENTITY" "$APP"
}

# Build the installer and updater archive only after the final app seal. Tauri
# has already built its `.app`; a direct DMG here avoids a second bundling pass
# that would reseal the resource tree after we have verified it.
FINAL_DIR="$TAURI_TARGET_DIR/release/final"
FINAL_APP="$FINAL_DIR/Synchri.app"
mkdir -p "$FINAL_DIR"
ditto "$TAURI_TARGET_DIR/release/bundle/macos/Synchri.app" "$FINAL_APP"
# Finder metadata on the source checkout is not part of the app and cannot be
# allowed to survive into a signed archive. This is a no-op on clean CI
# worktrees, but keeps local release validation faithful to the public build.
xattr -cr "$FINAL_APP" 2>/dev/null || true
if [ -n "${APPLE_SIGNING_IDENTITY:-}" ] && [ "${APPLE_SIGNING_IDENTITY}" != "-" ]; then
  sign_final_app "$FINAL_APP"
fi

DMG="$TAURI_TARGET_DIR/release/Synchri-macos-$ARCH.dmg"
# ``hdiutil`` occasionally leaves the target file momentarily busy on hosted
# macOS runners.  Build to a private, per-process path and retry the image
# creation before publishing it under Synchri's stable download name.  This
# only affects the installer container; the signed app bundle above is not
# changed between attempts.
attempt=1
while :; do
  DMG_STAGING="$TAURI_TARGET_DIR/release/.Synchri-macos-$ARCH-$$-$attempt.dmg"
  if hdiutil create -volname Synchri -srcfolder "$FINAL_APP" -format UDZO "$DMG_STAGING" >/dev/null; then
    break
  fi
  if [ "$attempt" -ge 3 ]; then
    echo "Could not create the Synchri DMG after $attempt attempts" >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 3
done
mv -f "$DMG_STAGING" "$DMG"

# Tauri deliberately signs a tarball of the app bundle on macOS. Updating from
# a DMG is not supported because a DMG is an installation medium, not an app.
# The DMG is the first-download asset; this tarball is the verified updater
# payload users never need to handle themselves.
UPDATE="$TAURI_TARGET_DIR/release/bundle/macos/Synchri.app.tar.gz"
ditto -c -k --sequesterRsrc --keepParent "$FINAL_APP" "$UPDATE"
(cd "$ROOT/desktop" && npm exec tauri signer sign -- "$UPDATE")
SIG="$UPDATE.sig"
[ -f "$SIG" ] || { echo "Synchri did not produce an updater signature" >&2; exit 1; }

OUT="$ROOT/release/tauri"
mkdir -p "$OUT"
install -m 644 "$DMG" "$OUT/Synchri-macos-$ARCH.dmg"
install -m 644 "$UPDATE" "$OUT/Synchri-macos-$ARCH.app.tar.gz"
install -m 644 "$SIG" "$OUT/Synchri-macos-$ARCH.app.tar.gz.sig"
shasum -a 256 "$OUT/Synchri-macos-$ARCH.dmg" > "$OUT/Synchri-macos-$ARCH.dmg.sha256"
printf 'Built %s\n' "$OUT/Synchri-macos-$ARCH.dmg"
