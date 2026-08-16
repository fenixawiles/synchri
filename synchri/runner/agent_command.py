"""How the conductor invokes a participating agent.

An agent is described by a **shell command the user supplies**, not by a
provider adapter.  Synchri knows nothing about Claude, Codex, Copilot, or
Gemini here — it knows how to run a command, hand it a prompt, and read its
output.  That keeps single-terminal operation provider-agnostic.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from ..errors import ValidationError

#: Placeholder substituted with the prompt inside the argv.  When absent, the
#: prompt is written to the process's stdin instead.
PROMPT_PLACEHOLDER = "{prompt}"

DEFAULT_TIMEOUT_SECONDS = 900.0

#: Trailing control lines an agent may emit to steer the room.  Documented in
#: docs/single-terminal.md and included in every generated prompt.
_DIRECTIVE = re.compile(
    r"^\s*SYNCHRI[-_](?P<key>TO|HANDOFF|PASS|STATUS|CONFIDENCE|GATE|EVIDENCE|TEST|COMMIT|COMPLETE|APPROVAL)\s*:?\s*(?P<value>.*?)\s*$",
    re.IGNORECASE,
)


@dataclass
class AgentCommand:
    """A managed participant: a name plus the command that speaks for it."""

    name: str
    argv: list[str]
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    #: Supervision hooks: called with the child's pid right after spawn and
    #: right after the invocation ends, so a registry can signal the process
    #: group even when this invoke's caller is gone.
    on_spawn: Callable[[int], None] | None = None
    on_exit: Callable[[int], None] | None = None
    #: Streaming ingestion (optional): a factory building a fresh stream
    #: parser per invocation, and a durable recorder receiving its normalized
    #: events. Both are best-effort — any failure falls back to raw stdout.
    parser_factory: Callable[[], object] | None = None
    recorder: object | None = None

    @property
    def takes_prompt_in_argv(self) -> bool:
        return any(PROMPT_PLACEHOLDER in part for part in self.argv)

    @classmethod
    def parse(cls, spec: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS, cwd: str | None = None):
        """Parse a ``name=command`` specification.

        The command is split with ``shlex`` and executed **without a shell**, so
        a prompt containing shell metacharacters cannot be interpreted as
        anything but an argument.
        """
        if not isinstance(spec, str) or "=" not in spec:
            raise ValidationError(
                f"--agent expects 'name=command', got {spec!r} "
                "(for example: --agent 'codex=codex exec {prompt}')"
            )
        name, _, command = spec.partition("=")
        name = name.strip()
        command = command.strip()
        if not name or not command:
            raise ValidationError(f"--agent expects 'name=command', got {spec!r}")
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise ValidationError(f"could not parse the command for {name!r}: {exc}") from exc
        if not argv:
            raise ValidationError(f"no command given for participant {name!r}")
        return cls(name=name, argv=argv, timeout=timeout, cwd=cwd)

    def build_argv(self, prompt: str) -> list[str]:
        return [part.replace(PROMPT_PLACEHOLDER, prompt) for part in self.argv]

    def invoke(self, prompt: str, *, cancel_event=None) -> "AgentResult":
        """Run the agent and capture what it said.

        Never raises for agent misbehavior: a crash, a non-zero exit, or a
        timeout all come back as an ``AgentResult`` so the conductor can report
        them into the room rather than dying.

        The child runs as its own session leader (POSIX), so cancellation and
        timeouts end the whole process group: wrapper CLIs fork tool processes
        of their own, and ending only the wrapper would leave those
        grandchildren alive and still writing to the worktree.
        """
        if cancel_event is not None and cancel_event.is_set():
            return AgentResult(self.name, "", "", None, cancelled=True)
        in_argv = self.takes_prompt_in_argv
        try:
            process = subprocess.Popen(
                self.build_argv(prompt) if in_argv else list(self.argv),
                stdin=subprocess.PIPE if not in_argv else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=self.cwd,
                env=_merged_env(self.env),
                start_new_session=(os.name == "posix"),
            )
        except (OSError, ValueError) as exc:
            return AgentResult(self.name, "", f"could not run the agent: {exc}", None)

        if self.on_spawn is not None:
            try:
                self.on_spawn(process.pid)
            except Exception:  # pragma: no cover - supervision must not break the run
                pass

        parser = None
        if self.parser_factory is not None:
            try:
                parser = self.parser_factory()
            except Exception:  # pragma: no cover - streaming is best-effort
                parser = None
        recorder = self.recorder if parser is not None else None
        if recorder is not None:
            try:
                recorder.begin()
            except Exception:  # pragma: no cover - streaming is best-effort
                recorder = None
        on_line = None
        if parser is not None:
            def on_line(line, _parser=parser, _recorder=recorder):
                events = _parser.feed(line)
                if _recorder is not None:
                    for event in events:
                        _recorder.write(event)

        stdout_buffer = _CappedText(limit=2_000_000)
        stderr_buffer = _CappedText(limit=200_000)
        workers = [
            threading.Thread(
                target=_pump, args=(process.stdout, stdout_buffer, on_line), daemon=True
            ),
            threading.Thread(target=_pump, args=(process.stderr, stderr_buffer), daemon=True),
        ]
        if not in_argv:
            workers.append(
                threading.Thread(target=_feed_stdin, args=(process.stdin, prompt), daemon=True)
            )
        for worker in workers:
            worker.start()

        started = time.monotonic()
        outcome = "ok"
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                _terminate_tree(process)
                outcome = "cancelled"
                break
            if time.monotonic() - started >= self.timeout:
                _terminate_tree(process)
                outcome = "timed_out"
                break
            time.sleep(0.2)
        if outcome == "ok" and cancel_event is not None and cancel_event.is_set():
            # The registry may have killed the process group directly (e.g. a
            # Stop that raced this loop); a cancel-induced death must never be
            # mistaken for the agent's own answer.
            outcome = "cancelled"

        for worker in workers:
            worker.join(timeout=2)
        try:
            process.wait(timeout=1)  # reap; the child ended above or was killed
        except subprocess.TimeoutExpired:  # pragma: no cover - kill-resistant child
            pass
        if self.on_exit is not None:
            try:
                self.on_exit(process.pid)
            except Exception:  # pragma: no cover - supervision must not break the run
                pass
        if recorder is not None:
            try:
                recorder.finish(parser, returncode=process.returncode, outcome=outcome)
            except Exception:  # pragma: no cover - streaming is best-effort
                pass

        stdout = stdout_buffer.text()
        stderr = stderr_buffer.text()
        tool_events = None
        if parser is not None and getattr(parser, "saw_stream", False):
            # The stream was real JSONL: hand downstream code the agent's
            # reconstructed reply, not the event soup. When the stream never
            # produced a final text (killed mid-run, schema drift), the raw
            # capture stands so behavior degrades to the plain-stdout path.
            try:
                final = parser.final_text()
            except Exception:  # pragma: no cover - defensive
                final = None
            if final:
                stdout = final
            tool_events = getattr(parser, "tool_events", None)
        if outcome == "cancelled":
            return AgentResult(
                self.name, stdout, stderr, process.returncode,
                cancelled=True, tool_events=tool_events,
            )
        if outcome == "timed_out":
            return AgentResult(
                self.name, stdout, stderr or f"timed out after {self.timeout:g}s",
                process.returncode, timed_out=True, tool_events=tool_events,
            )
        return AgentResult(self.name, stdout, stderr, process.returncode, tool_events=tool_events)


@dataclass
class AgentResult:
    """Raw output of one agent invocation."""

    name: str
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool = False
    cancelled: bool = False
    #: How many tool/command/file events the stream carried; ``None`` when the
    #: runtime does not stream. ``0`` on a clean exit is the low-signal marker.
    tool_events: int | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled


@dataclass
class Directives:
    """Control instructions parsed out of an agent's trailing output lines."""

    to: str | None = None
    handoff: str | None = None
    passed: bool = False
    status: str | None = None
    confidence: float | None = None
    gate_updates: list["GateUpdate"] = field(default_factory=list)
    complete_requested: bool = False
    approval_capability: str | None = None
    approval_request: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class GateUpdate:
    """An evidence-bearing gate report attached to one completed agent turn."""

    gate_id: str
    status: str | None = None
    assessment: str | None = None
    evidence: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)


