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

import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from ..storage import dao, db
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


# ----------------------------------------------------------------------
# retrieval: the smallest historical evidence set sufficient to answer
# ----------------------------------------------------------------------

#: Indexed body text is clipped; excerpts are windows around the first match.
_BODY_LIMIT = 8_000
_EXCERPT_LIMIT = 700

_TOKEN = re.compile(r"[A-Za-z0-9_]{2,}")
_STOPWORDS = frozenset(
    "a an and are at be but by did do does for from had has have how i in is "
    "it its me my no not of on or our so that the them they this to us was we "
    "were what when which who why with you your".split()
)

#: Which stratum each indexed source belongs to — preserved on every result
#: so consumers can tell canonical transcript evidence from structured
#: records and bounded telemetry.
_SOURCE_LAYERS = {
    "message": LAYER_TRANSCRIPT,
    "stream": LAYER_TELEMETRY,
    "plan_revision": LAYER_STRUCTURED,
    "objection": LAYER_STRUCTURED,
    "gate": LAYER_STRUCTURED,
    "drop": LAYER_STRUCTURED,
    "memory": LAYER_STRUCTURED,
    "conflict": LAYER_STRUCTURED,
    "idea": LAYER_STRUCTURED,
}

#: Small mutable sources refreshed wholesale on every reindex; the large
#: append-only sources advance by watermark instead.
_MUTABLE_KINDS = ("objection", "gate", "drop", "memory", "conflict", "idea")


def reindex(manager: "SessionManager", record: "SessionRecord") -> None:
    """Bring the search index up to date for one session, lazily.

    Append-only sources (messages, stream telemetry, plan revision bodies)
    advance by per-source watermarks; small mutable sources (objections,
    gates, drops, the memory ledger, contract conflicts, the idea) are
    reindexed wholesale — they are tens of rows and they change in place.
    The index stores excerptable copies plus refs; the underlying rows stay
    the only source of truth.
    """
    conn = manager.conn
    room_id, session_id = record.room_id, record.session_id
    with db.transaction(conn):
        marks = {
            row["source"]: row["watermark"]
            for row in conn.execute(
                "SELECT source, watermark FROM search_state WHERE room_id = ?",
                (room_id,),
            )
        }

        def bump(source: str, watermark: int) -> None:
            conn.execute(
                "INSERT INTO search_state(room_id, source, watermark) VALUES (?,?,?) "
                "ON CONFLICT(room_id, source) DO UPDATE SET watermark = excluded.watermark",
                (room_id, source, watermark),
            )

        def put(body, kind: str, ref: str, actor, created_at) -> None:
            text = " ".join(str(body or "").split())
            if not text:
                return
            conn.execute(
                "INSERT INTO search_index (body, kind, room_id, session_id, ref, "
                "actor, created_at) VALUES (?,?,?,?,?,?,?)",
                (text[:_BODY_LIMIT], kind, room_id, session_id, ref,
                 actor, created_at or ""),
            )

        last = marks.get("message", 0)
        top = last
        for envelope in dao.list_messages(conn, room_id, since_seq=last):
            text = "\n".join(
                part for part in (
                    envelope.content, envelope.goal, envelope.claim, envelope.evidence
                ) if part
            )
            put(text, "message", f"message:{envelope.message_id}",
                envelope.sender, envelope.timestamp)
            top = max(top, envelope.seq or 0)
        if top != last:
            bump("message", top)

        last = marks.get("stream", 0)
        top = last
        for row in conn.execute(
            "SELECT event_id, participant, title, detail, file_path, created_at "
            "FROM agent_stream_events WHERE room_id = ? AND event_id > ? "
            "ORDER BY event_id",
            (room_id, last),
        ):
            text = " ".join(p for p in (row["title"], row["detail"], row["file_path"]) if p)
            put(text, "stream", f"stream:{row['event_id']}",
                row["participant"], row["created_at"])
            top = max(top, row["event_id"])
        if top != last:
            bump("stream", top)

        last = marks.get("plan_revision", 0)
        top = last
        for row in conn.execute(
            "SELECT revision, author, body, created_at FROM session_plan_revisions "
            "WHERE session_id = ? AND revision > ? ORDER BY revision",
            (session_id, last),
        ):
            put(row["body"], "plan_revision", f"revision:{row['revision']}",
                row["author"], row["created_at"])
            top = max(top, row["revision"])
        if top != last:
            bump("plan_revision", top)

        placeholders = ",".join("?" for _ in _MUTABLE_KINDS)
        conn.execute(
            f"DELETE FROM search_index WHERE session_id = ? AND kind IN ({placeholders})",
            (session_id, *_MUTABLE_KINDS),
        )
        plan = planning_module.get_plan(manager, session_id)
        if plan:
            put(plan["idea"], "idea", "idea:1", None, plan["created_at"])
        for objection in planning_module.objections(manager, session_id):
            text = objection["text"]
            if objection.get("disposition"):
                text += f"\ndisposition: {objection['disposition']}"
            put(text, "objection", f"objection:{objection['objection_id']}",
                objection["raised_by"], objection["created_at"])
        for gate in manager.gates(session_id):
            text = " ".join([
                gate.gate_id, gate.description or "",
                *gate.evidence, *gate.tests, *gate.commits,
                gate.builder_assessment or "", gate.reviewer_assessment or "",
            ])
            put(text, "gate", f"gate:{gate.gate_id}", gate.updated_by, gate.updated_at)
        for drop in dropbox_module.items(manager, session_id):
            text = " ".join(
                str(part) for part in (
                    drop.get("title"), drop.get("prompt"), drop.get("proposal"),
                    drop.get("review"), drop.get("failure_report"),
                ) if part
            )
            put(text, "drop", f"drop:{drop['drop_id']}", None, drop["created_at"])
        for row in conn.execute(
            "SELECT participant, conflict, raw_reply, created_at "
            "FROM session_acknowledgments WHERE session_id = ? AND accepted = 0",
            (session_id,),
        ):
            put(row["conflict"] or row["raw_reply"], "conflict",
                f"ack:{row['participant']}", row["participant"], row["created_at"])
        try:
            ledger_path = Path(manager.broker.workspace.memory_path(room_id))
            ledger_text = ledger_path.read_text(encoding="utf-8") if ledger_path.exists() else ""
        except Exception:  # pragma: no cover - the ledger is optional evidence
            ledger_text = ""
        if ledger_text.strip():
            put(ledger_text, "memory", "memory:ledger", None, "")


