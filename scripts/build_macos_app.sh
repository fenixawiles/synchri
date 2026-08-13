#!/bin/sh
# Build a Finder-friendly Synchri.app from a clean checkout.
#
# This is release tooling, not a user installer.  It deliberately writes only
# under release/macos so a local build never tramples source, test, or user data.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUT="$ROOT/release/macos"
BUILD="$OUT/build"
DIST="$OUT/dist"
SPEC="$OUT/spec"

rm -rf "$BUILD" "$DIST" "$SPEC"
mkdir -p "$BUILD" "$DIST" "$SPEC"

PYTHON=${PYTHON:-python3}
"$PYTHON" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name Synchri \
  --icon "$ROOT/assets/Synchri.icns" \
  --osx-bundle-identifier com.synchri.app \
  --add-data "$ROOT/synchri/ui/static:synchri/ui/static" \
  --add-data "$ROOT/synchri/storage/schema.sql:synchri/storage" \
  --paths "$ROOT" \
  --distpath "$DIST" \
  --workpath "$BUILD" \
  --specpath "$SPEC" \
  "$ROOT/synchri/mac_app.py"

APP="$DIST/Synchri.app"
[ -d "$APP" ] || { echo "Synchri.app was not created" >&2; exit 1; }

# A local unsigned build is useful for development.  Release CI provides an
# identity, at which point this becomes the signed application users receive.
if [ -n "${CODESIGN_IDENTITY:-}" ]; then
  codesign --force --options runtime --deep --sign "$CODESIGN_IDENTITY" "$APP"
fi

ACTUAL_ARCH=$(uname -m)
ARCH=${SYNCHRI_ARCH:-$ACTUAL_ARCH}
[ "$ARCH" = "$ACTUAL_ARCH" ] || {
  echo "requested $ARCH build on a $ACTUAL_ARCH runner; refusing to mislabel it" >&2
  exit 1
}
ARCHIVE="$OUT/Synchri-macos-$ARCH.zip"
rm -f "$ARCHIVE"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ARCHIVE"
shasum -a 256 "$ARCHIVE" > "$ARCHIVE.sha256"
printf 'Built %s\n' "$ARCHIVE"