def parse_directives(text: str) -> tuple[str, Directives]:
    """Split trailing ``SYNCHRI-*`` control lines off an agent's reply.

    Only *trailing* lines count, so an agent quoting the convention in the
    middle of a review does not accidentally redirect the room.
    """
    directives = Directives()
    lines = (text or "").splitlines()
    end = len(lines)
    consumed: list[re.Match] = []

    while end > 0:
        line = lines[end - 1]
        if not line.strip():
            end -= 1
            continue
        match = _DIRECTIVE.match(line)
        if match is None:
            break
        consumed.append(match)
        end -= 1

    for match in reversed(consumed):
        key = match.group("key").upper()
        value = match.group("value").strip()
        if key == "TO":
            directives.to = value or None
        elif key == "HANDOFF":
            directives.handoff = value or None
        elif key == "PASS":
            directives.passed = True
        elif key == "STATUS":
            directives.status = value.lower() or None
        elif key == "CONFIDENCE":
            try:
                directives.confidence = max(0.0, min(1.0, float(value)))
            except ValueError:
                directives.warnings.append(f"ignored unparseable confidence {value!r}")
        elif key == "GATE":
            gate_id, separator, remainder = value.partition("|")
            gate_id = gate_id.strip().upper()
            if not gate_id:
                directives.warnings.append("ignored a gate update with no gate id")
                continue
            status, separator, assessment = remainder.partition("|") if separator else ("", "", "")
            directives.gate_updates.append(
                GateUpdate(
                    gate_id=gate_id,
                    status=status.strip().lower() or None,
                    assessment=assessment.strip() or None,
                )
            )
        elif key in {"EVIDENCE", "TEST", "COMMIT"}:
            if not directives.gate_updates:
                directives.warnings.append(f"ignored {key.lower()} with no preceding SYNCHRI-GATE")
                continue
            if value:
                field_name = {"EVIDENCE": "evidence", "TEST": "tests", "COMMIT": "commits"}[key]
                getattr(directives.gate_updates[-1], field_name).append(value)
        elif key == "COMPLETE":
            directives.complete_requested = True
        elif key == "APPROVAL":
            capability, separator, description = value.partition("|")
            directives.approval_capability = capability.strip() or None
            directives.approval_request = (
                description.strip() if separator and description.strip() else value or "Approval requested"
            )

    if directives.to and directives.handoff:
        # The envelope forbids both; addressing someone is the stronger signal.
        directives.warnings.append(
            f"agent asked to both address {directives.to!r} and hand off to "
            f"{directives.handoff!r}; using the direct address"
        )
        directives.handoff = None

    return "\n".join(lines[:end]).strip(), directives


