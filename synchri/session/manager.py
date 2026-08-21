"""Session lifecycle: configure, contract, acknowledge, activate, finish.

This is where the startup invariants are actually enforced, in this order:

* a session cannot activate without a validated worktree that is not the
  primary tree;
* a session cannot activate until every participant has acknowledged the
  *current* contract revision, matched by digest;
* changing permissions, the spec, the deadline, or the roster produces a new
  revision and clears acknowledgments, so nobody is working under terms they
  never saw;
* an optional timebox guides pacing but never falsely ends work or claims it is
  complete;
* completion requires every required gate satisfied with evidence and both
  sign-offs.

The room underneath is untouched: it keeps owning the queue, the transcript and
shared memory. A session just decides the terms the room runs under.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ..broker import Broker, Credential
from ..config import write_private
from ..models.envelope import MessageDraft
from ..errors import ConflictError, NotFoundError, StateError, ValidationError
from ..ids import new_id, utc_now
from ..protocol import events as ev
from ..storage import db
from . import contract as contract_module
from . import changelog as changelog_module
from . import dropbox as dropbox_module
from . import worktree as worktree_module
from .deadline import Deadline
from .escalation import EscalationPolicy
from .gates import Gate, GateStatus, summarize
from .modes import ModePolicy, ParticipantPlan, collaboration_pair, policy_for, resolve_role
from .permissions import BY_KEY, Decision, PermissionDenied, PermissionSet
from .spec import ProductSpec
from . import extract as extract_module
from . import verify as verify_module
from .worktree import Worktree


class SessionStatus(str, Enum):
    CONFIGURING = "configuring"       # wizard in progress
    AWAITING_ACK = "awaiting_ack"     # contract issued, waiting on agents
    ACTIVE = "active"                 # development permitted
    PAUSED = "paused"
    COMPLETE = "complete"
    TIMED_OUT = "timed_out"
    STOPPED = "stopped"
    FAILED = "failed"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


#: Statuses in which the session is finished and accepts no further work.
TERMINAL = {
    SessionStatus.COMPLETE.value,
    SessionStatus.TIMED_OUT.value,
    SessionStatus.STOPPED.value,
    SessionStatus.FAILED.value,
}


def _append_unique(existing: list[str], added: list[str]) -> list[str]:
    """Preserve evidence history while avoiding duplicate reports."""
    merged = list(existing)
    for value in added:
        text = str(value).strip()
        if text and text not in merged:
            merged.append(text)
    return merged


@dataclass
class SessionRecord:
    """A session as stored. Configuration plus lifecycle state."""

    session_id: str
    name: str
    mode: str
    status: str
    created_at: str
    updated_at: str
    repo_root: str
    repo_name: str
    base_branch: str
    room_id: str | None = None
    repo_remote: str | None = None
    activated_at: str | None = None
    ended_at: str | None = None
    ended_reason: str | None = None
    worktree_name: str | None = None
    worktree_path: str | None = None
    worktree_branch: str | None = None
    permissions: PermissionSet = field(default_factory=PermissionSet.defaults)
    spec: ProductSpec | None = None
    deadline: Deadline | None = None
    escalation: EscalationPolicy = field(default_factory=EscalationPolicy)
    metadata: dict = field(default_factory=dict)
    contract_revision: int = 0
    participants: list[ParticipantPlan] = field(default_factory=list)
    #: The durable phase: original_work -> appendix_evaluation ->
    #: extension_work -> closing. Only closing invokes terminal room-closing.
    phase: str = "original_work"

    @property
    def policy(self) -> ModePolicy:
        return policy_for(self.mode)

    @property
    def worktree(self) -> Worktree | None:
        if not (self.worktree_path and self.worktree_name):
            return None
        return Worktree(
            name=self.worktree_name,
            path=self.worktree_path,
            branch=self.worktree_branch or self.worktree_name,
            base_branch=self.base_branch,
            repo_root=self.repo_root,
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "room_id": self.room_id,
            "name": self.name,
            "mode": self.mode,
            "mode_label": self.policy.label,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "activated_at": self.activated_at,
            "ended_at": self.ended_at,
            "ended_reason": self.ended_reason,
            "repository": {
                "name": self.repo_name,
                "root": self.repo_root,
                "remote": self.repo_remote,
                "base_branch": self.base_branch,
            },
            "worktree": self.worktree.to_dict() if self.worktree else None,
            "permissions": self.permissions.to_dict(),
            "spec": self.spec.to_dict() if self.spec else None,
            "deadline": self.deadline.to_dict() if self.deadline else None,
            "escalation": self.escalation.to_dict(),
            "contract_revision": self.contract_revision,
            "phase": self.phase,
            "participants": [p.to_dict() for p in self.participants],
            "metadata": dict(self.metadata),
        }


class SessionManager:
    """Create and drive sessions on top of a :class:`Broker`."""

    def __init__(self, broker: Broker) -> None:
        self.broker = broker
        self.conn: sqlite3.Connection = broker.conn

    # ------------------------------------------------------------------
    # creation
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        mode: str,
        repo_root: str,
        base_branch: str | None = None,
        participants: list[ParticipantPlan] | None = None,
        permissions: PermissionSet | None = None,
        spec: ProductSpec | None = None,
        deadline: Deadline | None = None,
        escalation: EscalationPolicy | None = None,
        worktree_parent: str | None = None,
        worktree_name: str | None = None,
        existing_worktree_path: str | None = None,
        metadata: dict | None = None,
    ) -> SessionRecord:
        """Validate the configuration, cut the worktree, and create the room.

        Nothing here activates the session — that needs acknowledgment.
        """
        policy = policy_for(mode)
        if not name or not name.strip():
            raise ValidationError("the session needs a name")

        status = worktree_module.inspect_repository(repo_root)
        if not status.is_valid:
            raise ValidationError("; ".join(status.problems))
        branch = base_branch or status.branch
        if not branch:
            raise ValidationError("could not determine a base branch; select one explicitly")

        plans = list(participants or [])
        for plan in plans:
            plan.role = resolve_role(plan.role)
        if len(plans) < policy.min_agents:
            raise ValidationError(
                f"{policy.label} needs at least {policy.min_agents} agent(s); got {len(plans)}"
            )
        if len({p.name for p in plans}) != len(plans):
            raise ValidationError("participant names must be unique")

        if policy.requires_spec and spec is None:
            raise ValidationError(f"{policy.label} requires a product specification")
        if policy.requires_deadline and deadline is None:
            raise ValidationError(f"{policy.label} requires a deadline")
        if deadline and not policy.supports_deadline:
            raise ValidationError(f"{policy.label} does not use deadlines")

        grants = policy.apply_forced_denials(permissions or PermissionSet.defaults())
        rules = escalation or EscalationPolicy()

        # A fresh isolated worktree is the default. A user may deliberately
        # select an existing non-primary worktree for a follow-up session.
        # Both paths use the same validation; neither can target the primary
        # checkout.
        created_worktree = not bool(existing_worktree_path)
        if existing_worktree_path:
            selected = Path(existing_worktree_path).expanduser().resolve()
            tree = worktree_module.validate(status.root, selected, selected.name, branch)
        else:
            tree = worktree_module.create(
                status.root,
                branch,
                mode=policy.mode.value,
                name=worktree_name,
                parent_dir=worktree_parent,
            )

        session_id = new_id("sess")
        try:
            room = self.broker.create_room(
                name,
                agents=[p.name for p in plans],
                goal=(spec.text.strip().splitlines()[0][:200] if spec else None),
                max_consecutive_agent_turns=policy.max_consecutive_agent_turns,
                workspace_root=tree.path,
                invite_ttl_seconds=0,  # invites live as long as the session room
            )
        except Exception:
            # Only a brand-new tree is ours to clean up. A selected existing
            # worktree remains exactly where the user left it.
            if created_worktree:
                worktree_module.remove(tree, force=True, delete_branch=True)
            raise

        now = utc_now()
        record = SessionRecord(
            session_id=session_id,
            room_id=room["room_id"],
            name=name.strip(),
            mode=policy.mode.value,
            status=SessionStatus.CONFIGURING.value,
            created_at=now,
            updated_at=now,
            repo_root=status.root,
            repo_name=status.name,
            repo_remote=status.remote,
            base_branch=branch,
            worktree_name=tree.name,
            worktree_path=tree.path,
            worktree_branch=tree.branch,
            permissions=grants,
            spec=spec,
            deadline=deadline,
            escalation=rules,
            metadata={
                **(metadata or {}),
                "invites": room["invites"],
                "observer_token": room["observer_token"],
                "human": room["human"],
                "primary_tree_dirty": status.is_dirty,
                "worktree_strategy": "new" if created_worktree else "existing",
            },
            participants=plans,
        )
        self._insert(record)
        # Propose gates from the spec so the user does not restate their own
        # acceptance criteria. They land PENDING with no evidence -- a proposal,
        # not an authority.
        if spec:
            proposed = extract_module.extract_gates(spec.canonical_text())
            if proposed:
                self.set_gates(session_id, proposed)
        self._log(
            record,
            ev.SESSION_CREATED,
            {"mode": record.mode, "worktree": tree.name,
             "gates_detected": len(self.gates(session_id))},
        )
        return record

    # ------------------------------------------------------------------
    # contract and acknowledgment
    # ------------------------------------------------------------------

    def issue_contract(self, session_id: str, *, reason: str | None = None):
        """Generate the next contract revision and clear prior acknowledgments."""
        record = self.get(session_id)
        if record.is_terminal:
            raise StateError(f"session is {record.status}", code="session_finished")
        if not record.worktree:
            raise StateError("a session needs its worktree before a contract", code="no_worktree")
        if not worktree_module.exists(record.worktree):
            raise StateError(
                f"the authorized worktree is missing: {record.worktree_path}",
                code="worktree_missing",
            )

        revision = record.contract_revision + 1
        created_at = utc_now()
        document = contract_module.generate(
            session_id=record.session_id,
            revision=revision,
            policy=record.policy,
            repo_name=record.repo_name,
            repo_root=record.repo_root,
            repo_remote=record.repo_remote,
            base_branch=record.base_branch,
            worktree=record.worktree,
            participants=record.participants,
            permissions=record.permissions,
            spec=record.spec,
            deadline=record.deadline,
            escalation=record.escalation,
            created_at=created_at,
        )
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO session_contracts (session_id, revision, digest, core_text, "
                "role_sections, created_at, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    revision,
                    document.digest,
                    document.core_text,
                    json.dumps(document.role_sections),
                    created_at,
                    reason,
                ),
            )
            # A new revision invalidates every earlier acknowledgment: agreeing
            # to revision 1 is not agreeing to revision 2.
            self.conn.execute(
                "DELETE FROM session_acknowledgments WHERE session_id = ? AND revision >= ?",
                (session_id, revision),
            )
            self._update(
                session_id,
                contract_revision=revision,
                status=SessionStatus.AWAITING_ACK.value,
            )
        self._log(
            record,
            ev.SESSION_CONTRACT_ISSUED,
            {"revision": revision, "digest": document.digest, "reason": reason},
        )
        return document

    def current_contract(self, session_id: str):
        record = self.get(session_id)
        if record.contract_revision < 1:
            raise NotFoundError("no contract has been issued for this session yet")
        return self.contract_revision(session_id, record.contract_revision)

    def contract_revision(self, session_id: str, revision: int):
        row = self.conn.execute(
            "SELECT * FROM session_contracts WHERE session_id = ? AND revision = ?",
            (session_id, revision),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"no contract revision {revision} for this session")
        return contract_module.SessionContract(
            session_id=session_id,
            revision=row["revision"],
            core_text=row["core_text"],
            role_sections=json.loads(row["role_sections"] or "{}"),
            created_at=row["created_at"],
        )

    def acknowledge(self, session_id: str, participant: str, reply: str) -> dict:
        """Record one agent's response to the current contract."""
        record = self.get(session_id)
        if record.contract_revision < 1:
            raise StateError("no contract has been issued yet", code="no_contract")
        if participant not in {p.name for p in record.participants}:
            raise NotFoundError(f"{participant!r} is not a participant in this session")

        document = self.current_contract(session_id)
        accepted, conflict = contract_module.parse_acknowledgment(reply)
        now = utc_now()
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO session_acknowledgments (session_id, revision, participant, "
                "accepted, digest, raw_reply, conflict, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, revision, participant) DO UPDATE SET "
                "accepted=excluded.accepted, raw_reply=excluded.raw_reply, "
                "conflict=excluded.conflict, created_at=excluded.created_at",
                (
                    session_id,
                    document.revision,
                    participant,
                    int(accepted),
                    document.digest,
                    (reply or "")[:20_000],
                    conflict,
                    now,
                ),
            )
        self._log(
            record,
            ev.SESSION_ACKNOWLEDGED if accepted else ev.SESSION_CONFLICT,
            {
                "participant": participant,
                "revision": document.revision,
                "digest": document.digest,
                "conflict": conflict,
            },
        )
        return {
            "participant": participant,
            "accepted": accepted,
            "conflict": conflict,
            "revision": document.revision,
            "digest": document.digest,
        }

    def acknowledgment_state(self, session_id: str) -> dict:
        """Who has agreed to the *current* revision, and who has objected."""
        record = self.get(session_id)
        revision = record.contract_revision
        rows = self.conn.execute(
            "SELECT * FROM session_acknowledgments WHERE session_id = ? AND revision = ?",
            (session_id, revision),
        ).fetchall()
        by_name = {row["participant"]: row for row in rows}
        expected = [p.name for p in record.participants]

        accepted, conflicts, waiting = [], [], []
        for name in expected:
            row = by_name.get(name)
            if row is None:
                waiting.append(name)
            elif row["accepted"]:
                accepted.append(name)
            else:
                conflicts.append({"participant": name, "reason": row["conflict"]})
        return {
            "revision": revision,
            "expected": expected,
            "accepted": accepted,
            "conflicts": conflicts,
            "waiting": waiting,
            "all_accepted": bool(expected) and len(accepted) == len(expected),
        }

    # ------------------------------------------------------------------
    # activation
    # ------------------------------------------------------------------

    def activate(self, session_id: str) -> SessionRecord:
        """Permit development. Every startup invariant is checked here."""
        record = self.get(session_id)
        if record.status == SessionStatus.ACTIVE.value:
            return record
        if record.is_terminal:
            raise StateError(f"session is {record.status}", code="session_finished")

        # 1. worktree exists and is not the primary tree
        if not record.worktree:
            raise StateError("no authorized worktree for this session", code="no_worktree")
        worktree_module.validate(
            record.repo_root, record.worktree_path, record.worktree_name, record.base_branch
        )
        # 2. contract issued and unanimously acknowledged at the current revision
        if record.contract_revision < 1:
            raise StateError("no contract has been issued yet", code="no_contract")
        state = self.acknowledgment_state(session_id)
        if state["conflicts"]:
            raise StateError(
                "cannot activate: "
                + "; ".join(f"{c['participant']} reported a conflict" for c in state["conflicts"]),
                code="contract_conflict",
            )
        if not state["all_accepted"]:
            raise StateError(
                "cannot activate: still waiting on " + ", ".join(state["waiting"]),
                code="awaiting_acknowledgment",
            )
        # 3. Acknowledge is an agreement to the contract; joining proves that
        # the agent has a usable credential and can actually receive the first
        # task. Never turn an unconnected roster into a fake "active" room.
        room = self.broker.room_status(
            record.room_id, credential=self._human_credential(record)
        ) if record.room_id else {"participants": []}
        joined = {
            participant["name"]
            for participant in room.get("participants", [])
            if participant.get("status") != "removed"
        }
        waiting_to_join = [plan.name for plan in record.participants if plan.name not in joined]
        if waiting_to_join:
            raise StateError(
                "cannot activate: still waiting for " + ", ".join(waiting_to_join) + " to join",
                code="awaiting_join",
            )
        now = utc_now()
        # A duration is selected during setup, but it is a suggestion for the
        # collaboration—not a stopwatch that punishes the user while agents
        # join or review the contract. Rebase it at the one authoritative
        # transition into active work. Fixed timestamps remain untouched.
        activated_deadline = (
            record.deadline.start_when_collaboration_begins()
            if record.deadline
            else None
        )
        changes = {"status": SessionStatus.ACTIVE.value, "activated_at": now}
        if activated_deadline and activated_deadline is not record.deadline:
            changes["deadline"] = json.dumps(activated_deadline.to_dict())
        self._update(session_id, **changes)
        self._log(record, ev.SESSION_ACTIVATED, {"revision": record.contract_revision})
        try:
            self._open_collaboration(record)
        except Exception:
            # Activation promises a live collaboration, not a status badge.
            # A failure to create the opening turn pauses safely for the user.
            self._update(session_id, status=SessionStatus.PAUSED.value)
            raise
        return self.get(session_id)

    def _open_collaboration(self, record: SessionRecord) -> None:
        """Give the first actual turn to the builder when a session activates."""
        if not record.room_id:
            raise StateError("the session has no room", code="no_room")
        lead, reviewer = collaboration_pair(record.participants)
        if lead is None:
            raise StateError("the session has no participants", code="no_participants")
        review_line = (
            f"Then hand your opening findings and first evidence checkpoint to {reviewer.name} "
            "for adversarial review."
            if reviewer
            else "Then continue with the first evidence checkpoint."
        )
        if record.mode == "review_audit":
            opening = (
                "Begin the audit. Read the target, the canonical criteria, and the authorized "
                "worktree. Publish the first evidence-backed review approach: scope, likely risks, "
                "inspection order, and the first checks you will run. Do not implement product changes "
                "unless the human explicitly authorizes a fix. "
                + review_line
            )
        else:
            spec_line = (
                "Read the canonical product specification and the authorized worktree."
                if record.spec
                else "Inspect the authorized worktree and establish the concrete first objective."
            )
            opening = (
                f"Begin working on the specification. {spec_line} Publish your opening build approach: "
                "current repository facts, implementation order, first change, and risks. "
                "Do not stop at the proposal—start the first coherent unit of work. "
                + review_line
            )
        self.broker.send(
            record.room_id,
            credential=self._human_credential(record),
            draft=MessageDraft(
                message_type="task",
                target=lead.name,
                metadata={"source": "session_activation"},
                content=opening,
            ),
        )

    # ------------------------------------------------------------------
    # reconfiguration
    # ------------------------------------------------------------------

    def update_configuration(
        self,
        session_id: str,
        *,
        permissions: PermissionSet | None = None,
        spec: ProductSpec | None = None,
        deadline: Deadline | None = None,
        participants: list[ParticipantPlan] | None = None,
        escalation: EscalationPolicy | None = None,
        reason: str = "configuration changed",
    ) -> dict:
        """Change material terms mid-session.

        This never happens silently: it writes a new contract revision, drops the
        session back to awaiting acknowledgment, and every agent must agree again.
        """
        record = self.get(session_id)
        if record.is_terminal:
            raise StateError(f"session is {record.status}", code="session_finished")

        changes: dict = {}
        if permissions is not None:
            record.policy.apply_forced_denials(permissions)
            changes["permissions"] = json.dumps(permissions.to_dict())
            record.permissions = permissions
        if spec is not None:
            changes["spec"] = json.dumps(spec.to_dict())
            record.spec = spec
        if deadline is not None:
            changes["deadline"] = json.dumps(deadline.to_dict())
            record.deadline = deadline
        if escalation is not None:
            changes["escalation"] = json.dumps(escalation.to_dict())
            record.escalation = escalation
        if participants is not None:
            for plan in participants:
                plan.role = resolve_role(plan.role)
            record.participants = participants
        if not changes and participants is None:
            raise ValidationError("nothing to change")

        with db.transaction(self.conn):
            if changes:
                self._update(session_id, **changes)
            if participants is not None:
                self.conn.execute(
                    "DELETE FROM session_participants WHERE session_id = ?", (session_id,)
                )
                self._insert_participants(session_id, participants)

        document = self.issue_contract(session_id, reason=reason)
        return {
            "revision": document.revision,
            "digest": document.digest,
            "reason": reason,
            "requires_reacknowledgment": True,
        }

    def extend_timebox(self, session_id: str, duration: str) -> SessionRecord:
        """Add advisory time without disrupting a live session.

        A normal deadline edit changes the collaboration agreement and requires
        a new acknowledgment.  Extending an already-active *suggested* timebox
        is different: it only changes pacing, never execution authority, so it
        is durable and audited without stopping the agents in mid-turn.
        """
        record = self.get(session_id)
        if record.is_terminal:
            raise StateError(f"session is {record.status}", code="session_finished")
        if record.deadline is None:
            raise ValidationError("this session has no timebox to extend")
        extended = record.deadline.extend(duration)
        self._update(session_id, deadline=json.dumps(extended.to_dict()))
        self._log(
            record,
            ev.SESSION_TIMEBOX_EXTENDED,
            {"duration": duration, "ends_at": extended.ends_at},
        )
        return self.get(session_id)

    # ------------------------------------------------------------------
    # permission checks
    # ------------------------------------------------------------------

    def check_permission(self, session_id: str, capability: str, *, actor: str | None = None) -> bool:
        """The single gate for "may this agent do X in this session?".

        Raises :class:`PermissionDenied` for both DENY and ASK — ASK is not a
        grant, it is an instruction to escalate first.
        """
        record = self.get(session_id)
        decision = record.permissions.decision(capability)
        approvals = (record.metadata or {}).get("approved_capabilities") or {}
        if decision is Decision.ALLOW or approvals.get(capability):
            return True
        self._log(
            record,
            ev.SESSION_PERMISSION_DENIED,
            {"capability": capability, "decision": decision.value, "actor": actor},
        )
        raise PermissionDenied(capability, decision)

    def approve_capability(self, session_id: str, capability: str, *, actor: str | None = None) -> SessionRecord:
        """Record the human's approval of one explicitly requested ASK action.

        This is intentionally scoped: it may satisfy an ``ASK`` capability for
        the current session, but it cannot turn a policy ``DENY`` into a grant
        and it never touches a provider, operating-system, GitHub, or other
        external approval boundary.
        """
        record = self.get(session_id)
        if record.is_terminal:
            raise StateError(f"session is {record.status}", code="session_finished")
        if capability not in BY_KEY:
            raise ValidationError(f"unknown capability {capability!r}")
        decision = record.permissions.decision(capability)
        if decision is Decision.DENY:
            raise StateError(
                f"{BY_KEY[capability].label} is denied by this workflow. Change the workflow permissions before approving it.",
                code="permission_denied_by_workflow",
                resolution={"kind": "edit_permissions", "capability": capability},
            )
        if decision is Decision.ALLOW:
            return record
        metadata = dict(record.metadata or {})
        approved = dict(metadata.get("approved_capabilities") or {})
        approved[capability] = {"approved_at": utc_now(), "approved_by": actor or "human"}
        metadata["approved_capabilities"] = approved
        self._update(session_id, metadata=json.dumps(metadata))
        self._log(
            record,
            ev.SESSION_PERMISSION_APPROVED,
            {"capability": capability, "actor": actor or "human"},
        )
        return self.get(session_id)

    def permits(self, session_id: str, capability: str) -> bool:
        """Non-raising variant, for rendering UI state."""
        return self.get(session_id).permissions.allows(capability)

    # ------------------------------------------------------------------
    # gates and completion
    # ------------------------------------------------------------------

    def set_gates(self, session_id: str, gates: list[Gate]) -> list[Gate]:
        with db.transaction(self.conn):
            self.conn.execute("DELETE FROM session_gates WHERE session_id = ?", (session_id,))
            for gate in gates:
                self._write_gate(session_id, gate)
        return self.gates(session_id)

    def add_gate(self, session_id: str, gate: Gate) -> Gate:
        """Insert one new gate without touching the others' evidence."""
        record = self.get(session_id)
        if record.is_terminal:
            raise StateError(
                f"cannot add a gate after the session is {record.status}",
                code="session_finished",
            )
        if any(existing.gate_id == gate.gate_id for existing in self.gates(session_id)):
            raise ConflictError(f"gate {gate.gate_id} already exists in this session")
        with db.transaction(self.conn):
            self._write_gate(session_id, gate)
        self._log(
            record,
            ev.SESSION_GATE_UPDATED,
            {"gate_id": gate.gate_id, "status": gate.status, "actor": "human", "added": True},
        )
        return gate

    def update_gate(self, session_id: str, gate_id: str, *, actor: str | None = None, **fields) -> Gate:
        record = self.get(session_id)
        if record.is_terminal:
            raise StateError(
                f"cannot update a gate after the session is {record.status}",
                code="session_finished",
            )
        existing = {g.gate_id: g for g in self.gates(session_id)}
        if gate_id not in existing:
            raise NotFoundError(f"no gate {gate_id!r} in this session")
        gate = existing[gate_id]
        for key, value in fields.items():
            if not hasattr(gate, key):
                raise ValidationError(f"unknown gate field {key!r}")
            setattr(gate, key, value)
        gate.__post_init__()
        gate.updated_at = utc_now()
        gate.updated_by = actor
        with db.transaction(self.conn):
            self._write_gate(session_id, gate)
        self._log(
            record,
            ev.SESSION_GATE_UPDATED,
            {"gate_id": gate_id, "status": gate.status, "actor": actor},
        )
        return gate

    def report_gate(
        self,
        session_id: str,
        gate_id: str,
        *,
        actor: str,
        status: str | None = None,
        assessment: str | None = None,
        evidence: list[str] | None = None,
        tests: list[str] | None = None,
        commits: list[str] | None = None,
    ) -> Gate:
        """Merge one agent's evidence-bearing assessment into an existing gate.

        Agents can report their own work directly, but completion remains
        evidence-backed: builder and reviewer assessments both have to exist
        before a PASS can close the session.  Merging, instead of replacing,
        means a review cannot accidentally erase the builder's test evidence.
        """
        record = self.get(session_id)
        if record.is_terminal:
            raise StateError(
                f"cannot report a gate after the session is {record.status}",
                code="session_finished",
            )
        plan = next((item for item in record.participants if item.name == actor), None)
        if plan is None:
            raise ValidationError(f"{actor!r} is not a participant in this session")
        current = next((gate for gate in self.gates(session_id) if gate.gate_id == gate_id), None)
        if current is None:
            raise NotFoundError(f"no gate {gate_id!r} in this session")
        fields: dict[str, object] = {}
        if status:
            fields["status"] = status
        if evidence:
            fields["evidence"] = _append_unique(current.evidence, evidence)
        if tests:
            fields["tests"] = _append_unique(current.tests, tests)
        if commits:
            fields["commits"] = _append_unique(current.commits, commits)
        if assessment:
            if plan.role == "primary_builder":
                fields["builder_assessment"] = assessment
            elif plan.role in {"adversarial_reviewer", "verifier", "auditor"}:
                fields["reviewer_assessment"] = assessment
            else:
                fields["evidence"] = _append_unique(current.evidence, [assessment])
        return self.update_gate(session_id, gate_id, actor=actor, **fields)

    def gates(self, session_id: str) -> list[Gate]:
        rows = self.conn.execute(
            "SELECT * FROM session_gates WHERE session_id = ? ORDER BY gate_id", (session_id,)
        ).fetchall()
        return [
            Gate(
                gate_id=row["gate_id"],
                description=row["description"],
                status=row["status"],
                required=bool(row["required"]),
                evidence=json.loads(row["evidence"] or "[]"),
                tests=json.loads(row["tests"] or "[]"),
                commits=json.loads(row["commits"] or "[]"),
                builder_assessment=row["builder_assessment"],
                reviewer_assessment=row["reviewer_assessment"],
                updated_at=row["updated_at"],
                updated_by=row["updated_by"],
                origin_kind=(row["origin_kind"] if "origin_kind" in row.keys() else None) or "main",
                drop_id=row["drop_id"] if "drop_id" in row.keys() else None,
                extension_id=row["extension_id"] if "extension_id" in row.keys() else None,
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # per-agent runtime state
    # ------------------------------------------------------------------

    def participant_states(self, session_id: str) -> dict[str, dict]:
        """Durable supervision state per agent (state is None until launched)."""
        rows = self.conn.execute(
            "SELECT name, runtime_status, runtime_detail, consecutive_failures, "
            "runtime_updated_at, join_phase, join_detail, join_updated_at "
            "FROM session_participants WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        return {
            row["name"]: {
                "state": row["runtime_status"],
                "detail": row["runtime_detail"],
                "failures": row["consecutive_failures"] or 0,
                "updated_at": row["runtime_updated_at"],
                "join_phase": row["join_phase"],
                "join_detail": row["join_detail"],
                "join_updated_at": row["join_updated_at"],
            }
            for row in rows
        }

    def set_participant_resume_id(self, session_id: str, name: str, resume_id: str | None) -> None:
        """Store (or clear) the provider's own session id for one agent.

        Captured after every completed invocation that exposed one; recovery
        reads it to resume the provider session instead of relaunching cold.
        """
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE session_participants SET resume_id = ? WHERE session_id = ? AND name = ?",
                (resume_id, session_id, name),
            )

    def participant_recovery(self, session_id: str, name: str) -> dict:
        row = self.conn.execute(
            "SELECT resume_id, recovery_generation, runtime_status, metadata "
            "FROM session_participants WHERE session_id = ? AND name = ?",
            (session_id, name),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"{name!r} is not a participant in this session")
        return {
            "resume_id": row["resume_id"],
            "recovery_generation": row["recovery_generation"] or 0,
            "runtime_status": row["runtime_status"],
            "metadata": json.loads(row["metadata"] or "{}"),
        }

    def bump_recovery_generation(self, session_id: str, name: str, action: str) -> int:
        """One recovery action happened (resume, replace, restart): count it.

        The generation is provenance — an approval or a gate report recorded
        by generation 2 of an agent is distinguishable from generation 0.
        """
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE session_participants SET recovery_generation = recovery_generation + 1 "
                "WHERE session_id = ? AND name = ?",
                (session_id, name),
            )
            row = self.conn.execute(
                "SELECT recovery_generation FROM session_participants "
                "WHERE session_id = ? AND name = ?",
                (session_id, name),
            ).fetchone()
        generation = row["recovery_generation"] if row else 0
        self._log(
            self.get(session_id),
            "session.participant_recovery",
            {"participant": name, "action": action, "generation": generation},
        )
        return generation

    def mark_participant_replaced(self, session_id: str, name: str) -> None:
        """Record the one automatic replacement incarnation durably.

        Exactly one automatic replace per participant: a second breaker trip
        goes to the human instead of looping fail → replace → fail forever.
        """
        recovery_state = self.participant_recovery(session_id, name)
        metadata = {**recovery_state["metadata"], "auto_replaced": utc_now()}
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE session_participants SET metadata = ?, resume_id = NULL, "
                "consecutive_failures = 0 WHERE session_id = ? AND name = ?",
                (json.dumps(metadata), session_id, name),
            )

    def set_participant_join_phase(
        self, session_id: str, name: str, phase: str, detail: str | None = None
    ) -> None:
        """Record what an agent is doing to join — real transitions, no spinner.

        The launch UI renders these durable phases from Start until the room
        is live: launching -> injecting_bootstrap -> awaiting_acknowledgment
        -> ready, or failed with the reason. A retry targets only the failed
        participant, so phases survive per agent rather than per team.
        """
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE session_participants SET join_phase = ?, join_detail = ?, "
                "join_updated_at = ? WHERE session_id = ? AND name = ?",
                (phase, detail, utc_now(), session_id, name),
            )

    def set_participant_state(
        self,
        session_id: str,
        name: str,
        state: str,
        detail: str | None = None,
        *,
        reset_failures: bool = False,
    ) -> None:
        with db.transaction(self.conn):
            if reset_failures:
                self.conn.execute(
                    "UPDATE session_participants SET runtime_status = ?, runtime_detail = ?, "
                    "consecutive_failures = 0, runtime_updated_at = ? "
                    "WHERE session_id = ? AND name = ?",
                    (state, detail, utc_now(), session_id, name),
                )
            else:
                self.conn.execute(
                    "UPDATE session_participants SET runtime_status = ?, runtime_detail = ?, "
                    "runtime_updated_at = ? WHERE session_id = ? AND name = ?",
                    (state, detail, utc_now(), session_id, name),
                )

    def record_participant_failure(
        self, session_id: str, name: str, state: str, detail: str | None = None
    ) -> None:
        with db.transaction(self.conn):
            self.conn.execute(
                "UPDATE session_participants SET runtime_status = ?, runtime_detail = ?, "
                "consecutive_failures = consecutive_failures + 1, runtime_updated_at = ? "
                "WHERE session_id = ? AND name = ?",
                (state, detail, utc_now(), session_id, name),
            )

    def _set_all_participant_states(self, session_id: str, state: str, detail: str) -> None:
        self.conn.execute(
            "UPDATE session_participants SET runtime_status = ?, runtime_detail = ?, "
            "runtime_updated_at = ? WHERE session_id = ?",
            (state, detail, utc_now(), session_id),
        )

    def complete(self, session_id: str, *, force: bool = False) -> SessionRecord:
        """Drive the completion phase machine; only ``closing`` ends the room.

        Completion is deliberately unlike ``stop``, and it is no longer one
        transition. A valid completion request in ``original_work`` does not
        close a session whose dropbox holds side tasks: it atomically freezes
        capture, snapshots the appendix, and advances to
        ``appendix_evaluation``, where both main roles owe every item an
        explicit evaluation. Approvals materialize extension gates, and
        completion advances to ``extension_work`` until they pass. Only then
        does the terminal transition run, exactly as it always has: every
        required gate proven with evidence and both sign-offs, the room
        closed, a permanent delivery record written.

        ``force`` stays the human override at any phase: blocking gates are
        waived and every open appendix item is recorded ``waived`` — explicit
        dispositions on the record, never a silent drop.
        """
        record = self.get(session_id)
        if record.is_terminal:
            raise StateError(f"session is already {record.status}", code="session_finished")
        if not record.room_id:
            raise StateError("cannot complete a session without a room", code="no_room")
        if not self.gates(session_id):
            raise StateError(
                "cannot complete a session with no acceptance gates defined",
                code="no_gates",
            )
        if record.phase == dropbox_module.PHASE_ORIGINAL:
            return self._complete_original_work(record, force=force)
        if record.phase == dropbox_module.PHASE_EVALUATION:
            return self._complete_appendix_evaluation(record, force=force)
        return self._close_completed(record, force=force)

    def _complete_original_work(self, record: SessionRecord, *, force: bool) -> SessionRecord:
        """A valid completion in ``original_work`` advances the phase, not the end.

        The phase flip, the capture freeze, and the appendix snapshot happen
        in one ``BEGIN IMMEDIATE`` transaction — the capture/completion race
        point. A racing capture either lands inside the snapshot or is
        refused as frozen; it is never lost in between.
        """
        session_id = record.session_id
        if force:
            # The human override closes now: the terminal transition waives
            # the blocking gates and records a waiver on every open item.
            return self._close_completed(record, force=True)
        report = summarize(self.gates(session_id))
        if not report["complete"]:
            raise StateError(
                "cannot complete: " + "; ".join(report["blockers"]),
                code="gates_unsatisfied",
            )
        if not dropbox_module.items(self, session_id):
            # No side tasks were ever captured: completion behaves exactly as
            # it always has.
            return self._close_completed(record, force=False)
        with db.transaction(self.conn):
            record = self.get(session_id)
            if record.is_terminal:
                raise StateError(f"session is already {record.status}", code="session_finished")
            if record.phase != dropbox_module.PHASE_ORIGINAL:
                raise StateError(
                    f"completion is already underway: the session moved to {record.phase}",
                    code="phase_advanced",
                )
            report = summarize(self.gates(session_id))
            if not report["complete"]:
                raise StateError(
                    "cannot complete: " + "; ".join(report["blockers"]),
                    code="gates_unsatisfied",
                )
            # Anything captured but not yet reconciled joins the appendix now,
            # inside the freeze transaction, so the snapshot is total; the
            # cutoff then ends investigation — an item whose scout or review
            # never finished gets a timed-out failure report the pair can
            # decline on, so evaluation never waits on work that won't arrive.
            dropbox_module._reconcile_locked(self.conn, session_id)
            cutoff = dropbox_module.freeze_incomplete_locked(self.conn, session_id)
            snapshot = [
                row["drop_id"]
                for row in self.conn.execute(
                    "SELECT drop_id FROM session_appendix WHERE session_id = ? "
                    "ORDER BY position",
                    (session_id,),
                )
            ]
            metadata = {
                **record.metadata,
                "dropbox_snapshot": {"items": snapshot, "frozen_at": utc_now()},
            }
            self._update(
                session_id,
                phase=dropbox_module.PHASE_EVALUATION,
                metadata=json.dumps(metadata),
            )
            self._log(
                record,
                ev.SESSION_PHASE_ADVANCED,
                {
                    "phase": dropbox_module.PHASE_EVALUATION,
                    "snapshot": snapshot,
                    "cutoff_timed_out": cutoff,
                },
            )
            # The conductor exits on idle, so the transition itself enqueues
            # the turn that restarts the loop — atomically with the flip.
            self._send_phase_task(
                record,
                source="appendix_evaluation",
                content=(
                    "The original specification is complete — every required gate "
                    "passed with evidence and both sign-offs. Before the session can "
                    "close, the side-task appendix needs its explicit evaluations: "
                    + ", ".join(snapshot)
                    + ". Read the appendix section of your prompt, judge each item on "
                    "its recorded proposal or failure report, and record your verdict "
                    "with a SYNCHRI-DROP control line. Both of you must evaluate every "
                    "item. When every item has both evaluations, the Primary Builder "
                    "requests completion again with SYNCHRI-COMPLETE."
                ),
            )
        return self.get(session_id)

    def _complete_appendix_evaluation(self, record: SessionRecord, *, force: bool) -> SessionRecord:
        """All snapshot items terminal → extension work, or straight to closing."""
        session_id = record.session_id
        if force:
            return self._close_completed(record, force=True)
        open_items = dropbox_module.pending(self, record)
        if open_items:
            raise StateError(
                "cannot complete: appendix items still need both evaluations: "
                + ", ".join(open_items),
                code="appendix_unevaluated",
            )
        gates = {gate.gate_id: gate for gate in self.gates(session_id)}
        outstanding = [
            entry
            for entry in dropbox_module.approved_extensions(self, session_id)
            if entry["extension_id"] in gates
            and gates[entry["extension_id"]].blocks_completion()
        ]
        if not outstanding:
            return self._close_completed(record, force=False)
        with db.transaction(self.conn):
            record = self.get(session_id)
            if record.is_terminal:
                raise StateError(f"session is already {record.status}", code="session_finished")
            if record.phase != dropbox_module.PHASE_EVALUATION:
                raise StateError(
                    f"the session already moved to {record.phase}",
                    code="phase_advanced",
                )
            ordered = ", ".join(
                f"{entry['extension_id']} (from {entry['drop_id']})" for entry in outstanding
            )
            self._update(session_id, phase=dropbox_module.PHASE_EXTENSION)
            self._log(
                record,
                ev.SESSION_PHASE_ADVANCED,
                {
                    "phase": dropbox_module.PHASE_EXTENSION,
                    "extensions": [entry["extension_id"] for entry in outstanding],
                },
            )
            self._send_phase_task(
                record,
                source="extension_work",
                content=(
                    "Every appendix item has its disposition. The approved extensions "
                    f"are now part of the work, in appendix order: {ordered}. Implement "
                    "each one in the authorized worktree and record evidence on its "
                    "extension gate exactly as you did for the main gates; the session "
                    "closes when every gate passes."
                ),
            )
        return self.get(session_id)

    def _send_phase_task(self, record: SessionRecord, *, source: str, content: str) -> None:
        """Wake the main pair for a phase's work.

        Sent with the human credential and a durable ``human_direction``
        marker, so the lead acts first and the reviewer is guaranteed the
        following turn — the same discipline a human message enforces.
        """
        lead, reviewer = collaboration_pair(record.participants)
        if lead is None or not record.room_id:
            return
        metadata: dict = {"source": source}
        if reviewer is not None:
            metadata["human_direction"] = {"lead": lead.name, "reviewer": reviewer.name}
        self.broker.send(
            record.room_id,
            credential=self._human_credential(record),
            draft=MessageDraft(
                message_type="task",
                target=lead.name,
                metadata=metadata,
                content=content,
            ),
        )

    def _close_completed(self, record: SessionRecord, *, force: bool) -> SessionRecord:
        """The terminal transition: close a verified session, preserve its record.

        This is the only code path that ends a completed room — the
        ``closing`` phase. It proves every required gate (main and
        extension), refuses while any appendix item still owes its
        disposition, closes the room so no more work can be accepted, and
        writes a permanent human-readable delivery record. ``force`` waives
        blocking gates and open appendix items, recorded on each.

        Validation and git work (change summary) run before the write
        transaction so the database lock is never held across subprocess
        calls. The terminal transition re-checks its gates inside the
        transaction, so a racing ``stop`` and ``complete`` can never both
        finish the session and a forced waiver cannot outlive a failed finish.
        """
        session_id = record.session_id
        report = summarize(self.gates(session_id))
        if not report["complete"]:
            if not force:
                raise StateError(
                    "cannot complete: " + "; ".join(report["blockers"]),
                    code="gates_unsatisfied",
                )

        completed_at = utc_now()
        changes = self.changes(session_id)

        changelog_path = self.broker.workspace.final_changelog_path(record.room_id)
        previous_changelog = (
            changelog_path.read_text(encoding="utf-8") if changelog_path.exists() else None
        )
        wrote_changelog = False
        try:
            with db.transaction(self.conn):
                # Re-read under the write lock: another caller may have
                # finished the session while the changelog was rendering.
                record = self.get(session_id)
                if record.is_terminal:
                    raise StateError(f"session is already {record.status}", code="session_finished")
                gates = self.gates(session_id)
                report = summarize(gates)
                waived: list[str] = []
                if not report["complete"]:
                    if not force:
                        raise StateError(
                            "cannot complete: " + "; ".join(report["blockers"]),
                            code="gates_unsatisfied",
                        )
                    for gate in gates:
                        if gate.blocks_completion():
                            gate.status = GateStatus.WAIVED.value
                            gate.evidence = _append_unique(
                                gate.evidence, ["Waived by the user at completion"]
                            )
                            gate.updated_at = utc_now()
                            gate.updated_by = "human"
                            self._write_gate(session_id, gate)
                            waived.append(gate.gate_id)
                    report = summarize(gates)
                    if not report["complete"]:
                        raise StateError(
                            "cannot complete: " + "; ".join(report["blockers"]),
                            code="gates_unsatisfied",
                        )
                # Every appendix item owes an explicit disposition before the
                # room may close — a capture that raced this close is caught
                # here, under the same write lock capture takes.
                open_items = [
                    row["drop_id"]
                    for row in self.conn.execute(
                        "SELECT drop_id FROM session_drops WHERE session_id = ? "
                        "AND disposition IS NULL ORDER BY rowid",
                        (session_id,),
                    )
                ]
                waived_items: list[str] = []
                if open_items:
                    if not force:
                        hint = (
                            " — complete again to begin the appendix evaluation"
                            if record.phase == dropbox_module.PHASE_ORIGINAL
                            else ""
                        )
                        raise StateError(
                            "cannot complete: appendix items still need evaluation: "
                            + ", ".join(open_items)
                            + hint,
                            code="appendix_unevaluated",
                        )
                    waived_items = dropbox_module.waive_pending_locked(
                        self.conn, session_id, "Waived by the user at forced completion"
                    )
                markdown = changelog_module.render(
                    record,
                    gates,
                    changes,
                    self.last_test_run(session_id),
                    completed_at=completed_at,
                )
                reason = (
                    f"completed by the user ({len(waived)} gate{'s' if len(waived) != 1 else ''} waived)"
                    if waived
                    else "all acceptance gates satisfied"
                )
                write_private(changelog_path, markdown)
                wrote_changelog = True

                # Only ``closing`` invokes the terminal room-closing behavior;
                # the flip and the close commit together.
                self._update(session_id, phase=dropbox_module.PHASE_CLOSING)

                # The broker owns terminal room state. Because it shares this
                # connection, its stop is part of this same BEGIN IMMEDIATE
                # transaction rather than a best-effort follow-up.
                self.broker.stop_room(
                    record.room_id, credential=self._human_credential(record)
                )
                metadata = {
                    **record.metadata,
                    "final_changelog": {
                        "path": str(changelog_path),
                        "completed_at": completed_at,
                        "head": changes.get("head"),
                        "commits": changes.get("commits", 0),
                    },
                }
                self._set_all_participant_states(
                    session_id, "stopped", "the session completed"
                )
                completed = self._finish(
                    record,
                    SessionStatus.COMPLETE,
                    reason,
                    metadata=metadata,
                    ended_at=completed_at,
                )
                self._log(
                    completed,
                    ev.SESSION_COMPLETED,
                    {
                        "final_changelog": str(changelog_path),
                        "commits": changes.get("commits", 0),
                        "waived_gates": waived,
                        "waived_items": waived_items,
                    },
                )
        except Exception:
            if wrote_changelog:
                if previous_changelog is None:
                    changelog_path.unlink(missing_ok=True)
                else:
                    write_private(changelog_path, previous_changelog)
            raise
        return completed

    def complete_by_agent(self, session_id: str, actor: str) -> SessionRecord:
        """Allow the designated builder to request the normal verified finish.

        This is intentionally not a bypass: it invokes the same evidence and
        reviewer-signoff checks as a human pressing Complete in the app.  It
        simply gives the agent leading a long-horizon build a durable way to
        close its own room when the work is actually ready.
        """
        record = self.get(session_id)
        plan = next((item for item in record.participants if item.name == actor), None)
        if plan is None or plan.role != "primary_builder":
            raise StateError(
                "only the Primary Builder may complete a session",
                code="agent_not_authorized",
            )
        return self.complete(session_id)

    def final_changelog(self, session_id: str) -> dict:
        """Return the durable artifact created by a successful completion."""
        record = self.get(session_id)
        entry = (record.metadata or {}).get("final_changelog") or {}
        if record.status != SessionStatus.COMPLETE.value or not entry.get("path"):
            raise StateError("this session has not completed yet", code="session_not_complete")
        path = self.broker.workspace.final_changelog_path(record.room_id)
        if not path.exists():
            raise StateError("the final changelog is missing", code="changelog_missing")
        return {"path": str(path), "markdown": path.read_text(encoding="utf-8"), **entry}

    def expire(self, session_id: str) -> dict:
        """Compatibility read for callers that used to enforce a deadline.

        An elapsed timebox is deliberately not an escalation or state change.
        """
        self.get(session_id, sweep=False)
        return self.dashboard(session_id)

    def stop(self, session_id: str, reason: str = "stopped by the user") -> SessionRecord:
        record = self.get(session_id)
        if record.is_terminal:
            return record
        # Stopping a *session* must stop its room in the same durable state
        # transition.  Otherwise a CLI caller could see a stopped session
        # while the queued agents continued to work underneath it.
        with db.transaction(self.conn):
            # Re-read under the write lock: a racing complete() and stop()
            # must never both finish the session.
            record = self.get(session_id)
            if record.is_terminal:
                return record
            if record.room_id:
                self.broker.stop_room(
                    record.room_id, credential=self._human_credential(record)
                )
            self._set_all_participant_states(session_id, "stopped", reason)
            return self._finish(record, SessionStatus.STOPPED, reason)

    def rename_session(self, session_id: str, name: str) -> SessionRecord:
        cleaned = (name or "").strip()
        if not cleaned:
            raise ValidationError("a session needs a name")
        record = self.get(session_id)
        with db.transaction(self.conn):
            self._update(session_id, name=cleaned)
        self._log(record, "session.renamed", {"name": cleaned, "previous": record.name})
        return self.get(session_id)

    def delete_session(self, session_id: str) -> dict:
        """Remove a finished session from Synchri without ever losing git history.

        The database rows and room files always go. The session's worktree and
        local branch are removed only when that is provably safe: Synchri
        created the worktree, nothing in it is uncommitted, and its branch has
        an upstream with no unpushed commits. Anything less keeps the worktree
        on disk, and remote refs are never touched.
        """
        record = self.get(session_id)
        if not record.is_terminal:
            raise StateError(
                "stop the session before deleting it", code="session_not_finished"
            )

        tree = record.worktree
        remove_tree = False
        if tree and worktree_module.exists(tree):
            if (record.metadata or {}).get("worktree_strategy") != "new":
                worktree_note = "worktree kept — you selected it yourself, so Synchri never removes it"
            else:
                remove_tree, worktree_note = self._worktree_safely_removable(tree)
        else:
            worktree_note = "no worktree on disk"

        # Ancillary scratch trees and branches are ephemeral by contract:
        # session deletion always releases them, wherever retention stood.
        scratch_trees = [
            (row["scratch_path"], row["scratch_branch"])
            for row in self.conn.execute(
                "SELECT scratch_path, scratch_branch FROM session_drops "
                "WHERE session_id = ? AND (scratch_path IS NOT NULL "
                "OR scratch_branch IS NOT NULL)",
                (session_id,),
            )
        ]

        with db.transaction(self.conn):
            self.conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            if record.room_id:
                self.conn.execute("DELETE FROM rooms WHERE room_id = ?", (record.room_id,))
            self.conn.execute(
                "DELETE FROM agent_turn_usage WHERE session_id = ?", (session_id,)
            )

        # Post-commit, best-effort disk cleanup; the durable rows are gone.
        for scratch_path, scratch_branch in scratch_trees:
            worktree_module.remove_scratch(record.repo_root, scratch_path, scratch_branch)
        worktree_removed = False
        if remove_tree:
            try:
                worktree_module.remove(tree, force=True, delete_branch=True)
                worktree_removed = True
            except StateError as error:
                worktree_note = f"worktree kept — {error.message}"
        if record.room_id:
            shutil.rmtree(self.broker.workspace.room_dir(record.room_id), ignore_errors=True)
            if self.broker.workspace.sessions_dir.exists():
                for path in self.broker.workspace.sessions_dir.glob(f"{record.room_id}.*.json"):
                    path.unlink(missing_ok=True)
        return {
            "deleted": session_id,
            "worktree_removed": worktree_removed,
            "worktree_note": worktree_note,
        }

    @staticmethod
    def _worktree_safely_removable(tree: Worktree) -> tuple[bool, str]:
        """Safe means: clean, and every commit already lives on the upstream."""
        status = worktree_module.git(tree.path, "status", "--porcelain", check=False)
        if status.strip():
            return False, "worktree kept — it has uncommitted changes"
        upstream = worktree_module.git(
            tree.path, "rev-parse", "--abbrev-ref", "@{upstream}", check=False
        )
        if not upstream.strip():
            return False, "worktree kept — its branch was never pushed"
        unpushed = worktree_module.git(
            tree.path, "rev-list", "--count", "@{upstream}..HEAD", check=False
        )
        if unpushed.strip() and unpushed.strip() != "0":
            plural = "s" if unpushed.strip() != "1" else ""
            return False, f"worktree kept — {unpushed.strip()} commit{plural} not pushed"
        return True, "worktree removed — everything in it is pushed"

    def pause(self, session_id: str) -> SessionRecord:
        record = self.get(session_id)
        if record.status != SessionStatus.ACTIVE.value:
            raise StateError(f"cannot pause a session that is {record.status}", code="session_not_active")
        return self._finish(record, SessionStatus.PAUSED, "paused by the user")

    def resume(self, session_id: str) -> SessionRecord:
        record = self.get(session_id)
        if record.status != SessionStatus.PAUSED.value:
            raise StateError(f"cannot resume a session that is {record.status}", code="session_not_paused")
        self._update(session_id, status=SessionStatus.ACTIVE.value, ended_at=None, ended_reason=None)
        self._log(record, ev.SESSION_ACTIVATED, {"resumed": True})
        return self.get(session_id)

    def handoff_report(self, session_id: str) -> dict:
        """The honest end-of-session state. Never claims more than the gates show."""
        record = self.get(session_id)
        gates = self.gates(session_id)
        report = summarize(gates)
        tree = record.worktree
        head = None
        if tree and worktree_module.exists(tree):
            head = worktree_module.git(tree.path, "rev-parse", "HEAD", check=False) or None
        return {
            "session_id": session_id,
            "status": record.status,
            "reason_stopped": record.ended_reason,
            "complete": record.status == SessionStatus.COMPLETE.value,
            "final_changelog": ((record.metadata or {}).get("final_changelog") or {}).get("path"),
            "branch": record.worktree_branch,
            "head": head,
            "worktree": record.worktree_path,
            "gates": {
                "satisfied": [g.gate_id for g in gates if g.is_satisfied],
                "unmet": [g.gate_id for g in gates if not g.is_satisfied],
                "unverified": [g.gate_id for g in gates if g.status == "unverified"],
                "summary": report,
            },
            "open_blockers": report["blockers"],
            "open_escalations": self.open_escalations(session_id),
            "recommended_next_action": self._recommend(record, report),
        }

    # ------------------------------------------------------------------
    # evidence
    # ------------------------------------------------------------------

    def run_tests(self, session_id: str, command: str | None = None) -> dict:
        """Run the project's own tests in the authorized worktree.

        The result is recorded on the session so the dashboard shows measured
        numbers rather than an agent's summary of them.
        """
        record = self.get(session_id)
        if not record.worktree or not worktree_module.exists(record.worktree):
            raise StateError("the authorized worktree is missing", code="worktree_missing")
        result = verify_module.run_tests(
            record.worktree_path, command or (record.metadata or {}).get("test_command")
        )
        metadata = {**record.metadata, "last_test_run": result.to_dict()}
        with db.transaction(self.conn):
            self._update(session_id, metadata=json.dumps(metadata))
        return result.to_dict()

    def last_test_run(self, session_id: str) -> dict | None:
        return (self.get(session_id).metadata or {}).get("last_test_run")

    def set_test_command(self, session_id: str, command: str) -> None:
        record = self.get(session_id)
        metadata = {**record.metadata, "test_command": command}
        with db.transaction(self.conn):
            self._update(session_id, metadata=json.dumps(metadata))

    def changes(self, session_id: str) -> dict:
        """Commits and diff stats the session actually produced."""
        record = self.get(session_id)
        if not record.worktree or not worktree_module.exists(record.worktree):
            return verify_module.ChangeSummary().to_dict()
        return verify_module.summarize_changes(
            record.worktree_path, record.base_branch
        ).to_dict()

    def diff(self, session_id: str) -> str:
        record = self.get(session_id)
        if not record.worktree or not worktree_module.exists(record.worktree):
            return ""
        return verify_module.diff_text(record.worktree_path, record.base_branch)

    def file_diff(self, session_id: str, path: str) -> dict:
        """One file's live diff (committed + uncommitted) for the chat cards."""
        record = self.get(session_id)
        if not record.worktree or not worktree_module.exists(record.worktree):
            return {"path": path, "diff": "", "insertions": 0, "deletions": 0, "truncated": False}
        return verify_module.file_diff(record.worktree_path, record.base_branch, path)

    def file_changes(self, session_id: str) -> list[dict]:
        """Per-file diff cards for the Changes tab, uncommitted work included."""
        record = self.get(session_id)
        if not record.worktree or not worktree_module.exists(record.worktree):
            return []
        return verify_module.file_changes(record.worktree_path, record.base_branch)

    def detected_test_command(self, session_id: str) -> str | None:
        record = self.get(session_id)
        if not record.worktree_path:
            return None
        return (record.metadata or {}).get("test_command") or verify_module.detect_test_command(
            record.worktree_path
        )

    # ------------------------------------------------------------------
    # escalations
    # ------------------------------------------------------------------

    def escalate(self, session_id: str, rule: str, detail: str = "", raised_by: str | None = None) -> dict:
        record = self.get(session_id)
        escalation_id = new_id("esc")
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO session_escalations (escalation_id, session_id, rule, raised_by, "
                "detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (escalation_id, session_id, rule, raised_by, detail, utc_now()),
            )
        self._log(record, ev.SESSION_ESCALATED, {"rule": rule, "raised_by": raised_by})
        return {"escalation_id": escalation_id, "rule": rule, "detail": detail}

    def open_escalations(self, session_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM session_escalations WHERE session_id = ? AND resolved_at IS NULL "
            "ORDER BY created_at",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def resolve_escalation(self, escalation_id: str, resolution: str) -> None:
        self.conn.execute(
            "UPDATE session_escalations SET resolved_at = ?, resolution = ? WHERE escalation_id = ?",
            (utc_now(), resolution, escalation_id),
        )

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def get(self, session_id: str, *, sweep: bool = True) -> SessionRecord:
        """Read a session.

        ``sweep`` remains for compatibility with earlier callers. A timebox is
        advisory now, so reading a session never ends its work behind the
        user's back.
        """
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"no such session: {session_id}")
        record = self._hydrate(row)
        return record

    @staticmethod
    def _should_expire(record: SessionRecord) -> bool:
        return False

    def sweep_expired(self) -> list[str]:
        """Compatibility no-op: timeboxes are intentionally not hard stops."""
        return []

    def list_sessions(self, *, repo_root: str | None = None, active_only: bool = False) -> list[SessionRecord]:
        sql = "SELECT * FROM sessions"
        params: list[object] = []
        clauses = []
        if repo_root:
            clauses.append("repo_root = ?")
            params.append(str(Path(repo_root).expanduser().resolve()))
        if active_only:
            placeholders = ", ".join("?" for _ in TERMINAL)
            clauses.append(f"status NOT IN ({placeholders})")
            params.extend(sorted(TERMINAL))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC"
        return [self._hydrate(row) for row in self.conn.execute(sql, params).fetchall()]

    def dashboard(self, session_id: str) -> dict:
        """Everything a mission-control view needs, in one read."""
        record = self.get(session_id)
        gates = self.gates(session_id)
        report = summarize(gates)
        acknowledgments = (
            self.acknowledgment_state(session_id) if record.contract_revision else None
        )
        room_status = None
        if record.room_id:
            try:
                room_status = self.broker.room_status(
                    record.room_id, credential=self._human_credential(record)
                )
            except Exception:  # pragma: no cover - room may have been removed
                room_status = None

        changes = (
            verify_module.summarize_changes(record.worktree_path, record.base_branch).to_dict()
            if record.worktree and worktree_module.exists(record.worktree)
            else verify_module.ChangeSummary().to_dict()
        )
        deadline = record.deadline
        return {
            "session": record.to_dict(),
            "status": record.status,
            "time_remaining": deadline.remaining_text() if deadline else None,
            "deadline_expired": bool(deadline and deadline.is_expired()),
            "phase": deadline.phase()[0] if deadline else None,
            "phase_guidance": deadline.phase()[1] if deadline else None,
            "current_actor": (room_status or {}).get("active_speaker"),
            "active_turn": (room_status or {}).get("active_turn"),
            "queue": (room_status or {}).get("queue", []),
            "activities": (room_status or {}).get("activities", []),
            "activity_entries": (room_status or {}).get("activity_entries", []),
            "gates": {"summary": report, "items": [g.to_dict() for g in gates]},
            "dropbox": {
                "phase": record.phase,
                "summary": dropbox_module.summarize(self, session_id),
                "items": dropbox_module.items(self, session_id),
            },
            "participant_states": self.participant_states(session_id),
            "tests": (record.metadata or {}).get("last_test_run"),
            "test_command": self.detected_test_command(session_id),
            "changes": changes,
            "open_blockers": report["blockers"],
            "open_escalations": self.open_escalations(session_id),
            "acknowledgments": acknowledgments,
            "user_intervention_required": bool(
                self.open_escalations(session_id)
                or record.status
                in {SessionStatus.AWAITING_ACK.value, SessionStatus.PAUSED.value}
            ),
            "worktree_present": bool(record.worktree and worktree_module.exists(record.worktree)),
        }

    def restore(self, session_id: str) -> dict:
        """What to tell the user after a restart, and what is no longer true.

        Deliberately does not resume anything: participants must reconnect, and
        a mutating action never restarts on its own.
        """
        record = self.get(session_id)
        problems: list[str] = []
        tree_ok = bool(record.worktree and worktree_module.exists(record.worktree))
        if not tree_ok:
            problems.append(f"the authorized worktree is gone: {record.worktree_path}")
        repo = worktree_module.inspect_repository(record.repo_root)
        if not repo.is_valid:
            problems.extend(repo.problems)
        expired = bool(record.deadline and record.deadline.is_expired())
        if expired and not record.is_terminal:
            problems.append("the timebox elapsed while Synchri was not running; work can continue")

        blocking_problems = [
            problem for problem in problems if not problem.startswith("the timebox elapsed")
        ]
        return {
            "session": record.to_dict(),
            "was_active": record.status == SessionStatus.ACTIVE.value,
            "worktree_present": tree_ok,
            "repository_valid": repo.is_valid,
            "deadline_expired": expired,
            "problems": problems,
            "participants_must_reconnect": [p.name for p in record.participants],
            "resumable": not blocking_problems and not record.is_terminal,
            "contract_revision": record.contract_revision,
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _finish(
        self,
        record: SessionRecord,
        status: SessionStatus,
        reason: str,
        *,
        metadata: dict | None = None,
        ended_at: str | None = None,
    ) -> SessionRecord:
        fields = {
            "status": status.value,
            "ended_at": ended_at or utc_now(),
            "ended_reason": reason,
        }
        if metadata is not None:
            fields["metadata"] = json.dumps(metadata)
        self._update(record.session_id, **fields)
        self._log(record, ev.SESSION_ENDED, {"status": status.value, "reason": reason})
        return self.get(record.session_id)

    def _recommend(self, record: SessionRecord, report: dict) -> str:
        if record.status == SessionStatus.COMPLETE.value:
            return f"Review branch {record.worktree_branch} and merge if you agree."
        if report["blockers"]:
            return (
                f"Resume on branch {record.worktree_branch} and clear: "
                + "; ".join(report["blockers"][:3])
            )
        if not report["total"]:
            return "Define acceptance gates from the specification, then resume."
        return f"Review branch {record.worktree_branch} in {record.worktree_path}."

    def _human_credential(self, record: SessionRecord) -> Credential:
        human = (record.metadata or {}).get("human") or {}
        return Credential(participant=human.get("name"), secret=human.get("secret"))

    def _log(self, record: SessionRecord, event_type: str, payload: dict) -> None:
        """Session events land in the room's audit log, keeping one history."""
        if not record.room_id:
            return
        try:
            with db.transaction(self.conn):
                from ..storage import dao

                dao.insert_event(
                    self.conn,
                    record.room_id,
                    event_type,
                    seq=dao.next_seq(self.conn, record.room_id),
                    actor_name="synchri",
                    payload={"session_id": record.session_id, **payload},
                )
        except Exception:  # pragma: no cover - audit must never break the operation
            pass

    def _insert(self, record: SessionRecord) -> None:
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO sessions (session_id, room_id, name, mode, status, created_at, "
                "updated_at, repo_root, repo_name, repo_remote, base_branch, worktree_name, "
                "worktree_path, worktree_branch, permissions, spec, deadline, escalation, "
                "metadata, contract_revision) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.session_id, record.room_id, record.name, record.mode, record.status,
                    record.created_at, record.updated_at, record.repo_root, record.repo_name,
                    record.repo_remote, record.base_branch, record.worktree_name,
                    record.worktree_path, record.worktree_branch,
                    json.dumps(record.permissions.to_dict()),
                    json.dumps(record.spec.to_dict()) if record.spec else None,
                    json.dumps(record.deadline.to_dict()) if record.deadline else None,
                    json.dumps(record.escalation.to_dict()),
                    json.dumps(record.metadata),
                    record.contract_revision,
                ),
            )
            self._insert_participants(record.session_id, record.participants)

    def _insert_participants(self, session_id: str, participants: list[ParticipantPlan]) -> None:
        for plan in participants:
            self.conn.execute(
                "INSERT INTO session_participants (session_id, name, runtime, role, command, "
                "status, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id, plan.name, plan.runtime, plan.role, plan.command,
                    "invited", json.dumps(plan.metadata),
                ),
            )

    def _write_gate(self, session_id: str, gate: Gate) -> None:
        self.conn.execute(
            "INSERT INTO session_gates (session_id, gate_id, description, status, required, "
            "evidence, tests, commits, builder_assessment, reviewer_assessment, updated_at, "
            "updated_by, origin_kind, drop_id, extension_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(session_id, gate_id) DO UPDATE SET description=excluded.description, "
            "status=excluded.status, required=excluded.required, evidence=excluded.evidence, "
            "tests=excluded.tests, commits=excluded.commits, "
            "builder_assessment=excluded.builder_assessment, "
            "reviewer_assessment=excluded.reviewer_assessment, updated_at=excluded.updated_at, "
            "updated_by=excluded.updated_by, origin_kind=excluded.origin_kind, "
            "drop_id=excluded.drop_id, extension_id=excluded.extension_id",
            (
                session_id, gate.gate_id, gate.description, gate.status, int(gate.required),
                json.dumps(gate.evidence), json.dumps(gate.tests), json.dumps(gate.commits),
                gate.builder_assessment, gate.reviewer_assessment, gate.updated_at,
                gate.updated_by, gate.origin_kind or "main", gate.drop_id, gate.extension_id,
            ),
        )

    def _update(self, session_id: str, **fields) -> None:
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        self.conn.execute(
            f"UPDATE sessions SET {assignments} WHERE session_id = ?",
            (*fields.values(), session_id),
        )

    def _hydrate(self, row) -> SessionRecord:
        data = dict(row)
        participants = [
            ParticipantPlan(
                name=p["name"],
                runtime=p["runtime"],
                role=p["role"],
                command=p["command"],
                metadata=json.loads(p["metadata"] or "{}"),
            )
            for p in self.conn.execute(
                "SELECT * FROM session_participants WHERE session_id = ? ORDER BY rowid",
                (data["session_id"],),
            ).fetchall()
        ]
        return SessionRecord(
            session_id=data["session_id"],
            room_id=data["room_id"],
            name=data["name"],
            mode=data["mode"],
            status=data["status"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            activated_at=data["activated_at"],
            ended_at=data["ended_at"],
            ended_reason=data["ended_reason"],
            repo_root=data["repo_root"],
            repo_name=data["repo_name"],
            repo_remote=data["repo_remote"],
            base_branch=data["base_branch"],
            worktree_name=data["worktree_name"],
            worktree_path=data["worktree_path"],
            worktree_branch=data["worktree_branch"],
            permissions=PermissionSet.from_dict(json.loads(data["permissions"] or "{}")),
            spec=ProductSpec.from_dict(json.loads(data["spec"]) if data["spec"] else None),
            deadline=Deadline.from_dict(json.loads(data["deadline"]) if data["deadline"] else None),
            escalation=EscalationPolicy.from_dict(json.loads(data["escalation"] or "{}")),
            metadata=json.loads(data["metadata"] or "{}"),
            contract_revision=data["contract_revision"],
            participants=participants,
            phase=data["phase"] if "phase" in data.keys() and data["phase"] else "original_work",
        )
