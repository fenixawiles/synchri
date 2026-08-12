"""Evidence gathering: what the tests actually say, and what actually landed.

The completion model refuses to take an agent's word for it, so something has to
produce the facts independently. This module runs the project's own test command
inside the authorized worktree and reads git for what was committed.

Test-command detection is best-effort and always overridable: guessing wrong
should mean "no counts", never a false green.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..errors import ValidationError

DEFAULT_TIMEOUT_SECONDS = 900.0

#: (marker file, command). First match wins; a project may override entirely.
_PYTEST_COMMAND = f"{shlex.quote(sys.executable)} -m pytest -q"

DETECTORS: tuple[tuple[str, str], ...] = (
    ("pytest.ini", _PYTEST_COMMAND),
    ("tox.ini", _PYTEST_COMMAND),
    ("pyproject.toml", _PYTEST_COMMAND),
    ("setup.cfg", _PYTEST_COMMAND),
    ("Cargo.toml", "cargo test"),
    ("go.mod", "go test ./..."),
    ("Gemfile", "bundle exec rspec"),
    ("package.json", "npm test --silent"),
)

# pytest summary counts. Read independently rather than positionally: pytest
# prints "1 failed, 1 passed" on failure and "2 passed" on success, so a fixed
# order silently loses counts.
_PYTEST_COUNTS = {
    "passed": re.compile(r"(\d+) passed"),
    "failed": re.compile(r"(\d+) failed"),
    "errors": re.compile(r"(\d+) error"),
    "skipped": re.compile(r"(\d+) skipped"),
}
_PYTEST_SUMMARY = re.compile(r"\d+ (?:passed|failed|error|skipped)")
# jest / vitest: "Tests:  3 failed, 20 passed, 23 total"
_JEST = re.compile(r"Tests:\s+(?:(?P<failed>\d+) failed,\s*)?(?:(?P<passed>\d+) passed)")
# go: counts "--- FAIL" / "--- PASS" lines
_GO_PASS = re.compile(r"^--- PASS", re.M)
_GO_FAIL = re.compile(r"^--- FAIL", re.M)


@dataclass
class TestRun:
    """One execution of the project's test command."""

    command: str
    ran: bool
    exit_code: int | None = None
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    duration_seconds: float = 0.0
    timed_out: bool = False
    output_tail: str = ""
    detail: str = ""

    @property
    def green(self) -> bool:
        """Only an actually-clean run counts. Unknown is not green."""
        return self.ran and self.exit_code == 0 and self.failed == 0 and self.errors == 0

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["green"] = self.green
        payload["total"] = self.passed + self.failed + self.skipped + self.errors
        return payload

    def summary(self) -> str:
        if not self.ran:
            return self.detail or "not run"
        if self.timed_out:
            return f"timed out after {self.duration_seconds:.0f}s"
        parts = [f"{self.passed} passed"]
        if self.failed:
            parts.append(f"{self.failed} failed")
        if self.errors:
            parts.append(f"{self.errors} errors")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        return ", ".join(parts)


def detect_test_command(worktree_path: str | Path) -> str | None:
    """Guess how this project runs its tests, or admit we do not know."""
    root = Path(worktree_path)
    for marker, command in DETECTORS:
        if (root / marker).exists():
            if marker == "package.json":
                try:
                    import json

                    scripts = json.loads((root / marker).read_text(encoding="utf-8")).get(
                        "scripts", {}
                    )
                except (OSError, ValueError):  # pragma: no cover - malformed manifest
                    continue
                if "test" not in scripts:
                    continue
            return command
    return None


