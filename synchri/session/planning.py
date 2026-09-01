"""Planning Mode: an adversarially reviewed plan as executable state.

Planning should be executable state, not prose the user has to carry into the
next workflow. The user articulates an idea — in as much or as little detail
as they want, stored verbatim — and a planner turns it, plus real repository
inspection, into ``PLAN-DRAFT`` revision 1 **before** the adversarial plan
reviewer sees anything. The loop is then PLAN-DRAFT -> ADVERSARIAL REVIEW ->
REVISION -> RE-REVIEW -> PLAN-READY, where readiness requires review closure:
open blocking objections or unresolved architectural forks prevent it, and
nonblocking assumptions may remain only clearly classified.

Isolation is capability-based, not contract-only, and it is layered —
and its strength is stated honestly per adapter. Planning agents launch only
under the maintained ``planning_command``: for Codex that is ``--sandbox
read-only``, an **OS-enforced** sandbox (Seatbelt / Landlock, read-only
filesystem, no network); for Claude Code it is **provider enforcement**, not
an OS sandbox — the CLI's permission engine in plan mode, hardened with
``--safe-mode`` (user-configurable hooks, plugins, and other configuration
surfaces disabled; managed-policy hooks remain active by design),
``--strict-mcp-config`` with no MCP config (no servers at all), and an
explicit read-only ``--tools`` allowlist. Trusting that boundary means
trusting the Claude Code binary and the managed policy it enforces, not an
operating-system guarantee. Runtimes without a
verified enforcement mechanism are unavailable, and custom commands are
refused outright. The agents are given no sanctioned write path, so the plan
document travels through the reply protocol
(``SYNCHRI-PLAN-BEGIN``/``END``), never through a file write. Beneath that,
inspection happens in a **disposable planning workspace** — a local clone
anchored to one ``inspection_sha`` with its remotes removed, so even a
misbehaving process in it has no path back to the user's repository — whose
Git state is verified after every turn; the workspace is never reused for
coordination.

The loop is budgeted (turns, revisions, wall clock — whichever first, with
one user-authorized resume); exhaustion produces ``needs_human_resolution``,
preserving the latest draft and open objections rather than looping forever.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import hashlib

from ..broker import Credential
from ..errors import ConflictError, NotFoundError, StateError, ValidationError
from ..ids import utc_now
from ..models.envelope import MessageDraft
from ..storage import db
from . import worktree as worktree_module
from .gates import Gate
from .modes import ParticipantPlan, Role, collaboration_pair
from .spec import ProductSpec

if TYPE_CHECKING:  # pragma: no cover - imports for type checkers only
    from .manager import SessionManager, SessionRecord

MAX_IDEA_CHARS = 100_000
MAX_PLAN_CHARS = 500_000

#: The loop's mechanical terminal route — whichever arrives first — with one
#: user-authorized resume. Exhaustion goes to the human, never a loop.
TURN_BUDGET = 24
REVISION_BUDGET = 6
MINUTES_BUDGET = 60

DRAFTING = "drafting"
AWAITING_REVIEW = "awaiting_review"
UNDER_REVISION = "under_revision"
READY = "ready"
NEEDS_HUMAN = "needs_human_resolution"
APPROVED = "approved"

#: Statuses in which the loop is live and directives are accepted.
ACTIVE_STATUSES = frozenset({DRAFTING, AWAITING_REVIEW, UNDER_REVISION, READY})

#: Objection kinds that prevent PLAN-READY (and approval) while open.
BLOCKING_KINDS = frozenset({"blocking", "fork"})

_CRITERIA_HEADING = re.compile(r"^##\s*acceptance criteria\s*$", re.IGNORECASE)
_CRITERION = re.compile(r"^[-*]\s*(?P<gate_id>[A-Z][A-Z0-9]*-\d+)\s*:\s*(?P<text>.+)$")


# ----------------------------------------------------------------------
# the disposable planning workspace
# ----------------------------------------------------------------------


def create_workspace(workspace_home, repo_root: str, branch: str, session_id: str) -> tuple[str, str]:
    """Clone the repository's committed state into a disposable analysis area.

    The clone is anchored to the branch tip (``inspection_sha``) and its
    remotes are removed, so nothing an agent runs inside it — commit, branch,
    even ``git push`` — has a path back to the user's repository. That is the
    technical restriction behind the ``read_only_planning_workspace``
    capability; the contract's denials only restate it.
    """
    path = Path(workspace_home) / "planning" / session_id
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    worktree_module.git(
        path.parent, "clone", "--quiet", "--single-branch", "--branch", branch,
        str(repo_root), str(path),
    )
    inspection_sha = worktree_module.git(path, "rev-parse", "HEAD").strip()
    worktree_module.git(path, "remote", "remove", "origin", check=False)
    return str(path), inspection_sha


def remove_workspace(path: str | None) -> None:
    """Disposable means disposable: no push check, no keep rule."""
    if path:
        shutil.rmtree(path, ignore_errors=True)


def verify_workspace(manager: "SessionManager", record: "SessionRecord") -> bool:
    """Defense-in-depth: the workspace's Git state, checked after every turn.

    Notes and drafts (untracked or modified files) are expected — the
    workspace is the analysis area. A moved HEAD is not: it is restored to
    the inspection baseline and escalated, because a plan reviewed against
    drifted state is not the plan the human approves.
    """
    plan = get_plan(manager, record.session_id)
    if plan is None:
        return True
    workspace = Path(plan["workspace_path"])
    if not workspace.exists():
        _escalate_once(
            manager, record.session_id, "planning_workspace_missing",
            f"the planning workspace disappeared: {workspace}",
        )
        return False
    head = worktree_module.git(workspace, "rev-parse", "HEAD", check=False).strip()
    if head and head != plan["inspection_sha"]:
        worktree_module.git(
            workspace, "reset", "--hard", plan["inspection_sha"], check=False
        )
        _escalate_once(
            manager, record.session_id, "planning_workspace_mutated",
            f"the planning workspace's HEAD moved to {head[:12]} and was restored "
            f"to the inspection baseline {plan['inspection_sha'][:12]}",
        )
        manager._log(
            record, "session.planning_workspace_restored",
            {"from": head, "to": plan["inspection_sha"]},
        )
        return False
    return True


def _escalate_once(manager: "SessionManager", session_id: str, rule: str, detail: str) -> None:
    if any(e["rule"] == rule for e in manager.open_escalations(session_id)):
        return
    manager.escalate(session_id, rule, detail)


# ----------------------------------------------------------------------
# plan rows
# ----------------------------------------------------------------------


def initialize(
    manager: "SessionManager",
    record: "SessionRecord",
    *,
    idea: str,
    workspace_path: str,
    inspection_sha: str,
) -> dict:
    """Create the durable plan for a new planning session."""
    text = (idea or "").strip()
    if not text:
        raise ValidationError("articulate the idea before starting Planning Mode")
    if len(text) > MAX_IDEA_CHARS:
        raise ValidationError(f"the idea articulation is capped at {MAX_IDEA_CHARS:,} characters")
    now = utc_now()
    with db.transaction(manager.conn):
        highest = 0
        for row in manager.conn.execute("SELECT plan_id FROM session_plans"):
            match = re.fullmatch(r"PLAN-(\d+)", row["plan_id"])
            if match:
                highest = max(highest, int(match.group(1)))
        plan_id = f"PLAN-{highest + 1:03d}"
        manager.conn.execute(
            "INSERT INTO session_plans (session_id, plan_id, status, idea, revision, "
            "inspection_sha, source_branch, workspace_path, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (record.session_id, plan_id, DRAFTING, text, 0,
             inspection_sha, record.base_branch, workspace_path, now, now),
        )
    return get_plan(manager, record.session_id)


def get_plan(manager: "SessionManager", session_id: str) -> dict | None:
    row = manager.conn.execute(
        "SELECT * FROM session_plans WHERE session_id = ?", (session_id,)
    ).fetchone()
    return dict(row) if row else None


def require_plan(manager: "SessionManager", session_id: str) -> dict:
    plan = get_plan(manager, session_id)
    if plan is None:
        raise NotFoundError("this session has no plan; it is not a planning session")
    return plan


def objections(manager: "SessionManager", session_id: str) -> list[dict]:
    rows = manager.conn.execute(
        "SELECT * FROM session_plan_objections WHERE session_id = ? ORDER BY rowid",
        (session_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def open_blockers(manager: "SessionManager", session_id: str) -> list[dict]:
    return [
        entry for entry in objections(manager, session_id)
        if entry["status"] == "open" and entry["classification"] in BLOCKING_KINDS
    ]


def revisions(manager: "SessionManager", session_id: str) -> list[dict]:
    rows = manager.conn.execute(
        "SELECT revision, summary, author, created_at, LENGTH(body) AS chars "
        "FROM session_plan_revisions WHERE session_id = ? ORDER BY revision",
        (session_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def revision_body(manager: "SessionManager", session_id: str, revision: int) -> str:
    row = manager.conn.execute(
        "SELECT body FROM session_plan_revisions WHERE session_id = ? AND revision = ?",
        (session_id, revision),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"no plan revision {revision} for this session")
    return row["body"]


def budgets(plan: dict) -> dict:
    allowance = int(plan["resumes_used"] or 0) + 1
    return {
        "turns_used": plan["turns_used"] or 0,
        "turn_budget": TURN_BUDGET * allowance,
        "revisions_used": plan["revision"] or 0,
        "revision_budget": REVISION_BUDGET * allowance,
        "minutes_budget": MINUTES_BUDGET * allowance,
        "resumes_used": plan["resumes_used"] or 0,
        "resume_available": (plan["resumes_used"] or 0) < 1,
    }


def payload(manager: "SessionManager", session_id: str) -> dict:
    """The full PLAN-NNN artifact view: idea, loop state, review record."""
    plan = require_plan(manager, session_id)
    all_objections = objections(manager, session_id)
    latest = None
    if plan["revision"]:
        latest = revision_body(manager, session_id, plan["revision"])
    blockers = [
        entry for entry in all_objections
        if entry["status"] == "open" and entry["classification"] in BLOCKING_KINDS
    ]
    return {
        "plan_id": plan["plan_id"],
        "status": plan["status"],
        "idea": plan["idea"],
        "revision": plan["revision"],
        "inspection_sha": plan["inspection_sha"],
        "source_branch": plan["source_branch"],
        "workspace_path": plan["workspace_path"],
        "ready": plan["status"] == READY,
        "ready_at": plan["ready_at"],
        "budgets": budgets(plan),
        "revisions": revisions(manager, session_id),
        "latest_body": latest,
        "objections": all_objections,
        "open_objections": [o for o in all_objections if o["status"] == "open"],
        "resolved_objections": [o for o in all_objections if o["status"] != "open"],
        "open_blockers": blockers,
        "acceptance_criteria": (
            [{"gate_id": g, "description": d} for g, d in parse_acceptance_criteria(latest)]
            if latest else []
        ),
        "promotion": promotion(manager, session_id),
        "created_at": plan["created_at"],
        "updated_at": plan["updated_at"],
    }


def parse_acceptance_criteria(body: str | None) -> list[tuple[str, str]]:
    """Explicitly identified criteria from the plan's dedicated section.

    Deterministic on purpose: each bullet must carry its own gate id. This is
    what promotion materializes as gates — never the generic prose extraction,
    which can collapse a whole plan into a lone SPEC-01.
    """
    if not body:
        return []
    criteria: list[tuple[str, str]] = []
    in_section = False
    for line in body.splitlines():
        stripped = line.strip()
        if _CRITERIA_HEADING.match(stripped):
            in_section = True
            continue
        if in_section and stripped.startswith("#"):
            break
        if in_section:
            match = _CRITERION.match(stripped)
            if match:
                criteria.append((match.group("gate_id").upper(), match.group("text").strip()))
    return criteria


def validate_criteria(body: str | None) -> None:
    """The promotion-grade acceptance-criteria rule, in exactly one place.

    Enforced when the reviewer closes with approve and re-checked at human
    approval, so PLAN-READY implies approvable: review closure and promotion
    can never disagree about what a valid criteria section is.
    """
    criteria = parse_acceptance_criteria(body)
    if not criteria:
        raise StateError(
            "the plan names no explicit acceptance criteria — add an "
            "'## Acceptance criteria' section with gate ids and re-review; "
            "promotion never falls back to generic prose extraction",
            code="plan_criteria_missing",
        )
    seen: set[str] = set()
    for gate_id, _text in criteria:
        if gate_id in seen:
            raise StateError(
                f"the plan's acceptance criteria collide on gate id {gate_id}",
                code="plan_gate_collision",
            )
        seen.add(gate_id)


# ----------------------------------------------------------------------
# the review loop
# ----------------------------------------------------------------------


def _roles(record: "SessionRecord") -> tuple[str | None, str | None]:
    planner = next((p.name for p in record.participants if p.role == Role.PLANNER.value), None)
    reviewer = next(
        (p.name for p in record.participants if p.role == Role.PLAN_REVIEWER.value), None
    )
    if planner is None or reviewer is None:
        lead, second = collaboration_pair(record.participants)
        planner = planner or (lead.name if lead else None)
        reviewer = reviewer or (second.name if second else None)
    return planner, reviewer


def handle_turn(
    manager: "SessionManager",
    record: "SessionRecord",
    actor: str,
    directives,
    warnings: list[str],
) -> list[dict]:
    """Apply one turn's planning directives; the manager stays the authority.

    Role-scoped: only the planner submits and resolves, only the reviewer
    raises objections and closes reviews, either records a fork. Everything
    lands durably or lands as a warning — never silently.
    """
    plan = get_plan(manager, record.session_id)
    if plan is None:
        return []
    actions: list[dict] = []
    planner, reviewer = _roles(record)
    if plan["status"] not in ACTIVE_STATUSES:
        if _has_planning_directives(directives):
            warnings.append(
                f"planning directives ignored: the plan is {plan['status']}"
            )
        return []

    with db.transaction(manager.conn):
        now = utc_now()
        # Planner dispositions first: a revision that resolves objections
        # records those resolutions against the revision it submits below.
        for objection_id, disposition in directives.objection_resolutions:
            if actor != planner:
                warnings.append(f"{actor}: only the planner records objection dispositions")
                continue
            row = manager.conn.execute(
                "SELECT status FROM session_plan_objections "
                "WHERE session_id = ? AND objection_id = ?",
                (record.session_id, objection_id),
            ).fetchone()
            if row is None:
                warnings.append(f"{actor}: no objection {objection_id} to resolve")
                continue
            if row["status"] != "open":
                warnings.append(f"{actor}: {objection_id} is already {row['status']}")
                continue
            manager.conn.execute(
                "UPDATE session_plan_objections SET status = 'resolved', disposition = ?, "
                "resolved_by = ?, resolved_revision = ?, updated_at = ? "
                "WHERE session_id = ? AND objection_id = ?",
                ((disposition or "").strip()[:2000], actor, plan["revision"] + 1
                 if directives.plan_submitted is not None else plan["revision"],
                 now, record.session_id, objection_id),
            )
            actions.append({"kind": "resolved", "objection_id": objection_id})

        for text in directives.forks:
            objection_id = _raise(
                manager, record.session_id, "fork", text, actor, plan["revision"], now
            )
            actions.append({"kind": "fork", "objection_id": objection_id})

        for classification, text in directives.objections:
            if actor != reviewer:
                warnings.append(f"{actor}: only the plan reviewer raises objections")
                continue
            objection_id = _raise(
                manager, record.session_id, classification, text, actor, plan["revision"], now
            )
            actions.append({"kind": "objection", "objection_id": objection_id,
                            "classification": classification})

        if directives.plan_submitted is not None:
            if actor != planner:
                warnings.append(f"{actor}: only the planner submits plan revisions")
            else:
                submitted = _submit(
                    manager, record, plan, actor,
                    directives.plan_submitted, directives.plan_body,
                    reviewer, warnings, now,
                )
                if submitted:
                    actions.append(submitted)
                plan = get_plan(manager, record.session_id)

        if directives.plan_review is not None:
            verdict, note = directives.plan_review
            if actor != reviewer:
                warnings.append(f"{actor}: only the plan reviewer closes a review")
            elif plan["revision"] < 1:
                warnings.append(f"{actor}: there is no submitted revision to review yet")
            else:
                closed = _close_review(
                    manager, record, plan, actor, verdict, note, planner, warnings, now
                )
                if closed:
                    actions.append(closed)
    return actions


def _has_planning_directives(directives) -> bool:
    return bool(
        directives.plan_submitted is not None
        or directives.plan_review is not None
        or directives.objections
        or directives.objection_resolutions
        or directives.forks
    )


def _raise(manager, session_id: str, classification: str, text: str,
           actor: str, revision: int, now: str) -> str:
    highest = 0
    for row in manager.conn.execute(
        "SELECT objection_id FROM session_plan_objections WHERE session_id = ?",
        (session_id,),
    ):
        match = re.fullmatch(r"OBJ-(\d+)", row["objection_id"])
        if match:
            highest = max(highest, int(match.group(1)))
    objection_id = f"OBJ-{highest + 1:03d}"
    manager.conn.execute(
        "INSERT INTO session_plan_objections (session_id, objection_id, classification, "
        "text, raised_by, raised_revision, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (session_id, objection_id, classification, text.strip()[:8000],
         actor, revision, now, now),
    )
    return objection_id


def _submit(manager, record, plan, actor, summary, body, reviewer, warnings, now) -> dict | None:
    # The plan travels through the reply protocol: planning agents run under
    # their CLI's enforced read-only mode and hold no write authority, so
    # there is no file for a submission to read.
    if body is None:
        warnings.append(
            f"{actor}: submit ignored — include the full plan document between "
            "SYNCHRI-PLAN-BEGIN and SYNCHRI-PLAN-END in the same reply"
        )
        return None
    if not body.strip():
        warnings.append(f"{actor}: submit ignored — the plan block is empty")
        return None
    if len(body) > MAX_PLAN_CHARS:
        warnings.append(
            f"{actor}: submit ignored — the plan exceeds {MAX_PLAN_CHARS:,} characters"
        )
        return None
    revision = (plan["revision"] or 0) + 1
    manager.conn.execute(
        "INSERT INTO session_plan_revisions (session_id, revision, summary, body, "
        "author, created_at) VALUES (?,?,?,?,?,?)",
        (record.session_id, revision, (summary or "").strip()[:400], body, actor, now),
    )
    manager.conn.execute(
        "UPDATE session_plans SET revision = ?, status = ?, ready_at = NULL, "
        "updated_at = ? WHERE session_id = ?",
        (revision, AWAITING_REVIEW, now, record.session_id),
    )
    manager._log(
        record, "session.plan_submitted",
        {"plan_id": plan["plan_id"], "revision": revision, "by": actor},
    )
    criteria = parse_acceptance_criteria(body)
    if not criteria:
        warnings.append(
            "the plan has no parseable '## Acceptance criteria' section with explicit "
            "gate ids (e.g. '- AUTH-01: login works'); it cannot be promoted until it does"
        )
    else:
        seen = set()
        for gate_id, _text in criteria:
            if gate_id in seen:
                warnings.append(
                    f"the plan's acceptance criteria reuse the gate id {gate_id}; "
                    "ids must be unique or promotion will fail"
                )
            seen.add(gate_id)

    allowance = int(plan["resumes_used"] or 0) + 1
    if revision >= REVISION_BUDGET * allowance:
        _exhaust(
            manager, record,
            f"the revision budget ({REVISION_BUDGET * allowance}) is spent; "
            f"revision {revision} is preserved with its open objections",
        )
        return {"kind": "submitted", "revision": revision, "exhausted": True}

    open_items = [
        f"{o['objection_id']} ({o['classification']})"
        for o in objections(manager, record.session_id) if o["status"] == "open"
    ]
    _send_task(
        manager, record, target=reviewer, other=actor,
        source="plan_review",
        content=(
            f"Plan revision {revision} was submitted"
            + (f": {summary.strip()}" if summary and summary.strip() else ".")
            + " Review it adversarially against the repository state in the "
            "planning workspace (the revision text is in the planning state of "
            "your prompt). Raise findings with "
            "SYNCHRI-OBJECTION lines"
            + (f" (still open: {', '.join(open_items)})" if open_items else "")
            + ", then close with SYNCHRI-PLAN-REVIEW: approve or revise."
        ),
    )
    return {"kind": "submitted", "revision": revision}


def _close_review(manager, record, plan, actor, verdict, note, planner, warnings, now) -> dict | None:
    if verdict == "approve":
        blockers = open_blockers(manager, record.session_id)
        if blockers:
            names = ", ".join(f"{b['objection_id']} ({b['classification']})" for b in blockers)
            warnings.append(
                f"{actor}: approval refused — open blocking items prevent PLAN-READY: {names}"
            )
            return None
        # READY must imply approvable: the promotion-grade criteria rule runs
        # here, at review closure, so an invalid section is caught while the
        # loop can still fix it instead of failing at human approval.
        try:
            validate_criteria(revision_body(manager, record.session_id, plan["revision"]))
        except StateError as exc:
            warnings.append(f"{actor}: approval cannot reach PLAN-READY — {exc.message}")
            manager.conn.execute(
                "UPDATE session_plans SET status = ?, ready_at = NULL, updated_at = ? "
                "WHERE session_id = ?",
                (UNDER_REVISION, now, record.session_id),
            )
            _send_task(
                manager, record, target=planner, other=actor,
                source="plan_criteria_correction",
                content=(
                    f"The reviewer approved revision {plan['revision']}, but it cannot "
                    f"reach PLAN-READY: {exc.message}. Correct the '## Acceptance "
                    "criteria' section and submit the next revision between "
                    "SYNCHRI-PLAN-BEGIN and SYNCHRI-PLAN-END."
                ),
            )
            manager._log(
                record, "session.plan_criteria_refused",
                {"plan_id": plan["plan_id"], "revision": plan["revision"],
                 "detail": exc.message},
            )
            return {"kind": "review", "verdict": "revise", "revision": plan["revision"],
                    "criteria_correction": True}
        manager.conn.execute(
            "UPDATE session_plans SET status = ?, ready_at = ?, updated_at = ? "
            "WHERE session_id = ?",
            (READY, now, now, record.session_id),
        )
        manager._log(
            record, "session.plan_ready",
            {"plan_id": plan["plan_id"], "revision": plan["revision"], "by": actor},
        )
        return {"kind": "review", "verdict": "approve", "revision": plan["revision"]}
    # revise
    manager.conn.execute(
        "UPDATE session_plans SET status = ?, ready_at = NULL, updated_at = ? "
        "WHERE session_id = ?",
        (UNDER_REVISION, now, record.session_id),
    )
    open_items = [
        f"{o['objection_id']} ({o['classification']}): {o['text'][:160]}"
        for o in objections(manager, record.session_id) if o["status"] == "open"
    ]
    _send_task(
        manager, record, target=planner, other=actor,
        source="plan_revision",
        content=(
            f"The reviewer sent revision {plan['revision']} back"
            + (f": {note.strip()}" if note and note.strip() else ".")
            + " Address each open item, record its disposition with "
            "SYNCHRI-OBJECTION-RESOLVED, and submit the next revision between "
            "SYNCHRI-PLAN-BEGIN and SYNCHRI-PLAN-END."
            + ("\nOpen items:\n" + "\n".join(f"  {item}" for item in open_items)
               if open_items else "")
        ),
    )
    return {"kind": "review", "verdict": "revise", "revision": plan["revision"]}


def _send_task(manager, record, *, target, other, source, content) -> None:
    if not target or not record.room_id:
        return
    metadata: dict = {"source": source}
    if other:
        metadata["human_direction"] = {"lead": target, "reviewer": other}
    human = (record.metadata or {}).get("human") or {}
    manager.broker.send(
        record.room_id,
        credential=Credential(participant=human.get("name"), secret=human.get("secret")),
        draft=MessageDraft(
            message_type="task", target=target, metadata=metadata, content=content
        ),
    )


# ----------------------------------------------------------------------
# budgets
# ----------------------------------------------------------------------


def count_turn(manager: "SessionManager", record: "SessionRecord") -> None:
    """One completed agent invocation against the turn and wall-clock budgets."""
    plan = get_plan(manager, record.session_id)
    if plan is None or plan["status"] not in ACTIVE_STATUSES:
        return
    with db.transaction(manager.conn):
        manager.conn.execute(
            "UPDATE session_plans SET turns_used = turns_used + 1, updated_at = ? "
            "WHERE session_id = ?",
            (utc_now(), record.session_id),
        )
    plan = get_plan(manager, record.session_id)
    allowance = int(plan["resumes_used"] or 0) + 1
    if plan["turns_used"] >= TURN_BUDGET * allowance:
        _exhaust(
            manager, record,
            f"the agent-turn budget ({TURN_BUDGET * allowance}) is spent",
        )
        return
    elapsed_minutes = _elapsed_minutes(record, plan)
    if elapsed_minutes is not None and elapsed_minutes >= MINUTES_BUDGET * allowance:
        _exhaust(
            manager, record,
            f"the wall-clock budget ({MINUTES_BUDGET * allowance} minutes) is spent",
        )


def _elapsed_minutes(record: "SessionRecord", plan: dict) -> float | None:
    from datetime import datetime

    start = record.activated_at or plan["created_at"]
    try:
        begun = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        now = datetime.fromisoformat(utc_now().replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover - defensive
        return None
    return (now - begun).total_seconds() / 60.0


def _exhaust(manager, record, detail: str) -> None:
    plan = get_plan(manager, record.session_id)
    if plan is None or plan["status"] == NEEDS_HUMAN:
        return
    with db.transaction(manager.conn):
        manager.conn.execute(
            "UPDATE session_plans SET status = ?, updated_at = ? WHERE session_id = ?",
            (NEEDS_HUMAN, utc_now(), record.session_id),
        )
    _escalate_once(
        manager, record.session_id, "planning_budget_exhausted",
        detail + ". The latest draft and its open objections are preserved. "
        "Resume the budget once, give direction, or approve what stands.",
    )
    manager._log(record, "session.plan_budget_exhausted", {"detail": detail})


def resume_budget(manager: "SessionManager", session_id: str) -> dict:
    """The one user-authorized resume: a fresh set of all three budgets."""
    record = manager.get(session_id)
    plan = require_plan(manager, session_id)
    if plan["status"] != NEEDS_HUMAN:
        raise StateError("the planning budget is not exhausted", code="plan_not_exhausted")
    if (plan["resumes_used"] or 0) >= 1:
        raise StateError(
            "the one authorized budget resume was already used; give direction "
            "or approve what stands",
            code="plan_resume_spent",
        )
    planner, reviewer = _roles(record)
    with db.transaction(manager.conn):
        manager.conn.execute(
            "UPDATE session_plans SET resumes_used = resumes_used + 1, status = ?, "
            "updated_at = ? WHERE session_id = ?",
            (UNDER_REVISION, utc_now(), session_id),
        )
        for escalation in manager.open_escalations(session_id):
            if escalation["rule"] == "planning_budget_exhausted":
                manager.resolve_escalation(
                    escalation["escalation_id"], "budget resumed by the user"
                )
        _send_task(
            manager, record, target=planner, other=reviewer,
            source="plan_budget_resumed",
            content=(
                "The human resumed the planning budget — once. Address the open "
                "objections and submit the next revision; stop at the minimum "
                "sufficiently defensible plan."
            ),
        )
    manager._log(record, "session.plan_budget_resumed", {})
    return payload(manager, session_id)


# ----------------------------------------------------------------------
# human controls and invalidation
# ----------------------------------------------------------------------


def reopen(manager: "SessionManager", session_id: str, note: str) -> dict:
    """A human edit invalidates PLAN-READY: new revision required, re-review."""
    record = manager.get(session_id)
    plan = require_plan(manager, session_id)
    if plan["status"] not in ACTIVE_STATUSES and plan["status"] != NEEDS_HUMAN:
        raise StateError(f"the plan is {plan['status']}", code="plan_not_active")
    direction = (note or "").strip()
    if not direction:
        raise ValidationError("say what should change")
    planner, reviewer = _roles(record)
    with db.transaction(manager.conn):
        manager.conn.execute(
            "UPDATE session_plans SET status = ?, ready_at = NULL, updated_at = ? "
            "WHERE session_id = ?",
            (UNDER_REVISION, utc_now(), session_id),
        )
        _send_task(
            manager, record, target=planner, other=reviewer,
            source="plan_reopened",
            content=(
                "The human requested plan changes: " + direction
                + " Fold this into the next plan revision and submit it; it will "
                "be re-reviewed before PLAN-READY."
            ),
        )
    manager._log(record, "session.plan_reopened", {"note": direction[:400]})
    return payload(manager, session_id)


def waive_objection(
    manager: "SessionManager", session_id: str, objection_id: str, note: str
) -> dict:
    """The human explicitly waives one blocking decision.

    A waiver never slips through silently: it is recorded on the objection,
    and — while the loop is live — the plan returns to review so the reviewer
    closes with the waiver on the record. Once the budget is exhausted
    (``needs_human_resolution``) the waiver stands as recorded instead: the
    loop is over, the human owns the decision, and "approve what stands"
    checks the remaining open blockers directly.
    """
    record = manager.get(session_id)
    plan = require_plan(manager, session_id)
    if plan["status"] == APPROVED:
        raise StateError(
            "the plan is approved and immutable — its review record can no "
            "longer change",
            code="plan_approved_immutable",
        )
    row = manager.conn.execute(
        "SELECT * FROM session_plan_objections WHERE session_id = ? AND objection_id = ?",
        (session_id, objection_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"no objection {objection_id} in this plan")
    if row["status"] != "open":
        raise ConflictError(f"{objection_id} is already {row['status']}")
    reason = (note or "").strip()
    if not reason:
        raise ValidationError("a waiver needs the human's reason, on the record")
    planner, reviewer = _roles(record)
    with db.transaction(manager.conn):
        manager.conn.execute(
            "UPDATE session_plan_objections SET status = 'waived', disposition = ?, "
            "resolved_by = 'human', resolved_revision = ?, updated_at = ? "
            "WHERE session_id = ? AND objection_id = ?",
            (reason[:2000], plan["revision"], utc_now(), session_id, objection_id),
        )
        if plan["status"] in {READY, AWAITING_REVIEW, UNDER_REVISION, DRAFTING}:
            manager.conn.execute(
                "UPDATE session_plans SET status = ?, ready_at = NULL, updated_at = ? "
                "WHERE session_id = ?",
                (AWAITING_REVIEW if plan["revision"] else DRAFTING, utc_now(), session_id),
            )
            if plan["revision"]:
                _send_task(
                    manager, record, target=reviewer, other=planner,
                    source="plan_waiver_review",
                    content=(
                        f"The human waived {objection_id}: {reason} Re-review revision "
                        f"{plan['revision']} with that waiver on the record, then close "
                        "with SYNCHRI-PLAN-REVIEW: approve or revise."
                    ),
                )
        # In needs_human_resolution the waiver stands as recorded: the budget
        # is spent, so there is no reviewer turn to send it back through.
    manager._log(
        record, "session.plan_objection_waived",
        {"objection_id": objection_id, "note": reason[:400],
         "stands": plan["status"] == NEEDS_HUMAN},
    )
    return payload(manager, session_id)


def note_capture(manager: "SessionManager", record: "SessionRecord", drop_id: str) -> None:
    """A dropbox item promoted into planning is a new reviewed consideration.

    If the plan was READY, it no longer is: the consideration creates a new
    revision requirement and re-review. Mid-loop it simply renders into the
    planning context.
    """
    plan = get_plan(manager, record.session_id)
    if plan is None or plan["status"] != READY:
        return
    planner, reviewer = _roles(record)
    with db.transaction(manager.conn):
        manager.conn.execute(
            "UPDATE session_plans SET status = ?, ready_at = NULL, updated_at = ? "
            "WHERE session_id = ?",
            (UNDER_REVISION, utc_now(), record.session_id),
        )
        _send_task(
            manager, record, target=planner, other=reviewer,
            source="plan_consideration",
            content=(
                f"A new consideration ({drop_id}) was captured while the plan was "
                "ready. Fold it into the next plan revision — or record explicitly "
                "why it does not change the plan — and submit it for re-review."
            ),
        )
    manager._log(
        record, "session.plan_invalidated",
        {"reason": "new_consideration", "drop_id": drop_id},
    )


# ----------------------------------------------------------------------
# prompt rendering
# ----------------------------------------------------------------------


def render_status(manager: "SessionManager", record: "SessionRecord") -> str | None:
    """The planning panel for a managed agent's prompt."""
    plan = get_plan(manager, record.session_id)
    if plan is None:
        return None
    limits = budgets(plan)
    lines = [
        "--- planning state ---",
        f"{plan['plan_id']} · status: {plan['status']} · revision: {plan['revision']} · "
        f"inspection baseline: {plan['inspection_sha'][:12]}",
        f"budgets: {limits['turns_used']}/{limits['turn_budget']} turns · "
        f"{limits['revisions_used']}/{limits['revision_budget']} revisions · "
        f"{limits['minutes_budget']} minutes wall clock",
        "submit revisions between SYNCHRI-PLAN-BEGIN and SYNCHRI-PLAN-END lines "
        "in your reply — planning offers no sanctioned write path: your CLI "
        "runs read-only by its own enforcement, inside a disposable "
        "inspection clone",
    ]
    idea = plan["idea"]
    lines.append("")
    lines.append("--- the human's idea, verbatim ---")
    if len(idea) > 20_000:
        lines.append(idea[:20_000])
        lines.append("[synchri: truncated in this prompt; the full articulation is preserved on the plan]")
    else:
        lines.append(idea)
    # The durable revision text itself, for planner and reviewer alike: the
    # plan block is stripped from the room transcript and the agents relaunch
    # cold each turn, so this panel is the only place the loop's actual
    # subject can reach them. Submissions are already capped at
    # MAX_PLAN_CHARS, so the body renders untruncated.
    if plan["revision"]:
        lines.append("")
        lines.append(f"--- the plan: revision {plan['revision']}, full text, verbatim ---")
        lines.append(revision_body(manager, record.session_id, plan["revision"]))
    open_items = [o for o in objections(manager, record.session_id) if o["status"] == "open"]
    resolved = [o for o in objections(manager, record.session_id) if o["status"] != "open"]
    if open_items:
        lines.append("")
        lines.append("open objections and forks:")
        for entry in open_items:
            lines.append(
                f"  {entry['objection_id']} [{entry['classification']}] "
                f"(r{entry['raised_revision']} by {entry['raised_by']}): {entry['text']}"
            )
    if resolved:
        lines.append(f"resolved or waived: {', '.join(o['objection_id'] for o in resolved)}")
    if plan["status"] == READY:
        lines.append(
            "PLAN-READY: review closure reached. The human decides approval; do not "
            "begin implementation."
        )
    elif plan["status"] == NEEDS_HUMAN:
        lines.append(
            "The planning budget is exhausted; the human decides how to proceed."
        )
    return "\n".join(lines)


