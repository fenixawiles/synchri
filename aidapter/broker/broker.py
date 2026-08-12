"""The AIDapter broker.

This is the whole orchestration surface.  It is a plain Python class over a
SQLite file, deliberately *not* a server: every CLI invocation constructs a
``Broker``, performs one atomic operation, and exits.  A future HTTP/WebSocket
façade or MCP adapter wraps this same class without changing any of it.

Layering rules enforced here:

* orchestration state -> SQLite (authoritative)
* semantic memory     -> per-room Markdown ledger
* transcript          -> ``messages`` table, mirrored to JSONL

Filesystem side effects are deferred until after the database transaction
commits, so a rolled-back operation never leaves a stray ledger entry or
transcript line behind.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Callable, Iterable

from ..config import DEFAULT_MAX_CONSECUTIVE_AGENT_TURNS, Workspace, resolve_workspace
from ..errors import (
    AuthError,
    ConflictError,
    NotFoundError,
    StateError,
    TurnError,
    ValidationError,
)
from ..ids import NAME_PATTERN, is_valid_id, new_id, new_secret, utc_now
from ..memory.ledger import LedgerStore
from ..models.entities import Participant, Room, Turn
from ..models.enums import (
    MessageType,
    ParticipantKind,
    ParticipantStatus,
    Priority,
    QueueStatus,
    ResponseStatus,
    RoomStatus,
    TaskStatus,
    TurnKind,
    TurnState,
    TurnStatus,
)
from ..models.envelope import MessageDraft, MessageEnvelope
from ..protocol import events as ev
from ..queue import scheduler
from ..security import tokens
from ..storage import dao, db, transcript

#: How stale a participant's heartbeat may get before a poll refreshes it.
#: Keeps ``wait`` loops from taking the write lock on every single poll.
HEARTBEAT_INTERVAL_SECONDS = 5.0

#: States that end a ``wait`` loop.
TERMINAL_WAIT_STATES = {
    TurnState.YOUR_TURN.value,
    TurnState.AWAITING_HUMAN.value,
    TurnState.STOPPED.value,
    TurnState.REMOVED.value,
}


class Broker:
    """Room orchestration over a local SQLite workspace."""

    def __init__(self, workspace: Workspace | str | None = None) -> None:
        self.workspace = (
            workspace if isinstance(workspace, Workspace) else resolve_workspace(workspace)
        )
        self.workspace.ensure()
        self.conn: sqlite3.Connection = db.connect(self.workspace.db_path)
        db.initialize(self.conn)
        self.ledger = LedgerStore(self.workspace)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Broker":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------
    # rooms
    # ------------------------------------------------------------------

    def create_room(
        self,
        name: str,
        *,
        human_name: str = "human",
        goal: str | None = None,
        max_consecutive_agent_turns: int = DEFAULT_MAX_CONSECUTIVE_AGENT_TURNS,
    ) -> dict:
        """Create a room and its owning human participant.

        Returns the join token and the human's participant secret.  Both are
        shown exactly once; only salted hashes are stored.
        """
        name = _require_text(name, "room name", max_len=200)
        _validate_name(human_name)
        if not isinstance(max_consecutive_agent_turns, int) or max_consecutive_agent_turns < 1:
            raise ValidationError("max-agent-turns must be a positive integer")

        room_id = new_id("room")
        join_secret = new_secret()
        salt = tokens.new_salt()
        now = utc_now()

        with db.transaction(self.conn):
            dao.insert_room(
                self.conn,
                room_id=room_id,
                name=name,
                status=RoomStatus.ACTIVE.value,
                created_at=now,
                updated_at=now,
                token_salt=salt,
                token_hash=tokens.hash_secret(join_secret, salt),
                seq=0,
                consecutive_agent_turns=0,
                max_consecutive_agent_turns=max_consecutive_agent_turns,
                awaiting_human=0,
            )
            human = self._insert_participant(room_id, human_name, ParticipantKind.HUMAN)
            dao.update_room(self.conn, room_id, owner_participant_id=human["participant_id"])
            self._log(
                room_id,
                ev.ROOM_CREATED,
                actor_participant_id=human["participant_id"],
                actor_name=human_name,
                payload={
                    "name": name,
                    "max_consecutive_agent_turns": max_consecutive_agent_turns,
                    "goal": goal,
                },
            )

        memory_path = self.ledger.initialize(room_id, name, goal=goal)
        self.workspace.ensure_room_dir(room_id)
        return {
            "room_id": room_id,
            "name": name,
            "join_token": tokens.compose_join_token(room_id, join_secret),
            "human": human,
            "max_consecutive_agent_turns": max_consecutive_agent_turns,
            "memory_path": str(memory_path),
            "transcript_path": str(self.workspace.transcript_path(room_id)),
        }

    def list_rooms(self) -> list[dict]:
        return [
            {
                **room.to_dict(),
                "participants": [
                    p.name for p in dao.list_participants(self.conn, room.room_id, False)
                ],
            }
            for room in dao.list_rooms(self.conn)
        ]

    def room_status(self, room_id: str, *, credential: "Credential") -> dict:
        room = self._get_room(room_id)
        self._authorize_read(room, credential)
        active_turn = dao.get_active_turn(self.conn, room_id)
        participants = dao.list_participants(self.conn, room_id)
        return {
            "room": room.to_dict(),
            "active_speaker": self._name_of(room_id, room.active_participant_id),
            "active_turn": active_turn.to_dict() if active_turn else None,
            "queue": scheduler.queue_snapshot(self.conn, room_id),
            "participants": [p.to_dict() for p in participants],
            "open_tasks": [
                t.to_dict() for t in dao.list_tasks(self.conn, room_id, TaskStatus.OPEN.value)
            ],
            "memory_path": str(self.workspace.memory_path(room_id)),
            "transcript_path": str(self.workspace.transcript_path(room_id)),
            "autonomy": {
                "consecutive_agent_turns": room.consecutive_agent_turns,
                "max_consecutive_agent_turns": room.max_consecutive_agent_turns,
                "awaiting_human": room.awaiting_human,
            },
        }

    # ------------------------------------------------------------------
    # participants
    # ------------------------------------------------------------------

    def join(
        self,
        room_reference: str,
        name: str,
        *,
        token: str | None = None,
        kind: str = ParticipantKind.AGENT.value,
        rejoin_secret: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Join a room, or re-attach an existing identity with ``rejoin_secret``.

        ``room_reference`` may be a bare room id (with ``token`` supplied
        separately) or a compound join token that carries both.
        """
        _validate_name(name)
        if kind not in {k.value for k in ParticipantKind}:
            raise ValidationError(f"kind must be 'agent' or 'human', got {kind!r}")

        room_id, embedded_secret = self._resolve_room_reference(room_reference)
        join_secret = embedded_secret or token
        if not join_secret:
            raise AuthError("a join token is required to join a room")

        with db.transaction(self.conn):
            room = self._get_room(room_id)
            if room.is_stopped:
                raise StateError("room is stopped; it cannot accept new participants")
            salt_hash = dao.get_room_token_hash(self.conn, room_id)
            if salt_hash is None or not tokens.verify_secret(join_secret, *salt_hash):
                # Same error for "bad token" and "wrong room" on purpose.
                raise AuthError("invalid join token for this room")

            existing = dao.get_participant_by_name(self.conn, room_id, name)
            if existing is not None:
                return self._rejoin(room, existing, rejoin_secret, metadata)

            participant = self._insert_participant(
                room_id, name, ParticipantKind(kind), metadata=metadata
            )
            self._log(
                room_id,
                ev.PARTICIPANT_JOINED,
                actor_participant_id=participant["participant_id"],
                actor_name=name,
                payload={"kind": kind},
            )
            return {
                **participant,
                "room_id": room_id,
                "room_name": room.name,
                "rejoined": False,
                "memory_path": str(self.workspace.memory_path(room_id)),
            }

    def _rejoin(
        self,
        room: Room,
        existing: Participant,
        rejoin_secret: str | None,
        metadata: dict | None,
    ) -> dict:
        """Re-attach to an identity that already exists in this room."""
        if existing.status == ParticipantStatus.REMOVED.value:
            raise ConflictError(
                f"participant {existing.name!r} was removed from this room and cannot rejoin; "
                "pick a different name",
                code="participant_removed",
            )
        if not rejoin_secret:
            raise ConflictError(
                f"participant {existing.name!r} already exists in this room; "
                "pass the existing participant secret to re-attach",
                code="duplicate_participant",
            )
        stored = dao.get_participant_secret(self.conn, room.room_id, existing.participant_id)
        if stored is None or not tokens.verify_secret(rejoin_secret, *stored):
            raise AuthError("invalid participant secret")
        dao.update_participant(
            self.conn,
            room.room_id,
            existing.participant_id,
            last_seen_at=utc_now(),
            **({"metadata": _json(metadata)} if metadata is not None else {}),
        )
        self._log(
            room.room_id,
            ev.PARTICIPANT_REJOINED,
            actor_participant_id=existing.participant_id,
            actor_name=existing.name,
        )
        return {
            "participant_id": existing.participant_id,
            "name": existing.name,
            "kind": existing.kind,
            # No new secret: re-attaching proves you already hold it.
            "secret": rejoin_secret,
            "room_id": room.room_id,
            "room_name": room.name,
            "rejoined": True,
            "memory_path": str(self.workspace.memory_path(room.room_id)),
        }

    def list_participants(self, room_id: str, *, credential: "Credential") -> list[dict]:
        room = self._get_room(room_id)
        self._authorize_read(room, credential)
        return [p.to_dict() for p in dao.list_participants(self.conn, room_id)]

    def remove_participant(
        self, room_id: str, target_name: str, *, credential: "Credential"
    ) -> dict:
        """Remove a participant.  Human authority only, and immediately effective."""
        with db.transaction(self.conn):
            room = self._get_room(room_id)
            actor = self._authorize_actor(room, credential, require_human=True)
            self._require_mutable(room)
            target = self._resolve_participant(room_id, target_name)
            if target.status == ParticipantStatus.REMOVED.value:
                raise ConflictError(f"participant {target.name!r} is already removed")
            if target.participant_id == room.owner_participant_id:
                raise ValidationError("the room owner cannot be removed; stop the room instead")

            dao.update_participant(
                self.conn,
                room_id,
                target.participant_id,
                status=ParticipantStatus.REMOVED.value,
                removed_at=utc_now(),
            )
            active_turn = dao.get_active_turn(self.conn, room_id)
            if active_turn and active_turn.participant_id == target.participant_id:
                scheduler.end_turn(
                    self.conn,
                    room_id,
                    active_turn.turn_id,
                    TurnStatus.CANCELLED,
                    queue_status=QueueStatus.CANCELLED,
                )
            scheduler.dequeue(self.conn, room_id, target.participant_id, QueueStatus.CANCELLED)
            self._log(
                room_id,
                ev.PARTICIPANT_REMOVED,
                actor_participant_id=actor.participant_id,
                actor_name=actor.name,
                payload={"participant": target.name, "participant_id": target.participant_id},
            )
            self._activate_next(room_id)
            return {"removed": target.name, "participant_id": target.participant_id}

    # ------------------------------------------------------------------
    # messaging
    # ------------------------------------------------------------------

    def send(
        self,
        room_id: str,
        *,
        credential: "Credential",
        draft: MessageDraft,
        hold_floor: bool = False,
    ) -> dict:
        """Append a message and advance the queue.

        Agents may only speak when they hold the floor.  Humans always may, and
        a human message interrupts whatever is in flight.
        """
        draft.validate()
        after_commit: list[Callable[[], None]] = []

        with db.transaction(self.conn):
            room = self._get_room(room_id)
            actor = self._authorize_actor(room, credential)
            self._require_sendable(room, actor)

            target = self._resolve_addressee(room_id, draft.target, actor, "target")
            handoff = self._resolve_addressee(room_id, draft.handoff_target, actor, "handoff-to")

            if actor.is_human:
                # A human does not take a turn, so there is no turn to hand off
                # or to hold.  Rejecting is better than accepting the flag and
                # quietly dropping it.
                if handoff is not None:
                    raise ValidationError(
                        "a human message cannot hand off; use --to to direct the room instead"
                    )
                if hold_floor:
                    raise ValidationError(
                        "--hold applies to agent turns; a human may speak at any time"
                    )
                result = self._send_as_human(room, actor, draft, target, after_commit)
            else:
                result = self._send_as_agent(room, actor, draft, target, handoff, hold_floor, after_commit)

        warnings = _run_after_commit(after_commit)
        result["queue"] = scheduler.queue_snapshot(self.conn, room_id)
        result["next_speaker"] = self._name_of(
            room_id, (dao.get_room(self.conn, room_id) or room).active_participant_id
        )
        if warnings:
            result["warnings"] = warnings
        return result

    def _send_as_human(
        self,
        room: Room,
        actor: Participant,
        draft: MessageDraft,
        target: Participant | None,
        after_commit: list,
    ) -> dict:
        """Human message: always interrupts, then recalculates the sequence.

        Two shapes, and the difference matters:

        * **targeted** — a redirect.  The waiting queue is stale relative to the
          new instruction, so it is cleared and the addressee is promoted.
        * **untargeted** — a comment.  The queue survives, and whoever was
          interrupted is re-enqueued at direct-address priority so they can
          finish what they were asked to do.
        """
        room_id = room.room_id
        active_turn = dao.get_active_turn(self.conn, room_id)
        interrupted: Turn | None = None

        if active_turn is not None:
            scheduler.end_turn(
                self.conn,
                room_id,
                active_turn.turn_id,
                TurnStatus.INTERRUPTED,
                queue_status=QueueStatus.CANCELLED,
            )
            interrupted = active_turn
            self._log(
                room_id,
                ev.TURN_INTERRUPTED,
                actor_participant_id=actor.participant_id,
                actor_name=actor.name,
                payload={
                    "turn_id": active_turn.turn_id,
                    "interrupted_participant": active_turn.participant_name,
                    "was_blocking": active_turn.blocking,
                },
            )

        cleared = 0
        if target is not None:
            cleared = scheduler.clear_queue(self.conn, room_id)
            if cleared:
                self._log(
                    room_id,
                    ev.QUEUE_CLEARED,
                    actor_participant_id=actor.participant_id,
                    actor_name=actor.name,
                    payload={"cleared_entries": cleared, "reason": "human_redirect"},
                )

        overrode = interrupted is not None or cleared > 0
        envelope = self._append_message(
            room_id,
            actor,
            draft,
            target=target,
            handoff=None,
            turn_id=None,
            human_override=overrode,
            after_commit=after_commit,
        )

        scheduler.reset_agent_turn_budget(self.conn, room_id)
        self._log(
            room_id,
            ev.HUMAN_INTERRUPT,
            actor_participant_id=actor.participant_id,
            actor_name=actor.name,
            payload={
                "message_id": envelope.message_id,
                "target": target.name if target else None,
                "interrupted_turn": interrupted.turn_id if interrupted else None,
                "cleared_entries": cleared,
            },
        )

        task = self._open_task_if_needed(room_id, actor, draft, target, envelope)
        if target is not None:
            self._promote(
                room_id,
                target,
                Priority.DIRECT_ADDRESS,
                blocking=True,
                reason="human_direct_address",
                task_id=task,
                source_message_id=envelope.message_id,
                actor=actor,
            )
        elif interrupted is not None:
            # Untargeted comment: let the interrupted speaker resume next.
            resumed = dao.get_participant(self.conn, room_id, interrupted.participant_id)
            if resumed is not None and resumed.is_active:
                self._promote(
                    room_id,
                    resumed,
                    Priority.DIRECT_ADDRESS,
                    blocking=interrupted.blocking,
                    reason="resume_after_human_comment",
                    task_id=interrupted.task_id,
                    source_message_id=interrupted.request_message_id,
                    actor=actor,
                )

        self._activate_next(room_id)
        return {
            "message": envelope.to_dict(),
            "interrupted": interrupted.to_dict() if interrupted else None,
            "cleared_queue_entries": cleared,
            "task_id": task,
        }

    def _send_as_agent(
        self,
        room: Room,
        actor: Participant,
        draft: MessageDraft,
        target: Participant | None,
        handoff: Participant | None,
        hold_floor: bool,
        after_commit: list,
    ) -> dict:
        room_id = room.room_id
        turn = self._acquire_floor(room, actor)

        envelope = self._append_message(
            room_id,
            actor,
            draft,
            target=target,
            handoff=handoff,
            turn_id=turn.turn_id,
            human_override=False,
            after_commit=after_commit,
        )

        task_id = self._open_task_if_needed(room_id, actor, draft, target, envelope)
        self._close_task_if_needed(room_id, actor, draft, envelope)

        budget_exhausted = False
        if not hold_floor:
            scheduler.end_turn(
                self.conn,
                room_id,
                turn.turn_id,
                TurnStatus.COMPLETED,
                response_message_id=envelope.message_id,
                handoff_target=handoff.name if handoff else None,
            )
            self._log(
                room_id,
                ev.TURN_COMPLETED,
                actor_participant_id=actor.participant_id,
                actor_name=actor.name,
                payload={
                    "turn_id": turn.turn_id,
                    "message_id": envelope.message_id,
                    "handoff_target": handoff.name if handoff else None,
                },
            )
            budget_exhausted = scheduler.record_agent_turn(self.conn, room_id)
            if budget_exhausted:
                self._log(
                    room_id,
                    ev.AUTONOMY_LIMIT_REACHED,
                    payload={"max_consecutive_agent_turns": room.max_consecutive_agent_turns},
                )

        if target is not None:
            self._promote(
                room_id,
                target,
                Priority.DIRECT_ADDRESS,
                blocking=True,
                reason="direct_address",
                task_id=task_id,
                source_message_id=envelope.message_id,
                actor=actor,
            )
        elif handoff is not None:
            self._promote(
                room_id,
                handoff,
                Priority.HANDOFF,
                blocking=True,
                reason="handoff",
                task_id=draft.task_id,
                source_message_id=envelope.message_id,
                actor=actor,
            )
            self._log(
                room_id,
                ev.HANDOFF_RECORDED,
                actor_participant_id=actor.participant_id,
                actor_name=actor.name,
                payload={
                    "from": actor.name,
                    "to": handoff.name,
                    "message_id": envelope.message_id,
                },
            )
            after_commit.append(
                _ledger_writer(
                    self.ledger,
                    room_id,
                    room.name,
                    "Recent Handoffs",
                    f"{actor.name} → {handoff.name}: {_summarize(draft.content)}",
                    actor.name,
                    envelope.message_id,
                )
            )

        self._activate_next(room_id)
        return {
            "message": envelope.to_dict(),
            "turn_id": turn.turn_id,
            "floor_held": hold_floor,
            "task_id": task_id,
            "awaiting_human": budget_exhausted,
        }

    def pass_turn(self, room_id: str, *, credential: "Credential", reason: str | None = None) -> dict:
        """Decline to add anything material, and release the floor.

        A pass is a real, attributed message — silence in a shared room is
        ambiguous, and the other agents need to know the difference between
        "nothing to add" and "still working".
        """
        after_commit: list[Callable[[], None]] = []
        with db.transaction(self.conn):
            room = self._get_room(room_id)
            actor = self._authorize_actor(room, credential)
            self._require_sendable(room, actor)

            active_turn = dao.get_active_turn(self.conn, room_id)
            holds_floor = active_turn is not None and active_turn.participant_id == actor.participant_id
            entry = dao.get_live_queue_entry(self.conn, room_id, actor.participant_id)
            if not holds_floor and entry is None:
                raise StateError(
                    f"{actor.name} has neither the floor nor a queued turn to pass",
                    code="nothing_to_pass",
                )

            draft = MessageDraft(
                content=(reason or "").strip(),
                message_type=MessageType.PASS.value,
                response_status=ResponseStatus.DECLINED.value,
                task_id=active_turn.task_id if holds_floor and active_turn else None,
            ).validate()
            envelope = self._append_message(
                room_id,
                actor,
                draft,
                target=None,
                handoff=None,
                turn_id=active_turn.turn_id if holds_floor and active_turn else None,
                human_override=False,
                after_commit=after_commit,
            )

            if holds_floor and active_turn is not None:
                scheduler.end_turn(
                    self.conn,
                    room_id,
                    active_turn.turn_id,
                    TurnStatus.PASSED,
                    response_message_id=envelope.message_id,
                )
                if active_turn.task_id:
                    dao.update_task(
                        self.conn,
                        room_id,
                        active_turn.task_id,
                        status=TaskStatus.PASSED.value,
                        completed_at=utc_now(),
                        response_message_id=envelope.message_id,
                    )
                self._log(
                    room_id,
                    ev.TURN_PASSED,
                    actor_participant_id=actor.participant_id,
                    actor_name=actor.name,
                    payload={"turn_id": active_turn.turn_id, "reason": reason},
                )
                if not actor.is_human:
                    # Passes count against the budget too, otherwise two agents
                    # can pass at each other indefinitely.
                    scheduler.record_agent_turn(self.conn, room_id)
            else:
                scheduler.dequeue(self.conn, room_id, actor.participant_id, QueueStatus.DONE)
                self._log(
                    room_id,
                    ev.MESSAGE_PASSED,
                    actor_participant_id=actor.participant_id,
                    actor_name=actor.name,
                    payload={"reason": reason, "gave_up_queue_slot": True},
                )

            self._activate_next(room_id)
            result = {
                "message": envelope.to_dict(),
                "held_floor": holds_floor,
            }

        warnings = _run_after_commit(after_commit)
        result["queue"] = scheduler.queue_snapshot(self.conn, room_id)
        room_now = dao.get_room(self.conn, room_id)
        result["next_speaker"] = self._name_of(
            room_id, room_now.active_participant_id if room_now else None
        )
        if warnings:
            result["warnings"] = warnings
        return result

    def request_floor(
        self, room_id: str, *, credential: "Credential", priority: str = "queued"
    ) -> dict:
        """Ask for a turn without being addressed (priority 4 or 5)."""
        try:
            bucket = Priority.from_name(priority)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        if bucket not in (Priority.QUEUED, Priority.OPTIONAL):
            raise ValidationError(
                "request-floor accepts 'queued' or 'optional'; direct address and handoff "
                "priorities are assigned by the broker, not requested"
            )
        with db.transaction(self.conn):
            room = self._get_room(room_id)
            actor = self._authorize_actor(room, credential)
            self._require_sendable(room, actor)
            seq = dao.next_seq(self.conn, room_id)
            scheduler.enqueue(
                self.conn,
                room_id,
                actor.participant_id,
                bucket,
                seq=seq,
                blocking=False,
                reason=f"self_requested:{bucket.name.lower()}",
            )
            self._log(
                room_id,
                ev.QUEUE_FLOOR_REQUESTED,
                actor_participant_id=actor.participant_id,
                actor_name=actor.name,
                payload={"priority": bucket.name.lower()},
            )
            self._activate_next(room_id)
        return {
            "participant": credential.participant,
            "priority": bucket.name.lower(),
            "queue": scheduler.queue_snapshot(self.conn, room_id),
        }

    # ------------------------------------------------------------------
    # turn inspection
    # ------------------------------------------------------------------

    def turn_status(self, room_id: str, *, credential: "Credential") -> dict:
        """What should this participant do right now?"""
        room = self._get_room(room_id)
        actor = self._authorize_actor(room, credential, allow_removed=True)
        self._heartbeat(room_id, actor)

        active_turn = dao.get_active_turn(self.conn, room_id)
        queue = scheduler.queue_snapshot(self.conn, room_id)
        state, detail = self._classify_turn_state(room, actor, active_turn, queue)

        payload = {
            "state": state,
            "participant": actor.name,
            "participant_id": actor.participant_id,
            "room_id": room_id,
            "room_status": room.status,
            "active_speaker": self._name_of(room_id, room.active_participant_id),
            "queue": queue,
            "awaiting_human": room.awaiting_human,
            "consecutive_agent_turns": room.consecutive_agent_turns,
            "max_consecutive_agent_turns": room.max_consecutive_agent_turns,
            **detail,
        }
        if state == TurnState.YOUR_TURN.value and active_turn is not None:
            payload["turn_id"] = active_turn.turn_id
            payload["blocking"] = active_turn.blocking
            payload["task_id"] = active_turn.task_id
            if active_turn.request_message_id:
                request = dao.get_message(self.conn, room_id, active_turn.request_message_id)
                payload["request"] = request.to_dict() if request else None
        return payload

    def _classify_turn_state(
        self, room: Room, actor: Participant, active_turn: Turn | None, queue: list[dict]
    ) -> tuple[str, dict]:
        if not actor.is_active:
            return TurnState.REMOVED.value, {}
        if room.is_stopped:
            return TurnState.STOPPED.value, {}
        if active_turn is not None and active_turn.participant_id == actor.participant_id:
            return TurnState.YOUR_TURN.value, {}
        if actor.is_human:
            # The human outranks the queue and may speak at any time.
            return TurnState.YOUR_TURN.value, {"note": "human priority: you may speak at any time"}
        if room.status == RoomStatus.PAUSED.value:
            return TurnState.PAUSED.value, {}
        if room.awaiting_human:
            return TurnState.AWAITING_HUMAN.value, {}
        if active_turn is not None:
            return TurnState.BLOCKED.value, {
                "blocked_by": active_turn.participant_name,
                "blocking_turn": active_turn.blocking,
            }
        for item in queue:
            if item["participant_id"] == actor.participant_id:
                return TurnState.WAITING.value, {"position": item["position"]}
        return TurnState.IDLE.value, {}

    def wait_for_turn(
        self,
        room_id: str,
        *,
        credential: "Credential",
        timeout: float = 300.0,
        poll_interval: float = 0.5,
    ) -> dict:
        """Block until this participant may act, or until ``timeout`` elapses.

        Polling rather than push is a deliberate v0.1 decision: it keeps the
        broker daemonless.  See ``docs/architecture.md``.
        """
        if poll_interval <= 0:
            raise ValidationError("poll interval must be positive")
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            status = self.turn_status(room_id, credential=credential)
            if status["state"] in TERMINAL_WAIT_STATES:
                status["timed_out"] = False
                return status
            if time.monotonic() >= deadline:
                status["timed_out"] = True
                return status
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic()) or poll_interval))

    # ------------------------------------------------------------------
    # transcript
    # ------------------------------------------------------------------

    def read(
        self,
        room_id: str,
        *,
        credential: "Credential",
        since_seq: int = 0,
        limit: int | None = None,
        tail: int | None = None,
    ) -> dict:
        room = self._get_room(room_id)
        self._authorize_read(room, credential)
        if tail:
            messages = dao.list_recent_messages(self.conn, room_id, tail)
        else:
            messages = dao.list_messages(self.conn, room_id, since_seq=since_seq, limit=limit)
        return {
            "room_id": room_id,
            "room_name": room.name,
            "room_status": room.status,
            "messages": [m.to_dict() for m in messages],
            "latest_seq": room.seq,
        }

    def events(
        self, room_id: str, *, credential: "Credential", since_seq: int = 0, limit: int | None = None
    ) -> dict:
        room = self._get_room(room_id)
        self._authorize_read(room, credential)
        return {
            "room_id": room_id,
            "events": [e.to_dict() for e in dao.list_events(self.conn, room_id, since_seq, limit)],
            "latest_seq": room.seq,
        }

    def provenance(self, room_id: str, message_id: str, *, credential: "Credential") -> dict:
        """Full provenance record for one message."""
        room = self._get_room(room_id)
        self._authorize_read(room, credential)
        message = dao.get_message(self.conn, room_id, message_id)
        if message is None:
            raise NotFoundError(f"no message {message_id!r} in room {room_id}")
        turn = dao.get_turn(self.conn, room_id, message.turn_id) if message.turn_id else None
        task = dao.get_task(self.conn, room_id, message.task_id) if message.task_id else None
        replied_to = (
            dao.get_message(self.conn, room_id, message.in_reply_to) if message.in_reply_to else None
        )
        return {
            "message": message.to_dict(),
            "turn": turn.to_dict() if turn else None,
            "task": task.to_dict() if task else None,
            "in_reply_to": replied_to.to_dict() if replied_to else None,
        }

    def export(self, room_id: str, *, credential: "Credential") -> dict:
        """Rebuild on-disk artifacts from authoritative state."""
        room = self._get_room(room_id)
        self._authorize_read(room, credential)
        path = transcript.rebuild(self.workspace, self.conn, room_id)
        return {
            "room_id": room_id,
            "transcript_path": str(path),
            "memory_path": str(self.workspace.memory_path(room_id)),
            "message_count": len(dao.list_messages(self.conn, room_id)),
        }

    # ------------------------------------------------------------------
    # semantic memory
    # ------------------------------------------------------------------

    def memory_show(self, room_id: str, *, credential: "Credential") -> dict:
        room = self._get_room(room_id)
        self._authorize_read(room, credential)
        ledger = self.ledger.load(room_id, room.name)
        return {
            "room_id": room_id,
            "path": str(self.ledger.path(room_id)),
            "memory": ledger.to_dict(),
            "markdown": self.ledger.path(room_id).read_text(encoding="utf-8")
            if self.ledger.path(room_id).exists()
            else "",
        }

    def memory_write(
        self,
        room_id: str,
        *,
        credential: "Credential",
        section: str,
        value: str,
        mode: str = "add",
        message_id: str | None = None,
    ) -> dict:
        """Update the shared ledger.

        The write happens while holding the room's database write lock, which
        serializes concurrent ledger edits between processes even though the
        ledger itself is a file.
        """
        with db.transaction(self.conn):
            room = self._get_room(room_id)
            actor = self._authorize_actor(room, credential)
            self._require_mutable(room)
            if mode == "set":
                self.ledger.set_value(
                    room_id, section, value, actor=actor.name, room_name=room.name
                )
            elif mode == "add":
                self.ledger.add_entry(
                    room_id,
                    section,
                    value,
                    actor=actor.name,
                    message_id=message_id,
                    room_name=room.name,
                )
            else:
                raise ValidationError("mode must be 'set' or 'add'")
            self._log(
                room_id,
                ev.MEMORY_UPDATED,
                actor_participant_id=actor.participant_id,
                actor_name=actor.name,
                payload={"section": section, "mode": mode, "message_id": message_id},
            )
        return self.memory_show(room_id, credential=credential)

    def memory_remove(
        self, room_id: str, *, credential: "Credential", section: str, index: int
    ) -> dict:
        with db.transaction(self.conn):
            room = self._get_room(room_id)
            actor = self._authorize_actor(room, credential)
            self._require_mutable(room)
            self.ledger.remove_entry(room_id, section, index, room_name=room.name)
            self._log(
                room_id,
                ev.MEMORY_UPDATED,
                actor_participant_id=actor.participant_id,
                actor_name=actor.name,
                payload={"section": section, "mode": "remove", "index": index},
            )
        return self.memory_show(room_id, credential=credential)

    # ------------------------------------------------------------------
    # human controls
    # ------------------------------------------------------------------

    def pause_room(self, room_id: str, *, credential: "Credential") -> dict:
        with db.transaction(self.conn):
            room = self._get_room(room_id)
            actor = self._authorize_actor(room, credential, require_human=True)
            if room.is_stopped:
                raise StateError("room is stopped")
            dao.update_room(self.conn, room_id, status=RoomStatus.PAUSED.value)
            self._log(
                room_id,
                ev.ROOM_PAUSED,
                actor_participant_id=actor.participant_id,
                actor_name=actor.name,
            )
        return self.room_status(room_id, credential=credential)

    def resume_room(self, room_id: str, *, credential: "Credential") -> dict:
        with db.transaction(self.conn):
            room = self._get_room(room_id)
            actor = self._authorize_actor(room, credential, require_human=True)
            if room.is_stopped:
                raise StateError("a stopped room cannot be resumed; create a new room")
            dao.update_room(self.conn, room_id, status=RoomStatus.ACTIVE.value)
            self._log(
                room_id,
                ev.ROOM_RESUMED,
                actor_participant_id=actor.participant_id,
                actor_name=actor.name,
            )
            self._activate_next(room_id)
        return self.room_status(room_id, credential=credential)

    def stop_room(self, room_id: str, *, credential: "Credential") -> dict:
        """Hard stop.  Terminal, authoritative, and effective for every participant."""
        with db.transaction(self.conn):
            room = self._get_room(room_id)
            actor = self._authorize_actor(room, credential, require_human=True)
            if room.is_stopped:
                return self.room_status(room_id, credential=credential)
            active_turn = dao.get_active_turn(self.conn, room_id)
            if active_turn is not None:
                scheduler.end_turn(
                    self.conn,
                    room_id,
                    active_turn.turn_id,
                    TurnStatus.CANCELLED,
                    queue_status=QueueStatus.CANCELLED,
                )
            scheduler.clear_queue(self.conn, room_id)
            dao.cancel_open_tasks(self.conn, room_id)
            dao.update_room(
                self.conn,
                room_id,
                status=RoomStatus.STOPPED.value,
                stopped_at=utc_now(),
                active_participant_id=None,
                active_turn_id=None,
            )
            self._log(
                room_id,
                ev.ROOM_STOPPED,
                actor_participant_id=actor.participant_id,
                actor_name=actor.name,
            )
        return self.room_status(room_id, credential=credential)

    def set_room_config(
        self, room_id: str, *, credential: "Credential", max_consecutive_agent_turns: int
    ) -> dict:
        if not isinstance(max_consecutive_agent_turns, int) or max_consecutive_agent_turns < 1:
            raise ValidationError("max-agent-turns must be a positive integer")
        with db.transaction(self.conn):
            room = self._get_room(room_id)
            actor = self._authorize_actor(room, credential, require_human=True)
            self._require_mutable(room)
            dao.update_room(
                self.conn,
                room_id,
                max_consecutive_agent_turns=max_consecutive_agent_turns,
                awaiting_human=int(room.consecutive_agent_turns >= max_consecutive_agent_turns),
            )
            self._log(
                room_id,
                ev.ROOM_CONFIG_UPDATED,
                actor_participant_id=actor.participant_id,
                actor_name=actor.name,
                payload={"max_consecutive_agent_turns": max_consecutive_agent_turns},
            )
        return self.room_status(room_id, credential=credential)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _get_room(self, room_id: str) -> Room:
        if not is_valid_id("room", room_id):
            raise ValidationError(f"malformed room id: {room_id!r}")
        room = dao.get_room(self.conn, room_id)
        if room is None:
            raise NotFoundError(f"no such room: {room_id}")
        return room

    def _resolve_room_reference(self, reference: str) -> tuple[str, str | None]:
        if not isinstance(reference, str) or not reference.strip():
            raise ValidationError("a room id or join token is required")
        reference = reference.strip()
        if is_valid_id("room", reference):
            return reference, None
        room_id, secret = tokens.split_join_token(reference)
        if room_id is None:
            raise ValidationError(
                "could not determine the room from that reference; pass a room id "
                "(room_...) or a full join token (room_....<secret>)"
            )
        return room_id, secret

    def _insert_participant(
        self,
        room_id: str,
        name: str,
        kind: ParticipantKind,
        metadata: dict | None = None,
    ) -> dict:
        participant_id = new_id("part")
        secret = new_secret()
        salt = tokens.new_salt()
        dao.insert_participant(
            self.conn,
            participant_id=participant_id,
            room_id=room_id,
            name=name,
            kind=kind.value,
            status=ParticipantStatus.ACTIVE.value,
            secret_salt=salt,
            secret_hash=tokens.hash_secret(secret, salt),
            joined_at=utc_now(),
            last_seen_at=utc_now(),
            metadata=_json(metadata),
        )
        return {
            "participant_id": participant_id,
            "name": name,
            "kind": kind.value,
            "secret": secret,
        }

    def _resolve_participant(self, room_id: str, reference: str) -> Participant:
        if not isinstance(reference, str) or not reference.strip():
            raise ValidationError("a participant name or id is required")
        reference = reference.strip()
        participant = (
            dao.get_participant(self.conn, room_id, reference)
            if is_valid_id("part", reference)
            else dao.get_participant_by_name(self.conn, room_id, reference)
        )
        if participant is None:
            known = [p.name for p in dao.list_participants(self.conn, room_id, False)]
            raise NotFoundError(
                f"no participant {reference!r} in this room; current participants: "
                f"{', '.join(known) if known else '(none)'}"
            )
        return participant

    def _resolve_addressee(
        self, room_id: str, reference: str | None, actor: Participant, label: str
    ) -> Participant | None:
        if not reference:
            return None
        participant = self._resolve_participant(room_id, reference)
        if participant.participant_id == actor.participant_id:
            raise ValidationError(f"cannot set {label} to yourself ({actor.name})")
        if not participant.is_active:
            raise ConflictError(
                f"participant {participant.name!r} has been removed from this room",
                code="participant_removed",
            )
        return participant

    def _authorize_actor(
        self,
        room: Room,
        credential: "Credential",
        *,
        require_human: bool = False,
        allow_removed: bool = False,
    ) -> Participant:
        if not credential.participant:
            raise AuthError("this operation requires a participant identity (--as/--participant)")
        if not credential.secret:
            raise AuthError(
                f"no secret supplied for {credential.participant!r}; pass --secret, set "
                "AIDAPTER_SECRET, or use the session file written by 'aidapter join'"
            )
        participant = self._resolve_participant(room.room_id, credential.participant)
        stored = dao.get_participant_secret(self.conn, room.room_id, participant.participant_id)
        if stored is None or not tokens.verify_secret(credential.secret, *stored):
            raise AuthError(f"invalid secret for participant {participant.name!r}")
        if not allow_removed and participant.status != ParticipantStatus.ACTIVE.value:
            # A removed participant may still hold a valid secret; status is
            # what makes revocation authoritative.
            raise AuthError(
                f"participant {participant.name!r} has been removed from this room",
                code="participant_removed",
            )
        if require_human and not participant.is_human:
            raise AuthError(
                f"{participant.name!r} is an agent; this action requires a human participant",
                code="human_required",
            )
        return participant

    def _authorize_read(self, room: Room, credential: "Credential") -> None:
        """Reads accept either a participant secret or the room join token."""
        if credential.participant:
            self._authorize_actor(room, credential, allow_removed=True)
            return
        if credential.room_token:
            # Accept either the compound token ("<room_id>.<secret>") or a bare
            # secret.  A compound token addressed to a different room is
            # rejected outright rather than being tried against this room.
            embedded_room, secret = tokens.split_join_token(credential.room_token)
            if embedded_room is not None and embedded_room != room.room_id:
                raise AuthError("that join token belongs to a different room")
            salt_hash = dao.get_room_token_hash(self.conn, room.room_id)
            if salt_hash is not None and tokens.verify_secret(secret, *salt_hash):
                return
            raise AuthError("invalid room token")
        raise AuthError(
            "reading a room requires either a participant identity or the room join token"
        )

    def _require_mutable(self, room: Room) -> None:
        if room.is_stopped:
            raise StateError(
                "room is stopped; no further changes are accepted", code="room_stopped"
            )

    def _require_sendable(self, room: Room, actor: Participant) -> None:
        self._require_mutable(room)
        if room.status == RoomStatus.PAUSED.value and not actor.is_human:
            raise StateError(
                "room is paused; only the human may act until it is resumed",
                code="room_paused",
            )
        if room.awaiting_human and not actor.is_human:
            raise StateError(
                f"the room has reached its limit of {room.max_consecutive_agent_turns} "
                "consecutive agent turns and is waiting for human input",
                code="awaiting_human",
            )

    def _acquire_floor(self, room: Room, actor: Participant) -> Turn:
        """Return the actor's active turn, starting one if it is legitimately theirs."""
        room_id = room.room_id
        active_turn = dao.get_active_turn(self.conn, room_id)
        if active_turn is not None:
            if active_turn.participant_id == actor.participant_id:
                return active_turn
            if active_turn.blocking:
                raise TurnError(
                    f"{active_turn.participant_name} holds a blocking targeted turn; "
                    f"{actor.name} must wait until it completes",
                    code="blocked_targeted_turn",
                    active_speaker=active_turn.participant_name,
                )
            raise TurnError(
                f"{active_turn.participant_name} currently holds the floor",
                active_speaker=active_turn.participant_name,
            )

        head = scheduler.peek(self.conn, room_id)
        if head is not None and head.participant_id != actor.participant_id:
            raise TurnError(
                f"{head.participant_name} is next in the queue; {actor.name} must wait",
                active_speaker=None,
                next_speaker=head.participant_name,
            )
        if head is not None:
            turn = self._activate_next(room_id)
            if turn is None:  # pragma: no cover - guarded by _require_sendable
                raise StateError("the room is not currently accepting turns")
            return turn
        # Nobody is queued and nobody holds the floor: the agent may volunteer.
        turn = scheduler.start_turn(
            self.conn,
            room,
            participant_id=actor.participant_id,
            blocking=False,
            kind=TurnKind.HUMAN if actor.is_human else TurnKind.NORMAL,
        )
        self._log(
            room_id,
            ev.TURN_STARTED,
            actor_participant_id=actor.participant_id,
            actor_name=actor.name,
            payload={"turn_id": turn.turn_id, "reason": "volunteered"},
        )
        return turn

    def _activate_next(self, room_id: str) -> Turn | None:
        turn = scheduler.activate_next(self.conn, room_id)
        if turn is not None:
            self._log(
                room_id,
                ev.TURN_STARTED,
                actor_participant_id=turn.participant_id,
                actor_name=turn.participant_name,
                payload={
                    "turn_id": turn.turn_id,
                    "blocking": turn.blocking,
                    "reason": "queue_promotion",
                },
            )
        return turn

    def _promote(
        self,
        room_id: str,
        participant: Participant,
        priority: Priority,
        *,
        blocking: bool,
        reason: str,
        task_id: str | None,
        source_message_id: str | None,
        actor: Participant,
    ) -> None:
        seq = dao.next_seq(self.conn, room_id)
        scheduler.enqueue(
            self.conn,
            room_id,
            participant.participant_id,
            priority,
            seq=seq,
            blocking=blocking,
            reason=reason,
            task_id=task_id,
            source_message_id=source_message_id,
        )
        self._log(
            room_id,
            ev.QUEUE_PROMOTED,
            actor_participant_id=actor.participant_id,
            actor_name=actor.name,
            payload={
                "participant": participant.name,
                "priority": priority.name.lower(),
                "blocking": blocking,
                "reason": reason,
                "source_message_id": source_message_id,
            },
        )

    def _append_message(
        self,
        room_id: str,
        actor: Participant,
        draft: MessageDraft,
        *,
        target: Participant | None,
        handoff: Participant | None,
        turn_id: str | None,
        human_override: bool,
        after_commit: list,
    ) -> MessageEnvelope:
        if draft.in_reply_to and dao.get_message(self.conn, room_id, draft.in_reply_to) is None:
            raise NotFoundError(f"no message {draft.in_reply_to!r} in this room to reply to")
        seq = dao.next_seq(self.conn, room_id)
        envelope = MessageEnvelope(
            message_id=new_id("msg"),
            room_id=room_id,
            seq=seq,
            sender=actor.name,
            sender_participant_id=actor.participant_id,
            timestamp=utc_now(),
            message_type=draft.message_type,
            target=target.name if target else None,
            target_participant_id=target.participant_id if target else None,
            content=draft.content,
            goal=draft.goal,
            task_id=draft.task_id,
            artifact_references=draft.artifact_references,
            constraints=draft.constraints,
            claim=draft.claim,
            evidence=draft.evidence,
            request_type=draft.request_type,
            response_status=draft.response_status,
            confidence=draft.confidence,
            handoff_target=handoff.name if handoff else None,
            in_reply_to=draft.in_reply_to,
            turn_id=turn_id,
            was_targeted=target is not None,
            human_override=human_override,
            resulted_in_handoff=handoff is not None,
            metadata=draft.metadata,
        )
        dao.insert_message(self.conn, envelope)
        self._log(
            room_id,
            ev.MESSAGE_SENT,
            actor_participant_id=actor.participant_id,
            actor_name=actor.name,
            payload={
                "message_id": envelope.message_id,
                "message_type": envelope.message_type,
                "target": envelope.target,
                "task_id": envelope.task_id,
                "turn_id": turn_id,
            },
        )
        after_commit.append(lambda: transcript.append(self.workspace, envelope))
        return envelope

    def _open_task_if_needed(
        self,
        room_id: str,
        actor: Participant,
        draft: MessageDraft,
        target: Participant | None,
        envelope: MessageEnvelope,
    ) -> str | None:
        """A targeted message of type ``task`` opens a tracked unit of work."""
        if target is None or draft.message_type != MessageType.TASK.value:
            return draft.task_id
        task_id = draft.task_id or new_id("task")
        if dao.get_task(self.conn, room_id, task_id) is None:
            dao.insert_task(
                self.conn,
                task_id=task_id,
                room_id=room_id,
                status=TaskStatus.OPEN.value,
                goal=draft.goal or _summarize(draft.content),
                created_by=actor.name,
                assigned_to=target.name,
                created_at=utc_now(),
                request_message_id=envelope.message_id,
            )
            self._log(
                room_id,
                ev.TASK_OPENED,
                actor_participant_id=actor.participant_id,
                actor_name=actor.name,
                payload={"task_id": task_id, "assigned_to": target.name},
            )
        # Backfill the task id onto the message that opened it.
        self.conn.execute(
            "UPDATE messages SET task_id = ? WHERE room_id = ? AND message_id = ?",
            (task_id, room_id, envelope.message_id),
        )
        envelope.task_id = task_id
        return task_id

    def _close_task_if_needed(
        self, room_id: str, actor: Participant, draft: MessageDraft, envelope: MessageEnvelope
    ) -> None:
        if not draft.task_id or draft.response_status != ResponseStatus.COMPLETE.value:
            return
        task = dao.get_task(self.conn, room_id, draft.task_id)
        if task is None or task.status != TaskStatus.OPEN.value:
            return
        if task.assigned_to != actor.name:
            return
        dao.update_task(
            self.conn,
            room_id,
            task.task_id,
            status=TaskStatus.COMPLETED.value,
            completed_at=utc_now(),
            response_message_id=envelope.message_id,
        )
        self._log(
            room_id,
            ev.TASK_CLOSED,
            actor_participant_id=actor.participant_id,
            actor_name=actor.name,
            payload={"task_id": task.task_id},
        )

    def _log(
        self,
        room_id: str,
        event_type: str,
        *,
        actor_participant_id: str | None = None,
        actor_name: str | None = None,
        payload: dict | None = None,
    ) -> None:
        dao.insert_event(
            self.conn,
            room_id,
            event_type,
            seq=dao.next_seq(self.conn, room_id),
            actor_participant_id=actor_participant_id,
            actor_name=actor_name,
            payload=payload,
        )

    def _name_of(self, room_id: str, participant_id: str | None) -> str | None:
        if not participant_id:
            return None
        participant = dao.get_participant(self.conn, room_id, participant_id)
        return participant.name if participant else None

    def _heartbeat(self, room_id: str, actor: Participant) -> None:
        """Refresh ``last_seen_at``, but not on every single poll."""
        if actor.last_seen_at and _age_seconds(actor.last_seen_at) < HEARTBEAT_INTERVAL_SECONDS:
            return
        try:
            with db.transaction(self.conn):
                dao.touch_participant(self.conn, room_id, actor.participant_id)
        except sqlite3.OperationalError:  # pragma: no cover - lock contention is not fatal here
            pass


