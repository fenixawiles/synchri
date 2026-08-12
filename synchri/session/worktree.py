"""Isolated git worktrees.

Hard invariant: **agents never work in the user's primary tree.** Every session
gets a dedicated worktree on its own branch, created before the session can
activate, and every participant is told in the contract that it is the only
authorized place to make changes.

Names are human-readable on purpose — ``synchri-lh-amber-fox-4821`` is something
you can recognise in ``git worktree list`` and delete with confidence, which a
raw UUID is not. The trailing digits make collisions vanishingly unlikely, and
creation retries on the remaining chance.
"""

from __future__ import annotations

import re
import secrets
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from ..errors import StateError, ValidationError
from .modes import SessionMode

GIT_TIMEOUT_SECONDS = 60.0

MODE_ABBREVIATIONS = {
    SessionMode.INTERACTIVE.value: "ic",
    SessionMode.LONG_HORIZON.value: "lh",
    SessionMode.REVIEW_AUDIT.value: "review",
}

ADJECTIVES = (
    "amber", "brisk", "calm", "clever", "coral", "dusk", "eager", "fleet",
    "golden", "hardy", "ivory", "jade", "keen", "lucid", "mellow", "nimble",
    "opal", "prime", "quiet", "rapid", "slate", "steady", "teal", "umber",
    "vivid", "warm", "swift", "bright", "bold", "crisp",
)
NOUNS = (
    "fox", "hawk", "otter", "lynx", "heron", "ibis", "koi", "marten", "newt",
    "osprey", "puffin", "quail", "raven", "shrew", "tern", "vole", "wren",
    "yak", "zebra", "badger", "crane", "dingo", "elk", "finch", "gecko",
)

#: A session branch/worktree name we are willing to create or trust.
NAME_PATTERN = re.compile(r"^synchri-[a-z0-9-]{3,60}$")


@dataclass(frozen=True)
class Worktree:
    """The authorized mutation target for one session."""

    name: str
    path: str
    branch: str
    base_branch: str
    repo_root: str
    head: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        return f"{self.name} ({self.branch} from {self.base_branch}) at {self.path}"


def generate_name(mode: str | None = None) -> str:
    """A readable, collision-resistant session name."""
    abbreviation = MODE_ABBREVIATIONS.get(str(mode)) if mode else None
    parts = ["synchri"]
    if abbreviation:
        parts.append(abbreviation)
    parts.append(secrets.choice(ADJECTIVES))
    parts.append(secrets.choice(NOUNS))
    parts.append(f"{secrets.randbelow(9000) + 1000}")
    return "-".join(parts)


