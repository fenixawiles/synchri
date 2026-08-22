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
    # Keep the historical values readable so an existing room is never made
    # unreadable by an app upgrade.  New sessions expose Long Horizon
    # Development and Planning; the rest remain for persisted rooms.
    INTERACTIVE = "interactive"
    LONG_HORIZON = "long_horizon"
    REVIEW_AUDIT = "review_audit"
    PLANNING = "planning"

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
    #: Planning Mode: the session runs in a disposable read-only planning
    #: workspace instead of a repository worktree, and produces an approved
    #: plan rather than code.
    planning: bool = False

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
            "planning": self.planning,
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
    PLANNER = "planner"
    PLAN_REVIEWER = "plan_reviewer"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


ROLE_LABELS: dict[str, str] = {
    Role.PRIMARY_BUILDER.value: "Primary Builder",
    Role.ADVERSARIAL_REVIEWER.value: "Adversarial Reviewer",
    Role.SECONDARY_IMPLEMENTER.value: "Secondary Implementer",
    Role.VERIFIER.value: "Verifier",
    Role.AUDITOR.value: "Auditor",
    Role.PARTICIPANT.value: "Participant",
    Role.PLANNER.value: "Planner / Architect",
    Role.PLAN_REVIEWER.value: "Adversarial Plan Reviewer",
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
    Role.PLANNER.value: """\
You are the Planner / Architect. You have no implementation authority in this
session: you inspect the repository copy in the planning workspace, and you
produce an ordered implementation plan — never code changes meant to ship.

Your first pass is yours alone. Turn the human's idea articulation plus your
own repository inspection into PLAN-DRAFT revision 1 before the reviewer sees
anything: inspect the current implementation, dependencies, constraints,
acceptance criteria, migration risks, likely failure modes, and testing and
preservation requirements. You are launched under your CLI's read-only
enforcement with no sanctioned write path; the plan travels through your
reply: put the complete
plan document between a `SYNCHRI-PLAN-BEGIN` line and a `SYNCHRI-PLAN-END`
line, then end the reply with `SYNCHRI-PLAN-SUBMIT: <one-line summary>` to
record the revision.

The plan document must contain an `## Acceptance criteria` section whose
bullets each carry an explicit gate id, e.g. `- AUTH-01: login works`. Those
ids become the coordination session's gates verbatim; a plan without them
cannot be promoted.

When the reviewer raises objections, address each one: revise the plan,
record each disposition with `SYNCHRI-OBJECTION-RESOLVED: <id>|<what changed
or why the decision stands>`, and submit the next revision the same way. Defend a decision when
the evidence supports it; revise when the reviewer has found a real problem.
When multiple approaches remain genuinely defensible, record the fork
explicitly with `SYNCHRI-FORK: <the choice and both defensible sides>` — the
human resolves forks, and an open fork prevents PLAN-READY.""",
    Role.PLAN_REVIEWER.value: """\
You are the Adversarial Plan Reviewer. You have no implementation authority in
this session, and you do not restate the plan — you independently review its
implementation logic against the repository copy in the planning workspace.

Wait for the planner's first submitted draft; it is never your job to write
the plan. For each submitted revision, read its text (delivered in your
prompt's planning state) against the repository state it claims to describe,
then challenge: ordering, hidden dependencies,
unnecessary reconstruction, missing rollback and migration concerns,
insufficient tests, conflicting assumptions, unsafe sequencing, and places
where existing behavior should be preserved instead of changed. Inspect
enough repository state to judge executability — a review that never opened
the code is ceremony.

Record every finding as its own control line:
  `SYNCHRI-OBJECTION: blocking|<a problem that must be fixed before approval>`
  `SYNCHRI-OBJECTION: nonblocking|<an assumption that may stand, clearly stated>`
  `SYNCHRI-OBJECTION: advisory|<worth noting; the planner may defer it>`
Close every review with `SYNCHRI-PLAN-REVIEW: approve|<why the plan is now
sufficient>` or `SYNCHRI-PLAN-REVIEW: revise|<what must change>`. Approval is
review closure, not politeness: approve only when no blocking objection or
fork remains open and the plan is executable as ordered. Do not manufacture
objections to seem thorough — when remaining concerns are redundant or
low-value, say so and approve.""",
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

Record gate progress as it happens. At the end of a completed turn, use Synchri
control lines for every gate you touched: `SYNCHRI-GATE: ID|status|assessment`,
followed by `SYNCHRI-EVIDENCE`, `SYNCHRI-TEST`, and `SYNCHRI-COMMIT` lines for
that gate. These update the durable progress view; a claim in ordinary prose
does not. The Primary Builder may add `SYNCHRI-COMPLETE` only after all required
gates have evidence and both assessments. Synchri verifies that claim, closes
the room, and writes the final changelog; it never completes on prose alone.

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

_PLANNING_DOCTRINE = """\
This is a Planning Mode session. Planning should be executable state, not
prose the user has to carry into the next workflow: the deliverable is an
adversarially reviewed implementation plan the human can approve, and
approval staffs and starts the coordination that executes it.

Neither of you has implementation authority here, and this is enforced, not
requested: you are launched under your CLI's own read-only enforcement, with
the working directory set to a disposable planning copy of the repository —
anchored to one inspected commit, holding no remotes, never reused for
coordination. Inspect it freely; you have no sanctioned write path and must
not attempt one. The plan itself travels through your replies via the
SYNCHRI-PLAN-BEGIN/END protocol. Synchri verifies the workspace's Git state
after every turn.

The loop is PLAN-DRAFT -> ADVERSARIAL REVIEW -> REVISION -> RE-REVIEW ->
PLAN-READY. The planner drafts first, alone; only then does the reviewer
receive it. PLAN-READY requires adversarial-review closure, not planner
completion: open blocking objections or unresolved architectural forks
prevent it, while nonblocking assumptions may remain when clearly classified.
The loop has budgets — agent turns, plan revisions, and wall-clock time —
and exhausting them hands the decision to the human with the latest draft
and open objections preserved. Do not loop for the sake of thoroughness:
stop at the minimum sufficiently defensible plan."""


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
    SessionMode.PLANNING: ModePolicy(
        mode=SessionMode.PLANNING,
        label="Planning",
        summary=(
            "Articulate the idea; a planner drafts the implementation plan and an "
            "adversarial reviewer attacks it until it is ready to approve."
        ),
        doctrine=_PLANNING_DOCTRINE,
        # The planning turn budget: exhaustion goes to the human, never a loop.
        max_consecutive_agent_turns=24,
        requires_spec=False,
        requires_deadline=False,
        supports_deadline=False,
        min_agents=2,
        default_roles=(Role.PLANNER.value, Role.PLAN_REVIEWER.value),
        # Planning ships nothing. The workspace provides the real isolation;
        # the contract narrows the same way, never wider.
        forced_denials=(
            "git.push", "git.force_push", "gh.pr_create", "gh.pr_update",
            "gh.pr_merge", "gh.pr_close", "sys.ci_config", "sys.infra_config",
            "sys.deploy", "sys.destructive",
        ),
        planning=True,
    ),
}


