"""Session modes and their immutable operating policy.

A mode is not a label: it decides how much autonomy agents get, whether a
deadline and a specification are required, and what doctrine goes into the
contract.  Each mode carries its own policy so that adding a mode later is a
new entry in ``POLICIES``, not a redesign of startup.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from enum import Enum

from ..errors import ValidationError
from .permissions import Decision, PermissionSet


class SessionMode(str, Enum):
    INTERACTIVE = "interactive"
    LONG_HORIZON = "long_horizon"
    REVIEW_AUDIT = "review_audit"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class ModePolicy:
    """Defaults and requirements a mode imposes before a session can start."""

    mode: SessionMode
    label: str
    summary: str
    doctrine: str
    #: Consecutive agent turns before the room hands itself back to the human.
    #: Long-horizon sets this high on purpose: finishing a turn is not a reason
    #: to interrupt the user.
    max_consecutive_agent_turns: int
    requires_spec: bool = False
    requires_deadline: bool = False
    supports_deadline: bool = True
    min_agents: int = 1
    default_roles: tuple[str, ...] = ()
    #: Capabilities this mode forces off regardless of what the user picked.
    forced_denials: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "label": self.label,
            "summary": self.summary,
            "max_consecutive_agent_turns": self.max_consecutive_agent_turns,
            "requires_spec": self.requires_spec,
            "requires_deadline": self.requires_deadline,
            "supports_deadline": self.supports_deadline,
            "min_agents": self.min_agents,
            "default_roles": list(self.default_roles),
            "forced_denials": list(self.forced_denials),
        }

    def apply_forced_denials(self, permissions: PermissionSet) -> PermissionSet:
        """A mode may narrow the user's grant; it may never widen it."""
        for key in self.forced_denials:
            permissions.set(key, Decision.DENY)
        return permissions


class Role(str, Enum):
    PRIMARY_BUILDER = "primary_builder"
    ADVERSARIAL_REVIEWER = "adversarial_reviewer"
    SECONDARY_IMPLEMENTER = "secondary_implementer"
    VERIFIER = "verifier"
    AUDITOR = "auditor"
    PARTICIPANT = "participant"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


ROLE_LABELS: dict[str, str] = {
    Role.PRIMARY_BUILDER.value: "Primary Builder",
    Role.ADVERSARIAL_REVIEWER.value: "Adversarial Reviewer",
    Role.SECONDARY_IMPLEMENTER.value: "Secondary Implementer",
    Role.VERIFIER.value: "Verifier",
    Role.AUDITOR.value: "Auditor",
    Role.PARTICIPANT.value: "Participant",
}

#: Per-role instructions appended to the shared contract.  The core contract is
#: identical for everyone; only this differs, so both agents demonstrably agree
#: on the same terms.
ROLE_DOCTRINE: dict[str, str] = {
    Role.PRIMARY_BUILDER.value: """\
You are the Primary Builder and the conversation lead.

Your opening turn is mandatory. First inspect the specification and repository,
then send the room a compact opening build approach: what the repository does
now, the proposed implementation order, the first thing you will change, and
the important uncertainties or risks. This is a real working proposal, not a
request for the human to decide every detail. Then begin the first coherent unit
of implementation.

When you take a turn, the room starts a visible Live Work trail automatically.
Append a short public semantic update whenever the work meaningfully changes:
understanding the request, exploring the repository, implementing, testing, or
preparing a review. A work note is a plain-language status such as "Tracing the
startup path"; it is not private reasoning, a response, or a handoff. Your
completed room response clears the whole transient trail.

Keep the collaboration moving as a continuous build-review loop:
  1. Pick the highest-priority unmet acceptance gate or reviewer finding.
  2. Implement one coherent unit of work in the authorized worktree.
  3. Run the relevant tests and add targeted tests for changed behaviour.
  4. Send the reviewer the commit or diff, gate addressed, tests run, known
     concerns, and an explicit request for adversarial review.
  5. After review, address blocking findings and continue with the next unit.
Do not stop after a proposal, one implementation, or one reply. Do not mark a
gate PASS on your own assessment alone.

Human input has priority. When the human speaks, respond to that direction
first: revise the plan or work accordingly, report the consequential change,
then hand the result to the Adversarial Reviewer. The reviewer follows you on
human-directed work; do not make the human coordinate that sequence manually.""",
    Role.ADVERSARIAL_REVIEWER.value: """\
You are the Adversarial Reviewer. Your job is to falsify the builder's claims,
not to agree with them. The builder's opening approach is your first review:
challenge its assumptions, missing risks, implementation order, and test plan
before or alongside the first code change. Do not wait for a human prompt.

While the Primary Builder has the floor, stay silent. Synchri will give you the
builder's completed response automatically when it is your turn; do not emit
"waiting" updates or ask the human to relay it. Once you have the floor,
append a short public live update before inspecting the work and as your review
progresses. It is a high-level status, never private reasoning, a response, or
a handoff.

For every builder handoff, keep the continuous build-review loop moving:
  1. Inspect the repository state, proposed change, and relevant spec gates.
  2. Try to break the claim: look for regressions, missed requirements,
     architectural inconsistency, concurrency issues, unsafe assumptions,
     missing tests, and security implications.
  3. Run relevant tests yourself rather than trusting the report.
  4. Make corrective changes or tests directly when justified and authorized.
  5. Return concrete blocking and non-blocking findings, evidence, tests run,
     and the next action for the Primary Builder.
"Looks good to me" is not evidence. Cite tests, commits, or specific code.

When the human gives new direction, the Primary Builder responds first. Review
the builder's resulting plan or change next, then hand the work back. Do not
ask the human to relay between you and the builder.""",
    Role.SECONDARY_IMPLEMENTER.value: """\
You are the Secondary Implementer. Take work the builder hands you, implement it
to the same standard, run the relevant tests, and hand back with the same
reporting discipline. Do not duplicate work already in flight.""",
    Role.VERIFIER.value: """\
You are the Verifier. You do not implement. You independently confirm or refute
that each acceptance gate is satisfied, citing tests and commits as evidence.
Mark anything you cannot verify as UNVERIFIED rather than PASS.""",
    Role.AUDITOR.value: """\
You are the Auditor. Evaluate the target (repository, branch, commit, diff, or
pull request) against the stated criteria. Report findings with severity and
evidence. Do not modify code unless explicitly authorized and asked.""",
    Role.PARTICIPANT.value: """\
You are a participant in this room. Follow the shared contract, take your turns
when the queue gives them to you, and report with evidence.""",
}


