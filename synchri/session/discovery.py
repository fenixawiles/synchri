"""Finding repositories, so the user never types a path or a URL.

Two sources, both optional conveniences over the fully-supported "pick a
directory" path:

* **local** -- git repositories under the usual code directories;
* **GitHub** -- repositories the person explicitly granted the Synchri GitHub
  App access to, through its native device sign-in.

GitHub credentials are a local detail of the desktop application.  They do not
live in room state, are never sent to agents, and no `gh` installation is
required. Local repositories remain fully supported with no GitHub involvement
at all.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..config import Workspace, resolve_workspace
from ..errors import StateError, ValidationError
from . import github_auth

GITHUB_TIMEOUT_SECONDS = 15.0
SCAN_TIMEOUT_SECONDS = 5.0

#: Where people keep code. Scanned shallowly; never recursive-everything.
DEFAULT_SEARCH_ROOTS = ("~/code", "~/src", "~/projects", "~/dev", "~/repos", "~/work", "~/git")
MAX_DEPTH = 3
MAX_RESULTS = 60

# We intentionally accept only GitHub references here.  The quick-start UI is
# not a generic "run git against this string" surface: it is the friendly path
# for cloning a project the user selected into their own Desktop folder.
_GITHUB_REFERENCE = re.compile(
    r"^(?:(?:https?://)?(?:www\.)?github\.com/|(?:git@github\.com:))?"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}[A-Za-z0-9])?)/"
    r"(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?/?$"
)


def desktop_clone_root() -> Path:
    """The visible, user-owned home for repositories Synchri fetches."""
    # Keep user projects distinct from a source checkout of Synchri itself.
    return Path.home() / "Desktop" / "Synchri Projects"


def github_reference(value: str) -> tuple[str, str, str] | None:
    """Parse an ``owner/repo`` or GitHub URL without accepting a shell-ish path."""
    matched = _GITHUB_REFERENCE.fullmatch((value or "").strip())
    if not matched:
        return None
    owner, repo = matched.group("owner"), matched.group("repo")
    return owner, repo, f"https://github.com/{owner}/{repo}.git"


def clone_github_repository(
    reference: str,
    *,
    destination_root: str | Path | None = None,
    workspace: Workspace | None = None,
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
    credentials = github_auth.credentials(workspace or resolve_workspace().ensure())
    access_token = (credentials or {}).get("access_token")
    # Preserve the two-argument helper seam used by integrations that replace
    # the actual clone with a local fixture. Real GitHub cloning receives the
    # secure ephemeral credential only when one exists.
    if access_token:
        _clone(source, target, access_token=access_token)
    else:
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


def resolve_github_repository(
    reference: str,
    *,
    destination_root: str | Path | None = None,
    workspace: Workspace | None = None,
) -> dict:
    """Return a safe local checkout for a GitHub repository.

    A repository is a reusable source, not a session identity.  This resolver
    first reuses an existing matching checkout and only clones when no safe
    match exists.  The caller can therefore start any number of sessions from
    the same GitHub URL; :mod:`worktree` creates a fresh isolated target for
    each session afterwards.

    Nothing is ever overwritten.  If Synchri's visible clone location is
    occupied by a different folder, the error says exactly what the user can
    do next instead of asking them to guess.
    """
    parsed = github_reference(reference)
    if parsed is None:
        raise ValidationError("choose a GitHub repository, e.g. fenixawiles/synchri")
    owner, repo, _source = parsed
    parent = Path(destination_root).expanduser() if destination_root else desktop_clone_root()
    target = parent / f"{owner}-{repo}"

    from . import worktree as worktree_module

    # The expected Synchri checkout is the inexpensive, most common lookup.
    if target.exists():
        status = worktree_module.inspect_repository(target)
        if status.is_valid and _remote_matches_reference(status.remote, owner, repo):
            return _resolved_checkout(status, owner, repo, reused=True)
        message = (
            f"Synchri's checkout location {target} is already occupied by a different folder. "
            "Synchri will not overwrite it. Choose that folder if it is the project you want, "
            "or move it and try again."
        )
        raise ValidationError(
            message,
            code="clone_location_conflict",
            resolution={
                "kind": "choose_local_repository",
                "path": str(target),
                "label": "Choose the existing folder",
            },
        )

    # A user may have cloned the project elsewhere.  Search the intentionally
    # shallow discovery roots plus Synchri's own clone shelf before cloning.
    for candidate in local_repositories(extra=[str(parent)]):
        if _remote_matches_reference(candidate.get("remote"), owner, repo):
            status = worktree_module.inspect_repository(candidate["path"])
            if status.is_valid:
                return _resolved_checkout(status, owner, repo, reused=True)

    cloned = clone_github_repository(
        reference, destination_root=parent, workspace=workspace
    )
    return {**cloned, "reused": False}


def _resolved_checkout(status, owner: str, repo: str, *, reused: bool) -> dict:
    return {
        "path": status.root,
        "name": status.name,
        "source": f"{owner}/{repo}",
        "destination": status.root,
        "cloned": False,
        "reused": reused,
    }


def _remote_matches_reference(remote: str | None, owner: str, repo: str) -> bool:
    """Match GitHub HTTPS and SSH remotes without treating a bare name as proof."""
    if not remote:
        return False
    normalized = remote.strip().lower().removesuffix(".git").rstrip("/")
    expected = f"github.com/{owner.lower()}/{repo.lower()}"
    return normalized.endswith(expected)


def _clone(source: str, target: Path, access_token: str | None = None) -> None:
    """Run one bounded, argument-safe clone invocation."""
    env = os.environ.copy()
    askpass: Path | None = None
    if access_token:
        askpass = _create_git_askpass()
        env.update(
            {
                "GIT_ASKPASS": str(askpass),
                "GIT_TERMINAL_PROMPT": "0",
                "SYNCHRI_GITHUB_ACCESS_TOKEN": access_token,
            }
        )
    completed = None
    try:
        completed = subprocess.run(
            ["git", "clone", "--", source, str(target)],
            capture_output=True,
            text=True,
            timeout=60.0,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise StateError("cloning the repository timed out", code="clone_failed") from exc
    except OSError as exc:
        raise StateError(f"could not run git clone: {exc}", code="clone_failed") from exc
    finally:
        if askpass:
            askpass.unlink(missing_ok=True)
    if completed is None or completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "git clone failed").strip()
        if not access_token and "authentication" in detail.lower():
            detail += " Connect GitHub in Synchri, then choose the repository again."
        raise StateError(f"could not clone the repository: {detail}", code="clone_failed")


def _create_git_askpass() -> Path:
    """Create a one-use helper so a clone never writes its token into Git config."""
    descriptor, raw = tempfile.mkstemp(prefix="synchri-git-askpass-", text=True)
    helper = Path(raw)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  *Username*) printf '%s\\n' x-access-token ;;\n"
            "  *) printf '%s\\n' \"$SYNCHRI_GITHUB_ACCESS_TOKEN\" ;;\n"
            "esac\n"
        )
    helper.chmod(0o700)
    return helper


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


def github_status(workspace: Workspace | None = None) -> dict:
    """Describe the local GitHub sign-in, independently of repository grants."""
    active_workspace = workspace or resolve_workspace().ensure()
    try:
        connected = bool(github_auth.credentials(active_workspace))
    except StateError:
        connected = False
    if connected:
        return {
            "installed": True,
            "authenticated": True,
            "account": github_auth.account(active_workspace),
            "message": "GitHub is signed in. Repository access is managed separately.",
        }
    return {
        "installed": True,
        "authenticated": False,
        "account": None,
        "message": "Sign in with GitHub to create your Synchri profile.",
        "resolution": {"kind": "connect_github"},
    }


def github_available(workspace: Workspace | None = None) -> bool:
    """Compatibility shorthand for callers that only need an availability bit."""
    return bool(github_status(workspace)["authenticated"])


def begin_github_login() -> dict:
    """Start GitHub App device flow for non-UI callers.

    The local browser API deliberately wraps this operation and retains the
    device secret server-side.  This low-level helper stays useful to trusted
    native callers that need to manage their own device-flow polling.
    """
    authorization = github_auth.start_device_authorization()
    return {
        "started": True,
        "device_code": authorization.device_code,
        **authorization.to_dict(),
    }


def github_repositories(
    limit: int = 50,
    *,
    local: list[dict] | None = None,
    workspace: Workspace | None = None,
) -> list[dict]:
    """Repositories explicitly accessible through the Synchri App installation."""
    token_bundle = github_auth.credentials(workspace or resolve_workspace().ensure())
    if not token_bundle:
        return []
    token = str(token_bundle.get("access_token") or "")
    if not token:
        return []
    installations = _github_get("/user/installations?per_page=100", token).get("installations", [])
    raw: list[dict] = []
    for installation in installations if isinstance(installations, list) else []:
        installation_id = installation.get("id") if isinstance(installation, dict) else None
        if not installation_id:
            continue
        try:
            page = _github_get(
                f"/user/installations/{installation_id}/repositories?per_page={min(100, limit)}",
                token,
            )
        except StateError:
            continue
        items = page.get("repositories", [])
        if isinstance(items, list):
            raw.extend(item for item in items if isinstance(item, dict))
        if len(raw) >= limit:
            break

    # A user can have the same repository reachable through more than one
    # installation.  The chooser should show it exactly once.
    seen: set[str] = set()
    found = []
    for item in raw:
        full_name = str(item.get("full_name") or "")
        if not full_name or full_name.lower() in seen:
            continue
        seen.add(full_name.lower())
        found.append(
            {
                "source": "github",
                "name": item.get("name"),
                "full_name": full_name,
                "url": item.get("html_url"),
                "clone_url": item.get("clone_url") or item.get("html_url"),
                "description": item.get("description"),
                "updated_at": item.get("updated_at"),
                "private": item.get("private", False),
                # A GitHub entry is only usable once it exists on disk.
                "local_path": _matching_local_clone(full_name, local=local),
            }
        )
    return found[:limit]


def github_repository_access(workspace: Workspace | None = None) -> dict[str, Any]:
    """Expose the explicit GitHub App grant state without listing repositories.

    A GitHub sign-in identifies the person; the app installation is the second
    choice that allows a person or organization to select repositories.  This
    tiny endpoint lets the UI give that missing step a clear resolution instead
    of silently displaying an empty list.
    """
    token_bundle = github_auth.credentials(workspace or resolve_workspace().ensure())
    token = str((token_bundle or {}).get("access_token") or "")
    if not token:
        return {
            "authorized": False,
            "installations": 0,
            "install_url": github_auth.GITHUB_APP_INSTALL_URL,
            "message": "Choose the repositories Synchri may access in GitHub.",
        }
    payload = _github_get("/user/installations?per_page=100", token)
    installations = payload.get("installations", [])
    count = len(installations) if isinstance(installations, list) else 0
    return {
        "authorized": count > 0,
        "installations": count,
        "install_url": github_auth.GITHUB_APP_INSTALL_URL,
        "message": (
            "Repository access is ready."
            if count
            else "Choose the repositories Synchri may access in GitHub."
        ),
    }


def _github_get(path: str, token: str) -> dict[str, Any]:
    request = Request(
        f"{github_auth.GITHUB_API_URL}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": github_auth.GITHUB_API_VERSION,
        },
    )
    try:
        with urlopen(
            request,
            timeout=GITHUB_TIMEOUT_SECONDS,
            context=github_auth.trusted_ssl_context(),
        ) as response:  # noqa: S310 - fixed API host
            import json

            value = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 401:
            raise StateError(
                "Your GitHub connection expired. Connect GitHub again to continue.",
                code="github_connection_expired",
                resolution={"kind": "connect_github"},
            ) from exc
        raise StateError(
            "GitHub could not load your repositories. Try again in a moment.",
            code="github_unavailable",
            resolution={"kind": "retry_github_login"},
        ) from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise StateError(
            "GitHub could not load your repositories. Check your connection and try again.",
            code="github_unavailable",
            resolution={"kind": "retry_github_login"},
        ) from exc
    return value if isinstance(value, dict) else {}


def _matching_local_clone(full_name: str, *, local: list[dict] | None = None) -> str | None:
    """Is this GitHub repo already cloned somewhere we can see?"""
    if not full_name:
        return None
    tail = full_name.split("/")[-1]
    for repo in (local if local is not None else local_repositories()):
        remote = (repo.get("remote") or "").lower()
        if full_name.lower() in remote or (repo["name"] == tail and remote):
            return repo["path"]
    return None


def repositories(
    include_github: bool = True,
    *,
    include_local: bool = True,
    workspace: Workspace | None = None,
) -> dict:
    """Everything the repository step can offer, in one call."""
    local = local_repositories() if include_local else []
    github: list[dict] = []
    active_workspace = workspace or resolve_workspace().ensure()
    status = github_status(active_workspace) if include_github else {"installed": False, "authenticated": False}
    # Keep the small boolean seam for older API consumers and test doubles.
    # Retain the zero-argument seam used by external library consumers and
    # lightweight test doubles.  ``github_status`` already authenticated the
    # exact workspace above.
    access = {
        "authorized": False,
        "installations": 0,
        "install_url": github_auth.GITHUB_APP_INSTALL_URL,
        "message": "Choose the repositories Synchri may access in GitHub.",
    }
    available = bool(include_github and status.get("authenticated") and github_available())
    if available:
        try:
            access = github_repository_access(active_workspace)
            if access["authorized"]:
                github = github_repositories(local=local, workspace=active_workspace)
        except StateError as exc:
            # Local discovery remains instant and useful when GitHub is briefly
            # unavailable. A later background request gets another chance — but
            # the failure must stay distinguishable from "no repositories were
            # granted", or the chooser silently shows an empty list with no way
            # to tell a network fault from a missing installation.
            access["unavailable"] = True
            access["message"] = exc.message
    return {
        "local": local,
        "github": github,
        "github_available": bool(available and access["authorized"]),
        "github_status": status,
        "github_access": access,
        "cwd": str(Path.cwd()),
    }
