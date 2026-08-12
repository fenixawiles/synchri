"""Session modes and their immutable operating policy.

A mode is not a label: it decides how much autonomy agents get, whether a
deadline and a specification are required, and what doctrine goes into the
contract.  Each mode carries its own policy so that adding a mode later is a
new entry in ``POLICIES``, not a redesign of startup.
"""

from __future__ import annotations

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
You are the Primary Builder. Each cycle:
  1. Read the canonical product specification and the current repository state.
  2. Pick the highest-priority unmet acceptance gate.
  3. Implement one coherent unit of work toward it.
  4. Run all relevant existing tests.
  5. Add targeted tests wherever you changed behaviour.
  6. Commit a checkpoint (if committing is authorized).
  7. Hand to the reviewer with: the commit or diff, the gate you addressed, a
     summary of changes, the tests you ran, your known concerns, and an explicit
     request for adversarial review.
Do not mark a gate PASS on your own assessment alone.""",
    Role.ADVERSARIAL_REVIEWER.value: """\
You are the Adversarial Reviewer. Your job is to falsify the builder's claims,
not to agree with them. Each cycle:
  1. Inspect the current repository state and the checkpoint you were handed.
  2. Evaluate it against the canonical product specification.
  3. Actively try to break the completion claim.
  4. Look for: regressions, missed requirements, architectural inconsistency,
     concurrency bugs, unsafe assumptions, missing tests, security implications.
  5. Run the relevant tests yourself rather than trusting the report.
  6. Add or fix tests where coverage is missing.
  7. Make corrective changes directly when justified and authorized.
  8. Return: changes made, blocking findings, non-blocking findings, tests run,
     gates still unmet, and hand back to the builder.
"Looks good to me" is not evidence. Cite tests, commits, or specific code.""",
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
This is a Long Horizon Development session. You are expected to work
autonomously toward the canonical product specification until every acceptance
gate is satisfied, the deadline arrives, or an escalation condition fires.

Do NOT ask the user to continue merely because you finished a turn. Finishing a
turn is not an escalation condition. Hand to the next participant and keep going.

Completion requires evidence, not agreement. A gate is PASS only when there are
tests, commits, or inspectable artifacts backing it, and both the builder and the
reviewer have signed off. If you cannot verify a gate, mark it UNVERIFIED.

Behaviour should change as the deadline approaches: architecture and exploration
early, implementation and review in the middle, stabilisation and verification
late. Near the deadline, stop expanding scope, freeze non-essential work, run the
relevant suites, and reconcile open findings.

If the deadline arrives before the work is done, do not claim completion. Produce
an honest incomplete-status handoff."""

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
            "Agents build autonomously against a specification until the gates pass, "
            "the deadline hits, or something needs you."
        ),
        doctrine=_LONG_HORIZON_DOCTRINE,
        # High on purpose: the user must not be pinged just because a turn ended.
        max_consecutive_agent_turns=200,
        requires_spec=True,
        requires_deadline=True,
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


#: Runtimes the wizard offers.  These are labels and default command templates,
#: not integrations: Synchri still talks to every agent through the CLI.
KNOWN_RUNTIMES: dict[str, dict] = {
    "claude_code": {"label": "Claude Code", "suggested_command": "claude -p {prompt}"},
    "codex": {"label": "Codex", "suggested_command": "codex exec {prompt}"},
    "copilot": {"label": "GitHub Copilot CLI", "suggested_command": None},
    "gemini": {"label": "Gemini CLI", "suggested_command": "gemini -p {prompt}"},
    "generic": {"label": "Other terminal-capable agent", "suggested_command": None},
}
