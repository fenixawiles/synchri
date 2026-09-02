"""Deliberative provenance: a derived index over the durable record.

The transcript and the session's structured records — messages, plan
revisions and objections, gates, drops, escalations, acknowledgments, the
event log — remain the canonical evidence layer. Everything in this module
is computed at read time and only points back into that record: an index
that organizes the history, never a second source of truth.

Every derived entry carries two things beyond its summary:

* ``refs`` — enough identifiers to open the exact underlying row (message
  id and seq, event seq, objection id, plan revision, gate id, drop id,
  escalation id), so compression always remains inspectable; and
* ``layer`` — which stratum of the record it came from, preserved so later
  consumers (retrieval, the historian) can distinguish canonical transcript
  evidence from structured session records, deterministic test evidence,
  repository observations, bounded telemetry, and generated synthesis.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..storage import dao
from . import dropbox as dropbox_module
from . import planning as planning_module

if TYPE_CHECKING:  # pragma: no cover - imports for type checkers only
    from .manager import SessionManager, SessionRecord

#: Provenance strata. ``synthesis`` is reserved for historian output and is
#: never produced by this module — the derived timeline is organization, not
#: generation.
LAYER_TRANSCRIPT = "transcript"
LAYER_STRUCTURED = "structured"
LAYER_DETERMINISTIC = "deterministic"
LAYER_REPO = "repo"
LAYER_TELEMETRY = "telemetry"
LAYER_SYNTHESIS = "synthesis"

#: The deliberative vocabulary. Derived mechanically from typed records —
#: nothing here classifies prose.
KINDS = (
    "proposal",
    "revision",
    "objection",
    "counterproposal",
    "question",
    "evidence",
    "test_result",
    "failure",
    "resolution",
    "residual_concern",
    "acceptance",
    "rejection",
    "phase_block",
    "phase_completion",
)


def _clip(text, limit: int = 220) -> str:
    collapsed = " ".join(str(text or "").split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _entry(kind: str, at: str, actor, summary: str, layer: str, **refs) -> dict:
    return {
        "kind": kind,
        "at": at or "",
        "actor": actor,
        "summary": summary,
        "layer": layer,
        "refs": {key: value for key, value in refs.items() if value is not None},
    }


#: Planning milestone events worth surfacing; submission and waiver events
#: are skipped here because their rows carry the richer, text-bearing form.
_PLAN_EVENT_KINDS = {
    "session.plan_ready": "acceptance",
    "session.plan_criteria_refused": "rejection",
    "session.plan_budget_exhausted": "phase_block",
    "session.plan_budget_resumed": "resolution",
    "session.plan_reopened": "question",
    "session.plan_invalidated": "rejection",
    "session.plan_approved": "acceptance",
    "session.plan_promoted": "phase_completion",
}


def timeline(manager: "SessionManager", record: "SessionRecord", kinds=None) -> list[dict]:
    """The session's deliberative sequence, derived from the durable record.

    Sorted by time (event seq as the tiebreak). ``kinds`` filters to a
    subset of :data:`KINDS`.
    """
    entries: list[tuple[str, int, dict]] = []
    session_id = record.session_id

    def add(entry: dict, seq: int = 0) -> None:
        entries.append((entry["at"], seq, entry))

    # -- plan revisions: the proposals themselves -----------------------
    for revision in planning_module.revisions(manager, session_id):
        number = revision["revision"]
        summary = f"Plan revision {number} submitted"
        if revision.get("summary"):
            summary += f": {_clip(revision['summary'])}"
        add(_entry(
            "proposal" if number == 1 else "revision",
            revision["created_at"], revision["author"], summary,
            LAYER_STRUCTURED, revision=number,
        ))

    # -- objections and their settlements -------------------------------
    for objection in planning_module.objections(manager, session_id):
        kind = "counterproposal" if objection["classification"] == "fork" else "objection"
        add(_entry(
            kind, objection["created_at"], objection["raised_by"],
            f"{objection['objection_id']} [{objection['classification']}] "
            f"raised at revision {objection['raised_revision']}: {_clip(objection['text'])}",
            LAYER_STRUCTURED, objection_id=objection["objection_id"],
        ))
        if objection["status"] != "open":
            settled = (
                f"{objection['objection_id']} waived by the human"
                if objection["status"] == "waived"
                else f"{objection['objection_id']} resolved"
            )
            if objection.get("disposition"):
                settled += f": {_clip(objection['disposition'])}"
            add(_entry(
                "resolution", objection["updated_at"], objection.get("resolved_by"),
                settled, LAYER_STRUCTURED,
                objection_id=objection["objection_id"],
                revision=objection.get("resolved_revision"),
            ))

    # -- the event log: milestones, gates, conflicts, tests -------------
    for event in dao.list_events(manager.conn, record.room_id):
        payload = event.payload or {}
        event_type = event.event_type
        at = event.created_at
        seq = event.seq or 0
        actor = event.actor_name

        if event_type in _PLAN_EVENT_KINDS:
            kind = _PLAN_EVENT_KINDS[event_type]
            if event_type == "session.plan_ready":
                summary = (
                    f"Review closure: PLAN-READY at revision {payload.get('revision')}"
                )
                actor = payload.get("by") or actor
            elif event_type == "session.plan_criteria_refused":
                summary = f"Review closure refused: {_clip(payload.get('detail'))}"
            elif event_type == "session.plan_budget_exhausted":
                summary = f"Planning budget exhausted: {_clip(payload.get('detail'))}"
            elif event_type == "session.plan_budget_resumed":
                summary = "The human resumed the planning budget (once)"
                actor = "human"
            elif event_type == "session.plan_reopened":
                summary = f"The human requested plan changes: {_clip(payload.get('note'))}"
                actor = "human"
            elif event_type == "session.plan_invalidated":
                summary = f"PLAN-READY invalidated ({payload.get('reason', 'changed state')})"
            elif event_type == "session.plan_approved":
                summary = (
                    f"The human approved {payload.get('plan_id')} "
                    f"revision {payload.get('revision')}"
                )
                actor = "human"
            else:  # session.plan_promoted
                summary = (
                    f"{payload.get('plan_id')} promoted to coordination session "
                    f"{payload.get('coordination_session_id')}"
                )
            add(_entry(kind, at, actor, summary, LAYER_STRUCTURED, event_seq=seq), seq)

        elif event_type == "session.conflict":
            add(_entry(
                "objection", at, payload.get("participant") or actor,
                f"{payload.get('participant')} declined contract revision "
                f"{payload.get('revision')}: {_clip(payload.get('conflict'))}",
                LAYER_STRUCTURED, event_seq=seq,
            ), seq)

        elif event_type == "session.gate_updated" and not payload.get("added"):
            gate_id = payload.get("gate_id")
            status = payload.get("status")
            gate_actor = payload.get("actor") or actor
            if status == "fail":
                add(_entry(
                    "failure", at, gate_actor,
                    f"Gate {gate_id} reported failing",
                    LAYER_STRUCTURED, gate_id=gate_id, event_seq=seq,
                ), seq)
            elif status == "pass":
                add(_entry(
                    "acceptance", at, gate_actor,
                    f"Gate {gate_id} reported passing",
                    LAYER_STRUCTURED, gate_id=gate_id, event_seq=seq,
                ), seq)
            else:
                add(_entry(
                    "evidence", at, gate_actor,
                    f"Evidence recorded on gate {gate_id} ({status})",
                    LAYER_STRUCTURED, gate_id=gate_id, event_seq=seq,
                ), seq)

        elif event_type == "session.tests_run":
            green = bool(payload.get("green"))
            if payload.get("ran"):
                summary = (
                    f"Tests ran ({_clip(payload.get('command'), 80)}): "
                    f"{payload.get('passed', 0)} passed, {payload.get('failed', 0)} failed"
                )
            else:
                summary = f"Test run did not start: {_clip(payload.get('detail'))}"
            add(_entry(
                "test_result" if green else "failure", at, actor or "synchri",
                summary, LAYER_DETERMINISTIC, event_seq=seq,
            ), seq)

        elif event_type == "autonomy.limit_reached":
            add(_entry(
                "phase_block", at, None,
                "Autonomy limit reached; the room awaits the human",
                LAYER_STRUCTURED, event_seq=seq,
            ), seq)

        elif event_type == "session.completed":
            add(_entry(
                "phase_completion", at, actor,
                f"Session completed ({payload.get('commits', 0)} commit(s) delivered)",
                LAYER_STRUCTURED, event_seq=seq,
            ), seq)

    # -- the transcript: blocked turns and approval requests ------------
    for envelope in dao.list_messages(manager.conn, record.room_id):
        metadata = envelope.metadata or {}
        if metadata.get("approval_request"):
            add(_entry(
                "question", envelope.timestamp, envelope.sender,
                f"{envelope.sender} asked for approval: "
                f"{_clip(metadata['approval_request'])}",
                LAYER_TRANSCRIPT,
                message_id=envelope.message_id, seq=envelope.seq,
            ), envelope.seq)
        if envelope.response_status == "blocked":
            add(_entry(
                "phase_block", envelope.timestamp, envelope.sender,
                f"{envelope.sender} reported blocked: {_clip(envelope.content, 160)}",
                LAYER_TRANSCRIPT,
                message_id=envelope.message_id, seq=envelope.seq,
            ), envelope.seq)

    # -- side tasks: captured proposals and their dispositions ----------
    for drop in dropbox_module.items(manager, session_id):
        add(_entry(
            "proposal", drop["created_at"], None,
            f"Side task {drop['drop_id']} captured: "
            f"{_clip(drop.get('title') or drop.get('prompt'), 140)}",
            LAYER_STRUCTURED, drop_id=drop["drop_id"],
        ))
        disposition = drop.get("disposition")
        if disposition:
            kind = (
                "acceptance" if disposition in {"approved", "adopted", "accepted"}
                else "rejection" if disposition in {"rejected", "discarded"}
                else "resolution"
            )
            add(_entry(
                kind, drop["updated_at"], None,
                f"Side task {drop['drop_id']} {disposition}",
                LAYER_STRUCTURED, drop_id=drop["drop_id"],
            ))

    # -- escalations: questions to the human, and their answers ---------
    for escalation in manager.conn.execute(
        "SELECT * FROM session_escalations WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    ):
        row = dict(escalation)
        add(_entry(
            "question", row["created_at"], row.get("raised_by"),
            f"Escalation {row['rule']}: {_clip(row.get('detail'))}",
            LAYER_STRUCTURED, escalation_id=row["escalation_id"],
        ))
        if row.get("resolved_at"):
            add(_entry(
                "resolution", row["resolved_at"], "human",
                f"Escalation {row['rule']} resolved: {_clip(row.get('resolution'))}",
                LAYER_STRUCTURED, escalation_id=row["escalation_id"],
            ))

    # -- residual concerns: what stood unresolved at the threshold ------
    plan = planning_module.get_plan(manager, session_id)
    if plan is not None and plan["status"] == "approved":
        for objection in planning_module.objections(manager, session_id):
            if objection["status"] == "open":
                add(_entry(
                    "residual_concern", objection["updated_at"], objection["raised_by"],
                    f"{objection['objection_id']} [{objection['classification']}] "
                    f"remained open at approval: {_clip(objection['text'])}",
                    LAYER_STRUCTURED, objection_id=objection["objection_id"],
                ))
    for gate in manager.gates(session_id):
        if gate.status == "waived":
            add(_entry(
                "residual_concern", gate.updated_at or "", gate.updated_by,
                f"Gate {gate.gate_id} was waived: {_clip(gate.description, 140)}",
                LAYER_STRUCTURED, gate_id=gate.gate_id,
            ))
    if record.is_terminal:
        for escalation in manager.open_escalations(session_id):
            add(_entry(
                "residual_concern", record.ended_at or escalation["created_at"],
                escalation.get("raised_by"),
                f"Escalation {escalation['rule']} was still open at the end: "
                f"{_clip(escalation.get('detail'))}",
                LAYER_STRUCTURED, escalation_id=escalation["escalation_id"],
            ))

    entries.sort(key=lambda item: (item[0], item[1]))
    ordered = [entry for _at, _seq, entry in entries]
    if kinds:
        wanted = set(kinds)
        ordered = [entry for entry in ordered if entry["kind"] in wanted]
    return ordered
