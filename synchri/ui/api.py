"""JSON API behind the local UI.

Every route is a thin wrapper over :class:`SessionManager` or :class:`Broker`.
The UI holds no rules of its own — it cannot activate a session the manager
would refuse, because it asks the manager. That is what keeps the terminal and
the browser honest with each other.
"""

from __future__ import annotations

import shlex
import secrets
import threading
import time
from typing import Callable

from ..broker import Broker, Credential
from ..errors import NotFoundError, ValidationError
from ..session import deliberation, discovery, drafts as drafts_module, presets as presets_module, worktree as worktree_module
from ..session.draft import SessionDraft
from ..session.escalation import CATALOG as ESCALATION_CATALOG
from ..session.extract import (
    derivation_note,
    describe as describe_gates,
    extract_gates,
    extract_gates_with_derivation,
)
from ..session.gates import Gate
from ..session.manager import SessionManager
from ..session.modes import (
    ROLE_LABELS,
    ParticipantPlan,
    Role,
    collaboration_pair,
    connection_test_available,
    default_agent_name,
    list_modes,
    plan_launch_status,
    runtime_catalog,
)
from ..session.permissions import (
    BY_KEY,
    CATALOG as PERMISSION_CATALOG,
    PermissionSet,
    permission_profile,
    permission_profiles,
)
from ..session.spec import ProductSpec
from ..storage import dao, db
from ..runner import ancillary as ancillary_module
from ..runner import doctor as doctor_module
from ..runner.managed import ManagedRunnerRegistry

Route = Callable[[dict, dict], dict]

