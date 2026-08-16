"""Run a whole local Synchri session without making the human relay setup.

The registry is intentionally *not* another broker.  Its in-memory state is
only process supervision: rooms, identities, contracts, messages, and work all
remain authoritative in the existing SQLite workspace.  If the UI closes, a
managed run can stop, but nothing is lost and ``resume`` simply opens the same
durable session again.
"""

from __future__ import annotations

import shlex
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..broker import Broker, Credential
from ..cli import session as session_files
from ..errors import SynchriError, ValidationError
from ..session.modes import managed_command, plan_launch_status, stream_format_for
from .agent_command import AgentCommand, terminate_process_group
from .conductor import Conductor
from .stream_events import StreamRecorder, parser_for


class _SetupCancelled(Exception):
    """The user stopped the session while agents were still being attached."""


#: Stderr fragments that mean "this installed CLI predates its streaming
#: flags" — the one failure the registry retries with the plain command.
_FLAG_REJECTION_MARKERS = (
    "--output-format",
    "--json",
    "--verbose",
    "unknown option",
    "unexpected argument",
    "unrecognized option",
    "unknown flag",
    "unrecognized subcommand",
)


def _looks_like_flag_rejection(result) -> bool:
    if result.returncode in (None, 0) or result.timed_out or result.cancelled:
        return False
    stderr = (result.stderr or "").lower()
    return any(marker in stderr for marker in _FLAG_REJECTION_MARKERS)


class _ParticipantStateBridge:
    """Map conductor events onto durable per-agent runtime state.

    Runs on the worker thread, against the worker's own manager/connection.
    Supervision must never break the run, so every write is best-effort.
    """

    def __init__(self, manager, session_id: str) -> None:
        self.manager = manager
        self.session_id = session_id

    def handle(self, event: str, payload: dict) -> None:
        name = payload.get("participant")
        if not name:
            return
        try:
            if event == "agent.invoking":
                self.manager.set_participant_state(self.session_id, name, "active", "working")
            elif event == "agent.returned":
                if payload.get("cancelled"):
                    return  # the session-level stop writes the terminal states
                if payload.get("timed_out"):
                    self.manager.record_participant_failure(
                        self.session_id, name, "timed_out", "the last turn timed out"
                    )
                elif payload.get("returncode") not in (0, None):
                    self.manager.record_participant_failure(
                        self.session_id, name, "failed",
                        f"the last turn exited with code {payload.get('returncode')}",
                    )
                elif payload.get("returncode") == 0:
                    self.manager.set_participant_state(
                        self.session_id, name, "active", None, reset_failures=True
                    )
            elif event == "agent.low_signal":
                self.manager.set_participant_state(
                    self.session_id, name, "active",
                    "may be refusing or stuck — check its last message",
                )
        except Exception:  # pragma: no cover - supervision must not break the run
            pass

if TYPE_CHECKING:  # pragma: no cover - imports for type checkers only
    from ..config import Workspace
    from ..session.manager import SessionRecord


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ManagedRun:
    """Ephemeral supervision information exposed to the local UI."""

    session_id: str
    phase: str = "not_started"
    detail: str = ""
    started_at: str | None = None
    updated_at: str | None = None
    reason: str | None = None
    alive: bool = False

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "phase": self.phase,
            "detail": self.detail,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "reason": self.reason,
            "alive": self.alive,
        }


