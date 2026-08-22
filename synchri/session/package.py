"""Bundle a finished session into one reviewable archive.

The zip contains everything a person needs to review or hand off the work:
the rendered transcript (plus the raw JSONL), the final changelog, the gates
record, the usage summary, the commit list, and the full diff. It is built
from authoritative state, never from files that may have drifted, and it
must never contain credentials — session metadata carries invite tokens and
the human's room secret, so only rendered fields go in.
"""

from __future__ import annotations

import io
import json
import re
import zipfile

from ..errors import StateError
from ..storage import dao
from . import changelog as changelog_module
from . import dropbox as dropbox_module
from . import verify as verify_module
from .gates import summarize

_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    cleaned = _SLUG.sub("-", (text or "").lower()).strip("-")
    return cleaned[:48] or "session"


def _fmt_tokens(count) -> str:
    count = int(count or 0)
    return f"{count:,}"


def build(broker, manager, session_id: str) -> tuple[str, bytes]:
    """Assemble the package. Returns ``(filename, zip_bytes)``."""
    record = manager.get(session_id)
    if not record.is_terminal:
        raise StateError(
            "the session package is available once the session is finished",
            code="session_not_finished",
        )
    gates = manager.gates(session_id)
    report = summarize(gates)
    changes = manager.changes(session_id)
    usage = dao.usage_for_session(manager.conn, session_id)
    ancillary_usage = dao.ancillary_usage_for_session(manager.conn, session_id)
    drops = dropbox_module.items(manager, session_id)
    messages = dao.list_messages(broker.conn, record.room_id) if record.room_id else []

    files: dict[str, str] = {}
    files["session.md"] = _session_summary(record, report, changes, drops)
    files["transcript.md"] = _transcript_markdown(record, messages)
    files["transcript.jsonl"] = (
        "\n".join(
            json.dumps(message.to_dict(), ensure_ascii=False, sort_keys=True)
            for message in messages
        )
        + ("\n" if messages else "")
    )
    files["final-changelog.md"] = _changelog(manager, record, gates, changes, session_id)
    files["gates.md"] = _gates_markdown(gates, report)
    files["usage-summary.md"] = _usage_markdown(usage, ancillary_usage)
    # Main and ancillary spend live under separate keys — one origin never
    # folds into the other's totals.
    files["usage.json"] = json.dumps(
        {"main": usage, "ancillary": ancillary_usage},
        ensure_ascii=False, indent=2, default=str,
    ) + "\n"
    files["dropbox.md"] = _dropbox_markdown(record, drops)
    if drops:
        # Every stored field was screened and bounded at deposit; raw
        # sensitive content never reached the rows this serializes.
        files["dropbox.json"] = json.dumps(drops, ensure_ascii=False, indent=2, default=str) + "\n"
    files["commits.md"] = _commits_markdown(changes)
    # A handoff archive has to capture all work in the authorized worktree,
    # not only commits reachable from HEAD.  Agents often leave the final
    # polish staged or unstaged when they ask a human to review it.
    files["diff.patch"] = (
        verify_module.working_tree_diff_text(record.worktree_path, record.base_branch)
        if record.worktree_path
        else ""
    ) or "(no diff)\n"

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return f"synchri-{_slug(record.name)}.zip", buffer.getvalue()


def _session_summary(record, report, changes, drops=None) -> str:
    lines = [
        f"# {record.name}",
        "",
        f"- Session: {record.session_id}",
        f"- Status: {record.status}" + (f" — {record.ended_reason}" if record.ended_reason else ""),
        f"- Repository: {record.repo_name} ({record.repo_root})",
    ]
    if record.repo_remote:
        lines.append(f"- Remote: {record.repo_remote}")
    lines.append(f"- Branch: {record.worktree_branch or '—'} (base {record.base_branch})")
    if record.worktree_path:
        lines.append(f"- Worktree: {record.worktree_path}")
    lines.append(f"- Created: {record.created_at}")
    if record.ended_at:
        lines.append(f"- Ended: {record.ended_at}")
    lines.append("")
    lines.append("## Team")
    for plan in record.participants:
        lines.append(f"- {plan.name} — {plan.runtime}, {plan.role}")
    lines.extend(
        [
            "",
            "## Outcome",
            f"- Gates: {report['satisfied']}/{report['required']} satisfied"
            + (f" ({report['waived']} waived)" if report.get("waived") else ""),
            f"- Commits: {changes.get('commits', 0)}",
            f"- Files changed: {changes.get('files_changed', 0)} "
            f"(+{changes.get('insertions', 0)} −{changes.get('deletions', 0)})",
        ]
    )
    if drops:
        counts = {"approved": 0, "declined": 0, "waived": 0}
        for entry in drops:
            if entry.get("disposition") in counts:
                counts[entry["disposition"]] += 1
        lines.append(
            f"- Side tasks: {len(drops)} captured — {counts['approved']} approved as "
            f"extensions, {counts['declined']} declined, {counts['waived']} waived "
            "(see dropbox.md)"
        )
    lines.append("")
    return "\n".join(lines)


