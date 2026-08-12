"""Saved session configurations.

A preset stores the parts of a session that repeat -- mode, agents, roles,
permissions, escalation policy. It deliberately does NOT store the product
spec, the deadline, the worktree, or any session id: those are what makes a
session this session, and silently reusing them would be a footgun.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..config import Workspace, write_private
from ..errors import NotFoundError, ValidationError

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 +._-]{0,63}$")

#: Fields a preset may never carry, enforced on save rather than trusted.
FORBIDDEN = ("spec", "deadline", "worktree", "session_id", "repo_path", "room_id")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


def presets_dir(workspace: Workspace) -> Path:
    path = workspace.home / "presets"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def save(workspace: Workspace, name: str, preset: dict) -> str:
    if not NAME_PATTERN.match(name or ""):
        raise ValidationError(
            f"invalid preset name {name!r}: use letters, digits, spaces, dots, dashes"
        )
    cleaned = {k: v for k, v in (preset or {}).items() if k not in FORBIDDEN}
    if not cleaned.get("mode"):
        raise ValidationError("a preset needs a mode")
    cleaned["name"] = name
    path = presets_dir(workspace) / f"{_slug(name)}.json"
    write_private(path, json.dumps(cleaned, indent=2, sort_keys=True) + "\n")
    return str(path)


def load(workspace: Workspace, name: str) -> dict:
    path = presets_dir(workspace) / f"{_slug(name)}.json"
    if not path.exists():
        available = [p["name"] for p in list_presets(workspace)]
        raise NotFoundError(
            f"no preset named {name!r}"
            + (f"; available: {', '.join(available)}" if available else "")
        )
    return json.loads(path.read_text(encoding="utf-8"))


def list_presets(workspace: Workspace) -> list[dict]:
    found = []
    for path in sorted(presets_dir(workspace).glob("*.json")):
        try:
            found.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):  # pragma: no cover - corrupt file
            continue
    return found


def delete(workspace: Workspace, name: str) -> None:
    path = presets_dir(workspace) / f"{_slug(name)}.json"
    if not path.exists():
        raise NotFoundError(f"no preset named {name!r}")
    path.unlink()
