"""JSON API behind the local UI.

Every route is a thin wrapper over :class:`SessionManager` or :class:`Broker`.
The UI holds no rules of its own — it cannot activate a session the manager
would refuse, because it asks the manager. That is what keeps the terminal and
the browser honest with each other.
"""

from __future__ import annotations

import shlex
from typing import Callable

from ..broker import Broker, Credential
from ..errors import NotFoundError, ValidationError
from ..session import discovery, drafts as drafts_module, presets as presets_module
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

    def __init__(
        self, broker: Broker, manager: SessionManager, *, default_repo: str | None = None
    ) -> None:
        self.broker = broker
        self.manager = manager
        # ``synchri ui`` is normally launched from the repository being worked
        # on.  Keeping that small piece of context makes the default path a
        # room launch, not a repository-discovery exercise.  The draft still
        # validates it before anything is created.
        self.default_repo = self._valid_repository(default_repo)
        #: Wizard drafts are persisted, so closing the app does not lose an
        #: unfinished wizard and two tabs on one draft stay in step. They hold
        #: no authority: nothing is created until "Start session".
        self._routes: dict[tuple[str, str], Route] = {
            ("GET", "bootstrap"): self.bootstrap,
            ("GET", "repositories"): self.repositories,
            ("POST", "draft"): self.update_draft,
            ("GET", "draft"): self.get_draft,
            ("POST", "draft/reset"): self.reset_draft,
            ("POST", "start"): self.start,
            ("POST", "quick-start"): self.quick_start,
            ("GET", "launch"): self.launch,
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
            "default_repo": self.default_repo,
            "desktop_clone_root": str(discovery.desktop_clone_root()),
            "open_drafts": drafts_module.versions(self.broker.conn),
        }

    def repositories(self, query: dict, body: dict) -> dict:
        return discovery.repositories(
            include_github=query.get("github") != "0",
            include_local=query.get("local") != "0",
        )

    @staticmethod
    def _valid_repository(path: str | None) -> str | None:
        """Never turn the terminal's incidental CWD into a broken default."""
        if not path:
            return None
        from ..session import worktree as worktree_module

        status = worktree_module.inspect_repository(path)
        return status.root if status.is_valid else None

    def _draft(self, key: str) -> SessionDraft:
        stored = drafts_module.load(self.broker.conn, key or "default")
        return SessionDraft.from_state(stored[0]) if stored else SessionDraft()

    def _persist(self, key: str, draft: SessionDraft) -> int:
        return drafts_module.save(self.broker.conn, key or "default", draft.to_state())

    def get_draft(self, query: dict, body: dict) -> dict:
        key = query.get("draft", "default")
        return self._draft_payload(self._draft(key), key)

    def reset_draft(self, query: dict, body: dict) -> dict:
        key = body.get("draft", "default")
        drafts_module.delete(self.broker.conn, key)
        return self._draft_payload(SessionDraft(), key)

    def update_draft(self, query: dict, body: dict) -> dict:
        """Apply one wizard step. Validation lives in the draft, not here."""
        key = body.get("draft", "default")
        draft = self._draft(key)
        if body.get("preset"):
            draft = SessionDraft.from_preset(
                presets_module.load(self.broker.workspace, body["preset"])
            )
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
        for capability, decision in (body.get("permissions") or {}).items():
            draft.set_permission(capability, decision)
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
        version = self._persist(key, draft)
        return self._draft_payload(draft, key, version)

    def _draft_payload(
        self, draft: SessionDraft, key: str = "default", version: int | None = None
    ) -> dict:
        if version is None:
            stored = drafts_module.load(self.broker.conn, key)
            version = stored[1] if stored else 0
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
            "draft_id": key,
            "version": version,
        }

    def start(self, query: dict, body: dict) -> dict:
        key = body.get("draft", "default")
        draft = self._draft(key)
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
        drafts_module.delete(self.broker.conn, key)
        return self._launch_payload(self.manager.get(record.session_id), document=document)

    def quick_start(self, query: dict, body: dict) -> dict:
        """Create the safe, collaborative default without walking eight screens.

        The detailed draft remains available for a long-running autonomous job,
        a non-default branch, or unusual permissions.  Most people just need a
        worktree, a room, and paste-ready arrivals for the agents they already
        have open.
        """
        repo_path = (body.get("repo_path") or self.default_repo or "").strip()
        goal = (body.get("goal") or "").strip()
        if not repo_path:
            raise ValidationError("choose the repository for this room")
        if not goal:
            raise ValidationError("describe what you want the agents to do")

        cloned = None
        if discovery.github_reference(repo_path):
            cloned = discovery.clone_github_repository(repo_path)
            repo_path = cloned["path"]

        draft = SessionDraft()
        draft.set_mode("interactive")
        draft.set_repository(repo_path)
        draft.set_agents(self._plans(body.get("agents")))
        # Interactive mode permits an empty spec, but a quick room should
        # always carry the user's stated goal into the ledger and contract.
        draft.set_spec(goal)
        if body.get("name"):
            draft.name = body["name"]

        record = self.manager.create(
            name=draft.name or "Synchri collaboration",
            mode=draft.mode,
            repo_root=draft.repo_path,
            base_branch=draft.base_branch,
            participants=draft.participants,
            permissions=draft.permissions,
            spec=draft.spec,
            deadline=draft.deadline,
            escalation=draft.escalation,
        )
        document = self.manager.issue_contract(record.session_id, reason="quick start")
        payload = self._launch_payload(self.manager.get(record.session_id), document=document)
        if cloned:
            payload["clone"] = cloned
        return payload

    def launch(self, query: dict, body: dict) -> dict:
        """Return the only setup handoff a room owner needs to make."""
        return self._launch_payload(self.manager.get(self._session_id(query)))

    def _plans(self, values: list[dict] | None) -> list[ParticipantPlan]:
        if not isinstance(values, list):
            raise ValidationError("add at least one agent")
        return [
            ParticipantPlan(
                name=agent.get("name", "").strip(),
                runtime=agent.get("runtime", "generic"),
                role=agent.get("role", "participant"),
                command=agent.get("command"),
            )
            for agent in values
            if isinstance(agent, dict) and agent.get("name", "").strip()
        ]

    def _launch_payload(self, record, *, document=None) -> dict:
        """A room's setup state, including paste-ready instruction per agent."""
        if document is None:
            document = self.manager.current_contract(record.session_id)
        invites = {
            invite["participant_name"]: invite
            for invite in (record.metadata or {}).get("invites", [])
        }
        room = self.broker.room_status(record.room_id, credential=self._human(record))
        present = {
            participant["name"]
            for participant in room.get("participants", [])
            if participant.get("status") != "removed"
        }
        acknowledgments = self.manager.acknowledgment_state(record.session_id)
        worktree = record.worktree
        agents = []
        for plan in record.participants:
            invite = invites.get(plan.name)
            if invite is None:  # Defensive: a persisted session must still be viewable.
                continue
            plan_view = plan.to_dict()
            join_command = f"cd {shlex.quote(worktree.path)} && {invite['command']}"
            contract_command = f"synchri session contract --session {record.session_id}"
            acknowledge_command = (
                f"synchri session ack {shlex.quote(plan.name)} --reply UNDERSTOOD "
                f"--session {record.session_id}"
            )
            wait_command = f"synchri wait --as {shlex.quote(plan.name)} --watch-messages"
            activity_command = (
                f"synchri activity --as {shlex.quote(plan.name)} "
                "-m \"Inspecting the task and repository.\""
            )
            setup_prompt = "\n".join(
                [
                    f"Join the Synchri collaboration as {plan.name} ({plan_view['role_label']}).",
                    "Use a terminal in this order:",
                    f"1. {join_command}",
                    "2. Read the shared agreement:",
                    f"   {contract_command}",
                    "3. If you agree, acknowledge it:",
                    f"   {acknowledge_command}",
                    "4. Stay in the room loop for your first turn and every later turn:",
                    f"   {wait_command}",
                    "While wait is blocking, do nothing and send no status updates. It also wakes "
                    "for new room messages so you retain context; read those, do not reply unless "
                    "you have the turn, then immediately run the same wait command again. The Primary "
                    "Builder is started automatically; Synchri will hand you their completed response "
                    "when it is your turn. Do not repeatedly say that you are waiting.",
                    "When wait says it is your turn, act on the task immediately: read the room briefing, "
                    "then note that wait has already started a visible Live Work trail in the UI. "
                    "At each meaningful shift (understanding, exploring, implementing, testing, "
                    "or reviewing), append a short public semantic update with:",
                    f"   {activity_command}",
                    "Those updates are the human-facing live progress stream. They are not a response, "
                    "do not move the queue, and must never contain private reasoning or a handoff. "
                    "Work in the authorized worktree, then send one completed response and hand off. "
                    "If you need a human decision, send a blocked response to `human`, then return "
                    "to the same wait command; do not end this agent session or switch to the provider's "
                    "normal chat. Synchri routes the human's next UI reply back to the blocked requester. "
                    "The session activation task starts the Primary Builder automatically; do not "
                    "wait for the human to say 'begin'.",
                ]
            )
            agents.append(
                {
                    "name": plan.name,
                    "runtime": plan.runtime,
                    "role": plan.role,
                    "role_label": plan_view["role_label"],
                    "joined": plan.name in present,
                    "acknowledged": plan.name in acknowledgments["accepted"],
                    "join_command": join_command,
                    "setup_prompt": setup_prompt,
                }
            )
        return {
            "session": record.to_dict(),
            "contract": document.to_dict(),
            "launch": {
                "room_id": record.room_id,
                "worktree_path": worktree.path if worktree else None,
                "agents": agents,
                "joined_count": sum(agent["joined"] for agent in agents),
                "acknowledgments": acknowledgments,
                "ready_to_activate": bool(agents)
                and all(agent["joined"] for agent in agents)
                and acknowledgments["all_accepted"],
            },
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
        """Put a human reply back in front of the agent that needs it.

        The builder is only the sensible default when nobody is actively asking
        the human for something.  In particular, an agent that posts a blocked
        response to ``human`` must receive the next UI reply, even though its
        turn has ended while it waits for that decision.
        """
        from ..models.envelope import MessageDraft

        record = self.manager.get(self._session_id(query, body))
        target = self._human_reply_target(record, body.get("target"))
        result = self.broker.send(
            record.room_id,
            credential=self._human(record),
            draft=MessageDraft(
                content=body.get("content", ""),
                message_type="interrupt" if body.get("interrupt") else "chat",
                target=target,
            ),
        )
        result["routed_to"] = target
        return result

    def _human_reply_target(self, record, requested: str | None = None) -> str | None:
        """Resolve who should receive the next human message from the app.

        A direct recipient chosen by the human wins.  Otherwise, reply to the
        agent currently on point.  A permission question normally ends that
        agent's turn and directs a blocked response to ``human``; in that
        shape, recover the most recent blocked agent before falling back to the
        primary builder for a new direction.
        """
        if requested:
            return requested

        agent_names = {plan.name for plan in record.participants}
        room = self.broker.room_status(record.room_id, credential=self._human(record))
        if room.get("active_speaker") in agent_names:
            return room["active_speaker"]

        messages = self.broker.read(
            record.room_id, credential=self._human(record), tail=30
        )["messages"]
        for message in reversed(messages):
            if (
                message.get("sender") in agent_names
                and message.get("response_status") == "blocked"
                and message.get("target") == "human"
            ):
                return message["sender"]

        return next(
            (plan.name for plan in record.participants if plan.role == "primary_builder"),
            next(iter(agent_names), None),
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
