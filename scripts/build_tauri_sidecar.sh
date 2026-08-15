#!/bin/sh
# Package the existing local Synchri engine as a bundled resource. This process
# owns no UI window; its stdout gives the native shell the capability URL for
# the loopback-only interface.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON=${PYTHON:-python3}
TARGET=${SYNCHRI_TARGET_TRIPLE:-$(rustc --print host-tuple)}
PYINSTALLER_CODESIGN_IDENTITY=${PYINSTALLER_CODESIGN_IDENTITY:-}
OUT="$ROOT/release/tauri"
ENGINE_ROOT="$ROOT/desktop/src-tauri/engine"
BUILD="$OUT/sidecar-build"
DIST="$OUT/sidecar-dist"
SPEC="$OUT/sidecar-spec"
ENTITLEMENTS="$ROOT/desktop/src-tauri/EngineEntitlements.plist"

mkdir -p "$OUT" "$ENGINE_ROOT"
set -- \
  --noconfirm \
  --clean \
  --onedir \
  --name synchri-engine \
  --collect-data certifi \
  --add-data "$ROOT/synchri/ui/static:synchri/ui/static" \
  --add-data "$ROOT/synchri/storage/schema.sql:synchri/storage" \
  --paths "$ROOT" \
  --distpath "$DIST" \
  --workpath "$BUILD" \
  --specpath "$SPEC" \
  --osx-entitlements-file "$ENTITLEMENTS"

# Sign the embedded Python framework before it is placed in the outer native
# app. The release runner gives this the same Developer ID identity used for
# Tauri's final application signature.
if [ -n "$PYINSTALLER_CODESIGN_IDENTITY" ]; then
  set -- "$@" --codesign-identity "$PYINSTALLER_CODESIGN_IDENTITY"
fi
"$PYTHON" -m PyInstaller "$@" "$ROOT/synchri/mac_app.py"

ENGINE="$DIST/synchri-engine"
[ -x "$ENGINE/synchri-engine" ] || { echo "Synchri engine was not created" >&2; exit 1; }

# Keep the executable and its Python runtime together in a resource directory.
# PyInstaller's one-file mode unpacks a framework at runtime, which macOS's
# hardened runtime rejects after the outer app is Developer-ID signed. A
# one-directory engine lets Tauri sign every nested Mach-O up front.
TARGET_ROOT="$ENGINE_ROOT/$TARGET"
BUILD_ROOT="$ENGINE_ROOT/.build-$TARGET"
rm -rf "$BUILD_ROOT"
mkdir -p "$BUILD_ROOT"
ditto "$ENGINE" "$BUILD_ROOT"
mv "$BUILD_ROOT/synchri-engine" "$BUILD_ROOT/synchri-core"
rm -rf "$TARGET_ROOT"
mv "$BUILD_ROOT" "$TARGET_ROOT"
printf 'Built sidecar bundle %s\n' "$TARGET_ROOT"