_LONG_HORIZON_DOCTRINE = """\
This is a Long Horizon Development session. Work autonomously toward the
canonical product specification until every acceptance gate is satisfied or a
real escalation condition fires. The first turn belongs to the Primary Builder,
who must publish an opening build approach; the Adversarial Reviewer then
challenges it and reviews every subsequent implementation handoff. Continue
that build-review conversation until the work is actually complete.

Do NOT ask the user to continue merely because you finished a turn, published a
plan, or completed one review. Those are handoff points, not stop conditions.
Hand to the next participant and keep going.

When it is not your turn, remain silent. Synchri already delivers the completed
response that matters when the floor reaches you. Do not emit recurring
"waiting", "standing by", or progress messages while another participant works.

Completion requires evidence, not agreement. A gate is PASS only when there are
tests, commits, or inspectable artifacts backing it, and both the builder and the
reviewer have signed off. If you cannot verify a gate, mark it UNVERIFIED.

When the final evidence and both assessments are in the room, send one concise
completion-ready response to the human. Do not stop the room yourself or treat
an ordinary completed response as the end: the human performs Synchri's explicit
completion transition, which closes the room and preserves the final changelog.

If a timebox is present, use it as pacing guidance: explore early, implement and
review in the middle, and stabilise near its end. It is not a stop condition.
Finish early when the evidence supports completion; if more time is genuinely
needed, continue carefully and report the state rather than treating the clock
as a false completion or automatic failure."""

_INTERACTIVE_DOCTRINE = """\
This is an Interactive Collaboration session. The user is present and expected to
participate. Work with the other agents through the room, hand work off
explicitly, and give the user natural points to weigh in. Prefer smaller
increments and more frequent reporting than you would in an autonomous session."""

_REVIEW_DOCTRINE = """\
This is a Review / Audit session. Evaluate the target against the stated
criteria. You are assessing existing work, not building new features. Report
findings with severity, location, and evidence. Do not modify the repository
unless the permissions below explicitly authorize it and the user asked for
fixes."""


POLICIES: dict[SessionMode, ModePolicy] = {
    SessionMode.INTERACTIVE: ModePolicy(
        mode=SessionMode.INTERACTIVE,
        label="Interactive Collaboration",
        summary="You stay in the room. Agents hand work to each other; you weigh in when you want.",
        doctrine=_INTERACTIVE_DOCTRINE,
        max_consecutive_agent_turns=8,
        requires_spec=False,
        requires_deadline=False,
        min_agents=1,
        default_roles=(Role.PRIMARY_BUILDER.value, Role.ADVERSARIAL_REVIEWER.value),
    ),
    SessionMode.LONG_HORIZON: ModePolicy(
        mode=SessionMode.LONG_HORIZON,
        label="Long Horizon Development",
        summary=(
            "Agents build and review autonomously until the gates pass or something genuinely needs you."
        ),
        doctrine=_LONG_HORIZON_DOCTRINE,
        # High on purpose: the user must not be pinged just because a turn ended.
        max_consecutive_agent_turns=200,
        requires_spec=True,
        requires_deadline=False,
        min_agents=2,
        default_roles=(Role.PRIMARY_BUILDER.value, Role.ADVERSARIAL_REVIEWER.value),
    ),
    SessionMode.REVIEW_AUDIT: ModePolicy(
        mode=SessionMode.REVIEW_AUDIT,
        label="Review / Audit",
        summary="One or two agents evaluate existing work against criteria you set.",
        doctrine=_REVIEW_DOCTRINE,
        max_consecutive_agent_turns=24,
        requires_spec=True,
        requires_deadline=False,
        min_agents=1,
        default_roles=(Role.AUDITOR.value, Role.ADVERSARIAL_REVIEWER.value),
        # An audit does not ship code. Narrowing, never widening.
        forced_denials=("git.push", "gh.pr_merge", "sys.deploy", "sys.destructive"),
    ),
}