def run_tests(
    worktree_path: str | Path,
    command: str | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> TestRun:
    """Run the project's tests inside the worktree and count the results.

    Never raises: a missing runner, a crash, or a timeout all come back as a
    ``TestRun`` that is not green.
    """
    root = Path(worktree_path)
    if not root.exists():
        return TestRun(command or "", ran=False, detail=f"{root} does not exist")

    chosen = command or detect_test_command(root)
    if not chosen:
        return TestRun(
            "", ran=False,
            detail="could not detect a test command; set one explicitly to collect evidence",
        )

    import time

    started = time.monotonic()
    try:
        completed = subprocess.run(
            shlex.split(chosen),
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return TestRun(chosen, ran=True, timed_out=True,
                       duration_seconds=time.monotonic() - started,
                       detail=f"timed out after {timeout:g}s")
    except (OSError, ValueError) as exc:
        return TestRun(chosen, ran=False, detail=f"could not run tests: {exc}")

    output = (completed.stdout or "") + (completed.stderr or "")
    run = TestRun(
        command=chosen,
        ran=True,
        exit_code=completed.returncode,
        duration_seconds=time.monotonic() - started,
        output_tail=output[-4000:],
    )
    _parse_counts(output, run)
    return run


def _parse_counts(output: str, run: TestRun) -> None:
    jest = _JEST.search(output)
    if jest:
        run.passed = int(jest.group("passed") or 0)
        run.failed = int(jest.group("failed") or 0)
        return
    if "--- PASS" in output or "--- FAIL" in output:
        run.passed = len(_GO_PASS.findall(output))
        run.failed = len(_GO_FAIL.findall(output))
        return
    # pytest prints its summary on the last line that carries counts.
    for line in reversed([line for line in output.splitlines() if line.strip()]):
        if not _PYTEST_SUMMARY.search(line):
            continue
        for field_name, pattern in _PYTEST_COUNTS.items():
            match = pattern.search(line)
            setattr(run, field_name, int(match.group(1)) if match else 0)
        return


@dataclass
class ChangeSummary:
    """What the session has actually produced in its worktree."""

    commits: int = 0
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    recent: list[dict] = field(default_factory=list)
    dirty_files: list[str] = field(default_factory=list)
    head: str | None = None
    branch: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def summarize_changes(
    worktree_path: str | Path, base_branch: str, *, limit: int = 20
) -> ChangeSummary:
    """Commits and diff stats on the session branch, relative to its base."""
    from . import worktree as worktree_module

    root = Path(worktree_path)
    if not root.exists():
        return ChangeSummary()

    def run(*args: str) -> str:
        return worktree_module.git(root, *args, check=False)

    summary = ChangeSummary(
        head=run("rev-parse", "HEAD") or None,
        branch=run("rev-parse", "--abbrev-ref", "HEAD") or None,
    )
    span = f"{base_branch}..HEAD"
    log = run("log", span, f"--max-count={limit}", "--pretty=format:%h\x1f%an\x1f%ad\x1f%s",
              "--date=iso")
    for line in (line for line in log.splitlines() if line.strip()):
        sha, author, date, subject = (line.split("\x1f") + ["", "", "", ""])[:4]
        summary.recent.append(
            {"sha": sha, "author": author, "date": date, "subject": subject}
        )
    count = run("rev-list", "--count", span)
    summary.commits = int(count) if count.isdigit() else len(summary.recent)

    stat = run("diff", "--shortstat", span)
    for pattern, field_name in (
        (r"(\d+) files? changed", "files_changed"),
        (r"(\d+) insertions?", "insertions"),
        (r"(\d+) deletions?", "deletions"),
    ):
        match = re.search(pattern, stat)
        if match:
            setattr(summary, field_name, int(match.group(1)))

    summary.dirty_files = [
        line[3:] for line in run("status", "--porcelain").splitlines() if line.strip()
    ][:50]
    return summary


def diff_text(worktree_path: str | Path, base_branch: str, *, max_chars: int = 200_000) -> str:
    """The raw diff, for the transparency panel."""
    from . import worktree as worktree_module

    if not Path(worktree_path).exists():
        return ""
    text = worktree_module.git(
        worktree_path, "diff", f"{base_branch}...HEAD", check=False
    )
    return text if len(text) <= max_chars else text[:max_chars] + "\n… (truncated)"