def _merged_env(extra: dict[str, str]) -> dict[str, str] | None:
    if not extra:
        return None
    return {**os.environ, **extra}


class _CappedText:
    """Line sink that keeps the newest output once a size limit is passed.

    The tail is what matters operationally: trailing ``SYNCHRI-*`` directives
    steer the room, and error output ends with the actual failure. A dropped
    head is marked so a truncated reply is never mistaken for a complete one.
    """

    def __init__(self, *, limit: int) -> None:
        self._limit = limit
        self._lines: deque[str] = deque()
        self._size = 0
        self._dropped = False
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)
            self._size += len(line)
            while self._size > self._limit and len(self._lines) > 1:
                dropped = self._lines.popleft()
                self._size -= len(dropped)
                self._dropped = True

    def text(self) -> str:
        with self._lock:
            body = "".join(self._lines)
            if self._dropped:
                return "[synchri: earlier output was truncated]\n" + body
            return body


def _pump(stream, sink: _CappedText, on_line=None) -> None:
    try:
        for line in stream:
            sink.append(line)
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:  # streaming must never break output capture
                    on_line = None
    except (OSError, ValueError):  # pragma: no cover - stream torn down mid-read
        pass
    finally:
        try:
            stream.close()
        except OSError:  # pragma: no cover - already closed
            pass


def _feed_stdin(stdin, prompt: str) -> None:
    try:
        stdin.write(prompt or "")
        stdin.close()
    except (BrokenPipeError, OSError, ValueError):  # pragma: no cover - agent exited early
        pass


def terminate_process_group(pid: int, sig: int | None = None) -> bool:
    """Best-effort signal to the process group led by ``pid``.

    Returns False when the group cannot be signalled (already gone, or not
    POSIX) so callers can fall back to single-process termination.
    """
    if os.name != "posix":
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM if sig is None else sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _terminate_tree(process: subprocess.Popen) -> None:
    """End the invocation and everything it started.

    The child is its own session leader, so the group signal reaches the
    wrapper CLI's children — tool processes, MCP servers, spawned shells —
    that a plain ``terminate()`` would leave running. Known limit: a
    grandchild that itself calls ``setsid`` starts a new group and escapes;
    none of the supported agent CLIs do that.
    """
    if not terminate_process_group(process.pid):
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if not terminate_process_group(process.pid, signal.SIGKILL):
            process.kill()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
            pass
