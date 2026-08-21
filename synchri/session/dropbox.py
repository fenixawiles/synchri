"""The side-task dropbox: capture ideas mid-session without derailing the room.

A user watching two agents build will notice things — a refactor worth doing, a
bug in an adjacent module, a "what if" — that are not part of the original
specification. The dropbox gives those a place to land that never interrupts
the main agents: each idea becomes a ``DROP-NNN`` item, investigated off the
critical path, and appended to a durable **appendix** that is rendered after
the canonical specification, never written into it. Any edit to the
:class:`~synchri.session.spec.ProductSpec` changes its digest and forces every
agent to re-acknowledge a new contract, so the spec created at room creation
stays the untouched source of truth; the appendix is a separate append-only
stream.

Appendix items are *additional but not optional*: capture freezes when
completion validation begins, and every item captured before that moment must
receive an explicit evaluation — by both current main roles, over the same
immutable proposal digest — before the session can close. Approved items
become ``EXT-NNN`` extension gates, materialized atomically with the approval;
declined and failed items keep their recorded rationales and never block
closing. Nothing is silently dropped.

The durable session phase (``original_work -> appendix_evaluation ->
extension_work -> closing``) is driven by :meth:`SessionManager.complete`;
this module owns the item lifecycle, the exactly-once appendix, and the
evaluation rules.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING

from ..errors import ConflictError, NotFoundError, StateError, ValidationError
from ..ids import utc_now
from ..storage import db
from .gates import Gate
from .modes import collaboration_pair

if TYPE_CHECKING:  # pragma: no cover - imports for type checkers only
    from .manager import SessionManager, SessionRecord

# -- item lifecycle -----------------------------------------------------

CAPTURED = "captured"
INVESTIGATING = "investigating"
REVIEWING = "reviewing"
PROPOSED = "proposed"
EVALUATED = "evaluated"
FAILED = "failed"
TIMED_OUT = "timed_out"

#: States that carry everything an evaluation needs: a reviewed proposal, or a
#: failure report the main pair may decline on — so a timed-out scout can
#: never block completion forever.
EVALUABLE = frozenset({PROPOSED, FAILED, TIMED_OUT})

APPROVE = "approve"
DECLINE = "decline"

APPROVED = "approved"
DECLINED = "declined"
WAIVED = "waived"

# -- durable session phases (stored on sessions.phase) ------------------

PHASE_ORIGINAL = "original_work"
PHASE_EVALUATION = "appendix_evaluation"
PHASE_EXTENSION = "extension_work"
PHASE_CLOSING = "closing"

# -- artifact bounds: enforced at deposit, fail closed ------------------

MAX_PROMPT_CHARS = 4_000
MAX_TEXT_CHARS = 64_000        # evidence / rationale / failure report
MAX_RATIONALE_CHARS = 4_000    # one role's evaluation rationale
MAX_PATCH_CHARS = 256_000      # larger patches stay on the scratch branch
PATCH_PREVIEW_CHARS = 4_000

_TRUNCATION_MARK = "\n[synchri: truncated at the artifact bound]"
_WITHHELD_MARK = "[synchri: withheld — a potential secret was detected]"

#: Conservative, fail-closed: a false positive quarantines a patch (the note
#: says why and the scratch branch keeps the work); a false negative would
#: persist a credential. Raw sensitive content is never persisted, streamed,
#: or included in the completion package.
_SECRET_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\bgh[pousr]_[A-Za-z0-9]{20,}\b",
        r"\bsk-[A-Za-z0-9_-]{20,}\b",
        r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\baws_secret_access_key\s*[=:]\s*\S+",
        r"\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)\s*[=:]\s*['\"]?[A-Za-z0-9_\-/+]{16,}",
    )
)


def contains_secret(text: str | None) -> bool:
    if not text:
        return False
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def _bounded(text: str | None, limit: int) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - len(_TRUNCATION_MARK))] + _TRUNCATION_MARK


def _screened(text: str | None, limit: int) -> tuple[str, bool]:
    """Bound one text artifact, withholding it entirely on a secret hit."""
    if contains_secret(text):
        return _WITHHELD_MARK, True
    return _bounded(text, limit), False


# ----------------------------------------------------------------------
# capture and the exactly-once appendix
# ----------------------------------------------------------------------


def capture(
    manager: "SessionManager",
    session_id: str,
    prompt: str,
    *,
    title: str | None = None,
    skip_review: bool = False,
) -> dict:
    """Capture one side task, never interrupting the main agents.

    The freeze check runs under the write lock — this is the
    capture/completion race point. ``complete()`` flips the phase inside its
    own ``BEGIN IMMEDIATE`` transaction, so exactly one ordering happens: the
    capture lands in the snapshot, or it is refused as frozen.
    """
    record = manager.get(session_id)
    if record.is_terminal:
        raise StateError(f"session is {record.status}", code="session_finished")
    text = (prompt or "").strip()
    if not text:
        raise ValidationError("describe the side task")
    if len(text) > MAX_PROMPT_CHARS:
        raise ValidationError(
            f"a side task is capped at {MAX_PROMPT_CHARS:,} characters — "
            "keep the capture short; the scout does the investigating"
        )
    heading = (title or "").strip() or text.splitlines()[0][:80]
    now = utc_now()
    with db.transaction(manager.conn):
        _require_phase(manager.conn, session_id, PHASE_ORIGINAL, action="capture a side task")
        drop_id = _next_drop_id(manager.conn, session_id)
        manager.conn.execute(
            "INSERT INTO session_drops (session_id, drop_id, title, prompt, status, "
            "skip_review, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (session_id, drop_id, heading, text, CAPTURED, int(bool(skip_review)), now, now),
        )
        # Reconcile immediately: an idle room cannot miss an item, because
        # membership in the appendix never waited for a turn to happen.
        _reconcile_locked(manager.conn, session_id)
    manager._log(
        record,
        "session.drop_captured",
        {"drop_id": drop_id, "title": heading, "skip_review": bool(skip_review)},
    )
    return item(manager, session_id, drop_id)


def reconcile(manager: "SessionManager", session_id: str) -> list[str]:
    """Append captured items to the appendix — exactly once, in capture order.

    Runs after each completed main-agent turn and immediately on capture, so
    neither an idle room nor a busy one can miss an item. Once the phase
    leaves ``original_work`` the appendix is frozen along with capture, so
    reconciliation appends nothing. Exactly-once is structural: the appendix
    primary key admits each ``(session, drop)`` a single time.
    """
    with db.transaction(manager.conn):
        row = manager.conn.execute(
            "SELECT phase FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None or (row["phase"] or PHASE_ORIGINAL) != PHASE_ORIGINAL:
            return []
        return _reconcile_locked(manager.conn, session_id)


def _reconcile_locked(conn, session_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT d.drop_id FROM session_drops d LEFT JOIN session_appendix a "
        "ON a.session_id = d.session_id AND a.drop_id = d.drop_id "
        "WHERE d.session_id = ? AND a.drop_id IS NULL ORDER BY d.rowid",
        (session_id,),
    ).fetchall()
    if not rows:
        return []
    position = conn.execute(
        "SELECT COALESCE(MAX(position), 0) AS top FROM session_appendix WHERE session_id = ?",
        (session_id,),
    ).fetchone()["top"]
    now = utc_now()
    appended = []
    for row in rows:
        position += 1
        conn.execute(
            "INSERT INTO session_appendix (session_id, drop_id, position, appended_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, row["drop_id"], position, now),
        )
        appended.append(row["drop_id"])
    return appended


def _require_phase(conn, session_id: str, phase: str, *, action: str) -> None:
    row = conn.execute(
        "SELECT phase FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"no such session: {session_id}")
    current = row["phase"] or PHASE_ORIGINAL
    if current != phase:
        if phase == PHASE_ORIGINAL:
            raise StateError(
                f"cannot {action}: the dropbox froze when completion validation began "
                f"(the session is in {current})",
                code="dropbox_frozen",
            )
        raise StateError(
            f"cannot {action}: the session is in {current}, not {phase}",
            code="wrong_phase",
        )


def _next_drop_id(conn, session_id: str) -> str:
    highest = 0
    for row in conn.execute(
        "SELECT drop_id FROM session_drops WHERE session_id = ?", (session_id,)
    ):
        match = re.fullmatch(r"DROP-(\d+)", row["drop_id"])
        if match:
            highest = max(highest, int(match.group(1)))
    return f"DROP-{highest + 1:03d}"


def _next_extension_id(manager: "SessionManager", session_id: str) -> str:
    """Allocate the next EXT id, colliding with neither drops nor gates."""
    highest = 0
    for row in manager.conn.execute(
        "SELECT extension_id FROM session_drops WHERE session_id = ? "
        "AND extension_id IS NOT NULL",
        (session_id,),
    ):
        match = re.fullmatch(r"EXT-(\d+)", row["extension_id"] or "")
        if match:
            highest = max(highest, int(match.group(1)))
    taken = {gate.gate_id for gate in manager.gates(session_id)}
    candidate = f"EXT-{highest + 1:03d}"
    while candidate in taken:
        highest += 1
        candidate = f"EXT-{highest + 1:03d}"
    return candidate


# ----------------------------------------------------------------------
# reads
# ----------------------------------------------------------------------


def items(manager: "SessionManager", session_id: str) -> list[dict]:
    rows = manager.conn.execute(
        "SELECT d.*, a.position AS appendix_position, a.appended_at FROM session_drops d "
        "LEFT JOIN session_appendix a ON a.session_id = d.session_id AND a.drop_id = d.drop_id "
        "WHERE d.session_id = ? ORDER BY COALESCE(a.position, 1000000000), d.rowid",
        (session_id,),
    ).fetchall()
    return [_item_dict(row) for row in rows]


def item(manager: "SessionManager", session_id: str, drop_id: str) -> dict:
    row = manager.conn.execute(
        "SELECT d.*, a.position AS appendix_position, a.appended_at FROM session_drops d "
        "LEFT JOIN session_appendix a ON a.session_id = d.session_id AND a.drop_id = d.drop_id "
        "WHERE d.session_id = ? AND d.drop_id = ?",
        (session_id, drop_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"no dropbox item {drop_id!r} in this session")
    return _item_dict(row)


def _item_dict(row) -> dict:
    data = dict(row)
    for key in ("proposal", "review"):
        data[key] = json.loads(data[key]) if data.get(key) else None
    data["evaluations"] = json.loads(data.get("evaluations") or "{}")
    data["skip_review"] = bool(data.get("skip_review"))
    return data


def summarize(manager: "SessionManager", session_id: str) -> dict:
    entries = items(manager, session_id)
    return {
        "total": len(entries),
        "open": sum(1 for e in entries if not e["disposition"]),
        "approved": sum(1 for e in entries if e["disposition"] == APPROVED),
        "declined": sum(1 for e in entries if e["disposition"] == DECLINED),
        "waived": sum(1 for e in entries if e["disposition"] == WAIVED),
        "failed": sum(1 for e in entries if e["status"] in {FAILED, TIMED_OUT}),
    }


def snapshot_ids(record: "SessionRecord") -> list[str] | None:
    """The frozen evaluation obligation, recorded at the phase transition."""
    stored = (record.metadata or {}).get("dropbox_snapshot") or {}
    return list(stored.get("items")) if stored.get("items") is not None else None


def pending(manager: "SessionManager", record: "SessionRecord") -> list[str]:
    """Snapshot items that still owe a disposition, in appendix order."""
    frozen = snapshot_ids(record)
    open_rows = [
        row["drop_id"]
        for row in manager.conn.execute(
            "SELECT d.drop_id FROM session_drops d LEFT JOIN session_appendix a "
            "ON a.session_id = d.session_id AND a.drop_id = d.drop_id "
            "WHERE d.session_id = ? AND d.disposition IS NULL "
            "ORDER BY COALESCE(a.position, 1000000000), d.rowid",
            (record.session_id,),
        )
    ]
    if frozen is None:
        return open_rows
    return [drop_id for drop_id in open_rows if drop_id in set(frozen)]


def approved_extensions(manager: "SessionManager", session_id: str) -> list[dict]:
    """Approved items in appendix order — the order extensions execute in."""
    return [
        entry
        for entry in items(manager, session_id)
        if entry["disposition"] == APPROVED
    ]


# ----------------------------------------------------------------------
# investigation lifecycle (driven by the ancillary runner)
# ----------------------------------------------------------------------


def begin_investigation(
    manager: "SessionManager",
    session_id: str,
    drop_id: str,
    *,
    base_sha: str | None = None,
    scratch_branch: str | None = None,
    scratch_path: str | None = None,
) -> dict:
    with db.transaction(manager.conn):
        row = _drop_row(manager.conn, session_id, drop_id)
        if row["status"] != CAPTURED:
            raise StateError(
                f"{drop_id} is {row['status']}; only a captured item starts investigation",
                code="drop_wrong_status",
            )
        manager.conn.execute(
            "UPDATE session_drops SET status = ?, base_sha = ?, scratch_branch = ?, "
            "scratch_path = ?, updated_at = ? WHERE session_id = ? AND drop_id = ?",
            (INVESTIGATING, base_sha, scratch_branch, scratch_path, utc_now(),
             session_id, drop_id),
        )
    return item(manager, session_id, drop_id)


def deposit_proposal(
    manager: "SessionManager",
    session_id: str,
    drop_id: str,
    *,
    evidence: str,
    rationale: str,
    recommendation: str,
    patch: str | None = None,
    diffstat: str | None = None,
    commit: str | None = None,
) -> dict:
    """Store the scout's proposal as an immutable, bounded, screened artifact.

    Bounds are fail-closed at deposit: oversized text is truncated with a
    visible mark; an oversized patch is not stored (the proposal carries the
    diffstat and a bounded preview, and the scratch branch is retained until
    the extension completes, the item is explicitly rejected, or the patch is
    imported into the session worktree); a detected secret withholds the
    artifact entirely. The digest is computed over exactly the bytes stored,
    so both evaluations demonstrably judged the same proposal.
    """
    with db.transaction(manager.conn):
        row = _drop_row(manager.conn, session_id, drop_id)
        if row["status"] != INVESTIGATING:
            raise StateError(
                f"{drop_id} is {row['status']}; only an item under investigation "
                "can receive a proposal",
                code="drop_wrong_status",
            )
        withheld: list[str] = []
        stored_evidence, hit = _screened(evidence, MAX_TEXT_CHARS)
        if hit:
            withheld.append("evidence")
        stored_rationale, hit = _screened(rationale, MAX_TEXT_CHARS)
        if hit:
            withheld.append("rationale")
        stored_recommendation, hit = _screened(recommendation, MAX_RATIONALE_CHARS)
        if hit:
            withheld.append("recommendation")

        stored_patch = None
        patch_preview = None
        patch_note = None
        if patch:
            if contains_secret(patch):
                withheld.append("patch")
                patch_note = (
                    "quarantined: a potential secret was detected; the raw patch "
                    "was not stored"
                )
            elif len(patch) > MAX_PATCH_CHARS:
                patch_preview = patch[:PATCH_PREVIEW_CHARS]
                patch_note = (
                    f"oversized ({len(patch):,} chars > {MAX_PATCH_CHARS:,}); the full "
                    "patch stays on the scratch branch until its extension completes, "
                    "the item is rejected, or the patch is imported"
                )
            else:
                stored_patch = patch

        proposal = {
            "evidence": stored_evidence,
            "rationale": stored_rationale,
            "recommendation": stored_recommendation,
            "base_sha": row["base_sha"],
            "commit": commit,
            "patch": stored_patch,
            "diffstat": _bounded(diffstat, MAX_RATIONALE_CHARS) or None,
            "patch_preview": patch_preview,
            "patch_note": patch_note,
            "withheld": withheld,
            "deposited_at": utc_now(),
        }
        serialized = json.dumps(proposal, sort_keys=True)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        next_status = PROPOSED if row["skip_review"] else REVIEWING
        manager.conn.execute(
            "UPDATE session_drops SET status = ?, proposal = ?, proposal_digest = ?, "
            "updated_at = ? WHERE session_id = ? AND drop_id = ?",
            (next_status, serialized, digest, utc_now(), session_id, drop_id),
        )
    return item(manager, session_id, drop_id)


def record_review(
    manager: "SessionManager",
    session_id: str,
    drop_id: str,
    *,
    verdict: str,
    critique: str,
) -> dict:
    """Attach the ancillary adversarial review; the proposal digest is untouched."""
    with db.transaction(manager.conn):
        row = _drop_row(manager.conn, session_id, drop_id)
        if row["status"] != REVIEWING:
            raise StateError(
                f"{drop_id} is {row['status']}; only a proposal under review "
                "can receive its review",
                code="drop_wrong_status",
            )
        review = {
            "verdict": _bounded(verdict, 200),
            "critique": _bounded(critique, MAX_TEXT_CHARS),
            "proposal_digest": row["proposal_digest"],
            "reviewed_at": utc_now(),
        }
        manager.conn.execute(
            "UPDATE session_drops SET status = ?, review = ?, updated_at = ? "
            "WHERE session_id = ? AND drop_id = ?",
            (PROPOSED, json.dumps(review), utc_now(), session_id, drop_id),
        )
    return item(manager, session_id, drop_id)


def mark_failed(
    manager: "SessionManager",
    session_id: str,
    drop_id: str,
    *,
    kind: str = FAILED,
    report: str = "",
) -> dict:
    """Terminal route for a scout that crashed, timed out, or ran out of budget.

    The failure report is what the main pair declines on, so mandatory
    evaluation can never block completion forever. Further retries are
    user-initiated.
    """
    if kind not in {FAILED, TIMED_OUT}:
        raise ValidationError(f"a failed item is 'failed' or 'timed_out', not {kind!r}")
    with db.transaction(manager.conn):
        row = _drop_row(manager.conn, session_id, drop_id)
        if row["disposition"]:
            raise ConflictError(f"{drop_id} already has its disposition: {row['disposition']}")
        if row["status"] == EVALUATED:
            raise StateError(f"{drop_id} is already evaluated", code="drop_wrong_status")
        manager.conn.execute(
            "UPDATE session_drops SET status = ?, failure_report = ?, updated_at = ? "
            "WHERE session_id = ? AND drop_id = ?",
            (kind, _bounded(report, MAX_TEXT_CHARS), utc_now(), session_id, drop_id),
        )
    return item(manager, session_id, drop_id)


def _drop_row(conn, session_id: str, drop_id: str):
    row = conn.execute(
        "SELECT * FROM session_drops WHERE session_id = ? AND drop_id = ?",
        (session_id, drop_id),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"no dropbox item {drop_id!r} in this session")
    return row


# ----------------------------------------------------------------------
# evaluation: both main roles, one immutable digest
# ----------------------------------------------------------------------


def evaluate(
    manager: "SessionManager",
    session_id: str,
    drop_id: str,
    *,
    actor: str,
    verdict: str,
    rationale: str,
) -> dict:
    """Record one main role's evaluation; both present decides the disposition.

    Both approve → approved: an ``EXT-NNN`` extension id is allocated and its
    extension gate is materialized atomically with the disposition, before any
    further completion validation. Any non-approval → declined, with both
    rationales retained. The slot is role-based — a replacement participant
    holding a main role supplies the missing evaluation, and provenance
    records the participant and its recovery generation.
    """
    record = manager.get(session_id)
    if record.phase != PHASE_EVALUATION:
        raise StateError(
            "appendix items are evaluated after the original task completes "
            f"(the session is in {record.phase})",
            code="not_in_evaluation",
        )
    normalized = {"approve": APPROVE, "approved": APPROVE,
                  "decline": DECLINE, "declined": DECLINE,
                  "reject": DECLINE, "rejected": DECLINE}.get((verdict or "").strip().lower())
    if normalized is None:
        raise ValidationError(f"an evaluation verdict is approve or decline, not {verdict!r}")
    lead, reviewer = collaboration_pair(record.participants)
    if lead is not None and actor == lead.name:
        slot = "builder"
    elif reviewer is not None and actor == reviewer.name:
        slot = "reviewer"
    else:
        raise ValidationError(
            f"{actor!r} does not hold a main role in this session; "
            "appendix items are evaluated by the two main roles"
        )
    reason = (rationale or "").strip()
    if not reason:
        raise ValidationError(f"an evaluation of {drop_id} needs a rationale")

    with db.transaction(manager.conn):
        row = _drop_row(manager.conn, session_id, drop_id)
        if row["disposition"]:
            raise ConflictError(f"{drop_id} already has its disposition: {row['disposition']}")
        if row["status"] not in EVALUABLE:
            raise StateError(
                f"{drop_id} is {row['status']}; only a proposed item or one with a "
                "failure report can be evaluated",
                code="drop_not_evaluable",
            )
        if normalized == APPROVE and row["status"] != PROPOSED:
            raise ValidationError(
                f"{drop_id} carries only a failure report — there is no proposal to "
                "approve. Decline on the report; a retry is the human's call."
            )
        try:
            generation = manager.participant_recovery(session_id, actor)["recovery_generation"]
        except NotFoundError:
            generation = 0
        evaluations = json.loads(row["evaluations"] or "{}")
        evaluations[slot] = {
            "participant": actor,
            "verdict": normalized,
            "rationale": _bounded(reason, MAX_RATIONALE_CHARS),
            "proposal_digest": row["proposal_digest"],
            "recovery_generation": generation,
            "at": utc_now(),
        }
        disposition = None
        extension_id = None
        status = row["status"]
        if "builder" in evaluations and "reviewer" in evaluations:
            approved = all(
                evaluations[key]["verdict"] == APPROVE for key in ("builder", "reviewer")
            )
            disposition = APPROVED if approved else DECLINED
            if row["status"] == PROPOSED:
                status = EVALUATED
            if approved:
                extension_id = _next_extension_id(manager, session_id)
                proposal = json.loads(row["proposal"] or "{}")
                summary = (
                    (proposal.get("recommendation") or "").strip().splitlines()[0:1]
                    or [row["title"]]
                )[0][:240]
                # Materialized atomically with the approval, before any further
                # completion validation: the extension is now real work the
                # normal gate machinery blocks on.
                manager._write_gate(
                    session_id,
                    Gate(
                        gate_id=extension_id,
                        description=f"Extension of {drop_id}: {summary or row['title']}",
                        origin_kind="extension",
                        drop_id=drop_id,
                        extension_id=extension_id,
                    ),
                )
        manager.conn.execute(
            "UPDATE session_drops SET evaluations = ?, disposition = ?, extension_id = ?, "
            "status = ?, updated_at = ? WHERE session_id = ? AND drop_id = ?",
            (json.dumps(evaluations), disposition, extension_id or row["extension_id"],
             status, utc_now(), session_id, drop_id),
        )
    manager._log(
        record,
        "session.drop_evaluated",
        {"drop_id": drop_id, "by": actor, "slot": slot, "verdict": normalized,
         "disposition": disposition, "extension_id": extension_id},
    )
    return item(manager, session_id, drop_id)


def freeze_incomplete_locked(conn, session_id: str) -> list[str]:
    """The completion cutoff ends investigation; nothing may block evaluation.

    Runs inside the freeze transaction. An item whose investigation had not
    finished — no proposal reviewed, or none deposited at all — is marked
    ``timed_out`` with a failure report, so the main pair has a recorded
    artifact to decline on and mandatory evaluation can never wait on work
    that will not arrive. A deposited proposal stays stored and visible, but
    an unreviewed one is never approvable: review is mandatory unless the
    human explicitly skipped it for that item.
    """
    rows = conn.execute(
        "SELECT drop_id, status FROM session_drops WHERE session_id = ? "
        "AND status IN (?, ?, ?) ORDER BY rowid",
        (session_id, CAPTURED, INVESTIGATING, REVIEWING),
    ).fetchall()
    now = utc_now()
    frozen = []
    for row in rows:
        stage = {
            CAPTURED: "no investigation had started",
            INVESTIGATING: "the investigation had not completed",
            REVIEWING: "the adversarial review had not completed",
        }[row["status"]]
        conn.execute(
            "UPDATE session_drops SET status = ?, failure_report = ?, updated_at = ? "
            "WHERE session_id = ? AND drop_id = ?",
            (
                TIMED_OUT,
                f"{stage} when completion validation began — the completion "
                "cutoff ends investigation; a retry is the human's call",
                now, session_id, row["drop_id"],
            ),
        )
        frozen.append(row["drop_id"])
    return frozen


def waive_pending_locked(conn, session_id: str, note: str) -> list[str]:
    """Record the human's forced-completion waiver on every open item.

    Runs inside the caller's terminal transaction. A waiver is an explicit
    disposition by the human authority — recorded on the item, never a silent
    drop.
    """
    rows = conn.execute(
        "SELECT drop_id, evaluations FROM session_drops "
        "WHERE session_id = ? AND disposition IS NULL ORDER BY rowid",
        (session_id,),
    ).fetchall()
    waived = []
    now = utc_now()
    for row in rows:
        evaluations = json.loads(row["evaluations"] or "{}")
        evaluations["human"] = {
            "participant": "human",
            "verdict": WAIVED,
            "rationale": note,
            "at": now,
        }
        conn.execute(
            "UPDATE session_drops SET evaluations = ?, disposition = ?, updated_at = ? "
            "WHERE session_id = ? AND drop_id = ?",
            (json.dumps(evaluations), WAIVED, now, session_id, row["drop_id"]),
        )
        waived.append(row["drop_id"])
    return waived


# ----------------------------------------------------------------------
# prompt rendering: the appendix, after the spec, never inside it
# ----------------------------------------------------------------------


def render_appendix(manager: "SessionManager", record: "SessionRecord") -> str | None:
    """The appendix section for a managed agent's prompt, phase-appropriate.

    The canonical contract and specification are never touched: this renders
    after them, so an appendix item can never silently become spec text.
    """
    entries = [e for e in items(manager, record.session_id) if e["appendix_position"]]
    if not entries:
        return None
    lines: list[str] = ["--- appendix: side-task dropbox (additional, not optional) ---"]
    if record.phase == PHASE_ORIGINAL:
        lines.append(
            "The human captured side tasks. Each will be explicitly evaluated after the "
            "original specification is complete. They are NOT part of the canonical "
            "specification: do not act on them now, and do not let them interfere with "
            "the original work."
        )
        for entry in entries:
            lines.append(f"  {entry['drop_id']} [{entry['status']}] {entry['title']}")
    elif record.phase == PHASE_EVALUATION:
        lines.append(
            "The original work is complete. Every appendix item below now needs an "
            "explicit evaluation from BOTH of you — the same immutable proposal, two "
            "independent judgments. Record yours by ending a reply with:"
        )
        lines.append("  SYNCHRI-DROP: <id>|approve|<your rationale>")
        lines.append("  SYNCHRI-DROP: <id>|decline|<your rationale>")
        lines.append(
            "Both approving makes the item an EXT extension gate that must pass before "
            "the session closes; any non-approval declines it with both rationales kept. "
            "An item carrying only a failure report can only be declined — a retry is "
            "the human's call. When every item has both evaluations, the Primary "
            "Builder requests completion again with SYNCHRI-COMPLETE."
        )
        for entry in entries:
            lines.append("")
            lines.extend(_render_evaluation_entry(entry))
    else:
        approved = [e for e in entries if e["disposition"] == APPROVED]
        if not approved:
            return None
        lines.append(
            "Every appendix item has its disposition. The approved extensions below are "
            "now part of the work, in appendix order. Implement each one and record "
            "evidence on its extension gate exactly as you do for main gates."
        )
        for entry in approved:
            proposal = entry["proposal"] or {}
            summary = (proposal.get("recommendation") or entry["title"]).strip()
            lines.append(
                f"  {entry['extension_id']} (from {entry['drop_id']}, gate "
                f"{entry['extension_id']}): {_excerpt(summary, 200)}"
            )
    return "\n".join(lines)


def _render_evaluation_entry(entry: dict) -> list[str]:
    lines = [f"  {entry['drop_id']} [{entry['status']}] {entry['title']}"]
    if entry["evaluations"]:
        recorded = ", ".join(
            f"{slot}: {value['verdict']}" for slot, value in sorted(entry["evaluations"].items())
        )
        lines.append(f"    evaluations so far: {recorded}")
    if entry["status"] in {FAILED, TIMED_OUT}:
        lines.append(
            f"    the investigation {entry['status'].replace('_', ' ')}; failure report: "
            f"{_excerpt(entry['failure_report'] or 'none recorded', 400)}"
        )
        if not entry["proposal"]:
            return lines
        lines.append(
            "    a proposal was deposited before the failure — shown for the record; "
            "this item can only be declined"
        )
    proposal = entry["proposal"] or {}
    lines.append(f"    proposal digest: {entry['proposal_digest'] or 'missing'}")
    for label, key, limit in (
        ("recommendation", "recommendation", 400),
        ("rationale", "rationale", 600),
        ("evidence", "evidence", 600),
    ):
        value = (proposal.get(key) or "").strip()
        if value:
            lines.append(f"    {label}: {_excerpt(value, limit)}")
    if proposal.get("diffstat"):
        lines.append(f"    diffstat: {_excerpt(proposal['diffstat'], 300)}")
    if proposal.get("patch_note"):
        lines.append(f"    patch: {proposal['patch_note']}")
    review = entry["review"] or {}
    if review.get("critique"):
        lines.append(
            f"    ancillary adversarial review ({review.get('verdict') or 'reviewed'}): "
            f"{_excerpt(review['critique'], 600)}"
        )
    elif entry["skip_review"]:
        lines.append("    ancillary review: skipped by the human's explicit per-item choice")
    return lines


def _excerpt(text: str, limit: int) -> str:
    flattened = " ".join((text or "").split())
    if len(flattened) <= limit:
        return flattened
    return flattened[: limit - 1] + "…"
