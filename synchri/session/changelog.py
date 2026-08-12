"""The durable, evidence-backed record created when a session completes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .gates import Gate

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .manager import SessionRecord


def render(
    record: "SessionRecord",
    gates: list[Gate],
    changes: dict,
    test_run: dict | None,
    *,
    completed_at: str,
) -> str:
    """Render the final session artifact from measured state, never agent prose."""
    lines = [
        "# Synchri final changelog",
        "",
        "## Session",
        "",
        f"- **Session:** {record.name}",
        f"- **ID:** `{record.session_id}`",
        f"- **Completed:** {completed_at}",
        f"- **Mode:** {record.policy.label}",
        "",
        "## Delivery",
        "",
        f"- **Repository:** {record.repo_name}",
        f"- **Branch:** `{changes.get('branch') or record.worktree_branch or 'unknown'}`",
        f"- **Head:** `{changes.get('head') or 'unknown'}`",
        f"- **Worktree:** `{record.worktree_path or 'unknown'}`",
        "",
        "## Changes",
        "",
    ]
    commits = changes.get("recent") or []
    if commits:
        for commit in commits:
            sha = str(commit.get("sha") or "")[:12] or "unknown"
            subject = str(commit.get("subject") or "(no subject)")
            lines.append(f"- `{sha}` — {subject}")
    else:
        lines.append("- No commits were recorded on the session branch.")
    lines.extend(
        [
            f"- **Files changed:** {changes.get('files_changed', 0)}",
            f"- **Insertions:** {changes.get('insertions', 0)}",
            f"- **Deletions:** {changes.get('deletions', 0)}",
            "",
            "## Verification",
            "",
        ]
    )
    if test_run:
        result = "passed" if test_run.get("green") else "did not pass"
        lines.extend(
            [
                f"- **Test command:** `{test_run.get('command') or 'unknown'}`",
                f"- **Result:** {result}",
                f"- **Counts:** {test_run.get('passed', 0)} passed, "
                f"{test_run.get('failed', 0)} failed, {test_run.get('errors', 0)} errors",
            ]
        )
    else:
        lines.append("- No Synchri-managed test run was recorded for this session.")
    lines.extend(["", "## Acceptance gates", ""])
    for gate in gates:
        lines.append(f"### {gate.gate_id} — {gate.description}")
        lines.append(f"- **Status:** {gate.status}")
        for label, values in (("Evidence", gate.evidence), ("Tests", gate.tests), ("Commits", gate.commits)):
            if values:
                lines.append(f"- **{label}:** {', '.join(values)}")
        lines.append(f"- **Builder assessment:** {gate.builder_assessment or 'not recorded'}")
        lines.append(f"- **Reviewer assessment:** {gate.reviewer_assessment or 'not recorded'}")
        lines.append("")
    lines.extend(
        [
            "## Final state",
            "",
            "- Every required acceptance gate had evidence and both required assessments at completion.",
            "- The room has been closed; its transcript and shared memory remain available for review.",
            "",
        ]
    )
    return "\n".join(lines)
