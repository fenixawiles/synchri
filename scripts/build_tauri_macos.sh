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
  # Sign concrete nested Mach-O code first. The engine itself retains the
  # narrow hardened-runtime entitlements Python needs to load extension
  # modules. Do not use the executable bit as a proxy for code: PyInstaller
  # can mark data such as certifi's cacert.pem executable. Signing that data
  # attaches macOS code-signing xattrs; Tauri's updater faithfully treats the
  # resulting AppleDouble record as a literal ._* resource and breaks the
  # outer app seal after installation.
  find "$ENGINE" -type f ! -name synchri-core -print0 | \
    while IFS= read -r -d '' candidate; do
      if file -b "$candidate" | grep -q 'Mach-O'; then
        codesign --force --options runtime --timestamp --sign "$APPLE_SIGNING_IDENTITY" "$candidate"
      fi
    done
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
# `ditto --norsrc` prevents macOS resource-fork/Finder metadata from becoming
# AppleDouble sidecars during the copy. The app's code signatures are embedded
# in its executables and CodeResources file, so they do not rely on that data.
ditto --norsrc "$TAURI_TARGET_DIR/release/bundle/macos/Synchri.app" "$FINAL_APP"
# Finder metadata on the source checkout is not part of the app and cannot be
# allowed to survive into a signed archive. xattr cannot remove a literal
# AppleDouble file, so purge those exact transport artifacts too before the
# final app seal is made.
xattr -cr "$FINAL_APP" 2>/dev/null || true
find "$FINAL_APP" -name '._*' -type f -delete

verify_final_app() {
  APP="$1"
  ENGINE="$APP/Contents/Resources/engine"
  # Every Mach-O must verify individually, regardless of its permission bits.
  # This turns "a binary leaked through unsigned" into a release failure here,
  # instead of a Gatekeeper 'damaged' report on a customer's machine.
  find "$ENGINE" -type f -print0 | \
    while IFS= read -r -d '' candidate; do
      if file -b "$candidate" | grep -q 'Mach-O'; then
        codesign --verify --strict "$candidate" || {
          echo "Unsigned or invalid Mach-O leaked into the engine: $candidate" >&2
          exit 1
        }
      fi
    done
  codesign --verify --deep --strict "$APP"
}

notarize_final_app() {
  APP="$1"
  # Staple the app itself, not only the DMG. The updater tarball repacks this
  # exact bundle, and an updated install must carry its own Gatekeeper ticket
  # so it can pass assessment offline. A stapled ticket lives beside the seal
  # (Contents/CodeResources) and never invalidates the signature.
  NOTARIZE_ZIP="$TAURI_TARGET_DIR/release/Synchri-notarize.zip"
  rm -f "$NOTARIZE_ZIP"
  ditto -c -k --keepParent "$APP" "$NOTARIZE_ZIP"
  xcrun notarytool submit "$NOTARIZE_ZIP" \
    --apple-id "$SYNCHRI_NOTARY_APPLE_ID" \
    --password "$SYNCHRI_NOTARY_PASSWORD" \
    --team-id "$SYNCHRI_NOTARY_TEAM_ID" \
    --wait
  xcrun stapler staple "$APP"
  xcrun stapler validate "$APP"
  rm -f "$NOTARIZE_ZIP"
}

if [ -n "${APPLE_SIGNING_IDENTITY:-}" ] && [ "${APPLE_SIGNING_IDENTITY}" != "-" ]; then
  sign_final_app "$FINAL_APP"
  verify_final_app "$FINAL_APP"
fi
# The notary credentials use SYNCHRI_-prefixed names on purpose: exporting
# APPLE_ID/APPLE_PASSWORD around the earlier `tauri build` would trigger the
# bundler's own notarization pass on the intermediate app.
if [ -n "${SYNCHRI_NOTARY_APPLE_ID:-}" ] && [ -n "${SYNCHRI_NOTARY_PASSWORD:-}" ] && [ -n "${SYNCHRI_NOTARY_TEAM_ID:-}" ]; then
  notarize_final_app "$FINAL_APP"
fi

DMG="$TAURI_TARGET_DIR/release/Synchri-macos-$ARCH.dmg"
# Give first-time users the normal macOS installation gesture: drag Synchri
# into Applications. Running the bundle directly from a mounted image invokes
# App Translocation, where in-app replacement is intentionally refused.
DMG_CONTENTS="$TAURI_TARGET_DIR/release/dmg-contents"
rm -rf "$DMG_CONTENTS"
mkdir -p "$DMG_CONTENTS"
ditto --norsrc "$FINAL_APP" "$DMG_CONTENTS/Synchri.app"
ln -sfn /Applications "$DMG_CONTENTS/Applications"
# ``hdiutil`` occasionally leaves the target file momentarily busy on hosted
# macOS runners.  Build to a private, per-process path and retry the image
# creation before publishing it under Synchri's stable download name.  This
# only affects the installer container; the signed app bundle above is not
# changed between attempts.
attempt=1
while :; do
  DMG_STAGING="$TAURI_TARGET_DIR/release/.Synchri-macos-$ARCH-$$-$attempt.dmg"
  if hdiutil create -volname Synchri -srcfolder "$DMG_CONTENTS" -format UDZO "$DMG_STAGING" >/dev/null; then
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
# A stapled ticket alone does not give the disk image a Developer ID
# signature. Gatekeeper assesses the DMG itself before mounting it, so sign
# the outer distribution container before the release workflow submits it for
# notarization. The identifier is intentionally distinct from the app bundle.
if [ -n "${APPLE_SIGNING_IDENTITY:-}" ] && [ "${APPLE_SIGNING_IDENTITY}" != "-" ]; then
  codesign --force --timestamp --sign "$APPLE_SIGNING_IDENTITY" \
    --identifier "com.synchri.desktop.dmg" "$DMG"
  codesign --verify --verbose=2 "$DMG"
fi

# Tauri's macOS updater consumes a gzip-compressed tarball of the app bundle.
# Updating from a DMG is not supported because a DMG is an installation medium,
# not an app.  Do not use ``ditto -c -k`` here: it creates a ZIP archive even
# when the filename ends in ``.tar.gz``, which the updater correctly rejects.
# The DMG is the first-download asset; this tarball is the verified updater
# payload users never need to handle themselves.  BSD tar archives macOS
# extended attributes by default.  Tauri's Rust extractor turns those records
# into AppleDouble `._*` files inside the installed app, which invalidates its
# sealed code signature.  The signed app bundle is self-contained, so omit
# xattrs, ACLs, and file flags from the updater transport.
UPDATE="$TAURI_TARGET_DIR/release/bundle/macos/Synchri.app.tar.gz"
rm -f "$UPDATE" "$UPDATE.sig"
if find "$FINAL_APP" -name '._*' -print -quit | grep -q .; then
  echo "Final app contains AppleDouble sidecars" >&2
  exit 1
fi
# `--no-xattrs` alone controls pax attributes, not the separate AppleDouble
# transport used by macOS's copyfile implementation.  Explicitly disable both
# paths.  `COPYFILE_DISABLE` is the process-level guard; `--no-mac-metadata`
# is the archive-level guard.
env COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs --no-acls --no-fflags \
  -czf "$UPDATE" -C "$FINAL_DIR" Synchri.app
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
