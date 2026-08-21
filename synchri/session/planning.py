"""Planning Mode: an adversarially reviewed plan as executable state.

Planning should be executable state, not prose the user has to carry into the
next workflow. The user articulates an idea — in as much or as little detail
as they want, stored verbatim — and a planner turns it, plus real repository
inspection, into ``PLAN-DRAFT`` revision 1 **before** the adversarial plan
reviewer sees anything. The loop is then PLAN-DRAFT -> ADVERSARIAL REVIEW ->
REVISION -> RE-REVIEW -> PLAN-READY, where readiness requires review closure:
open blocking objections or unresolved architectural forks prevent it, and
nonblocking assumptions may remain only clearly classified.

Isolation is capability-based, not contract-only: planning runs in a
**disposable planning workspace** — a local clone anchored to one
``inspection_sha`` with its remotes removed, so writes technically cannot
reach the user's repository — offered only on runtimes whose adapter declares
``read_only_planning_workspace``. The workspace's Git state is verified after
every turn and the workspace is never reused for coordination.

The loop is budgeted (turns, revisions, wall clock — whichever first, with
one user-authorized resume); exhaustion produces ``needs_human_resolution``,
preserving the latest draft and open objections rather than looping forever.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ..broker import Credential
from ..errors import ConflictError, NotFoundError, StateError, ValidationError
from ..ids import utc_now
from ..models.envelope import MessageDraft
from ..storage import db
from . import worktree as worktree_module
from .modes import Role, collaboration_pair, planning_workspace_supported

if TYPE_CHECKING:  # pragma: no cover - imports for type checkers only
    from .manager import SessionManager, SessionRecord

PLAN_FILENAME = "PLAN.md"
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
                    directives.plan_submitted, reviewer, warnings, now,
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


def _submit(manager, record, plan, actor, summary, reviewer, warnings, now) -> dict | None:
    workspace = Path(plan["workspace_path"])
    plan_file = workspace / PLAN_FILENAME
    if not plan_file.exists():
        warnings.append(
            f"{actor}: submit ignored — write the plan to {PLAN_FILENAME} in the "
            "planning workspace first"
        )
        return None
    body = plan_file.read_text(encoding="utf-8", errors="replace")
    if not body.strip():
        warnings.append(f"{actor}: submit ignored — {PLAN_FILENAME} is empty")
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
            + f" Read {PLAN_FILENAME} in the planning workspace and review it "
            "adversarially against the repository state. Raise findings with "
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
            "SYNCHRI-OBJECTION-RESOLVED, update PLAN.md, and submit the next revision."
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
                "objections, update PLAN.md, and submit the next revision; stop at "
                "the minimum sufficiently defensible plan."
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
                + " Fold this into PLAN.md and submit the next revision; it will "
                "be re-reviewed before PLAN-READY."
            ),
        )
    manager._log(record, "session.plan_reopened", {"note": direction[:400]})
    return payload(manager, session_id)


def waive_objection(
    manager: "SessionManager", session_id: str, objection_id: str, note: str
) -> dict:
    """The human explicitly waives one blocking decision — via a reviewed revision.

    A waiver never slips through silently: it is recorded on the objection,
    and the plan returns to review so the reviewer closes with the waiver on
    the record. Only then can PLAN-READY and approval follow.
    """
    record = manager.get(session_id)
    plan = require_plan(manager, session_id)
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
    manager._log(
        record, "session.plan_objection_waived",
        {"objection_id": objection_id, "note": reason[:400]},
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
                "ready. Fold it into PLAN.md — or record explicitly why it does not "
                "change the plan — and submit the next revision for re-review."
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
        f"plan file: {PLAN_FILENAME} at the planning workspace root",
    ]
    idea = plan["idea"]
    lines.append("")
    lines.append("--- the human's idea, verbatim ---")
    if len(idea) > 20_000:
        lines.append(idea[:20_000])
        lines.append("[synchri: truncated in this prompt; the full articulation is preserved on the plan]")
    else:
        lines.append(idea)
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
