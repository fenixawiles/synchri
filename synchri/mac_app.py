"""Finder-friendly entry point for the packaged macOS app.

Opening ``Synchri.app`` starts the local interface.  Supplying arguments keeps
the very same executable useful to managed agents for ``activity``, ``read``,
and the rest of the CLI, so a bundled install never depends on PATH being set
up in a provider's terminal.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Running the source file directly is convenient when validating release
# tooling. PyInstaller already places the package on its import path.
if not getattr(sys, "frozen", False) and __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from synchri.cli.main import main as cli_main


def main() -> int:
    # Finder adds this opaque process-serial-number argument on some macOS
    # versions.  It is not a command the CLI should try to parse.
    args = [arg for arg in sys.argv[1:] if not arg.startswith("-psn_")]
    return cli_main(args or ["ui"])


if __name__ == "__main__":  # pragma: no cover - exercised by the release build
    raise SystemExit(main())
