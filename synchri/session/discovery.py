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
import re
import subprocess
from pathlib import Path

from ..errors import StateError, ValidationError

GH_TIMEOUT_SECONDS = 15.0
SCAN_TIMEOUT_SECONDS = 5.0

#: Where people keep code. Scanned shallowly; never recursive-everything.
DEFAULT_SEARCH_ROOTS = ("~/code", "~/src", "~/projects", "~/dev", "~/repos", "~/work", "~/git")
MAX_DEPTH = 3
MAX_RESULTS = 60

# We intentionally accept only GitHub references here.  The quick-start UI is
# not a generic "run git against this string" surface: it is the friendly path
# for cloning a project the user selected into their own Desktop folder.
_GITHUB_REFERENCE = re.compile(
    r"^(?:(?:https?://github\.com/)|(?:git@github\.com:))?"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}[A-Za-z0-9])?)/"
    r"(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?/?$"
)


def desktop_clone_root() -> Path:
    """The visible, user-owned home for repositories Synchri fetches."""
    return Path.home() / "Desktop" / "Synchri"


def github_reference(value: str) -> tuple[str, str, str] | None:
    """Parse an ``owner/repo`` or GitHub URL without accepting a shell-ish path."""
    matched = _GITHUB_REFERENCE.fullmatch((value or "").strip())
    if not matched:
        return None
    owner, repo = matched.group("owner"), matched.group("repo")
    return owner, repo, f"https://github.com/{owner}/{repo}.git"


def clone_github_repository(
    reference: str, *, destination_root: str | Path | None = None
) -> dict:
    """Clone a selected GitHub repository into a predictable Desktop folder.

    Existing directories are never replaced.  A failed clone is left intact so
    the user can inspect it; Synchri never attempts a hidden cleanup of files it
    just put on the desktop.
    """
    parsed = github_reference(reference)
    if parsed is None:
        raise ValidationError("enter a GitHub URL or owner/repository, e.g. fenixawiles/synchri")
    owner, repo, source = parsed
    parent = Path(destination_root).expanduser() if destination_root else desktop_clone_root()
    target = parent / f"{owner}-{repo}"

    if target.exists():
        raise ValidationError(
            f"{target} already exists; choose that local repository instead rather than overwriting it"
        )

    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise StateError(f"could not create {parent}: {exc}", code="clone_failed") from exc
    _clone(source, target)

    from . import worktree as worktree_module

    status = worktree_module.inspect_repository(target)
    if not status.is_valid:
        raise StateError(
            f"cloned {owner}/{repo}, but the result is not usable: {'; '.join(status.problems)}",
            code="clone_failed",
        )
    return {
        "path": status.root,
        "name": status.name,
        "source": f"{owner}/{repo}",
        "destination": str(target),
        "cloned": True,
    }


def _clone(source: str, target: Path) -> None:
    """Run one bounded, argument-safe clone invocation."""
    try:
        completed = subprocess.run(
            ["git", "clone", "--", source, str(target)],
            capture_output=True,
            text=True,
            timeout=60.0,
        )
    except subprocess.TimeoutExpired as exc:
        raise StateError("cloning the repository timed out", code="clone_failed") from exc
    except OSError as exc:
        raise StateError(f"could not run git clone: {exc}", code="clone_failed") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "git clone failed").strip()
        raise StateError(f"could not clone the repository: {detail}", code="clone_failed")


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