# ``POLICIES`` remains a compatibility registry for rooms made by early builds.
# This deliberately smaller list is the new-session product surface used by the
# UI and CLI.  Do not turn a persisted interactive/audit room into an error just
# because it no longer appears in the starter flow.
NEW_SESSION_MODES: tuple[SessionMode, ...] = (SessionMode.LONG_HORIZON, SessionMode.PLANNING)


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
    return [POLICIES[mode].to_dict() for mode in NEW_SESSION_MODES]


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


def collaboration_pair(
    participants: list[ParticipantPlan],
) -> tuple[ParticipantPlan | None, ParticipantPlan | None]:
    """Return the conversation lead and its adversarial reviewer, if present.

    This makes the intended ordering explicit rather than depending on the
    order in which the user happened to add agents to a session.  Development
    sessions lead with the Primary Builder; audit sessions lead with the
    Auditor.  The reviewer is the designated second voice for either shape.
    """
    lead = next(
        (
            plan
            for role in (Role.PRIMARY_BUILDER.value, Role.AUDITOR.value, Role.PLANNER.value)
            for plan in participants
            if plan.role == role
        ),
        participants[0] if participants else None,
    )
    reviewer = next(
        (
            plan
            for plan in participants
            if plan.role in (Role.ADVERSARIAL_REVIEWER.value, Role.PLAN_REVIEWER.value)
            and plan.name != getattr(lead, "name", None)
        ),
        None,
    )
    return lead, reviewer