def _transcript_markdown(record, messages) -> str:
    lines = [f"# Transcript — {record.name}", ""]
    if not messages:
        lines.append("_Nothing was said in this session._")
        return "\n".join(lines) + "\n"
    for message in messages:
        entry = message.to_dict()
        sender = entry.get("sender") or "?"
        target = entry.get("target")
        stamp = entry.get("timestamp") or entry.get("created_at") or ""
        heading = f"## {sender}" + (f" → {target}" if target else "")
        if stamp:
            heading += f"  ·  {stamp}"
        lines.extend([heading, "", (entry.get("content") or "").rstrip(), ""])
    return "\n".join(lines)


def _changelog(manager, record, gates, changes, session_id: str) -> str:
    if record.status == "complete":
        try:
            return manager.final_changelog(session_id)["markdown"]
        except StateError:
            pass
    rendered = changelog_module.render(
        record, gates, changes, manager.last_test_run(session_id),
        completed_at=record.ended_at or record.updated_at,
    )
    return (
        "> This session ended without a verified completion "
        f"({record.status}); the record below reflects the state it stopped in.\n\n" + rendered
    )


def _gates_markdown(gates, report) -> str:
    lines = ["# Acceptance gates", ""]
    if not gates:
        lines.append("_No gates were defined._")
        return "\n".join(lines) + "\n"
    lines.append(
        f"{report['satisfied']}/{report['required']} satisfied"
        + (f", {report['waived']} waived" if report.get("waived") else "")
    )
    lines.append("")
    for gate in gates:
        lines.extend(["```", gate.render(), "```", ""])
    if report["blockers"]:
        lines.append("Open blockers at the end: " + "; ".join(report["blockers"]))
        lines.append("")
    return "\n".join(lines)


def _usage_rows(lines: list[str], rows: list[dict], *, label=None) -> dict:
    totals = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0, "cost_usd": 0.0}
    for row in rows:
        header = f"## {row.get('participant')} ({row.get('runtime') or 'unknown runtime'})"
        if label:
            header += f" — {label}"
        if row.get("drop_id"):
            header += f" · {row['drop_id']}"
        lines.append(header)
        if row.get("model"):
            lines.append(f"- Model: {row['model']}")
        lines.append(f"- Measured invocations: {row.get('invocations', 0)}")
        lines.append(
            f"- Tokens: {_fmt_tokens(row.get('input_tokens'))} in "
            f"({_fmt_tokens(row.get('cached_input_tokens'))} cached) · "
            f"{_fmt_tokens(row.get('output_tokens'))} out"
        )
        if row.get("cost_usd"):
            lines.append(f"- Cost: ${float(row['cost_usd']):.4f}")
        if row.get("duration_seconds"):
            lines.append(f"- Time in provider calls: {float(row['duration_seconds']):.0f}s")
        lines.append("")
        for key in ("input_tokens", "output_tokens", "cached_input_tokens"):
            totals[key] += int(row.get(key) or 0)
        totals["cost_usd"] += float(row.get("cost_usd") or 0.0)
    return totals


