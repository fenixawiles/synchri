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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..broker import Broker, Credential
from ..cli import session as session_files
from ..errors import SynchriError, ValidationError
from ..models.envelope import MessageDraft
from ..session.modes import KNOWN_RUNTIMES, managed_command, plan_launch_status, stream_format_for
from . import recovery
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
                classification = payload.get("classification")
                if payload.get("returncode") == 0 and not payload.get("timed_out"):
                    self.manager.set_participant_state(
                        self.session_id, name, "active", None, reset_failures=True
                    )
                    resume_id = payload.get("provider_session_id")
                    if resume_id:
                        # The provider's own session id, captured while it is
                        # fresh: this is what recovery resumes instead of
                        # relaunching cold.
                        self.manager.set_participant_resume_id(self.session_id, name, resume_id)
                elif classification and classification != recovery.NORMAL_STOP:
                    self.manager.record_participant_failure(
                        self.session_id, name, classification,
                        payload.get("failure_detail") or "the last turn failed",
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
        #: Participant-scoped supervision: each agent gets its own cancel
        #: signal and its own live process groups, so restarting one agent
        #: never touches another agent's invocation.
        self._participant_cancels: dict[str, dict[str, threading.Event]] = {}
        self._participant_pgids: dict[tuple[str, str], set[int]] = {}
        #: Invocations launched as a provider-session resume, so a completed
        #: one can upgrade the runtime's "supported_unverified" resume proof.
        self._pending_resume: set[tuple[str, str]] = set()
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

    def _track_pid(self, session_id: str, participant: str, pid: int) -> None:
        with self._lock:
            self._pgids.setdefault(session_id, set()).add(pid)
            self._participant_pgids.setdefault((session_id, participant), set()).add(pid)

    def _untrack_pid(self, session_id: str, participant: str, pid: int) -> None:
        with self._lock:
            pids = self._pgids.get(session_id)
            if pids is not None:
                pids.discard(pid)
                if not pids:
                    self._pgids.pop(session_id, None)
            scoped = self._participant_pgids.get((session_id, participant))
            if scoped is not None:
                scoped.discard(pid)
                if not scoped:
                    self._participant_pgids.pop((session_id, participant), None)

    def _participant_cancel(self, session_id: str, name: str) -> threading.Event:
        with self._lock:
            return self._participant_cancels.setdefault(session_id, {}).setdefault(
                name, threading.Event()
            )

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

    def restart_participant(self, record: "SessionRecord", name: str) -> dict:
        """Restart exactly one agent, leaving every other agent's process alone.

        The participant's own cancel signal ends only its in-flight
        invocation (and its process group); the supervising loop then gives
        it a fresh invocation. When no worker is alive — the agent was
        dropped and supervision stopped — supervision is respawned, which
        touches no other process because none is running.
        """
        session_id = record.session_id
        event = self._participant_cancel(session_id, name)
        event.set()
        with self._lock:
            live = set(self._participant_pgids.get((session_id, name)) or ())
        for pid in live:
            terminate_process_group(pid)
        # Wait for the invocation to unwind before clearing the signal, so the
        # fresh invocation cannot race into an already-set cancel.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self._lock:
                if not self._participant_pgids.get((session_id, name)):
                    break
            time.sleep(0.05)
        time.sleep(0.2)
        event.clear()
        if record.status != "active" or not self.readiness(record)["available"]:
            return self.status(session_id)
        with self._lock:
            thread = self._threads.get(session_id)
            alive = thread is not None and thread.is_alive()
        if not alive:
            return self._spawn(session_id, "resuming", f"Restarting {name}.")
        run = self.status(session_id)
        self._set(session_id, run["phase"], f"{name} was restarted; the rest of the team was untouched.")
        return self.status(session_id)

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
            # The replace rung of the recovery ladder continues supervision in
            # this same worker: a replaced participant gets its fresh turn
            # immediately instead of waiting for a human to press anything.
            while self._drive(broker, manager, manager.get(session_id)) == "replaced":
                self._set(
                    session_id, "working",
                    "A replaced agent rejoined; the team is working again.",
                )
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
        """Assemble the team one agent at a time, with durable join phases.

        Every participant is attempted even when an earlier one fails: the
        room stays inactive on a partial join, agents that reached ``ready``
        stay attached and receive no work, and a retry re-runs only the
        participants that are not already acknowledged at the current
        contract revision.
        """
        invites = {
            invite["participant_name"]: invite for invite in (record.metadata or {}).get("invites", [])
        }
        self._set(record.session_id, "attaching", "Connecting each agent to this room.")
        document = manager.current_contract(record.session_id)
        already_ready = set(manager.acknowledgment_state(record.session_id)["accepted"])
        failures: list[tuple[str, str]] = []

        def phase(plan, value: str, detail: str | None = None) -> None:
            try:
                manager.set_participant_join_phase(record.session_id, plan.name, value, detail)
            except Exception:  # pragma: no cover - supervision must not break setup
                pass

        for plan in record.participants:
            if plan.name in already_ready:
                phase(plan, "ready", "already acknowledged the current contract")
                continue
            try:
                phase(plan, "launching", "Giving the agent its local room identity.")
                # A failed sign-in or a declined contract is retryable. The
                # first attempt has already redeemed this name-bound invite and
                # saved the local credential, so never mint a second identity
                # or make the human recreate the room.
                if not session_files.load(self.workspace, record.room_id, plan.name):
                    invite = invites.get(plan.name)
                    if invite is None:
                        raise ValidationError(f"no invite is available for {plan.name}")
                    joined = broker.join(
                        invite["token"], plan.name,
                        metadata={"managed": True, "runtime": plan.runtime},
                    )
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

                phase(plan, "injecting_bootstrap", "Injecting the session contract.")
                self._set(
                    record.session_id, "agreeing",
                    f"{plan.name} is reading and agreeing to the session contract.",
                )
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
                    raise ValidationError(
                        f"{plan.name} could not acknowledge the contract: {detail[:400]}"
                    )
                phase(plan, "awaiting_acknowledgment", "Verifying the agent's acknowledgment.")
                acknowledgement = manager.acknowledge(record.session_id, plan.name, result.stdout)
                if not acknowledgement["accepted"]:
                    raise ValidationError(
                        f"{plan.name} did not accept the contract: {acknowledgement['conflict']}"
                    )
                phase(plan, "ready", "Acknowledged. Waiting for the rest of the team.")
            except _SetupCancelled:
                phase(plan, None, None)
                raise
            except SynchriError as exc:
                phase(plan, "failed", exc.message[:400])
                failures.append((plan.name, exc.message))

        if failures:
            names = ", ".join(name for name, _ in failures)
            raise ValidationError(
                f"could not connect {names}; the room stays inactive and ready agents "
                "receive no work — retry relaunches only the failed agent(s)"
            )

        self._set(record.session_id, "starting", "Agreement confirmed. Starting the Primary Builder.")

    def _drive(self, broker: Broker, manager, record: "SessionRecord") -> str | None:
        session_event = self._cancel.get(record.session_id)
        agents = {
            plan.name: (lambda p=plan: self._turn_agent(manager, record, p))
            for plan in record.participants
        }
        cancel_events = {
            plan.name: recovery.CompositeCancel(
                session_event, self._participant_cancel(record.session_id, plan.name)
            )
            for plan in record.participants
        }
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

        def on_event(event: str, payload: dict) -> None:
            states.handle(event, payload)
            self._observe_turn(manager, record, event, payload)

        durable_states = manager.participant_states(record.session_id)
        conductor = Conductor(
            broker,
            record.room_id,
            agents,
            credentials,
            observer,
            role_guidance=role_guidance,
            session_id=record.session_id,
            cancel_event=session_event,
            cancel_events=cancel_events,
            on_event=on_event,
            # The breaker composes across idle/resume boundaries: strikes are
            # durable, so a crash before an idle stop still counts.
            initial_failures={
                name: state["failures"] for name, state in durable_states.items()
            },
        )
        report = conductor.run()
        if report.reason == "agent_failed" and report.failed_participant:
            return self._handle_agent_failure(
                broker,
                manager,
                record,
                report.failed_participant,
                report.failure_kind or recovery.CRASH,
                conductor.max_consecutive_failures,
            )
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
        return None

    def _handle_agent_failure(
        self, broker: Broker, manager, record: "SessionRecord", name: str, kind: str, strikes: int
    ) -> str | None:
        """One supervisor decision per breaker trip.

        Auth failures and provider refusals go straight to the human — those
        recoveries genuinely require them. Ladder kinds (crash, unresponsive)
        get exactly one automatic replacement incarnation, reconstructed from
        the durable room; a second trip is dropped and escalated.
        """
        session_id = record.session_id
        try:
            if kind == recovery.AUTH_FAILURE:
                manager.set_participant_state(
                    session_id, name, kind, recovery.FAILURE_DETAILS[kind]
                )
                manager.escalate(
                    session_id, "agent_auth_failed",
                    f"{name}'s provider sign-in failed. Sign in to the CLI again in your "
                    f"terminal, then Restart {name} from the conversation.",
                    raised_by=name,
                )
                self._set(
                    session_id, "needs_attention",
                    f"{name} is signed out of its provider and needs you.",
                    reason="agent_auth_failed",
                )
                return None
            if kind == recovery.PROVIDER_REFUSAL:
                manager.set_participant_state(
                    session_id, name, kind, recovery.FAILURE_DETAILS[kind]
                )
                manager.escalate(
                    session_id, "agent_refused",
                    f"{name}'s provider refused or rate-limited the request. Decide how to "
                    f"proceed, then Restart {name} or stop the session.",
                    raised_by=name,
                )
                self._set(
                    session_id, "needs_attention",
                    f"{name}'s provider refused the request and needs your decision.",
                    reason="agent_refused",
                )
                return None

            already_replaced = bool(
                manager.participant_recovery(session_id, name)["metadata"].get("auto_replaced")
            )
            if not already_replaced:
                manager.mark_participant_replaced(session_id, name)
                generation = manager.bump_recovery_generation(session_id, name, "replace")
                manager.set_participant_state(
                    session_id, name, "active",
                    f"replaced with a fresh incarnation (generation {generation}); "
                    "state reconstructed from the durable room",
                    reset_failures=True,
                )
                # Give the replacement its turn: the room idled when the old
                # incarnation broke, and a replaced agent that never gets the
                # floor is not a recovery. The task is sent with the human
                # credential so the lead-then-review discipline still applies.
                human = (record.metadata or {}).get("human") or {}
                other = next(
                    (p.name for p in record.participants if p.name != name), None
                )
                metadata: dict = {"source": "agent_replacement"}
                if other:
                    metadata["human_direction"] = {"lead": name, "reviewer": other}
                try:
                    broker.send(
                        record.room_id,
                        credential=Credential(
                            participant=human.get("name"), secret=human.get("secret")
                        ),
                        draft=MessageDraft(
                            message_type="task",
                            target=name,
                            content=(
                                "Your previous process stopped responding and was replaced. "
                                "Rebuild your working state from the durable room above — the "
                                "contract, the specification, and the recent conversation — "
                                "then continue the work from where it actually stands."
                            ),
                            metadata=metadata,
                        ),
                    )
                except SynchriError:  # pragma: no cover - room may be closing
                    pass
                self._set(
                    session_id, "working",
                    f"{name} stopped responding and was replaced; continuing the session.",
                )
                return "replaced"

            manager.set_participant_state(
                session_id, name, "dropped",
                f"dropped after {strikes} consecutive failed turns ({kind}), "
                "including one automatic replacement",
            )
            manager.escalate(
                session_id, "agent_failed",
                f"{name} kept failing ({kind}) even after an automatic replacement and was "
                "dropped. Restart it from the conversation, or stop the session.",
                raised_by=name,
            )
        except SynchriError:  # pragma: no cover - session may have ended underneath
            pass
        self._set(
            session_id, "needs_attention",
            f"{name} stopped and needs your attention.",
            reason="agent_failed",
        )
        return None

    def _observe_turn(self, manager, record: "SessionRecord", event: str, payload: dict) -> None:
        """Registry-side turn observation: prove resume at first real recovery."""
        if event != "agent.returned":
            return
        key = (record.session_id, payload.get("participant"))
        if key not in self._pending_resume:
            return
        self._pending_resume.discard(key)
        if payload.get("returncode") == 0 and not payload.get("timed_out") and not payload.get("cancelled"):
            plan = next((p for p in record.participants if p.name == key[1]), None)
            if plan is None:
                return
            try:
                # "supported_unverified" was an honest maybe; a real recovery
                # that resumed and completed is the proof the doctor could not
                # manufacture.
                manager.conn.execute(
                    "UPDATE runtime_connections SET resume = 'verified' "
                    "WHERE runtime = ? AND resume = 'supported_unverified'",
                    (plan.runtime,),
                )
            except Exception:  # pragma: no cover - proof upgrade is best-effort
                pass

    def _turn_agent(self, manager, record: "SessionRecord", plan) -> AgentCommand:
        """Build this participant's command for the turn that is starting.

        The resume rung: after a crash or an unresponsive kill, when the
        maintained adapter defines resume, the runtime's connection record
        does not say ``unsupported``, and a provider session id was captured,
        the next invocation resumes that session instead of relaunching cold.
        The stored id is consumed either way, so a failed resume can never
        loop.
        """
        use_plain = plan.runtime in self._plain_runtimes
        user_command = bool(plan.command and plan.command.strip())
        if not use_plain and not user_command:
            try:
                state = manager.participant_recovery(record.session_id, plan.name)
            except SynchriError:
                state = {"resume_id": None, "runtime_status": None}
            needs_recovery = state.get("runtime_status") in recovery.LADDER_KINDS
            resume_id = state.get("resume_id")
            template = (KNOWN_RUNTIMES.get(plan.runtime) or {}).get("resume_command")
            if needs_recovery and resume_id and template and self._resume_allowed(manager, plan.runtime):
                command = template.replace("{resume_id}", shlex.quote(resume_id))
                agent = self._build_agent(record, plan, command)
                try:
                    manager.set_participant_resume_id(record.session_id, plan.name, None)
                    generation = manager.bump_recovery_generation(
                        record.session_id, plan.name, "resume"
                    )
                    manager.set_participant_state(
                        record.session_id, plan.name, "active",
                        f"recovering — resuming the provider session (generation {generation})",
                    )
                except SynchriError:  # pragma: no cover - session may be ending
                    pass
                self._pending_resume.add((record.session_id, plan.name))
                return agent
        return self._agent(record, plan)

    @staticmethod
    def _resume_allowed(manager, runtime: str) -> bool:
        try:
            row = manager.conn.execute(
                "SELECT resume FROM runtime_connections WHERE runtime = ?", (runtime,)
            ).fetchone()
        except Exception:  # pragma: no cover - defensive
            return False
        # No record yet: the adapter defines resume, so attempt it — the
        # attempt itself becomes the verification.
        return row is None or row["resume"] != "unsupported"

    def _agent(self, record: "SessionRecord", plan) -> AgentCommand:
        use_plain = plan.runtime in self._plain_runtimes
        command = managed_command(plan, plain=use_plain)
        if not command:
            raise ValidationError(f"no managed command is configured for {plan.name}")
        return self._build_agent(record, plan, command, plain=use_plain)

    def _build_agent(
        self, record: "SessionRecord", plan, command: str, *, plain: bool = False
    ) -> AgentCommand:
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
        participant = plan.name
        agent.on_spawn = lambda pid: self._track_pid(session_id, participant, pid)
        agent.on_exit = lambda pid: self._untrack_pid(session_id, participant, pid)
        stream_format = None if plain else stream_format_for(plan)
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