#: Runtimes Synchri knows how to find on the local machine.  A managed command
#: is intentionally an opt-in convenience over the same CLI protocol used by
#: every other agent. Synchri owns no provider account, stores no provider
#: credential or API key, and uses no cloud relay; authentication remains
#: inside each provider CLI.
#:
#: These commands are the providers' documented non-interactive modes.  They
#: do not widen provider permissions; a provider that needs sign-in or asks for
#: approval still reports that truthfully during Synchri's contract step.
#:
#: The doctor metadata (``min_version``, ``auth_indicators``,
#: ``resume_command``) feeds the two-tier connection doctor in
#: ``runner/doctor.py``: ``auth_indicators`` are cached sign-in *indications*
#: only (an absent file is unknown, never "signed out" — several CLIs keep
#: auth in a keychain), and ``resume_command`` is defined only where the
#: maintained adapter actually knows the CLI's resume invocation.
KNOWN_RUNTIMES: dict[str, dict] = {
    "claude_code": {
        "label": "Claude Code",
        "default_name": "Claude",
        "executable": "claude",
        # The maintained managed command asks for the CLI's machine-readable
        # stream so the UI can show live work; the plain variant is the
        # fallback for installed CLI versions that predate those flags.
        "managed_command": "claude -p --verbose --output-format stream-json {prompt}",
        "plain_managed_command": "claude -p {prompt}",
        "suggested_command": "claude -p {prompt}",
        "stream_format": "claude",
        "min_version": (1, 0, 0),
        "auth_indicators": ["~/.claude/.credentials.json", "~/.claude/credentials.json"],
        "resume_command": "claude -p --verbose --output-format stream-json --resume {resume_id} {prompt}",
        # Planning launches under the CLI's own enforcement, hardened:
        # permission-mode plan denies edits, writes, and mutating commands;
        # --safe-mode disables the user-configurable surfaces that could
        # widen it (hooks, plugins, custom commands — managed-policy hooks
        # remain active by design); --strict-mcp-config with no --mcp-config
        # loads no MCP servers at all; and --tools pins the built-in tool set
        # to read-only inspection. This is provider enforcement (the CLI's
        # permission engine plus its managed policy), not an OS sandbox — the
        # honest limit of what this adapter can guarantee.
        "planning_command": (
            "claude -p --verbose --output-format stream-json --permission-mode plan "
            '--safe-mode --strict-mcp-config --tools "Read,Glob,Grep" {prompt}'
        ),
        "plain_planning_command": (
            'claude -p --permission-mode plan --safe-mode --strict-mcp-config '
            '--tools "Read,Glob,Grep" {prompt}'
        ),
        # The connection-test canary answers a sentinel prompt and needs no
        # tools at all: --tools "" disables every built-in tool on top of the
        # same hardening, so the canary technically cannot read or write.
        "connection_test_command": (
            "claude -p --verbose --output-format stream-json --permission-mode plan "
            '--safe-mode --strict-mcp-config --tools "" {prompt}'
        ),
        "plain_connection_test_command": (
            'claude -p --permission-mode plan --safe-mode --strict-mcp-config '
            '--tools "" {prompt}'
        ),
        "connection_test_resume_command": (
            "claude -p --verbose --output-format stream-json --permission-mode plan "
            '--safe-mode --strict-mcp-config --tools "" --resume {resume_id} {prompt}'
        ),
        "read_only_planning_workspace": True,
    },
    "codex": {
        "label": "Codex",
        "default_name": "Codex",
        "executable": "codex",
        "managed_command": "codex exec --json {prompt}",
        "plain_managed_command": "codex exec {prompt}",
        "suggested_command": "codex exec {prompt}",
        "stream_format": "codex",
        "min_version": (0, 20, 0),
        "auth_indicators": ["~/.codex/auth.json"],
        "resume_command": None,
        # Codex's sandbox is OS-enforced (Seatbelt / Landlock): read-only
        # filesystem, no network — the strongest confinement available here.
        # The connection-test canary runs under the same sandbox.
        "planning_command": "codex exec --json --sandbox read-only {prompt}",
        "plain_planning_command": "codex exec --sandbox read-only {prompt}",
        "connection_test_command": "codex exec --json --sandbox read-only {prompt}",
        "plain_connection_test_command": "codex exec --sandbox read-only {prompt}",
        "read_only_planning_workspace": True,
    },
    "copilot": {
        "label": "GitHub Copilot CLI",
        "default_name": "Copilot",
        "executable": "copilot",
        "managed_command": "copilot -sp {prompt}",
        "suggested_command": "copilot -sp {prompt}",
        "min_version": (0, 1, 0),
        "auth_indicators": [
            "~/.config/github-copilot/hosts.json",
            "~/.config/github-copilot/apps.json",
        ],
        "resume_command": None,
        # No verified read-only enforcement flag in the maintained adapter
        # yet: Planning Mode and the consented connection test are both
        # unavailable on Copilot rather than run without a technical sandbox.
    },
    "gemini": {
        "label": "Gemini CLI",
        "default_name": "Gemini",
        "executable": "gemini",
        # Kept in the chooser for external rooms. It does not appear behind
        # the reliable one-click path until its unattended lifecycle has the
        # same end-to-end coverage as the three maintained adapters above.
        "managed_command": None,
        "suggested_command": "gemini -p {prompt}",
    },
    "generic": {
        "label": "Other terminal-capable agent",
        "default_name": "Agent",
        "executable": None,
        "managed_command": None,
        "suggested_command": None,
    },
}


