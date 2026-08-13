"""How the conductor invokes a participating agent.

An agent is described by a **shell command the user supplies**, not by a
provider adapter.  Synchri knows nothing about Claude, Codex, Copilot, or
Gemini here — it knows how to run a command, hand it a prompt, and read its
output.  That keeps single-terminal operation provider-agnostic.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field

from ..errors import ValidationError

#: Placeholder substituted with the prompt inside the argv.  When absent, the
#: prompt is written to the process's stdin instead.
PROMPT_PLACEHOLDER = "{prompt}"

DEFAULT_TIMEOUT_SECONDS = 900.0

#: Trailing control lines an agent may emit to steer the room.  Documented in
#: docs/single-terminal.md and included in every generated prompt.
_DIRECTIVE = re.compile(
    r"^\s*SYNCHRI[-_](?P<key>TO|HANDOFF|PASS|STATUS|CONFIDENCE|GATE|EVIDENCE|TEST|COMMIT|COMPLETE)\s*:?\s*(?P<value>.*?)\s*$",
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
            )
            started = time.monotonic()
            while True:
                try:
                    stdout, stderr = process.communicate(
                        None if in_argv else prompt, timeout=0.2
                    )
                    break
                except subprocess.TimeoutExpired:
                    # The first communicate already wrote stdin, so later
                    # calls must not write it again.
                    prompt = None
                    if cancel_event is not None and cancel_event.is_set():
                        process.terminate()
                        try:
                            stdout, stderr = process.communicate(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            stdout, stderr = process.communicate()
                        return AgentResult(self.name, stdout or "", stderr or "", process.returncode, cancelled=True)
                    if time.monotonic() - started >= self.timeout:
                        process.terminate()
                        try:
                            stdout, stderr = process.communicate(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            stdout, stderr = process.communicate()
                        return AgentResult(
                            self.name, stdout or "", stderr or f"timed out after {self.timeout:g}s",
                            process.returncode, timed_out=True,
                        )
        except (OSError, ValueError) as exc:
            return AgentResult(self.name, "", f"could not run the agent: {exc}", None)
        return AgentResult(self.name, stdout or "", stderr or "", process.returncode)


@dataclass
class AgentResult:
    """Raw output of one agent invocation."""

    name: str
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool = False
    cancelled: bool = False

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
    import os

    return {**os.environ, **extra}
