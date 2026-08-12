"""JSON API behind the local UI.

Every route is a thin wrapper over :class:`SessionManager` or :class:`Broker`.
The UI holds no rules of its own — it cannot activate a session the manager
would refuse, because it asks the manager. That is what keeps the terminal and
the browser honest with each other.
"""

from __future__ import annotations

from typing import Callable

from ..broker import Broker, Credential
from ..errors import NotFoundError, ValidationError
from ..session import discovery, presets as presets_module
from ..session.draft import SessionDraft
from ..session.escalation import CATALOG as ESCALATION_CATALOG
from ..session.extract import describe as describe_gates, extract_gates
from ..session.gates import Gate
from ..session.manager import SessionManager
from ..session.modes import KNOWN_RUNTIMES, ParticipantPlan, Role, list_modes
from ..session.permissions import PermissionSet
from ..session.spec import ProductSpec

Route = Callable[[dict, dict], dict]


class Api:
    """Routing table for the local UI."""

    def __init__(self, broker: Broker, manager: SessionManager) -> None:
        self.broker = broker
        self.manager = manager
        #: Wizard drafts live in memory, keyed per browser session. They hold no
        #: authority: nothing exists on disk until "Start session".
        self.drafts: dict[str, SessionDraft] = {}
        self._routes: dict[tuple[str, str], Route] = {
            ("GET", "bootstrap"): self.bootstrap,
            ("GET", "repositories"): self.repositories,
            ("POST", "draft"): self.update_draft,
            ("GET", "draft"): self.get_draft,
            ("POST", "draft/reset"): self.reset_draft,
            ("POST", "start"): self.start,
            ("GET", "sessions"): self.sessions,
            ("GET", "session"): self.session,
            ("GET", "dashboard"): self.dashboard,
            ("GET", "contract"): self.contract,
            ("POST", "ack"): self.acknowledge,
            ("POST", "activate"): self.activate,
            ("GET", "conversation"): self.conversation,
            ("POST", "message"): self.message,
            ("GET", "gates"): self.gates,
            ("POST", "gate"): self.update_gate,
            ("POST", "tests/run"): self.run_tests,
            ("GET", "changes"): self.changes,
            ("GET", "diff"): self.diff,
            ("GET", "memory"): self.memory,
            ("GET", "events"): self.events,
            ("POST", "control"): self.control,
            ("GET", "presets"): self.presets,
            ("POST", "preset"): self.save_preset,
        }

    def route(self, method: str, path: str) -> Route | None:
        return self._routes.get((method, path.strip("/")))

    # -- wizard --------------------------------------------------------

    def bootstrap(self, query: dict, body: dict) -> dict:
        """Everything the app needs on first paint."""
        return {
            "modes": list_modes(),
            "runtimes": [{"key": k, **v} for k, v in KNOWN_RUNTIMES.items()],
            "roles": [
                {"key": r.value, "label": r.value.replace("_", " ").title()} for r in Role
            ],
            "permissions": PermissionSet.defaults().grouped(),
            "escalation_rules": [r.to_dict() for r in ESCALATION_CATALOG],
            "presets": presets_module.list_presets(self.broker.workspace),
            "sessions": [s.to_dict() for s in self.manager.list_sessions()],
            "workspace": str(self.broker.workspace.home),
        }

    def repositories(self, query: dict, body: dict) -> dict:
        return discovery.repositories(include_github=query.get("github") != "0")

    def _draft(self, key: str) -> SessionDraft:
        return self.drafts.setdefault(key or "default", SessionDraft())

    def get_draft(self, query: dict, body: dict) -> dict:
        return self._draft_payload(self._draft(query.get("draft", "default")))

    def reset_draft(self, query: dict, body: dict) -> dict:
        key = body.get("draft", "default")
        self.drafts[key] = SessionDraft()
        return self._draft_payload(self.drafts[key])

    def update_draft(self, query: dict, body: dict) -> dict:
        """Apply one wizard step. Validation lives in the draft, not here."""
        draft = self._draft(body.get("draft", "default"))
        if body.get("preset"):
            draft = SessionDraft.from_preset(
                presets_module.load(self.broker.workspace, body["preset"])
            )
            self.drafts[body.get("draft", "default")] = draft
        if body.get("mode"):
            draft.set_mode(body["mode"])
        if body.get("repo_path"):
            draft.set_repository(body["repo_path"], body.get("base_branch"))
        elif body.get("base_branch") and draft.repo_path:
            draft.set_repository(draft.repo_path, body["base_branch"])
        if "worktree_name" in body or "worktree_parent" in body:
            draft.set_worktree(body.get("worktree_name") or None, body.get("worktree_parent") or None)
        if body.get("agents") is not None:
            draft.set_agents(
                [
                    ParticipantPlan(
                        name=a["name"],
                        runtime=a.get("runtime", "generic"),
                        role=a.get("role", "participant"),
                        command=a.get("command"),
                    )
                    for a in body["agents"]
                ]
            )
        for key, decision in (body.get("permissions") or {}).items():
            draft.set_permission(key, decision)
        if body.get("spec") is not None:
            if body["spec"].strip():
                draft.set_spec(body["spec"])
            else:
                draft.spec = None
        if body.get("deadline"):
            draft.set_deadline_duration(body["deadline"])
        elif body.get("deadline_at"):
            draft.set_deadline_at(body["deadline_at"])
        if body.get("name"):
            draft.name = body["name"]
        return self._draft_payload(draft)

    def _draft_payload(self, draft: SessionDraft) -> dict:
        gates = extract_gates(draft.spec.canonical_text()) if draft.spec else []
        return {
            "draft": {
                "mode": draft.mode,
                "repo_path": draft.repo_path,
                "base_branch": draft.base_branch,
                "worktree_name": draft.worktree_name,
                "agents": [p.to_dict() for p in draft.participants],
                "permissions": draft.permissions.to_dict(),
                "spec": draft.spec.text if draft.spec else "",
                "deadline": draft.deadline.to_dict() if draft.deadline else None,
                "name": draft.name,
            },
            "steps": [{"key": k, "label": label} for k, label in draft.visible_steps()],
            "summary": draft.summary() if draft.mode else None,
            "detected_gates": [g.to_dict() for g in gates],
            "gate_note": describe_gates(gates),
            "problems": draft.blocking_problems() if draft.mode else ["choose a mode"],
            "ready": draft.is_ready,
            "repo_status": draft.repo_status().to_dict() if draft.repo_path else None,
        }

    def start(self, query: dict, body: dict) -> dict:
        draft = self._draft(body.get("draft", "default"))
        if not draft.is_ready:
            raise ValidationError("; ".join(draft.blocking_problems()))
        record = self.manager.create(
            name=draft.name or "Synchri session",
            mode=draft.mode,
            repo_root=draft.repo_path,
            base_branch=draft.base_branch,
            participants=draft.participants,
            permissions=draft.permissions,
            spec=draft.spec,
            deadline=draft.deadline,
            escalation=draft.escalation,
            worktree_parent=draft.worktree_parent,
            worktree_name=draft.worktree_name,
        )
        document = self.manager.issue_contract(record.session_id, reason="initial contract")
        if body.get("save_preset"):
            presets_module.save(self.broker.workspace, body["save_preset"], draft.to_preset())
        self.drafts.pop(body.get("draft", "default"), None)
        return {
            "session": self.manager.get(record.session_id).to_dict(),
            "contract": document.to_dict(),
        }

    # -- sessions ------------------------------------------------------

    def _session_id(self, query: dict, body: dict | None = None) -> str:
        session_id = (body or {}).get("session") or query.get("session")
        if session_id:
            return session_id
        sessions = self.manager.list_sessions()
        if not sessions:
            raise NotFoundError("no sessions yet")
        return sessions[0].session_id

    def sessions(self, query: dict, body: dict) -> dict:
        return {"sessions": [s.to_dict() for s in self.manager.list_sessions()]}

    def session(self, query: dict, body: dict) -> dict:
        return self.manager.get(self._session_id(query)).to_dict()

    def dashboard(self, query: dict, body: dict) -> dict:
        return self.manager.dashboard(self._session_id(query))

    def contract(self, query: dict, body: dict) -> dict:
        session_id = self._session_id(query)
        document = self.manager.current_contract(session_id)
        payload = document.to_dict()
        payload["per_participant"] = {
            plan.name: document.for_participant(plan.name)
            for plan in self.manager.get(session_id).participants
        }
        payload["acknowledgments"] = self.manager.acknowledgment_state(session_id)
        return payload

    def acknowledge(self, query: dict, body: dict) -> dict:
        session_id = self._session_id(query, body)
        result = self.manager.acknowledge(
            session_id, body.get("participant", ""), body.get("reply", "")
        )
        return {**result, "state": self.manager.acknowledgment_state(session_id)}

    def activate(self, query: dict, body: dict) -> dict:
        return self.manager.activate(self._session_id(query, body)).to_dict()

    # -- tabs ----------------------------------------------------------

    def conversation(self, query: dict, body: dict) -> dict:
        record = self.manager.get(self._session_id(query))
        if not record.room_id:
            return {"messages": []}
        return self.broker.read(record.room_id, credential=self._human(record))

    def message(self, query: dict, body: dict) -> dict:
        """The human speaking. Always outranks the queue."""
        from ..models.envelope import MessageDraft

        record = self.manager.get(self._session_id(query, body))
        return self.broker.send(
            record.room_id,
            credential=self._human(record),
            draft=MessageDraft(
                content=body.get("content", ""),
                message_type="interrupt" if body.get("interrupt") else "chat",
                target=body.get("target") or None,
            ),
        )

    def gates(self, query: dict, body: dict) -> dict:
        session_id = self._session_id(query)
        from ..session.gates import summarize

        gates = self.manager.gates(session_id)
        return {"gates": [g.to_dict() for g in gates], "summary": summarize(gates)}

    def update_gate(self, query: dict, body: dict) -> dict:
        session_id = self._session_id(query, body)
        if body.get("replace"):
            self.manager.set_gates(
                session_id,
                [Gate(gate_id=g["gate_id"], description=g["description"]) for g in body["replace"]],
            )
            return self.gates({"session": session_id}, {})
        fields = {k: v for k, v in body.items() if k in {
            "status", "evidence", "tests", "commits", "builder_assessment",
            "reviewer_assessment", "description", "required",
        }}
        gate = self.manager.update_gate(
            session_id, body["gate_id"], actor=body.get("actor", "human"), **fields
        )
        return gate.to_dict()

    def run_tests(self, query: dict, body: dict) -> dict:
        session_id = self._session_id(query, body)
        if body.get("command"):
            self.manager.set_test_command(session_id, body["command"])
        return self.manager.run_tests(session_id, body.get("command"))

    def changes(self, query: dict, body: dict) -> dict:
        return self.manager.changes(self._session_id(query))

    def diff(self, query: dict, body: dict) -> dict:
        return {"diff": self.manager.diff(self._session_id(query))}

    def memory(self, query: dict, body: dict) -> dict:
        record = self.manager.get(self._session_id(query))
        if not record.room_id:
            return {"markdown": ""}
        return self.broker.memory_show(record.room_id, credential=self._human(record))

    def events(self, query: dict, body: dict) -> dict:
        record = self.manager.get(self._session_id(query))
        if not record.room_id:
            return {"events": []}
        return self.broker.events(
            record.room_id, credential=self._human(record), since_seq=int(query.get("since", 0))
        )

    def control(self, query: dict, body: dict) -> dict:
        """Pause, resume, stop, escalate — the human's controls."""
        session_id = self._session_id(query, body)
        record = self.manager.get(session_id)
        action = body.get("action")
        credential = self._human(record)

        if action == "pause":
            self.broker.pause_room(record.room_id, credential=credential)
        elif action == "resume":
            self.broker.resume_room(record.room_id, credential=credential)
        elif action == "stop":
            if record.room_id:
                self.broker.stop_room(record.room_id, credential=credential)
            self.manager.stop(session_id, body.get("reason") or "stopped by the user")
        elif action == "remove":
            self.broker.remove_participant(
                record.room_id, body["participant"], credential=credential
            )
        elif action == "escalate":
            self.manager.escalate(session_id, body.get("rule", "user_interrupt"), body.get("detail", ""))
        elif action == "permissions":
            self.manager.update_configuration(
                session_id,
                permissions=PermissionSet.from_dict(body.get("permissions")),
                reason="permissions changed by the user",
            )
        elif action == "spec":
            current = record.spec
            revised = (
                current.revise(text=body["spec"]) if current else ProductSpec(text=body["spec"])
            )
            self.manager.update_configuration(
                session_id, spec=revised, reason="specification changed by the user"
            )
        elif action == "deadline":
            from ..session.deadline import Deadline

            self.manager.update_configuration(
                session_id,
                deadline=Deadline.from_duration(body["deadline"]),
                reason="deadline changed by the user",
            )
        else:
            raise ValidationError(f"unknown control action {action!r}")
        return self.manager.dashboard(session_id)

    # -- presets -------------------------------------------------------

    def presets(self, query: dict, body: dict) -> dict:
        return {"presets": presets_module.list_presets(self.broker.workspace)}

    def save_preset(self, query: dict, body: dict) -> dict:
        draft = self._draft(body.get("draft", "default"))
        presets_module.save(self.broker.workspace, body["name"], draft.to_preset())
        return {"presets": presets_module.list_presets(self.broker.workspace)}

    # -- internals -----------------------------------------------------

    def _human(self, record) -> Credential:
        human = (record.metadata or {}).get("human") or {}
        return Credential(participant=human.get("name"), secret=human.get("secret"))
