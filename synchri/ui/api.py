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
from ..session.modes import (
    ParticipantPlan,
    Role,
    collaboration_pair,
    list_modes,
    plan_launch_status,
    runtime_catalog,
)
from ..session.permissions import PermissionSet
from ..session.spec import ProductSpec
from ..runner.managed import ManagedRunnerRegistry

Route = Callable[[dict, dict], dict]


class Api:
    """Routing table for the local UI."""

    def __init__(
        self,
        broker: Broker,
        manager: SessionManager,
        *,
        default_repo: str | None = None,
        managed: ManagedRunnerRegistry | None = None,
    ) -> None:
        self.broker = broker
        self.manager = manager
        self.managed = managed or ManagedRunnerRegistry(broker.workspace)
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
            ("POST", "managed/start"): self.start_managed,
            ("GET", "managed"): self.managed_status,
            ("GET", "sessions"): self.sessions,
            ("GET", "session"): self.session,
            ("GET", "dashboard"): self.dashboard,
            ("GET", "contract"): self.contract,
            ("POST", "ack"): self.acknowledge,
            ("POST", "activate"): self.activate,
            ("GET", "conversation"): self.conversation,
            ("GET", "changelog"): self.changelog,
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
            ("POST", "preset/rename"): self.rename_preset,
            ("POST", "preset/delete"): self.delete_preset,
        }

    def route(self, method: str, path: str) -> Route | None:
        return self._routes.get((method, path.strip("/")))

    # -- wizard --------------------------------------------------------

    def bootstrap(self, query: dict, body: dict) -> dict:
        """Everything the app needs on first paint."""
        return {
            "modes": list_modes(),
            "runtimes": runtime_catalog(),
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
        if "deadline" in body:
            if str(body["deadline"] or "").strip():
                draft.set_deadline_duration(body["deadline"])
            else:
                draft.deadline = None
                draft.deadline_duration = None
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
                "deadline_duration": draft.deadline_duration,
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
        """Create the one supported session shape without a setup gauntlet."""
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

        preset_name = (body.get("preset") or "").strip()
        draft = (
            SessionDraft.from_preset(presets_module.load(self.broker.workspace, preset_name))
            if preset_name
            else SessionDraft()
        )
        draft.set_mode("long_horizon")
        draft.set_repository(repo_path)
        if body.get("agents") is not None:
            draft.set_agents(self._plans(body.get("agents")))
        elif not draft.participants:
            raise ValidationError("choose a workflow with at least two agents")
        # Any non-empty text is a valid brief. Synchri preserves it verbatim;
        # Markdown is merely rendered nicely later, never required here.
        draft.set_spec(goal)
        if body.get("deadline"):
            draft.set_deadline_duration(body["deadline"])
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

    def start_managed(self, query: dict, body: dict) -> dict:
        """Attach, obtain agreement from, and launch detected local agents."""
        record = self.manager.get(self._session_id(query, body))
        run = (
            self.managed.resume(record)
            if record.status == "active"
            else self.managed.start(record)
        )
        return {
            "managed": run,
            "launch": self._launch_payload(record)["launch"],
        }

    def managed_status(self, query: dict, body: dict) -> dict:
        record = self.manager.get(self._session_id(query))
        return {
            "managed": self.managed.status(record.session_id),
            "readiness": self.managed.readiness(record),
        }

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
        # The packaged macOS app is also a CLI helper.  Generated external
        # prompts must use that concrete executable, not assume the provider's
        # terminal inherited a PATH entry from the user's shell.
        cli = self.managed.cli_command
        for plan in record.participants:
            invite = invites.get(plan.name)
            if invite is None:  # Defensive: a persisted session must still be viewable.
                continue
            plan_view = plan.to_dict()
            launch_status = plan_launch_status(plan)
            join_command = (
                f"cd {shlex.quote(worktree.path)} && {cli} join {invite['token']} "
                f"--name {shlex.quote(plan.name)}"
            )
            setup_prompt = "\n".join(
                [
                    f"You are {plan.name}, the {plan_view['role_label']} in a Synchri build room.",
                    "In your terminal, join the room:",
                    join_command,
                    "",
                    "Then handle room protocol yourself — do not ask the human to relay it:",
                    f"{cli} session contract --session {record.session_id}",
                    f"{cli} session ack {shlex.quote(plan.name)} --reply UNDERSTOOD --session {record.session_id}",
                    f"{cli} wait --as {shlex.quote(plan.name)} --watch-messages",
                    "Only acknowledge after reading the contract. Stay connected until the room ends. "
                    "Work only when Synchri gives you the turn; while waiting, do not send status updates.",
                    f"When you finish a gate, record it with `{cli} session gates GATE-ID --set pass "
                    f"--as {shlex.quote(plan.name)} --assessment \"…\" --evidence \"…\" --session {record.session_id}`.",
                    (
                        f"When every required gate is evidenced and reviewed, end the room with `{cli} session complete "
                        f"--as {shlex.quote(plan.name)} --session {record.session_id}`."
                        if plan.role == "primary_builder" else "The Primary Builder owns final session completion."
                    ),
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
                    "launch_mode": launch_status["mode"],
                    "managed_ready": launch_status["ready"],
                    "launch_detail": launch_status["detail"],
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
                "managed": {
                    "readiness": self.managed.readiness(record),
                    "run": self.managed.status(record.session_id),
                },
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
        session_id = self._session_id(query)
        record = self.manager.get(session_id)
        dashboard = self.manager.dashboard(session_id)
        dashboard["managed"] = {
            "readiness": self.managed.readiness(record),
            "run": self.managed.status(session_id),
        }
        return dashboard

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

    def changelog(self, query: dict, body: dict) -> dict:
        return self.manager.final_changelog(self._session_id(query))

    def message(self, query: dict, body: dict) -> dict:
        """Put a human reply back in front of the agent that needs it.

        The builder is only the sensible default when nobody is actively asking
        the human for something.  In particular, an agent that posts a blocked
        response to ``human`` must receive the next UI reply, even though its
        turn has ended while it waits for that decision.
        """
        from ..models.envelope import MessageDraft

        record = self.manager.get(self._session_id(query, body))
        target, enforce_review = self._human_reply_route(record, body.get("target"))
        lead, reviewer = collaboration_pair(record.participants)
        metadata = {"source": "human_input"}
        if enforce_review and lead and reviewer:
            # The conductor reads this durable marker after the lead posts a
            # completed response, then gives the reviewer the next turn. This
            # is an orchestration rule, not a hope that a model remembers it.
            metadata["human_direction"] = {
                "lead": lead.name,
                "reviewer": reviewer.name,
            }
        result = self.broker.send(
            record.room_id,
            credential=self._human(record),
            draft=MessageDraft(
                content=body.get("content", ""),
                message_type="interrupt" if body.get("interrupt") else "chat",
                target=target,
                metadata=metadata,
            ),
        )
        result["routed_to"] = target
        # A managed conductor yields while the human holds the floor.  Once
        # this reply returns the floor to a managed participant, pick the
        # durable session back up without asking the human to restart anything.
        result["managed"] = self.managed.resume(self.manager.get(record.session_id))
        return result

    def _human_reply_route(self, record, requested: str | None = None) -> tuple[str | None, bool]:
        """Resolve who should receive the next human message from the app.

        A direct recipient chosen by the human wins.  Otherwise, reply to the
        agent that is genuinely waiting on a decision. A new human direction
        always starts with the conversation lead, even if a reviewer happened
        to have the floor when the human spoke; the managed conductor then
        gives the designated reviewer the next turn.
        """
        if requested:
            return requested, False

        agent_names = {plan.name for plan in record.participants}
        human = self._human(record)
        room = self.broker.room_status(record.room_id, credential=human)
        if room.get("active_speaker") == human.participant:
            pending = self.broker.turn_status(record.room_id, credential=human).get("request") or {}
            if (
                pending.get("sender") in agent_names
                and pending.get("response_status") == "blocked"
                and pending.get("target") == human.participant
            ):
                return pending["sender"], False

        lead, reviewer = collaboration_pair(record.participants)
        return (lead.name if lead else next(iter(agent_names), None)), bool(lead and reviewer)

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
        """Pause, resume, complete, stop, or reconfigure — the human's controls."""
        session_id = self._session_id(query, body)
        record = self.manager.get(session_id)
        action = body.get("action")
        credential = self._human(record)

        if action == "pause":
            self.managed.cancel(session_id, reason="Pausing the session.")
            self.broker.pause_room(record.room_id, credential=credential)
            self.manager.pause(session_id)
        elif action == "resume":
            self.broker.resume_room(record.room_id, credential=credential)
            resumed = self.manager.resume(session_id)
            self.managed.resume(resumed)
        elif action == "stop":
            self.managed.cancel(session_id, reason="Stopping the session.")
            self.manager.stop(session_id, body.get("reason") or "stopped by the user")
        elif action == "complete":
            self.managed.cancel(session_id, reason="Completing the session.")
            self.manager.complete(session_id)
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
        name = body["name"]
        presets_module.save(self.broker.workspace, name, draft.to_preset())
        previous = (body.get("replace") or "").strip()
        if previous and previous != name:
            presets_module.delete(self.broker.workspace, previous)
        return {"presets": presets_module.list_presets(self.broker.workspace)}

    def rename_preset(self, query: dict, body: dict) -> dict:
        presets_module.rename(self.broker.workspace, body["name"], body["new_name"])
        return {"presets": presets_module.list_presets(self.broker.workspace)}

    def delete_preset(self, query: dict, body: dict) -> dict:
        presets_module.delete(self.broker.workspace, body["name"])
        return {"presets": presets_module.list_presets(self.broker.workspace)}

    # -- internals -----------------------------------------------------

    def _human(self, record) -> Credential:
        human = (record.metadata or {}).get("human") or {}
        return Credential(participant=human.get("name"), secret=human.get("secret"))