def _excerpt(body: str, tokens: list[str], limit: int = _EXCERPT_LIMIT) -> str:
    text = " ".join((body or "").split())
    if len(text) <= limit:
        return text
    lowered = text.lower()
    position = min(
        (lowered.find(token) for token in tokens if lowered.find(token) >= 0),
        default=0,
    )
    start = max(0, position - limit // 3)
    window = text[start:start + limit]
    prefix = "…" if start > 0 else ""
    suffix = "…" if start + limit < len(text) else ""
    return f"{prefix}{window}{suffix}"


def _question_tokens(question: str) -> list[str]:
    ordered = dict.fromkeys(match.group(0).lower() for match in _TOKEN.finditer(question or ""))
    return [token for token in ordered if token not in _STOPWORDS]


def search(
    manager: "SessionManager",
    question: str,
    session_id: str | None = None,
    *,
    limit: int = 8,
    char_budget: int = 10_000,
) -> dict:
    """Hybrid retrieval over the indexed record: lexical rank (bm25 where
    FTS5 exists, token counting otherwise), structured signals, and recency,
    fused with reciprocal ranks. Returns the smallest evidence set that
    fits the budget, each item carrying its ref and provenance layer, plus
    the timeline events overlapping the evidence window for scoped queries.
    """
    conn = manager.conn
    if session_id:
        records = [manager.get(session_id)]
        reindex(manager, records[0])
    else:
        records = manager.list_sessions()
        for record in records:
            try:
                reindex(manager, record)
            except Exception:  # pragma: no cover - one broken session must not sink the query
                continue

    engine = db.search_engine(conn)
    tokens = _question_tokens(question)
    if not tokens:
        return {"evidence": [], "events": [], "engine": engine, "tokens": []}

    scope_sql = " AND session_id = ?" if session_id else ""
    scope_args: list = [session_id] if session_id else []
    hits: list[dict] = []
    if engine == "fts5":
        # Raw questions carry MATCH metacharacters; the query is rebuilt from
        # word tokens only, each quoted, with prefix expansion.
        match = " OR ".join(f'"{token}"*' for token in tokens)
        try:
            hits = [dict(row) for row in conn.execute(
                "SELECT body, kind, room_id, session_id, ref, actor, created_at, "
                f"bm25(search_index) AS rank FROM search_index "
                f"WHERE search_index MATCH ?{scope_sql} ORDER BY rank LIMIT 80",
                (match, *scope_args),
            )]
        except sqlite3.OperationalError:
            engine = "like"
    if engine == "like":
        clauses = " OR ".join("body LIKE ? COLLATE NOCASE" for _ in tokens)
        hits = [dict(row) for row in conn.execute(
            f"SELECT body, kind, room_id, session_id, ref, actor, created_at "
            f"FROM search_index WHERE ({clauses}){scope_sql} LIMIT 200",
            (*[f"%{token}%" for token in tokens], *scope_args),
        )]

    for hit in hits:
        lowered = hit["body"].lower()
        hit["matches"] = sum(1 for token in tokens if token in lowered)
        actor = (hit.get("actor") or "").lower()
        hit["structured"] = (
            (2 if hit["kind"] not in {"message", "stream"} else 0)
            + (1 if actor and actor in tokens else 0)
        )

    lexical = sorted(
        hits,
        key=lambda hit: hit.get("rank", 0.0) if engine == "fts5" else -hit["matches"],
    )
    structured = sorted(hits, key=lambda hit: (-hit["structured"], hit.get("created_at") or ""))
    recency = sorted(hits, key=lambda hit: hit.get("created_at") or "", reverse=True)
    fused: dict[str, float] = {}
    for ranking in (lexical, structured, recency):
        for position, hit in enumerate(ranking):
            fused[hit["ref"]] = fused.get(hit["ref"], 0.0) + 1.0 / (60 + position)
    by_ref = {hit["ref"]: hit for hit in hits}
    ranked = sorted(by_ref.values(), key=lambda hit: fused[hit["ref"]], reverse=True)

    evidence: list[dict] = []
    seen: set[str] = set()
    used = 0

    def append(hit: dict, *, context: bool = False) -> None:
        nonlocal used
        if hit["ref"] in seen:
            return
        excerpt = _excerpt(hit["body"], tokens)
        entry = {
            "ref": hit["ref"],
            "kind": hit["kind"],
            "layer": _SOURCE_LAYERS.get(hit["kind"], LAYER_STRUCTURED),
            "actor": hit.get("actor"),
            "at": hit.get("created_at") or "",
            "session_id": hit["session_id"],
            "excerpt": excerpt,
        }
        if context:
            entry["context"] = True
        evidence.append(entry)
        seen.add(hit["ref"])
        used += len(excerpt)

    for hit in ranked:
        if len([e for e in evidence if not e.get("context")]) >= limit or used >= char_budget:
            break
        append(hit)
        # Thread expansion: a message hit brings the message it replied to,
        # so proposal-and-answer pairs arrive together.
        if hit["kind"] == "message" and used < char_budget:
            message_id = hit["ref"].split(":", 1)[1]
            envelope = dao.get_message(conn, hit["room_id"], message_id)
            if envelope and envelope.in_reply_to:
                parent = dao.get_message(conn, hit["room_id"], envelope.in_reply_to)
                if parent and parent.content:
                    append({
                        "ref": f"message:{parent.message_id}", "kind": "message",
                        "room_id": hit["room_id"], "session_id": hit["session_id"],
                        "actor": parent.sender, "created_at": parent.timestamp,
                        "body": parent.content,
                    }, context=True)

    events: list[dict] = []
    if session_id and evidence:
        window = [entry["at"] for entry in evidence if entry["at"]]
        if window:
            low, high = min(window), max(window)
            events = [
                entry for entry in timeline(manager, records[0])
                if low <= entry["at"] <= high
            ][:60]
    return {"evidence": evidence, "events": events, "engine": engine, "tokens": tokens}
