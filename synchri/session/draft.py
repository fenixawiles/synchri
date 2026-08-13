"""The startup wizard, as a headless state machine.

Deliberately contains no printing and no prompting. It exposes steps, the
choices each step needs, per-step validation, and free movement backwards and
forwards. A terminal wizard and a desktop GUI can both drive it, and neither
owns the rules.

Progressive disclosure is a property of the data: each step declares only the
fields it needs, and ``visible_steps`` hides the ones the chosen mode does not
use (no timebox step for a review, for example).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import ValidationError
from . import worktree as worktree_module
from .deadline import Deadline
from .escalation import EscalationPolicy
from .modes import (
    KNOWN_RUNTIMES,
    ParticipantPlan,
    Role,
    SessionMode,
    list_modes,
    policy_for,
    resolve_role,
)
from .permissions import PermissionSet
from .spec import ProductSpec

STEPS = (
    ("repository", "Choose the repository"),
    ("agents", "Choose agents and roles"),
    ("permissions", "Decide what agents may do"),
    ("spec", "Describe the work"),
    ("deadline", "Set a timebox (optional)"),
    ("review", "Review the session"),
)


@dataclass
class SessionDraft:
    """Everything the user has chosen so far. Nothing here touches disk."""

    # Long Horizon Development is the whole new-session product.  Historic
    # modes remain loadable through ``set_mode`` for old rooms, but nobody new
    # should have to make a mode decision before describing their work.
    mode: str | None = SessionMode.LONG_HORIZON.value
    repo_path: str | None = None
    base_branch: str | None = None
    worktree_name: str | None = None
    worktree_parent: str | None = None
    participants: list[ParticipantPlan] = field(default_factory=list)
    permissions: PermissionSet = field(default_factory=PermissionSet.defaults)
    spec: ProductSpec | None = None
    deadline: Deadline | None = None
    #: Reusable pacing preference.  Unlike a resolved Deadline, this belongs
    #: in a preset and begins only when collaboration actually begins.
    deadline_duration: str | None = None
    escalation: EscalationPolicy = field(default_factory=EscalationPolicy)
    name: str | None = None
    _repo_status: worktree_module.RepoStatus | None = field(default=None, repr=False)

    # -- steps ---------------------------------------------------------

    @property
    def policy(self):
        return policy_for(self.mode) if self.mode else None

    def visible_steps(self) -> list[tuple[str, str]]:
        """Only the steps this mode actually needs."""
        steps = []
        for key, label in STEPS:
            if key == "deadline" and self.policy and not self.policy.supports_deadline:
                continue
            if key == "spec" and self.policy and not self.policy.requires_spec:
                # Still offered, just relabelled: an optional brief is useful.
                steps.append((key, "Describe the work (optional)"))
                continue
            if key == "deadline" and self.policy and not self.policy.requires_deadline:
                steps.append((key, "Set a timebox (optional)"))
                continue
            steps.append((key, label))
        return steps

    def options(self, step: str) -> dict:
        """What a UI needs to render one step. No side effects except repo reads."""
        if step == "mode":
            return {"modes": list_modes()}
        if step == "repository":
            return {"detected": self.repo_status().to_dict() if self.repo_path else None}
        if step == "worktree":
            status = self.repo_status() if self.repo_path else None
            return {
                "base_branch": self.base_branch or (status.branch if status else None),
                "branches": status.branches if status else [],
                "preview_name": self.worktree_name or worktree_module.generate_name(self.mode),
                "parent": str(
                    worktree_module.default_worktree_parent(status.root)
                    if status
                    else self.worktree_parent or ""
                ),
                "explanation": (
                    "Synchri creates an isolated Git worktree so participating agents do not "
                    "modify your primary working directory."
                ),
            }
        if step == "agents":
            return {
                "runtimes": [{"key": k, **v} for k, v in KNOWN_RUNTIMES.items()],
                "roles": [{"key": r.value, "label": r.value.replace("_", " ").title()} for r in Role],
                "default_roles": list(self.policy.default_roles) if self.policy else [],
                "selected": [p.to_dict() for p in self.participants],
            }
        if step == "permissions":
            return {"groups": self.permissions.grouped()}
        if step == "spec":
            return {
                "required": bool(self.policy and self.policy.requires_spec),
                "loaded": self.spec.summary() if self.spec else None,
            }
        if step == "deadline":
            return {
                "required": bool(self.policy and self.policy.requires_deadline),
                "resolved": self.deadline.to_dict() if self.deadline else None,
            }
        if step == "review":
            return self.summary()
        raise ValidationError(f"unknown step {step!r}")

    # -- setters, each validating its own step --------------------------

    def set_mode(self, mode: str) -> "SessionDraft":
        policy = policy_for(mode)
        self.mode = policy.mode.value
        # Adopt the mode's default roles for any agents already chosen.
        for index, plan in enumerate(self.participants):
            if index < len(policy.default_roles):
                plan.role = policy.default_roles[index]
        policy.apply_forced_denials(self.permissions)
        if not policy.supports_deadline:
            self.deadline = None
        return self

    def set_repository(self, path: str, base_branch: str | None = None) -> "SessionDraft":
        status = worktree_module.inspect_repository(path)
        if not status.is_valid:
            raise ValidationError("; ".join(status.problems))
        self._repo_status = status
        self.repo_path = status.root
        self.base_branch = base_branch or status.branch
        if self.base_branch and self.base_branch not in status.branches:
            raise ValidationError(
                f"branch {self.base_branch!r} is not in this repository "
                f"(found: {', '.join(status.branches[:8]) or 'none'})"
            )
        if not self.name:
            self.name = f"{status.name} session"
        return self

    def repo_status(self) -> worktree_module.RepoStatus:
        if self._repo_status is None:
            if not self.repo_path:
                raise ValidationError("choose a repository first")
            self._repo_status = worktree_module.inspect_repository(self.repo_path)
        return self._repo_status

    def set_worktree(self, name: str | None = None, parent: str | None = None) -> "SessionDraft":
        self.worktree_name = name
        self.worktree_parent = parent
        return self

    def set_agents(self, participants: list[ParticipantPlan]) -> "SessionDraft":
        names = [p.name for p in participants]
        if len(set(names)) != len(names):
            raise ValidationError("participant names must be unique")
        for plan in participants:
            plan.role = resolve_role(plan.role)
            if plan.runtime not in KNOWN_RUNTIMES:
                raise ValidationError(
                    f"unknown runtime {plan.runtime!r}; expected one of {sorted(KNOWN_RUNTIMES)}"
                )
        self.participants = participants
        return self

    def set_permission(self, key: str, decision: str) -> "SessionDraft":
        self.permissions.set(key, decision)
        if self.policy:
            self.policy.apply_forced_denials(self.permissions)
        return self

    def set_spec(self, text: str, structured: dict | None = None) -> "SessionDraft":
        self.spec = ProductSpec(text=text, structured=structured or {})
        return self

    def set_deadline_duration(self, text: str) -> "SessionDraft":
        self.deadline = Deadline.from_duration(text)
        self.deadline_duration = text.strip()
        return self

    def set_deadline_at(self, timestamp: str) -> "SessionDraft":
        self.deadline = Deadline.from_timestamp(timestamp)
        self.deadline_duration = None
        return self

    def set_escalation(self, policy: EscalationPolicy) -> "SessionDraft":
        self.escalation = policy
        return self

    # -- validation ----------------------------------------------------

    def validate_step(self, step: str) -> list[str]:
        """Problems blocking this step, in plain language. Empty means ready."""
        problems: list[str] = []
        if step == "mode" and not self.mode:
            problems.append("choose how this session should run")
        if step == "repository":
            if not self.repo_path:
                problems.append("choose a repository")
            else:
                status = self.repo_status()
                problems.extend(status.problems)
                if not self.base_branch:
                    problems.append("choose a base branch")
        if step == "agents":
            minimum = self.policy.min_agents if self.policy else 1
            if len(self.participants) < minimum:
                problems.append(
                    f"select at least {minimum} agent{'s' if minimum != 1 else ''}"
                )
        if step == "spec" and self.policy and self.policy.requires_spec and not self.spec:
            problems.append(f"{self.policy.label} needs a product specification")
        if step == "deadline" and self.policy and self.policy.requires_deadline and not self.deadline:
            problems.append(f"{self.policy.label} needs a deadline")
        return problems

    def blocking_problems(self) -> list[str]:
        """Everything preventing 'Start session'."""
        problems: list[str] = []
        for key, _ in self.visible_steps():
            if key == "review":
                continue
            problems.extend(self.validate_step(key))
        return problems

    @property
    def is_ready(self) -> bool:
        return not self.blocking_problems()

    # -- review --------------------------------------------------------

    def summary(self) -> dict:
        """The final review screen, as data."""
        status = self._repo_status
        warnings: list[str] = []
        if status and status.is_dirty:
            warnings.append(
                f"Your primary working tree has {len(status.dirty_files)} uncommitted change(s). "
                "Synchri will not touch them — agents work in the isolated worktree."
            )
        if self.permissions.allows("git.push"):
            warnings.append("Agents may push branches to the remote.")
        if self.permissions.allows("gh.pr_merge"):
            warnings.append("Agents may merge pull requests.")
        if self.permissions.allows("sys.deploy"):
            warnings.append("Agents may deploy.")

        return {
            "mode": self.policy.label if self.policy else None,
            "mode_summary": self.policy.summary if self.policy else None,
            "repository": {
                "name": status.name if status else None,
                "path": status.root if status else self.repo_path,
                "remote": status.remote if status else None,
                "branch": self.base_branch,
                "dirty": bool(status and status.is_dirty),
            },
            "worktree": {
                "name": self.worktree_name or "(generated at start)",
                "parent": self.worktree_parent
                or (str(worktree_module.default_worktree_parent(status.root)) if status else None),
                "explanation": (
                    "Synchri creates an isolated Git worktree so participating agents do not "
                    "modify your primary working directory."
                ),
            },
            "agents": [p.to_dict() for p in self.participants],
            "permissions": self.permissions.grouped(),
            "permissions_granted": self.permissions.granted_keys(),
            "permissions_denied": self.permissions.denied_keys(),
            "spec": self.spec.summary() if self.spec else "Not provided",
            "deadline": self.deadline.describe() if self.deadline else "None",
            "escalation": self.escalation.rules(),
            "warnings": warnings,
            "problems": self.blocking_problems(),
            "ready": self.is_ready,
        }

    # -- persistence ---------------------------------------------------

    def to_state(self) -> dict:
        """The complete draft, including the parts a preset deliberately omits.

        A preset is a reusable template; this is one specific unfinished wizard,
        so it keeps the spec, the deadline, and the repository too.
        """
        return {
            **self.to_preset(),
            "repo_path": self.repo_path,
            "base_branch": self.base_branch,
            "worktree_name": self.worktree_name,
            "worktree_parent": self.worktree_parent,
            "spec": self.spec.to_dict() if self.spec else None,
            # A duration draft has no meaningful absolute timestamp until the
            # session begins. Persist only its text preference so reopening a
            # setup screen never starts a hidden countdown.
            "deadline": (
                self.deadline.to_dict()
                if self.deadline and not self.deadline_duration
                else None
            ),
            "deadline_duration": self.deadline_duration,
            "name": self.name,
        }

    @classmethod
    def from_state(cls, state: dict) -> "SessionDraft":
        """Rebuild an unfinished wizard.

        Tolerant on purpose: a repository that has since been deleted should
        not stop the wizard from opening. An elapsed timebox is preserved: it
        is historical pacing information, not an invalid configuration.
        """
        draft = cls.from_preset(state or {})
        if state.get("repo_path"):
            try:
                draft.set_repository(state["repo_path"], state.get("base_branch"))
            except ValidationError:
                draft.repo_path = None
        draft.worktree_name = state.get("worktree_name")
        draft.worktree_parent = state.get("worktree_parent")
        draft.name = state.get("name")
        if state.get("spec"):
            draft.spec = ProductSpec.from_dict(state["spec"])
        if state.get("deadline_duration"):
            draft.set_deadline_duration(state["deadline_duration"])
        elif state.get("deadline"):
            restored = Deadline.from_dict(state["deadline"])
            draft.deadline = restored
        return draft

    def to_preset(self) -> dict:
        """Reusable configuration only. No spec, worktree, or session ids.

        A duration is a preference rather than an absolute timestamp, so it is
        safe—and useful—to reuse. The actual clock starts on activation.
        """
        return {
            "mode": self.mode,
            "agents": [
                {"name": p.name, "runtime": p.runtime, "role": p.role, "command": p.command}
                for p in self.participants
            ],
            "permissions": self.permissions.to_dict(),
            "escalation": self.escalation.to_dict(),
            "deadline_duration": self.deadline_duration,
        }

    @classmethod
    def from_preset(cls, preset: dict) -> "SessionDraft":
        draft = cls()
        if preset.get("mode"):
            draft.set_mode(preset["mode"])
        draft.permissions = PermissionSet.from_dict(preset.get("permissions"))
        draft.escalation = EscalationPolicy.from_dict(preset.get("escalation"))
        if preset.get("deadline_duration"):
            draft.set_deadline_duration(preset["deadline_duration"])
        draft.participants = [
            ParticipantPlan(
                name=a["name"],
                runtime=a.get("runtime", "generic"),
                role=resolve_role(a.get("role", Role.PARTICIPANT.value)),
                command=a.get("command"),
            )
            for a in preset.get("agents") or []
        ]
        return draft
