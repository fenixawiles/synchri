"""Finding repositories, so the user never types a path or a URL.

Two sources, both optional conveniences over the fully-supported "pick a
directory" path:

* **local** -- git repositories under the usual code directories;
* **GitHub** -- whatever `gh` already has access to.

GitHub is deliberately shelled out to the user's own `gh` CLI rather than
integrated: no tokens to store, no API client, and it simply does not appear if
`gh` is absent or logged out. Local repositories remain fully supported with no
GitHub involvement at all.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

GH_TIMEOUT_SECONDS = 15.0
SCAN_TIMEOUT_SECONDS = 5.0

#: Where people keep code. Scanned shallowly; never recursive-everything.
DEFAULT_SEARCH_ROOTS = ("~/code", "~/src", "~/projects", "~/dev", "~/repos", "~/work", "~/git")
MAX_DEPTH = 3
MAX_RESULTS = 60


def local_repositories(
    roots: list[str] | None = None, *, extra: list[str] | None = None
) -> list[dict]:
    """Git repositories the user probably wants, newest activity first."""
    from . import worktree as worktree_module

    candidates: list[Path] = []
    search = [Path(r).expanduser() for r in (roots or DEFAULT_SEARCH_ROOTS)]
    search += [Path(p).expanduser() for p in (extra or [])]
    cwd = Path.cwd()
    search.insert(0, cwd)

    seen: set[Path] = set()
    for root in search:
        if not root.exists() or not root.is_dir():
            continue
        for path in _walk_for_git(root, MAX_DEPTH):
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                candidates.append(resolved)
        if len(candidates) >= MAX_RESULTS:
            break

    found = []
    for path in candidates[:MAX_RESULTS]:
        status = worktree_module.inspect_repository(path)
        if not status.is_valid:
            continue
        # Synchri's own worktrees are not projects to start sessions in.
        if path.name.startswith("synchri-"):
            continue
        found.append(
            {
                "source": "local",
                "name": status.name,
                "path": status.root,
                "branch": status.branch,
                "remote": status.remote,
                "dirty": status.is_dirty,
                "last_commit": _last_commit_time(status.root),
            }
        )
    found.sort(key=lambda r: r["last_commit"] or "", reverse=True)
    return found


def _walk_for_git(root: Path, depth: int) -> list[Path]:
    if depth < 0:
        return []
    if (root / ".git").exists():
        return [root]
    found: list[Path] = []
    try:
        for entry in sorted(root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            found.extend(_walk_for_git(entry, depth - 1))
            if len(found) >= MAX_RESULTS:
                break
    except (OSError, PermissionError):  # pragma: no cover - unreadable directory
        return found
    return found


def _last_commit_time(path: str) -> str | None:
    from . import worktree as worktree_module

    return worktree_module.git(path, "log", "-1", "--format=%cI", check=False) or None


def github_available() -> bool:
    """Is `gh` installed and authenticated?"""
    try:
        completed = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def github_repositories(limit: int = 50) -> list[dict]:
    """Repositories the user's own `gh` can see. Empty if unavailable."""
    try:
        completed = subprocess.run(
            [
                "gh", "repo", "list", "--limit", str(limit),
                "--json", "name,nameWithOwner,url,sshUrl,description,updatedAt,isPrivate",
            ],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    try:
        raw = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError:  # pragma: no cover - unexpected gh output
        return []

    return [
        {
            "source": "github",
            "name": item.get("name"),
            "full_name": item.get("nameWithOwner"),
            "url": item.get("url"),
            "clone_url": item.get("sshUrl") or item.get("url"),
            "description": item.get("description"),
            "updated_at": item.get("updatedAt"),
            "private": item.get("isPrivate", False),
            # A GitHub entry is only usable once it exists on disk.
            "local_path": _matching_local_clone(item.get("nameWithOwner") or ""),
        }
        for item in raw
    ]


def _matching_local_clone(full_name: str) -> str | None:
    """Is this GitHub repo already cloned somewhere we can see?"""
    if not full_name:
        return None
    tail = full_name.split("/")[-1]
    for repo in local_repositories():
        remote = (repo.get("remote") or "").lower()
        if full_name.lower() in remote or (repo["name"] == tail and remote):
            return repo["path"]
    return None


def repositories(include_github: bool = True) -> dict:
    """Everything the repository step can offer, in one call."""
    local = local_repositories()
    github: list[dict] = []
    available = include_github and github_available()
    if available:
        github = github_repositories()
    return {
        "local": local,
        "github": github,
        "github_available": available,
        "cwd": str(Path.cwd()),
    }
