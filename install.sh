#!/bin/sh
# Synchri installer.
#
#   curl -fsSL https://synchri.com/install.sh | sh
#
# Installs the `synchri` command with pipx when available (isolated venv, no
# interference with your system Python), otherwise falls back to pip --user.
# Nothing runs as root and nothing is written outside your own directories.

set -eu

RED='\033[0;31m'; GREEN='\033[0;32m'; DIM='\033[2m'; BOLD='\033[1m'; OFF='\033[0m'
say()  { printf '%b\n' "$1"; }
fail() { say "${RED}$1${OFF}" >&2; exit 1; }

say "${BOLD}Synchri${OFF} — installing"
say ""

# --- python -----------------------------------------------------------------
PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
            PY="$candidate"; break
        fi
    fi
done
[ -n "$PY" ] || fail "Synchri needs Python 3.10 or newer. Install it, then run this again."
say "  python   $($PY --version 2>&1)  ${DIM}($(command -v $PY))${OFF}"

# --- git --------------------------------------------------------------------
if command -v git >/dev/null 2>&1; then
    say "  git      $(git --version | awk '{print $3}')"
else
    fail "Synchri needs git: it creates an isolated worktree for every session."
fi

# --- install ----------------------------------------------------------------
say ""
if command -v pipx >/dev/null 2>&1; then
    say "  installing with pipx…"
    pipx install --force synchri >/dev/null
    METHOD="pipx"
else
    say "  pipx not found; using pip --user"
    say "  ${DIM}(pipx is tidier: brew install pipx  /  apt install pipx)${OFF}"
    "$PY" -m pip install --user --upgrade --quiet synchri
    METHOD="pip --user"
fi

# --- verify -----------------------------------------------------------------
say ""
if command -v synchri >/dev/null 2>&1; then
    say "${GREEN}  installed:${OFF} $(synchri --version)  ${DIM}via ${METHOD}${OFF}"
else
    USER_BIN="$("$PY" -c 'import site,os;print(os.path.join(site.USER_BASE,"bin"))' 2>/dev/null || echo "")"
    say "${GREEN}  installed${OFF} via ${METHOD}, but ${BOLD}synchri${OFF} is not on your PATH yet."
    [ -n "$USER_BIN" ] && say "  Add this to your shell profile:"
    [ -n "$USER_BIN" ] && say "      export PATH=\"$USER_BIN:\$PATH\""
fi

say ""
say "${BOLD}Next:${OFF} open the app from any repository"
say ""
say "      cd ~/your-project"
say "      synchri ui"
say ""
say "  ${DIM}Prefer the terminal?  synchri start${OFF}"
say ""