class Credential:
    """How a caller proves who it is.

    Either a participant identity plus its secret (full authority), or the room
    join token (read-only observer access).
    """

    __slots__ = ("participant", "secret", "room_token")

    def __init__(
        self,
        participant: str | None = None,
        secret: str | None = None,
        room_token: str | None = None,
    ) -> None:
        self.participant = participant.strip() if isinstance(participant, str) else None
        self.secret = secret
        self.room_token = room_token

    def __repr__(self) -> str:  # pragma: no cover - debugging aid, never logs secrets
        return f"Credential(participant={self.participant!r}, secret=<redacted>)"


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _run_after_commit(actions: Iterable[Callable[[], None]]) -> list[str]:
    """Run deferred filesystem writes; report rather than raise.

    These are mirrors of committed state (transcript lines, ledger entries).  A
    failure here must not make a successful, durable orchestration operation
    look like it failed — ``aidapter export`` can regenerate them.
    """
    warnings: list[str] = []
    for action in actions:
        try:
            action()
        except OSError as exc:
            warnings.append(f"post-commit write failed: {exc}")
    return warnings


def _ledger_writer(
    ledger: LedgerStore,
    room_id: str,
    room_name: str,
    section: str,
    entry: str,
    actor: str,
    message_id: str,
) -> Callable[[], None]:
    def write() -> None:
        ledger.add_entry(
            room_id, section, entry, actor=actor, message_id=message_id, room_name=room_name
        )

    return write


def _summarize(text: str, limit: int = 120) -> str:
    collapsed = " ".join((text or "").split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _require_text(value: str, label: str, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} is required")
    value = value.strip()
    if len(value) > max_len:
        raise ValidationError(f"{label} exceeds {max_len} characters")
    return value


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise ValidationError(
            f"invalid participant name {name!r}: use 1-64 characters from "
            "[A-Za-z0-9._-], starting with a letter or digit"
        )


def _json(value: dict | None) -> str:
    import json

    return json.dumps(value or {}, ensure_ascii=False)


def _age_seconds(timestamp: str) -> float:
    from datetime import datetime, timezone

    try:
        parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:  # pragma: no cover - defensive
        return float("inf")
    return (datetime.now(timezone.utc) - parsed).total_seconds()