# ----------------------------------------------------------------------
# approval: PLANNING -> APPROVED_PLAN -> COORDINATION
# ----------------------------------------------------------------------


def promotion(manager: "SessionManager", session_id: str) -> dict | None:
    row = manager.conn.execute(
        "SELECT * FROM session_promotions WHERE planning_session_id = ?", (session_id,)
    ).fetchone()
    return dict(row) if row else None


def queue_capture(
    manager: "SessionManager",
    session_id: str,
    *,
    prompt: str,
    title: str | None = None,
    skip_review: bool = False,
) -> dict | None:
    """Durably queue a capture that raced the promotion window.

    Approval froze the planning session's dropbox and the coordination
    session may not exist yet, so the capture must be neither lost nor
    bounced back to the user: it lands on the promotion record — screened at
    the same fail-closed boundary as every capture — and drains into the
    coordination session's dropbox inside the transaction that finalizes the
    promotion. Returns ``None`` when the promotion has already finalized,
    telling the caller to capture into the coordination session directly.
    """
    from . import dropbox as dropbox_module

    text = (prompt or "").strip()
    if not text:
        raise ValidationError("describe the side task")
    if len(text) > dropbox_module.MAX_PROMPT_CHARS:
        raise ValidationError(
            f"a side task is capped at {dropbox_module.MAX_PROMPT_CHARS:,} characters"
        )
    text, _hit = dropbox_module._screened(text, dropbox_module.MAX_PROMPT_CHARS)
    # The title is screened and bounded here, at queue time, not only when the
    # drain re-captures it: the queued entry itself persists on the promotion
    # row — indefinitely, after a crash — and must never carry a raw secret.
    heading = (title or "").strip() or None
    if heading is not None:
        heading, _hit = dropbox_module._screened(heading, 200)
    with db.transaction(manager.conn):
        row = manager.conn.execute(
            "SELECT coordination_session_id, pending_captures FROM session_promotions "
            "WHERE planning_session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("no promotion is in flight for this session")
        if row["coordination_session_id"]:
            return None
        pending = json.loads(row["pending_captures"] or "[]")
        pending.append(
            {
                "prompt": text,
                "title": heading,
                "skip_review": bool(skip_review),
                "queued_at": utc_now(),
            }
        )
        manager.conn.execute(
            "UPDATE session_promotions SET pending_captures = ?, updated_at = ? "
            "WHERE planning_session_id = ?",
            (json.dumps(pending), utc_now(), session_id),
        )
    return {
        "queued": True,
        "position": len(pending),
        "detail": (
            "captured — it will enter the coordination session's dropbox as "
            "provisioning finishes"
        ),
    }


def plan_digest(plan_id: str, revision: int, inspection_sha: str, body: str) -> str:
    """The frozen identity of what the human approved."""
    material = f"{plan_id}\nrevision {revision}\n{inspection_sha}\n{body}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def approve(manager: "SessionManager", session_id: str, *, staffing=None) -> dict:
    """Approve the plan and promote it into a new, linked coordination session.

    The approval transaction boundary is concrete: one ``BEGIN IMMEDIATE``
    transaction reserves the unique promotion record — freezing the plan
    digest, the approved revision, and the inspected SHA — marks the plan
    approved, and freezes the planning session's dropbox capture. The
    coordination session is then provisioned *resumably* against that record:
    a retry resumes it, adopting a half-provisioned session instead of ever
    creating a second one. Exactly one coordination session per approved
    plan; the planning session and PLAN-NNN remain immutable.
    """
    record = manager.get(session_id)
    plan = require_plan(manager, session_id)

    existing = promotion(manager, session_id)
    if existing is None:
        # Two approvable states: PLAN-READY (review closure), and — honoring
        # the exhaustion copy's "approve what stands" — needs_human_resolution
        # with a preserved revision, no open blockers, and valid criteria.
        if plan["status"] not in {READY, NEEDS_HUMAN}:
            blockers = open_blockers(manager, session_id)
            names = ", ".join(f"{b['objection_id']} ({b['classification']})" for b in blockers)
            raise StateError(
                "the plan is not PLAN-READY"
                + (f"; open blocking items: {names}" if names else f" (it is {plan['status']})"),
                code="plan_not_ready",
            )
        if not plan["revision"]:
            raise StateError(
                "there is no submitted revision to approve — the budget ran out "
                "before the first draft; resume it or stop the session",
                code="plan_not_ready",
            )
        # Blocking decisions cannot cross approval. For a READY plan this
        # re-checks review closure against direct edits; for an exhausted one
        # it is the rule itself: open blockers must be resolved or explicitly
        # waived before what stands can be approved.
        blockers = open_blockers(manager, session_id)
        if blockers:
            raise StateError(
                "blocking decisions cannot cross approval: "
                + ", ".join(b["objection_id"] for b in blockers)
                + " must be resolved or explicitly waived first",
                code="plan_blocking_open",
            )
        body = revision_body(manager, session_id, plan["revision"])
        validate_criteria(body)
        # Revalidate the repository baseline: drift returns the plan to
        # review rather than silently executing stale assumptions.
        current_tip = worktree_module.git(
            record.repo_root, "rev-parse", plan["source_branch"], check=False
        ).strip()
        if current_tip and current_tip != plan["inspection_sha"]:
            _return_to_review_for_drift(manager, record, plan, current_tip)
            raise StateError(
                f"the repository moved past the inspected baseline "
                f"({plan['inspection_sha'][:12]} -> {current_tip[:12]}); the plan "
                "returned to review against the new baseline",
                code="baseline_drift",
            )

        digest = plan_digest(plan["plan_id"], plan["revision"], plan["inspection_sha"], body)
        now = utc_now()
        with db.transaction(manager.conn):
            fresh = get_plan(manager, session_id)
            if (
                fresh is None
                or fresh["status"] not in {READY, NEEDS_HUMAN}
                or fresh["revision"] != plan["revision"]
            ):
                raise StateError(
                    "the plan changed while approval was in flight; review its "
                    "state and approve again",
                    code="plan_moved",
                )
            manager.conn.execute(
                "INSERT INTO session_promotions (planning_session_id, plan_id, "
                "plan_revision, plan_digest, inspection_sha, status, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (session_id, plan["plan_id"], plan["revision"], digest,
                 plan["inspection_sha"], "reserved", now, now),
            )
            manager.conn.execute(
                "UPDATE session_plans SET status = ?, updated_at = ? WHERE session_id = ?",
                (APPROVED, now, session_id),
            )
            # Approval atomically freezes the planning session's dropbox
            # capture; a later capture routes to the coordination session.
            manager.conn.execute(
                "UPDATE sessions SET phase = 'closing', updated_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            # Approving what stands settles the exhaustion: the decision the
            # escalation asked for has been made.
            for escalation in manager.open_escalations(session_id):
                if escalation["rule"] == "planning_budget_exhausted":
                    manager.resolve_escalation(
                        escalation["escalation_id"], "the human approved what stands"
                    )
        manager._log(
            record, "session.plan_approved",
            {"plan_id": plan["plan_id"], "revision": plan["revision"], "digest": digest},
        )
        existing = promotion(manager, session_id)
    elif existing["status"] == "provisioned" and existing["coordination_session_id"]:
        # Duplicate approval: idempotent, never a second session. Any window
        # captures were drained by the approval that finalized.
        return {
            "promoted": True,
            "already_promoted": True,
            "coordination_session_id": existing["coordination_session_id"],
            "plan_id": existing["plan_id"],
            "plan_digest": existing["plan_digest"],
            "window_captures": [],
        }

    coordination = _provision(manager, record, existing, staffing)
    now = utc_now()
    with db.transaction(manager.conn):
        cursor = manager.conn.execute(
            "UPDATE session_promotions SET coordination_session_id = ?, status = "
            "'provisioned', updated_at = ? WHERE planning_session_id = ? "
            "AND (coordination_session_id IS NULL OR coordination_session_id = ?)",
            (coordination.session_id, now, session_id, coordination.session_id),
        )
        if cursor.rowcount == 0:
            # Defensive: the unique provenance index makes a different linked
            # session impossible, so a zero-row finalize means the record
            # changed underneath us in a way we must not paper over.
            raise ConflictError(
                "the promotion was finalized concurrently with a different "
                "coordination session; refusing to overwrite it"
            )
        # Drain the window captures into the coordination session's dropbox —
        # atomically with the finalize, exactly once.
        from . import dropbox as dropbox_module

        pending_row = manager.conn.execute(
            "SELECT pending_captures FROM session_promotions WHERE planning_session_id = ?",
            (session_id,),
        ).fetchone()
        window_captures = json.loads(
            (pending_row["pending_captures"] if pending_row else "[]") or "[]"
        )
        drained = [
            dropbox_module.capture(
                manager,
                coordination.session_id,
                entry.get("prompt") or "",
                title=entry.get("title"),
                skip_review=bool(entry.get("skip_review")),
            )
            for entry in window_captures
        ]
        if window_captures:
            manager.conn.execute(
                "UPDATE session_promotions SET pending_captures = '[]', updated_at = ? "
                "WHERE planning_session_id = ?",
                (now, session_id),
            )
        from .manager import SessionStatus

        metadata = {
            **manager.get(session_id).metadata,
            "promoted_to": coordination.session_id,
        }
        manager._update(session_id, metadata=json.dumps(metadata))
        current = manager.get(session_id)
        if not current.is_terminal:
            manager._finish(
                current, SessionStatus.COMPLETE,
                f"plan {existing['plan_id']} approved and promoted to "
                f"coordination session {coordination.session_id}",
            )
    manager._log(
        record, "session.plan_promoted",
        {"plan_id": existing["plan_id"], "coordination_session_id": coordination.session_id},
    )
    return {
        "promoted": True,
        "already_promoted": False,
        "coordination_session_id": coordination.session_id,
        "plan_id": existing["plan_id"],
        "plan_digest": existing["plan_digest"],
        # Window captures drained at finalize: the caller starts their scouts
        # exactly as it would for a directly captured item.
        "window_captures": drained,
    }


def _return_to_review_for_drift(manager, record, plan, current_tip: str) -> None:
    """Re-anchor the workspace to the new tip and send the plan back to review."""
    workspace = Path(plan["workspace_path"])
    if workspace.exists():
        worktree_module.git(
            workspace, "fetch", "--quiet", str(record.repo_root), plan["source_branch"],
            check=False,
        )
        worktree_module.git(workspace, "reset", "--hard", "FETCH_HEAD", check=False)
        head = worktree_module.git(workspace, "rev-parse", "HEAD", check=False).strip()
    else:
        head = current_tip
    now = utc_now()
    planner, reviewer = _roles(record)
    with db.transaction(manager.conn):
        manager.conn.execute(
            "UPDATE session_plans SET status = ?, ready_at = NULL, inspection_sha = ?, "
            "updated_at = ? WHERE session_id = ?",
            (UNDER_REVISION, head or current_tip, now, record.session_id),
        )
        metadata = dict(record.metadata or {})
        entry = dict(metadata.get("planning_workspace") or {})
        entry["inspection_sha"] = head or current_tip
        metadata["planning_workspace"] = entry
        manager._update(record.session_id, metadata=json.dumps(metadata))
        _raise(
            manager, record.session_id, "blocking",
            f"the repository moved past the inspected baseline "
            f"({plan['inspection_sha'][:12]} -> {(head or current_tip)[:12]}); "
            "re-verify the plan's repository facts against the new baseline",
            "synchri", plan["revision"], now,
        )
        _send_task(
            manager, record, target=planner, other=reviewer,
            source="plan_drift",
            content=(
                "The repository moved past the inspected baseline, so approval was "
                "refused and the workspace was re-anchored to the new tip. Re-verify "
                "the plan's repository facts, resolve the drift objection, update "
                "the plan text if needed, and submit the next revision for re-review."
            ),
        )
    manager._log(
        record, "session.plan_invalidated",
        {"reason": "baseline_drift", "from": plan["inspection_sha"], "to": head or current_tip},
    )


def _provision(manager, record, promo: dict, staffing):
    """Create (or adopt and complete) the coordination session for a reserved
    promotion.

    Resumable in both directions: the session is created with its provenance
    in metadata first, so a crash after creation is recovered by adopting the
    orphan on retry — and adoption always **finishes** the idempotent steps
    (gate materialization, contract) a crash may have skipped, so a
    half-provisioned session is never handed back as done. A unique index on
    that provenance makes concurrent retries converge: the loser of a create
    race adopts the winner's session instead of ever producing a second one.
    """
    orphan = _linked_session(manager, record.session_id)
    if orphan is not None:
        return _complete_provisioning(manager, orphan, promo)

    body = revision_body(manager, record.session_id, promo["plan_revision"])
    spec = _spec_from_plan(manager, record, promo, body)
    participants = _staff(record, staffing)
    try:
        coordination = manager.create(
            name=record.name,
            mode="long_horizon",
            repo_root=record.repo_root,
            base_branch=promo_branch(manager, record),
            participants=participants,
            spec=spec,
            metadata={
                "promoted_from": record.session_id,
                "plan_id": promo["plan_id"],
                "plan_revision": promo["plan_revision"],
                "plan_digest": promo["plan_digest"],
                "inspection_sha": promo["inspection_sha"],
            },
            # The coordination worktree branches from the frozen
            # inspection_sha exactly — not from wherever the source branch
            # has moved. Retries reuse the recorded SHA.
            worktree_start_point=promo["inspection_sha"],
        )
    except Exception:
        winner = _linked_session(manager, record.session_id)
        if winner is not None:
            return _complete_provisioning(manager, winner, promo)
        raise
    return _complete_provisioning(manager, coordination, promo)


def _linked_session(manager, planning_session_id: str):
    for candidate in manager.list_sessions():
        if (candidate.metadata or {}).get("promoted_from") == planning_session_id:
            return candidate
    return None


def _complete_provisioning(manager, coordination, promo: dict):
    """Idempotently finish gates and contract, fresh or adopted alike.

    Gate materialization is deterministic: each explicitly identified
    criterion becomes its own gate, verbatim — never the generic extraction.
    """
    body = revision_body(manager, promo["planning_session_id"], promo["plan_revision"])
    criteria = parse_acceptance_criteria(body)
    existing = [gate.gate_id for gate in manager.gates(coordination.session_id)]
    if existing != [gate_id for gate_id, _text in criteria]:
        manager.set_gates(
            coordination.session_id,
            [Gate(gate_id=gate_id, description=text) for gate_id, text in criteria],
        )
    # The gates' provenance is the approved plan, not whatever the extraction
    # heuristics made of the promoted spec text at create time.
    metadata = dict(manager.get(coordination.session_id).metadata or {})
    if metadata.get("gate_derivation") != "approved_plan":
        metadata["gate_derivation"] = "approved_plan"
        manager._update(coordination.session_id, metadata=json.dumps(metadata))
    if coordination.contract_revision < 1:
        manager.issue_contract(
            coordination.session_id,
            reason=f"promoted from approved plan {promo['plan_id']}",
        )
    return manager.get(coordination.session_id)


def promo_branch(manager, record) -> str:
    plan = get_plan(manager, record.session_id)
    return plan["source_branch"] if plan else record.base_branch


def _staff(record, staffing) -> list[ParticipantPlan]:
    """Default staffing: the connected planner and reviewer, remapped.

    The planner becomes the Primary Builder and the plan reviewer the
    implementation's Adversarial Reviewer — same names, same runtimes, same
    commands, no second wizard. An explicit staffing list overrides it.
    """
    if staffing:
        return [
            ParticipantPlan(
                name=entry["name"],
                runtime=entry.get("runtime", "generic"),
                role=entry.get("role", "participant"),
                command=entry.get("command"),
            )
            for entry in staffing
        ]
    remap = {
        Role.PLANNER.value: Role.PRIMARY_BUILDER.value,
        Role.PLAN_REVIEWER.value: Role.ADVERSARIAL_REVIEWER.value,
    }
    return [
        ParticipantPlan(
            name=plan.name,
            runtime=plan.runtime,
            role=remap.get(plan.role, plan.role),
            command=plan.command,
            metadata=dict(plan.metadata),
        )
        for plan in record.participants
    ]


def _spec_from_plan(manager, record, promo: dict, body: str) -> ProductSpec:
    """Deterministic plan-to-contract conversion: ProductSpec v1.

    The frozen revision's ordered work, constraints, preservation
    requirements, and explicit acceptance criteria come through verbatim;
    accepted implementation-affecting assumptions (open nonblocking items)
    and resolved reviewer findings are serialized after them, so agents are
    never told to follow constraints that are not in their canonical
    contract. Superseded review history and the verbatim idea remain
    provenance on the plan, not spec text.
    """
    all_objections = objections(manager, record.session_id)
    lines = [
        f"# Approved implementation plan {promo['plan_id']} (revision {promo['plan_revision']})",
        "",
        body.strip(),
        "",
        "---",
        "",
    ]
    accepted = [
        o for o in all_objections
        if o["status"] == "open" and o["classification"] in {"nonblocking", "advisory"}
    ]
    if accepted:
        lines.append("## Accepted assumptions and deferred advisories (from plan review)")
        for entry in sorted(accepted, key=lambda o: o["objection_id"]):
            lines.append(
                f"- {entry['objection_id']} ({entry['classification']}): {entry['text']}"
            )
        lines.append("")
    settled = [o for o in all_objections if o["status"] in {"resolved", "waived"}]
    if settled:
        lines.append("## Resolved review findings")
        for entry in sorted(settled, key=lambda o: o["objection_id"]):
            disposition = entry.get("disposition") or ""
            lines.append(
                f"- {entry['objection_id']} ({entry['classification']}, {entry['status']}"
                + (" by the human" if entry.get("resolved_by") == "human" else "")
                + f"): {entry['text']}"
                + (f" — {disposition}" if disposition else "")
            )
        lines.append("")
    lines.append("## Plan provenance")
    lines.append(f"- Plan: {promo['plan_id']} revision {promo['plan_revision']} "
                 f"(digest {promo['plan_digest'][:16]})")
    lines.append(f"- Inspected baseline: {promo['inspection_sha']} on {promo_branch(manager, record)}")
    planner, reviewer = _roles(record)
    lines.append(f"- Planned by {planner} · adversarially reviewed by {reviewer}")
    lines.append(
        "- Deviations from this plan must be explicit: materially necessary "
        "divergence is recorded with rationale, and a change to scope or "
        "acceptance criteria is a contract revision. Plan approval is not "
        "implementation sign-off."
    )
    return ProductSpec(text="\n".join(lines))