def default_agent_name(runtime: str, taken: set[str] | None = None) -> str:
    """Runtime-derived participant name, deduplicated with a hyphen suffix.

    A participant name is identity — credential file paths, the room roster,
    directive routing — so the derived value must satisfy ``ids.NAME_PATTERN``
    (no spaces; hence "Codex-2", never "Codex 2").
    """
    definition = KNOWN_RUNTIMES.get(runtime, KNOWN_RUNTIMES["generic"])
    base = definition.get("default_name") or "Agent"
    used = taken or set()
    if base not in used:
        return base
    counter = 2
    while f"{base}-{counter}" in used:
        counter += 1
    return f"{base}-{counter}"


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
        detail = "Installed. Synchri can launch it after it agrees to the session."
    elif installed:
        detail = "Installed. Use the ready-to-paste prompt to connect this agent."
    elif executable:
        detail = f"{definition['label']} was not detected."
    else:
        detail = "Use a ready-to-paste prompt, or add a managed command in Advanced setup."
    return {
        "installed": installed,
        "managed": managed,
        "executable": executable,
        "path": path,
        "detail": detail,
        # Capability-based, never contract-only: Planning Mode is offered only
        # on runtimes whose maintained adapter confines writes to the
        # disposable planning workspace. Others are shown unavailable.
        "planning_supported": planning_workspace_supported(runtime),
    }


def planning_workspace_supported(runtime: str) -> bool:
    """Whether this runtime's adapter declares the planning-isolation capability."""
    definition = KNOWN_RUNTIMES.get(runtime, KNOWN_RUNTIMES["generic"])
    return bool(definition.get("read_only_planning_workspace"))


def runtime_catalog() -> list[dict]:
    """The UI's runtime chooser plus its real local availability."""
    return [{"key": key, **value, **runtime_status(key)} for key, value in KNOWN_RUNTIMES.items()]


def managed_command(plan: ParticipantPlan, *, plain: bool = False) -> str | None:
    """Resolve a user-supplied command before a maintained default.

    ``plain`` asks for the non-streaming maintained command — the fallback
    when an installed CLI rejects its streaming flags.
    """
    if plan.command and plan.command.strip():
        return plan.command.strip()
    definition = KNOWN_RUNTIMES.get(plan.runtime, KNOWN_RUNTIMES["generic"])
    if runtime_status(plan.runtime)["managed"]:
        if plain:
            return definition.get("plain_managed_command") or definition.get("managed_command")
        return definition.get("managed_command")
    return None


def planning_command(plan: ParticipantPlan, *, plain: bool = False) -> str | None:
    """The maintained planning invocation, carrying the CLI's own enforcement.

    Deliberately never a user-supplied command: a custom command carries no
    verified read-only guarantee, and Planning Mode never downgrades to
    contract-only isolation. ``None`` means this runtime cannot plan.
    """
    definition = KNOWN_RUNTIMES.get(plan.runtime, KNOWN_RUNTIMES["generic"])
    if not definition.get("read_only_planning_workspace"):
        return None
    if not runtime_status(plan.runtime)["installed"]:
        return None
    if plain:
        return definition.get("plain_planning_command") or definition.get("planning_command")
    return definition.get("planning_command")


def stream_format_for(plan: ParticipantPlan) -> str | None:
    """The maintained stream format — only when the maintained command runs.

    A user-supplied command keeps plain-stdout behavior: Synchri cannot know
    what such a command prints, so it never pretends to parse it.
    """
    if plan.command and plan.command.strip():
        return None
    definition = KNOWN_RUNTIMES.get(plan.runtime, KNOWN_RUNTIMES["generic"])
    return definition.get("stream_format")


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