def git(cwd: str | Path, *args: str, check: bool = True) -> str:
    """Run git and return stdout, raising ``StateError`` with git's own message."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise StateError(f"git {' '.join(args)} timed out after {GIT_TIMEOUT_SECONDS:g}s") from exc
    except OSError as exc:
        raise StateError(f"could not run git: {exc}") from exc
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise StateError(f"git {' '.join(args)} failed: {detail}", code="git_error")
    return completed.stdout.strip()


# ----------------------------------------------------------------------
# repository inspection
# ----------------------------------------------------------------------


@dataclass
class RepoStatus:
    """Everything the repository step needs to show and validate."""

    root: str
    name: str
    branch: str | None
    head: str | None
    remote: str | None
    is_dirty: bool
    dirty_files: list[str]
    is_valid: bool
    problems: list[str]
    branches: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def inspect_repository(path: str | Path) -> RepoStatus:
    """Validate a candidate repository and describe it in plain terms.

    Never mutates anything, including a dirty working tree: a dirty primary tree
    is reported so the user can see it, not cleaned up on their behalf.
    """
    candidate = Path(path).expanduser()
    problems: list[str] = []

    if not candidate.exists():
        return RepoStatus(str(candidate), candidate.name, None, None, None, False, [], False,
                          [f"{candidate} does not exist"], [])
    if not candidate.is_dir():
        return RepoStatus(str(candidate), candidate.name, None, None, None, False, [], False,
                          [f"{candidate} is not a directory"], [])

    try:
        root = git(candidate, "rev-parse", "--show-toplevel")
    except StateError:
        return RepoStatus(str(candidate.resolve()), candidate.name, None, None, None, False, [],
                          False, [f"{candidate} is not a git repository"], [])

    root_path = Path(root).resolve()
    branch = git(root_path, "rev-parse", "--abbrev-ref", "HEAD", check=False) or None
    head = git(root_path, "rev-parse", "HEAD", check=False) or None
    remote = git(root_path, "config", "--get", "remote.origin.url", check=False) or None
    porcelain = git(root_path, "status", "--porcelain", check=False)
    dirty_files = [line[3:] for line in porcelain.splitlines() if line.strip()]

    if head is None:
        problems.append("the repository has no commits yet; make one before starting a session")
    for marker, label in (
        ("MERGE_HEAD", "an unfinished merge"),
        ("REBASE_HEAD", "an unfinished rebase"),
        ("CHERRY_PICK_HEAD", "an unfinished cherry-pick"),
        ("BISECT_LOG", "an active bisect"),
    ):
        if (root_path / ".git" / marker).exists():
            problems.append(f"the repository is in {label}; finish or abort it first")

    branches = [
        line.strip().lstrip("* ").strip()
        for line in git(root_path, "branch", "--format=%(refname:short)", check=False).splitlines()
        if line.strip()
    ]

    return RepoStatus(
        root=str(root_path),
        name=root_path.name,
        branch=branch,
        head=head,
        remote=remote,
        is_dirty=bool(dirty_files),
        dirty_files=dirty_files[:50],
        is_valid=not problems,
        problems=problems,
        branches=branches,
    )


# ----------------------------------------------------------------------
# worktree lifecycle
# ----------------------------------------------------------------------


def default_worktree_parent(repo_root: str | Path) -> Path:
    """Worktrees live beside the repo, never inside it.

    Inside would mean the session's own changes show up as untracked files in
    the primary tree, which is exactly the interference this avoids.
    """
    root = Path(repo_root).resolve()
    return root.parent / f".synchri-worktrees-{root.name}"


def create(
    repo_root: str | Path,
    base_branch: str,
    *,
    mode: str | None = None,
    name: str | None = None,
    parent_dir: str | Path | None = None,
    attempts: int = 5,
) -> Worktree:
    """Create the session's isolated worktree on a fresh branch.

    Retries on name collision, then gives up rather than silently reusing an
    existing tree — reuse would break the isolation guarantee.
    """
    root = Path(repo_root).resolve()
    status = inspect_repository(root)
    if not status.is_valid:
        raise StateError("; ".join(status.problems), code="repo_unusable")
    if base_branch not in status.branches and not _rev_exists(root, base_branch):
        raise ValidationError(f"base branch {base_branch!r} does not exist in this repository")

    parent = Path(parent_dir).expanduser() if parent_dir else default_worktree_parent(root)
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    last_error: StateError | None = None
    for _ in range(max(1, attempts)):
        candidate = name or generate_name(mode)
        if not NAME_PATTERN.match(candidate):
            raise ValidationError(
                f"invalid worktree name {candidate!r}: expected 'synchri-' followed by "
                "lowercase letters, digits, and hyphens"
            )
        path = parent / candidate
        if path.exists() or _rev_exists(root, candidate):
            if name:
                raise ValidationError(f"a worktree or branch named {candidate!r} already exists")
            continue
        try:
            git(root, "worktree", "add", "-b", candidate, str(path), base_branch)
        except StateError as exc:
            last_error = exc
            if name:
                raise
            continue
        return validate(root, path, candidate, base_branch)

    raise StateError(
        f"could not create an isolated worktree after {attempts} attempts"
        + (f": {last_error.message}" if last_error else ""),
        code="worktree_failed",
    )


def validate(
    repo_root: str | Path, path: str | Path, name: str, base_branch: str
) -> Worktree:
    """Confirm the worktree exists, is a real checkout, and is not the primary tree."""
    root = Path(repo_root).resolve()
    tree = Path(path).resolve()

    if not tree.exists():
        raise StateError(f"worktree path {tree} does not exist", code="worktree_missing")
    if tree == root:
        # The invariant, enforced rather than documented.
        raise StateError(
            "the session worktree must not be the primary working tree",
            code="worktree_is_primary",
        )
    if not (tree / ".git").exists():
        raise StateError(f"{tree} is not a git worktree", code="worktree_invalid")

    actual_root = git(tree, "rev-parse", "--show-toplevel", check=False)
    if Path(actual_root).resolve() != tree:
        raise StateError(
            f"{tree} does not look like its own working tree", code="worktree_invalid"
        )
    branch = git(tree, "rev-parse", "--abbrev-ref", "HEAD", check=False) or name
    head = git(tree, "rev-parse", "HEAD", check=False) or None

    return Worktree(
        name=name,
        path=str(tree),
        branch=branch,
        base_branch=base_branch,
        repo_root=str(root),
        head=head,
    )


def exists(worktree: Worktree) -> bool:
    """Cheap check used on restart before trusting a stored worktree."""
    try:
        validate(worktree.repo_root, worktree.path, worktree.name, worktree.base_branch)
    except (StateError, ValidationError):
        return False
    return True


def remove(worktree: Worktree, *, force: bool = False, delete_branch: bool = False) -> None:
    """Detach a session worktree. Never touches the primary tree."""
    tree = Path(worktree.path).resolve()
    if tree == Path(worktree.repo_root).resolve():  # pragma: no cover - defensive
        raise StateError("refusing to remove the primary working tree")
    args = ["worktree", "remove", str(tree)]
    if force:
        args.append("--force")
    git(worktree.repo_root, *args, check=False)
    if delete_branch:
        git(worktree.repo_root, "branch", "-D", worktree.branch, check=False)
    git(worktree.repo_root, "worktree", "prune", check=False)


def list_session_worktrees(repo_root: str | Path) -> list[dict]:
    """Every Synchri-created worktree in this repository, for cleanup UIs."""
    raw = git(repo_root, "worktree", "list", "--porcelain", check=False)
    found: list[dict] = []
    current: dict = {}
    for line in raw.splitlines():
        if line.startswith("worktree "):
            if current:
                found.append(current)
            current = {"path": line.split(" ", 1)[1]}
        elif line.startswith("branch "):
            current["branch"] = line.split(" ", 1)[1].replace("refs/heads/", "")
        elif line.startswith("HEAD "):
            current["head"] = line.split(" ", 1)[1]
    if current:
        found.append(current)
    return [w for w in found if Path(w.get("path", "")).name.startswith("synchri-")]


def _rev_exists(repo_root: str | Path, ref: str) -> bool:
    try:
        git(repo_root, "rev-parse", "--verify", "--quiet", ref)
    except StateError:
        return False
    return True
