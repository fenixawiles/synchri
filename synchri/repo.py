"""Repository binding.

A room is about a codebase, so it records which one at creation time. That gives
two things the user should never have to think about:

* **rediscovery** — starting a new session inside the same repo finds the room
  again, with no room id to remember;
* **orientation** — a joining agent is told which repo the room is about, and
  warned if it is running somewhere else.

Git detection is best-effort: no git, no repository, or a broken checkout all
degrade to "just a directory", never to an error.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

GIT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class RepoInfo:
    """What we know about the working tree a room is bound to."""

    root: str
    is_git: bool = False
    branch: str | None = None
    head: str | None = None
    remote: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def short_head(self) -> str | None:
        return self.head[:12] if self.head else None

    def describe(self) -> str:
        if not self.is_git:
            return f"{self.root} (not a git repository)"
        where = f"{self.branch or 'detached'} @ {self.short_head or 'unknown'}"
        return f"{self.root} [{where}]" + (f" origin {self.remote}" if self.remote else "")


def detect(path: str | os.PathLike | None = None) -> RepoInfo:
    """Describe the repository containing ``path`` (default: the cwd)."""
    start = Path(path or Path.cwd()).expanduser()
    try:
        start = start.resolve()
    except OSError:  # pragma: no cover - unreadable path
        return RepoInfo(root=str(start))

    root = _git(start, "rev-parse", "--show-toplevel")
    if not root:
        return RepoInfo(root=str(start))

    return RepoInfo(
        root=str(Path(root).resolve()),
        is_git=True,
        branch=_git(start, "rev-parse", "--abbrev-ref", "HEAD") or None,
        head=_git(start, "rev-parse", "HEAD") or None,
        remote=_git(start, "config", "--get", "remote.origin.url") or None,
    )


def _git(cwd: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def same_tree(room_root: str | None, here: RepoInfo) -> bool:
    """Is ``here`` the working tree this room is bound to?

    A subdirectory of the room's root counts as the same tree — an agent
    working in ``src/`` is not in the wrong place.
    """
    if not room_root:
        return True
    try:
        room_path = Path(room_root).resolve()
        here_path = Path(here.root).resolve()
    except OSError:  # pragma: no cover - unreadable path
        return False
    return here_path == room_path or room_path in here_path.parents