THEME_KEYS = frozenset(
    {"", "terminal", "midnight", "ember", "copper", "orchid",
     "daylight", "sage", "solar", "harbor", "iris"}
)


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
        self.connections = doctor_module.ConnectionTester(broker.workspace)
        self.ancillary = ancillary_module.AncillaryRunner(broker.workspace)
        # ``synchri ui`` is normally launched from the repository being worked
        # on.  Keeping that small piece of context makes the default path a
        # room launch, not a repository-discovery exercise.  The draft still
        # validates it before anything is created.
        self.default_repo = self._valid_repository(default_repo)
        # Device codes are short-lived, single-use, and deliberately stay in
        # this local server process.  The browser receives only the harmless
        # code it asks the person to type into GitHub.
        self._github_device_codes: dict[str, tuple[str, float]] = {}
        self._github_device_lock = threading.Lock()
        #: Wizard drafts are persisted, so closing the app does not lose an
        #: unfinished wizard and two tabs on one draft stay in step. They hold
        #: no authority: nothing is created until "Start session".
        self._routes: dict[tuple[str, str], Route] = {
            ("GET", "bootstrap"): self.bootstrap,
            ("POST", "appearance"): self.update_appearance,
            ("GET", "repositories"): self.repositories,
            ("GET", "worktrees"): self.worktrees,
            ("POST", "repository/resolve"): self.resolve_repository,
            ("POST", "github/connect"): self.connect_github,
            ("POST", "github/poll"): self.poll_github,
            ("POST", "github/disconnect"): self.disconnect_github,
            ("POST", "draft"): self.update_draft,
            ("GET", "draft"): self.get_draft,
            ("POST", "draft/reset"): self.reset_draft,
            ("POST", "start"): self.start,
            ("POST", "quick-start"): self.quick_start,
            ("GET", "launch"): self.launch,
            ("POST", "managed/start"): self.start_managed,
            ("GET", "managed"): self.managed_status,
            ("GET", "runtimes/doctor"): self.runtimes_doctor,
            ("POST", "runtimes/connect"): self.connect_runtime,
            ("GET", "runtimes/connect"): self.connect_runtime_status,
            ("POST", "runtimes/connect/cancel"): self.cancel_connect_runtime,
            ("GET", "sessions"): self.sessions,
            ("GET", "session"): self.session,
            ("POST", "session/rename"): self.rename_session,
            ("POST", "session/delete"): self.delete_session,
            ("POST", "package/save"): self.save_package,
            ("GET", "dashboard"): self.dashboard,
            ("GET", "contract"): self.contract,
            ("POST", "ack"): self.acknowledge,
            ("POST", "activate"): self.activate,
            ("GET", "conversation"): self.conversation,
            ("GET", "changelog"): self.changelog,
            ("POST", "message"): self.message,
            ("POST", "approval"): self.approval,
            ("GET", "gates"): self.gates,
            ("POST", "gate"): self.update_gate,
            ("GET", "dropbox"): self.dropbox,
            ("POST", "dropbox/capture"): self.capture_drop,
            ("GET", "plan"): self.plan,
            ("POST", "plan/control"): self.plan_control,
            ("POST", "plan/approve"): self.approve_plan,
            ("POST", "gates/preview"): self.preview_gates,
            ("POST", "tests/run"): self.run_tests,
            ("GET", "changes"): self.changes,
            ("GET", "changes/files"): self.file_changes,
            ("GET", "diff"): self.diff,
            ("GET", "diff/file"): self.file_diff,
            ("GET", "memory"): self.memory,
            ("GET", "events"): self.events,
            ("GET", "history/timeline"): self.history_timeline,
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
        runtimes = runtime_catalog()
        return {
            "modes": list_modes(),
            "runtimes": runtimes,
            "roles": [
                {"key": r.value, "label": r.value.replace("_", " ").title()} for r in Role
            ],
            "permissions": PermissionSet.defaults().grouped(),
            "permission_profiles": permission_profiles(),
            "escalation_rules": [r.to_dict() for r in ESCALATION_CATALOG],
            "presets": presets_module.list_presets(self.broker.workspace),
            "sessions": [s.to_dict() for s in self.manager.list_sessions()],
            "workspace": str(self.broker.workspace.home),
            # GitHub is the sign-in identity for this local installation. It
            # never makes the session database cloud-hosted; it simply lets the
            # person see who is signed in before choosing repository grants.
            "github": discovery.github_status(self.broker.workspace),
            # Stored connection outcomes only — no probes on first paint. The
            # doctor endpoint refreshes these with live invalidation checks.
            "runtime_connections": doctor_module.stored_connections(self.broker.conn),
            # In-memory test state makes a closed dialog and the home page
            # agree while a connection test is still running.
            "runtime_connection_tests": {
                entry["key"]: self.connections.status(entry["key"])
                for entry in runtimes if entry.get("managed_command")
            },
            "appearance": {"theme": self.appearance_theme()},
            "default_repo": self.default_repo,
            "desktop_clone_root": str(discovery.desktop_clone_root()),
            "open_drafts": drafts_module.versions(self.broker.conn),
        }

    def appearance_theme(self) -> str:
        """The validated, durable theme injected before the UI's first paint."""
        theme = dao.get_app_preference(self.broker.conn, "theme")
        return theme if theme in THEME_KEYS else ""

    def update_appearance(self, query: dict, body: dict) -> dict:
        theme = body.get("theme")
        if not isinstance(theme, str) or theme not in THEME_KEYS:
            raise ValidationError("choose a theme offered by Synchri")
        with db.transaction(self.broker.conn):
            dao.set_app_preference(self.broker.conn, "theme", theme or None)
        return {"appearance": {"theme": theme}}

    def repositories(self, query: dict, body: dict) -> dict:
        return discovery.repositories(
            include_github=query.get("github") != "0",
            include_local=query.get("local") != "0",
            workspace=self.broker.workspace,
        )

    def worktrees(self, query: dict, body: dict) -> dict:
        """Selectable existing worktrees for a repository.

        This is deliberately small: the UI presents a new worktree by default
        and this endpoint supplies the optional alternatives.  The primary
        checkout is excluded by ``list_worktrees`` and validated again on use.
        """
        repo_path = (query.get("repo") or "").strip()
        status = worktree_module.inspect_repository(repo_path)
        if not status.is_valid:
            raise ValidationError("choose a usable repository before selecting a worktree")
        return {"repository": status.root, "worktrees": worktree_module.list_worktrees(status.root)}

    def resolve_repository(self, query: dict, body: dict) -> dict:
        """Prepare a selected source repository without conflating it with a session.

        This deliberately returns a reusable primary checkout.  ``create`` then
        creates a new worktree per session, so resolving the same GitHub repo
        repeatedly is safe and expected.
        """
        reference = (body.get("reference") or "").strip()
        if not reference:
            raise ValidationError("choose a repository first")
        if discovery.github_reference(reference):
            return {
                "repository": discovery.resolve_github_repository(
                    reference, workspace=self.broker.workspace
                )
            }
        status = self._valid_repository(reference)
        if not status:
            raise ValidationError(
                "That folder is not a usable Git repository. Choose a repository from the list, "
                "or select Connect GitHub to browse one.",
                code="repository_unusable",
                resolution={"kind": "choose_repository"},
            )
        return {
            "repository": {
                "path": status,
                "name": status.rsplit("/", 1)[-1],
                "source": "local",
                "cloned": False,
                "reused": True,
            }
        }

    def connect_github(self, query: dict, body: dict) -> dict:
        from ..session import github_auth

        # A stale click should never restart device authorization for a person
        # who already has a valid local Synchri profile. The UI refreshes its
        # account label from this response instead.
        status = discovery.github_status(self.broker.workspace)
        if status.get("authenticated"):
            return {
                "state": "connected",
                "already_connected": True,
                "account": status.get("account"),
            }
        authorization = github_auth.start_device_authorization()
        request_id = secrets.token_urlsafe(24)
        with self._github_device_lock:
            self._discard_expired_github_codes()
            self._github_device_codes[request_id] = (
                authorization.device_code,
                authorization.expires_at,
            )
        return {"started": True, "request_id": request_id, **authorization.to_dict()}

    def poll_github(self, query: dict, body: dict) -> dict:
        """Finish one native device-flow poll without exposing a credential to JS."""
        from ..session import github_auth

        request_id = str(body.get("request_id") or "")
        with self._github_device_lock:
            self._discard_expired_github_codes()
            stored = self._github_device_codes.get(request_id)
        if not stored:
            raise ValidationError(
                "That GitHub sign-in request expired. Connect GitHub again to get a fresh code.",
                code="github_login_expired",
                resolution={"kind": "connect_github"},
            )
        result = github_auth.complete_device_authorization(self.broker.workspace, stored[0])
        if result.get("state") in {"connected", "expired"}:
            with self._github_device_lock:
                self._github_device_codes.pop(request_id, None)
        return result

    def _discard_expired_github_codes(self) -> None:
        now = time.time()
        for request_id, (_device_code, expires_at) in list(self._github_device_codes.items()):
            if expires_at <= now:
                self._github_device_codes.pop(request_id, None)

    def disconnect_github(self, query: dict, body: dict) -> dict:
        from ..session import github_auth

        github_auth.delete_credentials(self.broker.workspace)
        return {"disconnected": True, "message": "GitHub is disconnected."}

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
        if ("worktree_name" in body or "worktree_parent" in body
                or "existing_worktree_path" in body):
            draft.set_worktree(
                body.get("worktree_name") or None,
                body.get("worktree_parent") or None,
                body.get("existing_worktree_path") or None,
            )
        if body.get("agents") is not None:
            draft.set_agents(self._plans(body["agents"]))
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
                "existing_worktree_path": draft.existing_worktree_path,
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
        # Planning Mode's spec on-ramp: the wizard's text is the user's idea
        # articulation, stored verbatim on the plan — not a ProductSpec.
        planning = bool(draft.policy and draft.policy.planning)
        record = self.manager.create(
            name=draft.name or "Synchri session",
            mode=draft.mode,
            repo_root=draft.repo_path,
            base_branch=draft.base_branch,
            participants=draft.participants,
            permissions=draft.permissions,
            spec=None if planning else draft.spec,
            deadline=draft.deadline,
            escalation=draft.escalation,
            worktree_parent=draft.worktree_parent,
            worktree_name=draft.worktree_name,
            existing_worktree_path=draft.existing_worktree_path,
            metadata={"idea": draft.spec.text} if planning and draft.spec else None,
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

        repository = None
        if discovery.github_reference(repo_path):
            repository = discovery.resolve_github_repository(repo_path)
            repo_path = repository["path"]

        preset_name = (body.get("preset") or "").strip()
        draft = (
            SessionDraft.from_preset(presets_module.load(self.broker.workspace, preset_name))
            if preset_name
            else SessionDraft()
        )
        draft.set_mode("long_horizon")
        draft.set_repository(repo_path)
        strategy = (body.get("worktree_strategy") or "").strip()
        if strategy:
            # The UI always sends an explicit choice; when present it must be
            # internally consistent so a half-migrated client can never get a
            # worktree it did not pick. Absent = legacy default-to-new.
            existing = (body.get("existing_worktree_path") or "").strip()
            if strategy == "existing" and not existing:
                raise ValidationError("choose which existing worktree to use")
            if strategy == "new" and existing:
                raise ValidationError("a new worktree cannot also name an existing one")
            if strategy not in {"new", "existing"}:
                raise ValidationError(f"unknown worktree strategy {strategy!r}")
        if "existing_worktree_path" in body:
            draft.set_worktree(existing_path=body.get("existing_worktree_path") or None)
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
            existing_worktree_path=draft.existing_worktree_path,
        )
        document = self.manager.issue_contract(record.session_id, reason="quick start")
        payload = self._launch_payload(self.manager.get(record.session_id), document=document)
        if repository:
            payload["repository"] = repository
            # Kept for the existing desktop handoff copy.  New clients use
            # ``repository`` so they can distinguish a fresh clone from a
            # safely reused checkout.
            if repository.get("cloned"):
                payload["clone"] = repository
        return payload

    def launch(self, query: dict, body: dict) -> dict:
        """Return the only setup handoff a room owner needs to make."""
        return self._launch_payload(self.manager.get(self._session_id(query)))

    def start_managed(self, query: dict, body: dict) -> dict:
        """Attach, obtain agreement from, and launch detected local agents.

        An optional ``participants`` list names the subset Synchri should
        launch — a mixed team's externally-run agents stay outside it. The
        split persists on the session, so resume, restart, and post-restart
        reconstruction all honor it without being told again.
        """
        record = self.manager.get(self._session_id(query, body))
        names = body.get("participants")
        if names is not None:
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                raise ValidationError("participants must be a list of participant names")
            record = self.manager.set_managed_participants(record.session_id, names)
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

    # -- runtime connections -------------------------------------------

    def runtimes_doctor(self, query: dict, body: dict) -> dict:
        """The two-tier doctor view: passive checks plus live connection state.

        This endpoint runs the passive probes (cheap, local, timeout-bounded);
        it never invokes a provider. The consented connection test lives
        behind ``runtimes/connect``.
        """
        runtimes = []
        for entry in runtime_catalog():
            runtime = entry["key"]
            report = doctor_module.passive_report(runtime)
            runtimes.append(
                {
                    **entry,
                    "doctor": report["checks"],
                    "connection": doctor_module.connection_state(self.broker.conn, runtime),
                    "test": self.connections.status(runtime),
                }
            )
        return {"runtimes": runtimes}

    def connect_runtime(self, query: dict, body: dict) -> dict:
        """Start the user-initiated connection test for one runtime."""
        runtime = (body.get("runtime") or "").strip()
        if not runtime:
            raise ValidationError("choose which runtime to connect")
        if not connection_test_available(runtime):
            raise ValidationError(
                f"the connection test is unavailable for {runtime!r} in this "
                "Synchri version; this CLI cannot be marked connected"
            )
        return {"test": self.connections.start(runtime)}

    def connect_runtime_status(self, query: dict, body: dict) -> dict:
        runtime = (query.get("runtime") or "").strip()
        if not runtime:
            raise ValidationError("name the runtime to check")
        return {
            "test": self.connections.status(runtime),
            "connection": doctor_module.connection_state(self.broker.conn, runtime),
        }

    def cancel_connect_runtime(self, query: dict, body: dict) -> dict:
        runtime = (body.get("runtime") or "").strip()
        if not runtime:
            raise ValidationError("name the runtime to cancel")
        return {"test": self.connections.cancel(runtime)}

    def _plans(self, values: list[dict] | None) -> list[ParticipantPlan]:
        if not isinstance(values, list):
            raise ValidationError("add at least one agent")
        # Names derive from the runtime when absent — the UI no longer asks
        # for them — with a hyphen suffix keeping duplicates unique.
        taken: set[str] = set()
        plans: list[ParticipantPlan] = []
        for agent in values:
            if not isinstance(agent, dict):
                continue
            name = (agent.get("name") or "").strip()
            if not name:
                name = default_agent_name(agent.get("runtime", "generic"), taken)
            taken.add(name)
            plans.append(
                ParticipantPlan(
                    name=name,
                    runtime=agent.get("runtime", "generic"),
                    role=agent.get("role", "participant"),
                    command=agent.get("command"),
                )
            )
        return plans

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
        workdir = worktree.path if worktree else (
            record.planning_workspace.get("path") or record.repo_root
        )
        participant_states = self.manager.participant_states(record.session_id)
        # Stored connection outcomes gate the paste-free presentation; the
        # managed registry stays the mechanical authority when Start is hit.
        connections = doctor_module.stored_connections(self.broker.conn)
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
                f"cd {shlex.quote(workdir)} && {cli} join {invite['token']} "
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
                    f"At the beginning of every turn, publish a concise live state with `{cli} activity "
                    f"--as {shlex.quote(plan.name)} -m \"Planning the next step. This can take a few minutes.\"`. "
                    "Update it before a long skill or tool call; it clears automatically when you respond.",
                    f"When you finish a gate, record it with `{cli} session gates GATE-ID --set pass "
                    f"--as {shlex.quote(plan.name)} --assessment \"…\" --evidence \"…\" --session {record.session_id}`.",
                    (
                        f"When every required gate is evidenced and reviewed, end the room with `{cli} session complete "
                        f"--as {shlex.quote(plan.name)} --session {record.session_id}`."
                        if plan.role == "primary_builder" else "The Primary Builder owns final session completion."
                    ),
                ]
            )
            state = participant_states.get(plan.name) or {}
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
                    "connected": connections.get(plan.runtime, {}).get("state") == "connected",
                    "managed_by_synchri": bool((plan.metadata or {}).get("managed_by_synchri")),
                    "join_phase": state.get("join_phase"),
                    "join_detail": state.get("join_detail"),
                    "join_command": join_command,
                    "setup_prompt": setup_prompt,
                }
            )
        return {
            "session": record.to_dict(),
            "contract": {**document.to_dict(), "human_summary": self._contract_summary(record)},
            "launch": {
                "room_id": record.room_id,
                "worktree_path": workdir,
                "agents": agents,
                "joined_count": sum(agent["joined"] for agent in agents),
                "acknowledgments": acknowledgments,
                "managed": {
                    "readiness": self.managed.readiness(record),
                    "run": self.managed.status(record.session_id),
                },
                # Planning sessions launch only under the managed runner:
                # directives are parsed from managed stdout, so a pasted agent
                # could never submit a plan revision. The preflight hides the
                # manual path rather than offering a dead one.
                "managed_only": bool(record.policy.planning),
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

    def rename_session(self, query: dict, body: dict) -> dict:
        record = self.manager.rename_session(
            self._session_id(query, body), body.get("name", "")
        )
        return {
            "session": record.to_dict(),
            "sessions": [s.to_dict() for s in self.manager.list_sessions()],
        }

    def delete_session(self, query: dict, body: dict) -> dict:
        outcome = self.manager.delete_session(self._session_id(query, body))
        return {**outcome, "sessions": [s.to_dict() for s in self.manager.list_sessions()]}

    def save_package(self, query: dict, body: dict) -> dict:
        """Write the session package into the room directory and say where.

        The desktop webview cannot reliably run anchor downloads, so the
        native path is: build once, save next to the room's other artifacts,
        show the user the path.
        """
        from ..session import package as package_module

        session_id = self._session_id(query, body)
        filename, data = package_module.build(self.broker, self.manager, session_id)
        record = self.manager.get(session_id)
        if not record.room_id:
            raise ValidationError("this session has no room directory to save into")
        path = self.broker.workspace.ensure_room_dir(record.room_id) / "session-package.zip"
        path.write_bytes(data)
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass
        return {"path": str(path), "filename": filename, "bytes": len(data)}

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
        record = self.manager.get(session_id)
        payload = document.to_dict()
        payload["per_participant"] = {
            plan.name: document.for_participant(plan.name)
            for plan in record.participants
        }
        payload["acknowledgments"] = self.manager.acknowledgment_state(session_id)
        payload["human_summary"] = self._contract_summary(record)
        return payload

    def _contract_summary(self, record) -> dict:
        """The human-addressed cover for the verbatim contract beneath it.

        Derived at serve time from the same session configuration the contract
        was generated from — never stored — so old sessions get it too and it
        can never drift from what the exact text says.
        """
        permissions = record.permissions
        ask = [
            capability.key for capability in PERMISSION_CATALOG
            if permissions.requires_escalation(capability.key)
        ]
        gates = self.manager.gates(record.session_id)
        if record.policy.planning:
            goal = (record.metadata or {}).get("idea") or ""
            done = (
                "The planner and the adversarial reviewer close the loop on a "
                "written plan; nothing is executed until you approve it."
            )
        else:
            goal = record.spec.canonical_text() if record.spec else ""
            done = (
                f"All {len(gates)} acceptance gate(s) pass with recorded "
                "evidence and both sign-offs."
                if gates else "No acceptance gates are defined yet."
            )
        goal_line = next(
            (line.strip("# ").strip() for line in goal.splitlines() if line.strip()), ""
        )
        return {
            "mode": record.policy.label,
            "goal": goal_line[:200],
            "team": [
                {
                    "name": plan.name,
                    "role_label": ROLE_LABELS.get(plan.role, plan.role),
                    "runtime": plan.runtime,
                }
                for plan in record.participants
            ],
            "allowed": [BY_KEY[key].label for key in permissions.granted_keys()],
            "ask_first": [BY_KEY[key].label for key in ask],
            "denied": [BY_KEY[key].label for key in permissions.denied_keys()],
            "done": done,
            "gate_ids": [gate.gate_id for gate in gates[:8]],
            "timebox": record.deadline.describe() if record.deadline else None,
            "workspace": (
                record.worktree.path if record.worktree
                else (record.planning_workspace.get("path") or record.repo_root)
            ),
            "acknowledgment": (
                "The text below is the agents' copy of these terms. Each must "
                "reply UNDERSTOOD before work begins, and changing the "
                "permissions, brief, timebox, or team issues a new revision "
                "they must acknowledge again."
            ),
        }

    def acknowledge(self, query: dict, body: dict) -> dict:
        session_id = self._session_id(query, body)
        result = self.manager.acknowledge(
            session_id, body.get("participant", ""), body.get("reply", "")
        )
        return {**result, "state": self.manager.acknowledgment_state(session_id)}

    def activate(self, query: dict, body: dict) -> dict:
        record = self.manager.activate(self._session_id(query, body))
        # A mixed team: the human's Begin collaboration is the moment its
        # managed subset starts supervision. Rosters that never chose managed
        # launch carry no flags and are untouched.
        if any((plan.metadata or {}).get("managed_by_synchri") for plan in record.participants):
            self.managed.resume(record)
        return record.to_dict()

    # -- tabs ----------------------------------------------------------

    def conversation(self, query: dict, body: dict) -> dict:
        record = self.manager.get(self._session_id(query))
        if not record.room_id:
            return {"messages": [], "live_events": []}
        payload = self.broker.read(record.room_id, credential=self._human(record))
        payload["live_events"] = dao.list_stream_events(
            self.broker.conn, record.room_id, limit=200
        )
        return payload

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

    def approval(self, query: dict, body: dict) -> dict:
        """Act on an explicit agent decision request, then wake the waiting agent.

        The approval record is meaningful only when the agent named a known
        capability that the active workflow marked ASK.  Generic decisions are
        still routed as normal human replies; they cannot become a hidden
        permission escalation.
        """
        record = self.manager.get(self._session_id(query, body))
        target = (body.get("target") or "").strip()
        if target not in {plan.name for plan in record.participants}:
            raise ValidationError("choose the agent that requested this decision")
        approved = bool(body.get("approved"))
        capability = (body.get("capability") or "").strip()
        if approved and capability:
            self.manager.approve_capability(record.session_id, capability, actor="human")
        detail = (body.get("detail") or "the specific request in your prior message").strip()
        content = (
            f"Approved: proceed with {detail}. Synchri recorded the named session permission where applicable. "
            "Provider, operating-system, GitHub, and other external controls still apply."
            if approved else
            f"Denied: do not proceed with {detail}. Explain a safer alternative if one exists."
        )
        return self.message(
            {},
            {"session": record.session_id, "target": target, "content": content, "interrupt": True},
        )

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
        # Where the gates came from, persisted at create or promotion time.
        # Sessions from before the field existed simply carry no label.
        derivation = (self.manager.get(session_id).metadata or {}).get("gate_derivation")
        return {
            "gates": [g.to_dict() for g in gates],
            "summary": summarize(gates),
            "derivation": derivation,
            "derivation_note": derivation_note(derivation),
        }

    def history_timeline(self, query: dict, body: dict) -> dict:
        """The session's deliberative sequence, derived from the durable record."""
        record = self.manager.get(self._session_id(query))
        kinds = [k for k in (query.get("kinds") or "").split(",") if k] or None
        events = deliberation.timeline(self.manager, record, kinds=kinds)
        return {
            "events": events,
            "kinds": sorted({event["kind"] for event in events}),
        }

    def preview_gates(self, query: dict, body: dict) -> dict:
        """What gate detection would make of a brief, before anything exists."""
        gates, derivation = extract_gates_with_derivation(body.get("spec") or "")
        if len(gates) == 1 and gates[0].gate_id == "SPEC-01":
            note = (
                "No explicit acceptance criteria detected — the session gets one generic "
                "SPEC-01 gate. Add 'Acceptance criteria:' bullets or AUTH-01-style ids to "
                "track more."
            )
        else:
            note = describe_gates(gates)
        return {
            "gates": [{"gate_id": g.gate_id, "description": g.description} for g in gates],
            "note": note,
            "derivation": derivation,
        }

    def update_gate(self, query: dict, body: dict) -> dict:
        session_id = self._session_id(query, body)
        if body.get("add"):
            added = body["add"] or {}
            gate = Gate(
                gate_id=str(added.get("gate_id", "")).strip().upper(),
                description=str(added.get("description", "")).strip(),
            )
            self.manager.add_gate(session_id, gate)
            return self.gates({"session": session_id}, {})
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
        actor = body.get("actor", "human")
        if actor == "human" and fields.get("status") == "pass":
            # A human marking a gate PASS is an acceptance decision. Completion
            # demands evidence and both sign-offs, so record the acceptance as
            # those where they are missing instead of leaving the gate in a
            # passing-but-still-blocking state.
            current = next(
                (g for g in self.manager.gates(session_id) if g.gate_id == body.get("gate_id")),
                None,
            )
            if current is not None:
                has_evidence = bool(
                    fields.get("evidence", current.evidence)
                    or fields.get("tests", current.tests)
                    or fields.get("commits", current.commits)
                )
                if not has_evidence:
                    fields["evidence"] = ["Accepted by the user from the Gates panel"]
                if not fields.get("builder_assessment", current.builder_assessment):
                    fields["builder_assessment"] = "Accepted by the user"
                if not fields.get("reviewer_assessment", current.reviewer_assessment):
                    fields["reviewer_assessment"] = "Accepted by the user"
        gate = self.manager.update_gate(
            session_id, body["gate_id"], actor=actor, **fields
        )
        return gate.to_dict()

    def dropbox(self, query: dict, body: dict) -> dict:
        """The side-task inbox: every item's lifecycle, in appendix order."""
        from ..session import dropbox as dropbox_module

        session_id = self._session_id(query)
        record = self.manager.get(session_id)
        return {
            "phase": record.phase,
            "frozen": record.phase != dropbox_module.PHASE_ORIGINAL,
            "items": dropbox_module.items(self.manager, session_id),
            "summary": dropbox_module.summarize(self.manager, session_id),
        }

    def capture_drop(self, query: dict, body: dict) -> dict:
        """Capture a side task. The main agents are never interrupted.

        Reconciliation happens inside the capture, so an idle room still gets
        the item into the appendix exactly once.
        """
        from ..session import dropbox as dropbox_module

        from ..session import planning as planning_module
        from ..storage import db as db_module

        session_id = self._session_id(query, body)
        self.manager.get(session_id)
        # Routing and capture are one transaction: the decision — planning
        # dropbox, promotion queue, or coordination dropbox — is made against
        # the same state the insert lands in. An approval finalizing on
        # another request thread waits for the connection's write lock, so a
        # capture can never target a session the approval just closed.
        queued = None
        with db_module.transaction(self.manager.conn):
            promotion = planning_module.promotion(self.manager, session_id)
            if promotion is not None:
                # Capture froze at plan approval; an item captured after it
                # enters the new coordination session's dropbox. Before the
                # promotion finalizes there is nothing to capture into yet,
                # so the item queues durably on the promotion record and
                # drains into the coordination dropbox atomically with the
                # finalize — never lost, never bounced back to the user.
                target = promotion.get("coordination_session_id")
                if target is None:
                    queued = planning_module.queue_capture(
                        self.manager,
                        session_id,
                        prompt=body.get("prompt", ""),
                        title=body.get("title"),
                        skip_review=bool(body.get("skip_review")),
                    )
                    if queued is None:
                        # The promotion had already finalized: route directly.
                        target = planning_module.promotion(self.manager, session_id)[
                            "coordination_session_id"
                        ]
                        session_id = target
                else:
                    session_id = target
            if queued is None:
                item = dropbox_module.capture(
                    self.manager,
                    session_id,
                    body.get("prompt", ""),
                    title=body.get("title"),
                    skip_review=bool(body.get("skip_review")),
                )
        if queued is not None:
            return {"item": None, "investigating": False, **queued}
        investigating = self._start_scout_if_connected(session_id, item["drop_id"])
        return {
            "item": item,
            "investigating": investigating,
            **self.dropbox({"session": session_id}, {}),
        }

    def _start_scout_if_connected(self, session_id: str, drop_id: str) -> bool:
        """Start the scout in parallel, off the critical path.

        Only for an agent the user actually wired up: an explicit command, or
        a runtime whose consented connection test passed. A merely-installed
        CLI is never invoked implicitly. Otherwise the item waits honestly as
        captured; the completion cutoff has a terminal route for it, and the
        human can retry once an agent is connected. Planning considerations
        are folded into the plan by the planner — they are never scouted.
        """
        record = self.manager.get(session_id)
        lead, _reviewer = collaboration_pair(record.participants)
        if lead is None or record.policy.planning:
            return False
        investigating = False
        if lead.command and lead.command.strip():
            investigating = True
        elif plan_launch_status(lead)["ready"]:
            connections = doctor_module.stored_connections(self.broker.conn)
            investigating = (
                connections.get(lead.runtime, {}).get("state") == "connected"
            )
        if investigating:
            self.ancillary.investigate(session_id, drop_id)
        return investigating

    def plan(self, query: dict, body: dict) -> dict:
        """The PLAN-NNN artifact and loop state for a planning session."""
        from ..session import planning as planning_module

        session_id = self._session_id(query)
        payload = planning_module.payload(self.manager, session_id)
        if query.get("revision"):
            payload["revision_body"] = planning_module.revision_body(
                self.manager, session_id, int(query["revision"])
            )
        return payload

    def plan_control(self, query: dict, body: dict) -> dict:
        """The human's planning controls: reopen, waive, resume the budget."""
        from ..session import planning as planning_module

        session_id = self._session_id(query, body)
        action = body.get("action")
        if action == "reopen":
            payload = planning_module.reopen(self.manager, session_id, body.get("note", ""))
        elif action == "waive_objection":
            payload = planning_module.waive_objection(
                self.manager, session_id, body.get("objection_id", ""), body.get("note", "")
            )
        elif action == "resume_budget":
            payload = planning_module.resume_budget(self.manager, session_id)
        else:
            raise ValidationError(f"unknown plan control {action!r}")
        # Every control queues work for the agents; pick the room back up.
        record = self.manager.get(session_id)
        if record.status == "active":
            self.managed.resume(record)
        return payload

    def approve_plan(self, query: dict, body: dict) -> dict:
        """Approve & Start Coordination: a state transition, not an acknowledgment.

        Approval reserves the promotion, converts the frozen plan revision
        into ProductSpec v1 with its gates materialized deterministically,
        and provisions the new linked coordination session — resumably, and
        exactly one per approved plan. The planning session's agents stop;
        Stream A's paste-free start then launches the coordination team.
        """
        from ..session import planning as planning_module

        session_id = self._session_id(query, body)
        result = planning_module.approve(
            self.manager, session_id, staffing=body.get("staffing")
        )
        self.managed.cancel(session_id, reason="Plan approved; planning has concluded.")
        coordination = self.manager.get(result["coordination_session_id"])
        # Captures drained from the promotion queue get the same scout start
        # as a direct capture would — a queued item must not idle as captured
        # until the completion cutoff times it out just because it arrived
        # mid-window.
        for entry in result.get("window_captures", []):
            self._start_scout_if_connected(coordination.session_id, entry["drop_id"])
        return {**result, "coordination": self._launch_payload(coordination)}

    def run_tests(self, query: dict, body: dict) -> dict:
        session_id = self._session_id(query, body)
        if body.get("command"):
            self.manager.set_test_command(session_id, body["command"])
        return self.manager.run_tests(session_id, body.get("command"))

    def changes(self, query: dict, body: dict) -> dict:
        return self.manager.changes(self._session_id(query))

    def diff(self, query: dict, body: dict) -> dict:
        return {"diff": self.manager.diff(self._session_id(query))}

    def file_diff(self, query: dict, body: dict) -> dict:
        return self.manager.file_diff(self._session_id(query), query.get("path", ""))

    def file_changes(self, query: dict, body: dict) -> dict:
        return {"files": self.manager.file_changes(self._session_id(query))}

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
        """Pause, resume, restart, complete, stop, or reconfigure — human controls."""
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
        elif action == "restart":
            if record.status != "active":
                raise ValidationError("Only an active session can be restarted. Resume it first.")
            readiness = self.managed.readiness(record)
            if not readiness["available"]:
                raise ValidationError(
                    "This session includes an agent Synchri cannot relaunch automatically. "
                    "Restart that CLI from its setup prompt instead."
                )
            for plan in record.participants:
                self.manager.set_participant_state(
                    session_id, plan.name, "active", "session restarted by the user",
                    reset_failures=True,
                )
                self.manager.bump_recovery_generation(session_id, plan.name, "restart")
            for escalation in self.manager.open_escalations(session_id):
                if escalation["rule"] in {
                    "agent_failed", "agent_auth_failed", "agent_refused"
                }:
                    self.manager.resolve_escalation(
                        escalation["escalation_id"], "session restarted by the user"
                    )
            self.managed.restart(record)
        elif action == "restart_agent":
            participant = (body.get("participant") or "").strip()
            plan = next((p for p in record.participants if p.name == participant), None)
            if plan is None:
                raise ValidationError(f"{participant!r} is not a participant in this session")
            self.manager.set_participant_state(
                session_id, participant, "active", "restarted by the user",
                reset_failures=True,
            )
            self.manager.bump_recovery_generation(session_id, participant, "restart")
            for escalation in self.manager.open_escalations(session_id):
                if escalation["rule"] in {"agent_failed", "agent_auth_failed", "agent_refused"}:
                    self.manager.resolve_escalation(
                        escalation["escalation_id"], f"{participant} restarted by the user"
                    )
            refreshed = self.manager.get(session_id)
            if refreshed.status == "active":
                # Participant-scoped: only this agent's invocation is touched;
                # every other agent's process keeps running.
                self.managed.restart_participant(refreshed, participant)
        elif action == "stop":
            # Durable state first: once the session row says stopped, the
            # cancel below is cleanup, not the thing the user is waiting on.
            self.manager.stop(session_id, body.get("reason") or "stopped by the user")
            self.managed.cancel(session_id, reason="Stopping the session.")
            self.ancillary.cancel(session_id)
        elif action == "complete":
            # Validate and drive the phase machine BEFORE touching the agents:
            # a refused completion (unsatisfied gates, unevaluated appendix
            # items) must leave the team running. A non-terminal outcome is a
            # phase transition — appendix evaluation or extension work — whose
            # wake task the agents must be running to receive. Either way the
            # capture freeze has ended investigation, so in-flight ancillary
            # work stops now.
            completed = self.manager.complete(session_id, force=bool(body.get("force")))
            self.ancillary.cancel(session_id)
            if completed.is_terminal:
                self.managed.cancel(session_id, reason="Completing the session.")
                ancillary_module.sweep(self.manager, completed)
            else:
                self.managed.resume(completed)
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
        elif action == "extend_deadline":
            self.manager.extend_timebox(session_id, body.get("duration", ""))
        elif action == "permission_profile":
            profile = permission_profile(body.get("profile", ""))
            self.manager.update_configuration(
                session_id,
                permissions=PermissionSet.from_dict(profile.decisions()),
                reason=f"permission profile changed to {profile.label}",
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
