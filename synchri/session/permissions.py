"""The user's session-level authorization.

Two things make this module worth reading carefully.

**Permissions are declarative, never inferred.** An agent does not acquire a
capability because the conversation implied it, because it argued for it, or
because it already had it in a previous session.  ``PermissionSet.check`` is the
only place a capability is decided, and it reads stored state.

**Synchri's grant is a ceiling, not an override.** A capability marked ALLOW
here means *the user authorizes it for this session*.  It does not and cannot
override the agent provider's own restrictions, the operating system, terminal
sandboxing, git credentials, repository protections, or host policy.  Where the
two disagree, the stricter one wins — and where Synchri says DENY, the session
contract forbids the action even if the agent could technically perform it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..errors import ValidationError


class Decision(str, Enum):
    """What the user authorized for a capability."""

    ALLOW = "allow"
    ASK = "ask"      # permitted, but the agent must escalate before doing it
    DENY = "deny"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class Risk(str, Enum):
    """How much damage getting this wrong does. Drives UI labelling."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class Capability:
    """One thing an agent may or may not be authorized to do."""

    key: str
    group: str
    label: str
    description: str
    risk: Risk
    default: Decision

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "group": self.group,
            "label": self.label,
            "description": self.description,
            "risk": self.risk.value,
            "default": self.default.value,
        }


def _cap(key, group, label, description, risk, default) -> Capability:
    return Capability(key, group, label, description, risk, default)


#: The full catalogue, in display order.  Defaults are deliberately conservative:
#: everything that writes outside the worktree, or that cannot be undone, starts
#: off.
CATALOG: tuple[Capability, ...] = (
    # -- repository ----------------------------------------------------
    _cap("repo.read", "Repository", "Read the repository",
         "Open and read any file in the authorized worktree.",
         Risk.LOW, Decision.ALLOW),
    _cap("repo.edit", "Repository", "Edit files",
         "Modify existing files in the authorized worktree.",
         Risk.MEDIUM, Decision.ALLOW),
    _cap("repo.create_delete", "Repository", "Create and delete files",
         "Add new files, or remove files, inside the authorized worktree.",
         Risk.MEDIUM, Decision.ALLOW),
    _cap("repo.test", "Repository", "Run tests",
         "Execute the project's test suites.",
         Risk.LOW, Decision.ALLOW),
    _cap("repo.build", "Repository", "Run build commands",
         "Execute the project's build.",
         Risk.LOW, Decision.ALLOW),
    _cap("repo.lint", "Repository", "Run lint and type checks",
         "Execute linters, formatters in check mode, and type checkers.",
         Risk.LOW, Decision.ALLOW),
    _cap("repo.install_deps", "Repository", "Install dependencies",
         "Add or install packages. Reaches the network and mutates the environment.",
         Risk.HIGH, Decision.ASK),
    # -- git -----------------------------------------------------------
    _cap("git.commit", "Git", "Create commits",
         "Commit work inside the authorized worktree.",
         Risk.LOW, Decision.ALLOW),
    _cap("git.amend", "Git", "Amend commits",
         "Rewrite the most recent commit.",
         Risk.MEDIUM, Decision.DENY),
    _cap("git.branch", "Git", "Create branches",
         "Create new branches from the session branch.",
         Risk.LOW, Decision.ALLOW),
    _cap("git.push", "Git", "Push branches",
         "Publish commits to the remote.",
         Risk.HIGH, Decision.DENY),
    _cap("git.force_push", "Git", "Force push",
         "Overwrite remote history. Can destroy other people's work.",
         Risk.DESTRUCTIVE, Decision.DENY),
    _cap("git.rebase", "Git", "Rebase",
         "Rewrite local history.",
         Risk.HIGH, Decision.DENY),
    _cap("git.reset", "Git", "Reset",
         "Move refs and potentially discard committed work.",
         Risk.DESTRUCTIVE, Decision.DENY),
    _cap("git.delete_branch", "Git", "Delete branches",
         "Remove branches locally or on the remote.",
         Risk.DESTRUCTIVE, Decision.DENY),
    # -- host ----------------------------------------------------------
    _cap("gh.pr_create", "GitHub", "Create pull requests",
         "Open a pull request against the remote repository.",
         Risk.HIGH, Decision.DENY),
    _cap("gh.pr_update", "GitHub", "Update pull requests",
         "Edit the title, body, or state of an existing pull request.",
         Risk.MEDIUM, Decision.DENY),
    _cap("gh.pr_review", "GitHub", "Review pull requests",
         "Read and evaluate a pull request. Read-only analysis.",
         Risk.LOW, Decision.ALLOW),
    _cap("gh.pr_comment", "GitHub", "Comment on pull requests",
         "Post comments visible to other people.",
         Risk.MEDIUM, Decision.DENY),
    _cap("gh.pr_merge", "GitHub", "Merge pull requests",
         "Merge a pull request into its base branch.",
         Risk.DESTRUCTIVE, Decision.DENY),
    _cap("gh.pr_close", "GitHub", "Close pull requests",
         "Close a pull request without merging.",
         Risk.MEDIUM, Decision.DENY),
    # -- system --------------------------------------------------------
    _cap("sys.ci_config", "System", "Modify CI configuration",
         "Change workflow files that run on the project's infrastructure.",
         Risk.HIGH, Decision.DENY),
    _cap("sys.infra_config", "System", "Modify infrastructure config",
         "Change deployment, container, or infrastructure-as-code files.",
         Risk.HIGH, Decision.DENY),
    _cap("sys.deploy", "System", "Deploy",
         "Ship code to a running environment.",
         Risk.DESTRUCTIVE, Decision.DENY),
    _cap("sys.destructive", "System", "Execute destructive commands",
         "Anything irreversible outside git: deleting data, dropping tables, rm -rf.",
         Risk.DESTRUCTIVE, Decision.DENY),
)