class ManagedRunnerRegistry:
    """Starts and resumes supervised local agent sessions.

    One registry belongs to the opt-in UI process.  Every worker opens its own
    Broker connection because SQLite connections are thread-affine; that also
    makes a UI restart just another safe reader/writer of the durable room.
    """

    def __init__(self, workspace: "Workspace", *, cli_command: str | None = None) -> None:
        self.workspace = workspace
        self.cli_command = cli_command or _local_cli_command()
        self._runs: dict[str, ManagedRun] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._pgids: dict[str, set[int]] = {}
        #: Runtimes whose installed CLI rejected the streaming flags; they run
        #: the plain maintained command for the rest of this process.
        self._plain_runtimes: set[str] = set()
        self._lock = threading.Lock()

    def readiness(self, record: "SessionRecord") -> dict:
        agents = []
        for plan in record.participants:
            launch = plan_launch_status(plan)
            agents.append({"name": plan.name, "runtime": plan.runtime, **launch})
        return {
            "available": bool(agents) and all(agent["ready"] for agent in agents),
            "agents": agents,
        }

    def status(self, session_id: str) -> dict:
        with self._lock:
            run = self._runs.get(session_id)
            return (run or ManagedRun(session_id=session_id)).to_dict()

    def cancel(self, session_id: str, *, reason: str) -> dict:
        """Signal an in-flight provider command to end without losing room state."""
        with self._lock:
            event = self._cancel.get(session_id)
            if event is not None:
                event.set()
            live_groups = set(self._pgids.get(session_id) or ())
            run = self._runs.setdefault(session_id, ManagedRun(session_id=session_id))
            run.phase = "stopping"
            run.detail = reason
            run.reason = "cancelled"
            run.updated_at = _now()
            run.alive = event is not None
            payload = run.to_dict()
        # Outside the lock: signal any live process group directly, so Stop
        # still ends provider processes when the worker thread is wedged or
        # its cancel event has already been discarded. The supervising invoke
        # escalates to SIGKILL itself when it is alive to do so.
        for pid in live_groups:
            terminate_process_group(pid)
        return payload

    def _track_pid(self, session_id: str, pid: int) -> None:
        with self._lock:
            self._pgids.setdefault(session_id, set()).add(pid)

    def _untrack_pid(self, session_id: str, pid: int) -> None:
        with self._lock:
            pids = self._pgids.get(session_id)
            if pids is not None:
                pids.discard(pid)
                if not pids:
                    self._pgids.pop(session_id, None)

    def start(self, record: "SessionRecord") -> dict:
        readiness = self.readiness(record)
        if not readiness["available"]:
            unavailable = [a["name"] for a in readiness["agents"] if not a["ready"]]
            raise ValidationError(
                "Synchri cannot launch " + ", ".join(unavailable)
                + " yet; use their setup prompt or choose installed managed agents"
            )
        return self._spawn(record.session_id, "attaching", "Connecting the local agents to this room.")

    def resume(self, record: "SessionRecord") -> dict:
        """Resume after a human reply reaches a managed agent."""
        if not self.readiness(record)["available"] or record.status != "active":
            return self.status(record.session_id)
        # Pause/Stop signal a running agent asynchronously.  If the user
        # resumes immediately, wait briefly for that supervised worker to
        # acknowledge cancellation before spawning the new one; otherwise the
        # old thread would win the race and make Resume look like a no-op.
        with self._lock:
            thread = self._threads.get(record.session_id)
            event = self._cancel.get(record.session_id)
        if thread is not None and thread.is_alive() and event is not None and event.is_set():
            thread.join(timeout=4)
        return self._spawn(record.session_id, "resuming", "Your reply reached the agents; continuing the session.")

    def restart(self, record: "SessionRecord") -> dict:
        """Respawn supervision after the user restarts a dropped agent.

        Unlike ``resume``, this actively cancels a live worker first: the
        point of Restart is a fresh invocation, not joining a wedged one.
        """
        if not self.readiness(record)["available"] or record.status != "active":
            return self.status(record.session_id)
        with self._lock:
            thread = self._threads.get(record.session_id)
            event = self._cancel.get(record.session_id)
        if thread is not None and thread.is_alive():
            if event is not None:
                event.set()
            thread.join(timeout=4)
        return self._spawn(record.session_id, "resuming", "Restarting the agent team.")

    def _spawn(self, session_id: str, phase: str, detail: str) -> dict:
        with self._lock:
            thread = self._threads.get(session_id)
            if thread is not None and thread.is_alive():
                return self._runs[session_id].to_dict()
            run = ManagedRun(
                session_id=session_id,
                phase=phase,
                detail=detail,
                started_at=_now(),
                updated_at=_now(),
                alive=True,
            )
            self._runs[session_id] = run
            self._cancel[session_id] = threading.Event()
            worker = threading.Thread(
                target=self._worker,
                args=(session_id, phase == "attaching"),
                name=f"synchri-managed-{session_id}",
                daemon=True,
            )
            self._threads[session_id] = worker
            worker.start()
            return run.to_dict()

    def _set(self, session_id: str, phase: str, detail: str, *, reason: str | None = None) -> None:
        with self._lock:
            run = self._runs.setdefault(session_id, ManagedRun(session_id=session_id))
            run.phase = phase
            run.detail = detail
            run.reason = reason
            run.updated_at = _now()
            run.alive = phase in {"attaching", "agreeing", "starting", "working", "resuming"}

    def _worker(self, session_id: str, attach: bool) -> None:
        broker = Broker(self.workspace)
        try:
            from ..session.manager import SessionManager

            manager = SessionManager(broker)
            record = manager.get(session_id)
            if attach:
                self._attach_and_agree(broker, manager, record)
                record = manager.get(session_id)
                manager.activate(session_id)
            self._set(session_id, "working", "The agents are working in the isolated workspace.")
            self._drive(broker, manager, manager.get(session_id))
        except _SetupCancelled:
            self._set(session_id, "stopped", "Session control applied.", reason="cancelled")
        except SynchriError as exc:
            self._set(session_id, "needs_attention", exc.message, reason=exc.code)
        except Exception as exc:  # pragma: no cover - defensive UI boundary
            self._set(session_id, "failed", f"Synchri could not run this session: {exc}", reason="internal")
        finally:
            with self._lock:
                self._cancel.pop(session_id, None)
            broker.close()

    def _attach_and_agree(self, broker: Broker, manager, record: "SessionRecord") -> None:
        invites = {
            invite["participant_name"]: invite for invite in (record.metadata or {}).get("invites", [])
        }
        self._set(record.session_id, "attaching", "Giving each managed agent its local room identity.")
        for plan in record.participants:
            # A failed sign-in or a declined contract is retryable. The first
            # attempt has already redeemed this name-bound invite and saved the
            # local credential, so never try to mint a second identity or make
            # the human recreate the room.
            if session_files.load(self.workspace, record.room_id, plan.name):
                continue
            invite = invites.get(plan.name)
            if invite is None:
                raise ValidationError(f"no invite is available for {plan.name}")
            joined = broker.join(invite["token"], plan.name, metadata={"managed": True, "runtime": plan.runtime})
            session_files.save(
                self.workspace,
                session_files.SessionRecord(
                    room_id=joined["room_id"],
                    participant=joined["name"],
                    participant_id=joined["participant_id"],
                    secret=joined["secret"],
                    kind=joined["kind"],
                    room_name=joined.get("room_name", ""),
                ),
            )

        self._set(record.session_id, "agreeing", "Each agent is reading and agreeing to the session contract.")
        document = manager.current_contract(record.session_id)
        for plan in record.participants:
            prompt = _agreement_prompt(document.for_participant(plan.name))
            result = self._agent(record, plan).invoke(
                prompt, cancel_event=self._cancel.get(record.session_id)
            )
            if result.cancelled:
                raise _SetupCancelled()
            if (
                not result.ok
                and stream_format_for(plan)
                and plan.runtime not in self._plain_runtimes
                and _looks_like_flag_rejection(result)
            ):
                # The installed CLI predates its streaming flags. Remember
                # that for this runtime and retry once with the plain
                # maintained command — behavior degrades to plain stdout.
                self._plain_runtimes.add(plan.runtime)
                result = self._agent(record, plan).invoke(
                    prompt, cancel_event=self._cancel.get(record.session_id)
                )
                if result.cancelled:
                    raise _SetupCancelled()
            if not result.ok:
                detail = (result.stderr or "no response").strip()
                raise ValidationError(f"{plan.name} could not acknowledge the contract: {detail[:400]}")
            acknowledgement = manager.acknowledge(record.session_id, plan.name, result.stdout)
            if not acknowledgement["accepted"]:
                raise ValidationError(
                    f"{plan.name} did not accept the contract: {acknowledgement['conflict']}"
                )

        self._set(record.session_id, "starting", "Agreement confirmed. Starting the Primary Builder.")

    def _drive(self, broker: Broker, manager, record: "SessionRecord") -> None:
        agents = {plan.name: self._agent(record, plan) for plan in record.participants}
        credentials = {
            name: session_files.resolve_credential(self.workspace, record.room_id, name)
            for name in agents
        }
        human = (record.metadata or {}).get("human") or {}
        observer = Credential(participant=human.get("name"), secret=human.get("secret"))
        document = manager.current_contract(record.session_id)
        role_guidance = {
            plan.name: document.core_text + "\n\n" + document.role_sections.get(plan.name, "")
            for plan in record.participants
        }

        states = _ParticipantStateBridge(manager, record.session_id)
        conductor = Conductor(
            broker,
            record.room_id,
            agents,
            credentials,
            observer,
            role_guidance=role_guidance,
            session_id=record.session_id,
            cancel_event=self._cancel.get(record.session_id),
            on_event=states.handle,
        )
        report = conductor.run()
        if report.reason == "agent_failed" and report.failed_participant:
            name = report.failed_participant
            try:
                manager.set_participant_state(
                    record.session_id, name, "dropped",
                    f"dropped after {conductor.max_consecutive_failures} consecutive failed turns",
                )
                manager.escalate(
                    record.session_id,
                    "agent_failed",
                    f"{name} failed {conductor.max_consecutive_failures} turns in a row and was "
                    "dropped. Restart it from the conversation, or stop the session.",
                    raised_by=name,
                )
            except SynchriError:  # pragma: no cover - session may have ended underneath
                pass
            self._set(
                record.session_id,
                "needs_attention",
                f"{name} stopped and needs your attention.",
                reason="agent_failed",
            )
            return
        phrases = {
            "awaiting_human": "The agents need your decision before they can continue.",
            "room_paused": "The session is paused.",
            "room_stopped": "The session has ended.",
            "idle": "The agents have no next handoff. Review their last message.",
            "unmanaged_speaker": "A human or externally-run participant has the floor.",
            "cancelled": "Session control applied.",
        }
        self._set(
            record.session_id,
            "waiting" if report.reason in {"awaiting_human", "idle", "unmanaged_speaker"} else "stopped",
            phrases.get(report.reason, f"Managed run ended: {report.reason}."),
            reason=report.reason,
        )

    def _agent(self, record: "SessionRecord", plan) -> AgentCommand:
        use_plain = plan.runtime in self._plain_runtimes
        command = managed_command(plan, plain=use_plain)
        if not command:
            raise ValidationError(f"no managed command is configured for {plan.name}")
        agent = AgentCommand.parse(
            f"{plan.name}={command}",
            cwd=record.worktree.path if record.worktree else None,
        )
        agent.env.update(
            {
                "SYNCHRI_HOME": str(self.workspace.home),
                "SYNCHRI_ROOM": record.room_id,
                "SYNCHRI_PARTICIPANT": plan.name,
                "SYNCHRI_CLI": self.cli_command,
            }
        )
        session_id = record.session_id
        agent.on_spawn = lambda pid: self._track_pid(session_id, pid)
        agent.on_exit = lambda pid: self._untrack_pid(session_id, pid)
        stream_format = None if use_plain else stream_format_for(plan)
        if stream_format and record.room_id:
            agent.parser_factory = lambda fmt=stream_format: parser_for(fmt)
            agent.recorder = StreamRecorder(
                self.workspace,
                room_id=record.room_id,
                session_id=session_id,
                participant=plan.name,
                runtime=plan.runtime,
            )
        return agent


def _agreement_prompt(contract: str) -> str:
    return "\n".join(
        [
            "You are being launched by Synchri as a local coding agent.",
            "Read this session contract in full. Do not begin work, call tools, or add commentary.",
            "If you can follow it, output exactly UNDERSTOOD. Otherwise output exactly",
            "CONFLICT: followed by the specific reason.",
            "",
            contract.rstrip(),
        ]
    )


def _local_cli_command() -> str:
    """Use a concrete helper path for bundled apps; `synchri` for normal installs."""
    if getattr(sys, "frozen", False):
        return shlex.quote(sys.executable)
    import shutil

    return shutil.which("synchri") or f"{shlex.quote(sys.executable)} -m synchri"