def _usage_markdown(usage: list[dict], ancillary: list[dict] | None = None) -> str:
    lines = ["# Usage summary", ""]
    ancillary = ancillary or []
    if not usage and not ancillary:
        lines.append(
            "_No usage was recorded for this session (agents ran before v0.4.0, "
            "or via runtimes that do not report token telemetry)._"
        )
        return "\n".join(lines) + "\n"
    totals = _usage_rows(lines, usage)
    lines.append("## Total (main room)")
    lines.append(
        f"- Tokens: {_fmt_tokens(totals['input_tokens'])} in "
        f"({_fmt_tokens(totals['cached_input_tokens'])} cached) · "
        f"{_fmt_tokens(totals['output_tokens'])} out"
    )
    if totals["cost_usd"]:
        lines.append(f"- Cost: ${totals['cost_usd']:.4f}")
    lines.append("")
    if ancillary:
        # Reported separately by doctrine: side-task investigation spend never
        # merges into an ordinary participant's totals.
        lines.append("# Ancillary usage (dropbox side tasks)")
        lines.append("")
        side_totals = _usage_rows(lines, ancillary, label="ancillary")
        lines.append("## Total (ancillary)")
        lines.append(
            f"- Tokens: {_fmt_tokens(side_totals['input_tokens'])} in "
            f"({_fmt_tokens(side_totals['cached_input_tokens'])} cached) · "
            f"{_fmt_tokens(side_totals['output_tokens'])} out"
        )
        if side_totals["cost_usd"]:
            lines.append(f"- Cost: ${side_totals['cost_usd']:.4f}")
        lines.append("")
    return "\n".join(lines)


def _dropbox_markdown(record, drops: list[dict]) -> str:
    lines = ["# Side-task dropbox", ""]
    if not drops:
        lines.append("_No side tasks were captured during this session._")
        return "\n".join(lines) + "\n"
    lines.append(
        "Items captured mid-session and investigated off the critical path. The "
        "canonical specification was never edited; these landed in the append-only "
        "appendix and each received an explicit disposition."
    )
    lines.append("")
    for entry in drops:
        state = entry.get("disposition") or entry.get("status")
        lines.append(f"## {entry['drop_id']} — {entry['title']}  [{state}]")
        lines.append(f"- Captured: {entry.get('created_at')}")
        lines.append(f"- Status: {entry.get('status')}"
                     + (f", disposition {entry['disposition']}" if entry.get("disposition") else ""))
        if entry.get("extension_id"):
            lines.append(f"- Extension: {entry['extension_id']} (gate {entry['extension_id']})")
        if entry.get("base_sha"):
            lines.append(f"- Scout base: {entry['base_sha']}")
        if entry.get("proposal_digest"):
            lines.append(f"- Proposal digest: {entry['proposal_digest']}")
        proposal = entry.get("proposal") or {}
        if proposal.get("recommendation"):
            lines.append(f"- Recommendation: {proposal['recommendation']}")
        if proposal.get("diffstat"):
            lines.append(f"- Diffstat: {' '.join(proposal['diffstat'].split())}")
        if proposal.get("patch_note"):
            lines.append(f"- Patch: {proposal['patch_note']}")
        review = entry.get("review") or {}
        if review.get("verdict"):
            lines.append(f"- Ancillary review: {review['verdict']}")
        elif entry.get("skip_review"):
            lines.append("- Ancillary review: skipped by the human's explicit per-item choice")
        if entry.get("failure_report"):
            lines.append(f"- Failure report: {' '.join(entry['failure_report'].split())}")
        evaluations = entry.get("evaluations") or {}
        for slot in ("builder", "reviewer", "human"):
            value = evaluations.get(slot)
            if not value:
                continue
            generation = value.get("recovery_generation")
            suffix = f", generation {generation}" if generation else ""
            lines.append(
                f"- Evaluation ({slot}: {value.get('participant')}{suffix}): "
                f"{value.get('verdict')} — {value.get('rationale')}"
            )
        lines.append("")
    return "\n".join(lines)


def _commits_markdown(changes: dict) -> str:
    lines = ["# Commits", ""]
    recent = changes.get("recent") or []
    if not recent:
        lines.append("_No commits were made on the session branch._")
        return "\n".join(lines) + "\n"
    lines.append(f"Branch {changes.get('branch') or '—'} · head {changes.get('head') or '—'}")
    lines.append("")
    for commit in recent:
        sha = commit.get("full_sha") or commit.get("sha") or ""
        lines.append(
            f"- `{sha}` {commit.get('subject') or ''} — {commit.get('author') or ''}"
            + (f", {commit.get('date')}" if commit.get("date") else "")
        )
    lines.append("")
    return "\n".join(lines)