BY_KEY: dict[str, Capability] = {c.key: c for c in CATALOG}
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(c.group for c in CATALOG))

#: The sentence that appears in every contract, verbatim.  Synchri must never
#: imply that its grant can widen what another system allows.
AUTHORITY_DISCLAIMER = (
    "These permissions are the user's session-level authorization only. They do "
    "not override restrictions imposed by your runtime, your provider, the "
    "operating system, the repository host, available credentials, branch "
    "protections, or any other applicable policy. Where those are stricter, they "
    "win. Where Synchri says NO, this session forbids the action even if you are "
    "technically able to perform it."
)


@dataclass
class PermissionSet:
    """A user's decisions for one session."""

    decisions: dict[str, str] = field(default_factory=dict)

    @classmethod
    def defaults(cls) -> "PermissionSet":
        return cls({c.key: c.default.value for c in CATALOG})

    @classmethod
    def from_dict(cls, data: dict | None) -> "PermissionSet":
        """Load stored decisions, filling gaps from the catalogue defaults.

        Unknown keys are dropped rather than honoured: a stale preset must not
        smuggle in a capability this version does not know how to gate.
        """
        merged = {c.key: c.default.value for c in CATALOG}
        for key, value in (data or {}).items():
            if key not in BY_KEY:
                continue
            merged[key] = Decision(value).value if not isinstance(value, Decision) else value.value
        return cls(merged)

    def set(self, key: str, decision: str | Decision) -> "PermissionSet":
        if key not in BY_KEY:
            raise ValidationError(f"unknown capability {key!r}")
        try:
            value = Decision(decision.value if isinstance(decision, Decision) else str(decision))
        except ValueError as exc:
            raise ValidationError(
                f"decision must be one of allow/ask/deny, got {decision!r}"
            ) from exc
        self.decisions[key] = value.value
        return self

    def decision(self, key: str) -> Decision:
        if key not in BY_KEY:
            raise ValidationError(f"unknown capability {key!r}")
        return Decision(self.decisions.get(key, BY_KEY[key].default.value))

    def allows(self, key: str) -> bool:
        """True only for an outright ALLOW; ASK is not a grant."""
        return self.decision(key) is Decision.ALLOW

    def requires_escalation(self, key: str) -> bool:
        return self.decision(key) is Decision.ASK

    def granted_keys(self) -> list[str]:
        return [c.key for c in CATALOG if self.allows(c.key)]

    def denied_keys(self) -> list[str]:
        return [c.key for c in CATALOG if self.decision(c.key) is Decision.DENY]

    def to_dict(self) -> dict:
        return dict(self.decisions)

    def grouped(self) -> list[dict]:
        """Catalogue plus current decisions, shaped for a UI to render directly."""
        out = []
        for group in GROUPS:
            out.append(
                {
                    "group": group,
                    "capabilities": [
                        {**c.to_dict(), "decision": self.decision(c.key).value}
                        for c in CATALOG
                        if c.group == group
                    ],
                }
            )
        return out

    def render_contract_block(self) -> str:
        """The permissions section as it appears in the session contract."""
        width = max(len(c.label) for c in CATALOG)
        lines = []
        for group in GROUPS:
            lines.append(f"  {group}:")
            for capability in (c for c in CATALOG if c.group == group):
                decision = self.decision(capability.key)
                mark = {
                    Decision.ALLOW: "YES",
                    Decision.ASK: "ASK FIRST",
                    Decision.DENY: "NO",
                }[decision]
                lines.append(f"    {capability.label:<{width}}  {mark}")
        return "\n".join(lines)


class PermissionDenied(Exception):
    """Raised when a capability check fails. Carries why, for the transcript."""

    def __init__(self, key: str, decision: Decision) -> None:
        capability = BY_KEY.get(key)
        label = capability.label if capability else key
        if decision is Decision.ASK:
            message = (
                f"'{label}' requires the user's approval in this session; "
                "escalate rather than proceeding"
            )
        else:
            message = f"'{label}' is not authorized in this session"
        super().__init__(message)
        self.key = key
        self.decision = decision
        self.message = message
