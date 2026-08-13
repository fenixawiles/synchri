#!/bin/sh
# Exercise the actual Finder application bundle before it reaches notarization.
#
# A successful PyInstaller build only proves a ZIP exists. This check starts the
# packaged executable with a brand-new Synchri home, verifies its required
# runtime resource, and confirms that the process remains alive long enough to
# serve its local interface. It catches the class of missing-resource launch
# failure that a unit test against source Python cannot see.
set -eu

APP=${1:-release/macos/dist/Synchri.app}
EXECUTABLE="$APP/Contents/MacOS/Synchri"
SCHEMA="$APP/Contents/Resources/synchri/storage/schema.sql"

[ -x "$EXECUTABLE" ] || {
  printf 'Packaged Synchri executable is missing: %s\n' "$EXECUTABLE" >&2
  exit 1
}
[ -f "$SCHEMA" ] || {
  printf 'Packaged Synchri schema is missing: %s\n' "$SCHEMA" >&2
  exit 1
}

SMOKE_HOME=$(mktemp -d -t synchri-smoke)
LOG="$SMOKE_HOME/launch.log"
PID=""

cleanup() {
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  rm -rf "$SMOKE_HOME"
}
trap cleanup EXIT INT TERM

# Windowed macOS apps deliberately do not inherit a useful stdout stream, so
# observe the thing a user actually needs instead: a working loopback server.
# The runner has no competing desktop process, and this explicit port makes the
# check independent of a bundle's detached console behaviour.
SMOKE_PORT=$(python3 - <<'PY'
import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
PY
)
SYNCHRI_HOME="$SMOKE_HOME/home" "$EXECUTABLE" ui --no-open --port "$SMOKE_PORT" >"$LOG" 2>&1 &
PID=$!

attempt=0
while [ "$attempt" -lt 20 ]; do
  if ! kill -0 "$PID" 2>/dev/null; then
    printf 'Packaged Synchri exited during launch. Output follows:\n' >&2
    sed -n '1,160p' "$LOG" >&2
    exit 1
  fi
  if curl --silent --show-error --fail --max-time 1 "http://127.0.0.1:$SMOKE_PORT/" >/dev/null 2>&1; then
    printf 'Packaged Synchri launch smoke test passed.\n'
    exit 0
  fi
  attempt=$((attempt + 1))
  sleep 0.25
done

printf 'Packaged Synchri did not start its local interface. Output follows:\n' >&2
sed -n '1,160p' "$LOG" >&2
exit 1