def policy_for(mode: str | SessionMode) -> ModePolicy:
    try:
        resolved = mode if isinstance(mode, SessionMode) else SessionMode(str(mode))
    except ValueError as exc:
        raise ValidationError(
            f"unknown mode {mode!r}; expected one of {[m.value for m in SessionMode]}"
        ) from exc
    return POLICIES[resolved]


def resolve_role(value: str | Role) -> str:
    try:
        return (value if isinstance(value, Role) else Role(str(value))).value
    except ValueError as exc:
        raise ValidationError(
            f"unknown role {value!r}; expected one of {[r.value for r in Role]}"
        ) from exc


def list_modes() -> list[dict]:
    return [POLICIES[mode].to_dict() for mode in SessionMode]


@dataclass
class ParticipantPlan:
    """One agent the user selected, before the room exists."""

    name: str
    runtime: str = "generic"
    role: str = Role.PARTICIPANT.value
    command: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "runtime": self.runtime,
            "role": self.role,
            "role_label": ROLE_LABELS.get(self.role, self.role),
            "command": self.command,
            "metadata": dict(self.metadata),
        }


#: Runtimes Synchri knows how to find on the local machine.  A managed command
#: is intentionally an opt-in convenience over the same CLI protocol used by
#: every other agent: there is no provider account, API key, or cloud relay.
#:
#: These commands are the providers' documented non-interactive modes.  They
#: do not widen provider permissions; a provider that needs sign-in or asks for
#: approval still reports that truthfully during Synchri's contract step.
KNOWN_RUNTIMES: dict[str, dict] = {
    "claude_code": {
        "label": "Claude Code",
        "executable": "claude",
        "managed_command": "claude -p {prompt}",
        "suggested_command": "claude -p {prompt}",
    },
    "codex": {
        "label": "Codex",
        "executable": "codex",
        "managed_command": "codex exec {prompt}",
        "suggested_command": "codex exec {prompt}",
    },
    "copilot": {
        "label": "GitHub Copilot CLI",
        "executable": "copilot",
        "managed_command": "copilot -sp {prompt}",
        "suggested_command": "copilot -sp {prompt}",
    },
    "gemini": {
        "label": "Gemini CLI",
        "executable": "gemini",
        # Kept in the chooser for external rooms. It does not appear behind
        # the reliable one-click path until its unattended lifecycle has the
        # same end-to-end coverage as the three maintained adapters above.
        "managed_command": None,
        "suggested_command": "gemini -p {prompt}",
    },
    "generic": {
        "label": "Other terminal-capable agent",
        "executable": None,
        "managed_command": None,
        "suggested_command": None,
    },
}


def runtime_status(runtime: str) -> dict:
    """Return an honest, cheap local readiness check for one runtime.

    Finding an executable proves only that Synchri can launch it.  Provider
    sign-in and an agent's agreement to a particular session are deliberately
    separate, visible steps; the UI must not turn either into a fake green
    checkmark.
    """
    definition = KNOWN_RUNTIMES.get(runtime, KNOWN_RUNTIMES["generic"])
    executable = definition.get("executable")
    path = shutil.which(executable) if executable else None
    installed = bool(path)
    managed = bool(path and definition.get("managed_command"))
    if managed:
        detail = "Installed on this Mac. Synchri can launch it after it agrees to the session."
    elif installed:
        detail = "Installed. Use the ready-to-paste prompt to connect this agent."
    elif executable:
        detail = f"{definition['label']} was not found on this Mac."
    else:
        detail = "Use a ready-to-paste prompt, or add a managed command in Advanced setup."
    return {
        "installed": installed,
        "managed": managed,
        "executable": executable,
        "path": path,
        "detail": detail,
    }


def runtime_catalog() -> list[dict]:
    """The UI's runtime chooser plus its real local availability."""
    return [{"key": key, **value, **runtime_status(key)} for key, value in KNOWN_RUNTIMES.items()]


def managed_command(plan: ParticipantPlan) -> str | None:
    """Resolve a user-supplied command before a maintained default."""
    if plan.command and plan.command.strip():
        return plan.command.strip()
    definition = KNOWN_RUNTIMES.get(plan.runtime, KNOWN_RUNTIMES["generic"])
    if runtime_status(plan.runtime)["managed"]:
        return definition.get("managed_command")
    return None


def plan_launch_status(plan: ParticipantPlan) -> dict:
    """Describe whether this exact participant can be launched by Synchri."""
    runtime = runtime_status(plan.runtime)
    command = managed_command(plan)
    if command:
        return {
            "mode": "managed",
            "ready": True,
            "command": command,
            "detail": "Synchri will attach this local agent, obtain its agreement, and start it.",
        }
    if runtime["installed"]:
        detail = "This tool is installed, but Synchri does not yet launch it unattended."
    else:
        detail = runtime["detail"]
    return {"mode": "external", "ready": False, "command": None, "detail": detail}
