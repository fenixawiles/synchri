"""Workspace layout and filesystem permissions.

Everything AIDapter persists lives under a single workspace directory,
``~/.aidapter`` by default, overridable with ``AIDAPTER_HOME``.  The directory
and every file inside it are created owner-only (0700 / 0600): the workspace
holds room join tokens and participant secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import ValidationError
from .ids import is_valid_id

ENV_HOME = "AIDAPTER_HOME"
DIR_MODE = 0o700
FILE_MODE = 0o600

DEFAULT_MAX_CONSECUTIVE_AGENT_TURNS = 8


@dataclass(frozen=True)
class Workspace:
    """Resolved paths for one AIDapter workspace."""

    home: Path

    @property
    def db_path(self) -> Path:
        return self.home / "aidapter.db"

    @property
    def rooms_dir(self) -> Path:
        return self.home / "rooms"

    @property
    def sessions_dir(self) -> Path:
        return self.home / "sessions"

    def room_dir(self, room_id: str) -> Path:
        """Per-room directory.

        The path component is the room id, which is validated against a strict
        pattern first.  User-supplied strings (room names, participant names)
        never reach the filesystem, so there is no traversal surface here.
        """
        if not is_valid_id("room", room_id):
            raise ValidationError(f"malformed room id: {room_id!r}")
        return self.rooms_dir / room_id

    def memory_path(self, room_id: str) -> Path:
        return self.room_dir(room_id) / "memory.md"

    def transcript_path(self, room_id: str) -> Path:
        return self.room_dir(room_id) / "transcript.jsonl"

    def session_path(self, room_id: str, participant_name: str) -> Path:
        from .ids import NAME_PATTERN  # local import to keep module import cheap

        if not NAME_PATTERN.match(participant_name or ""):
            raise ValidationError(f"malformed participant name: {participant_name!r}")
        return self.sessions_dir / f"{room_id}.{participant_name}.json"

    def ensure(self) -> "Workspace":
        """Create the workspace tree if absent, with owner-only permissions."""
        for path in (self.home, self.rooms_dir, self.sessions_dir):
            path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        _harden(self.home)
        return self

    def ensure_room_dir(self, room_id: str) -> Path:
        path = self.room_dir(room_id)
        path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        return path


def _harden(path: Path) -> None:
    """Best-effort tighten of an existing directory (no-op on filesystems that refuse)."""
    try:
        os.chmod(path, DIR_MODE)
    except OSError:  # pragma: no cover - platform dependent
        pass


def resolve_workspace(home: str | os.PathLike | None = None) -> Workspace:
    """Resolve the workspace from an explicit path, ``AIDAPTER_HOME``, or the default."""
    raw = home or os.environ.get(ENV_HOME) or (Path.home() / ".aidapter")
    return Workspace(Path(raw).expanduser().resolve())


def write_private(path: Path, data: str) -> None:
    """Atomically write ``data`` to ``path`` with 0600 permissions.

    Writes go to a temp file in the same directory and are then renamed, so a
    concurrent reader sees either the old file or the new one, never a partial.
    """
    path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():  # pragma: no cover - only on write failure
            tmp.unlink(missing_ok=True)


def append_private(path: Path, line: str) -> None:
    """Append one line to a 0600 file, creating it if needed."""
    path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(line if line.endswith("\n") else line + "\n")
