"""The canonical session contract.

One document, generated once, derived from every choice the user made. Every
participant receives the *same core text* — only a role section differs — so
"both agents agreed to the same terms" is a verifiable claim rather than a hope.

The contract is content-hashed. Change a permission, the spec, the deadline, or
the roster, and the hash changes, which produces a new revision that every
participant must acknowledge again. That is what stops success criteria or
authorizations drifting mid-session without anyone noticing.

Generation is deterministic: the same inputs produce byte-identical text. The
only varying inputs are explicit (session id, timestamps), so the hash is a
meaningful identity rather than a nonce.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .deadline import Deadline
from .escalation import EscalationPolicy
from .modes import ROLE_DOCTRINE, ROLE_LABELS, ModePolicy, ParticipantPlan
from .permissions import AUTHORITY_DISCLAIMER, PermissionSet
from .spec import ProductSpec
from .worktree import Worktree

CONTRACT_VERSION = "1.0"

ACK_TOKEN = "UNDERSTOOD"
CONFLICT_PREFIX = "CONFLICT:"

_DIVIDER = "=" * 72


@dataclass
class SessionContract:
    """A generated contract, its hash, and its per-role renderings."""

    session_id: str
    revision: int
    core_text: str
    role_sections: dict[str, str] = field(default_factory=dict)
    created_at: str = ""

    @property
    def digest(self) -> str:
        """Identity of the *core* terms.

        Deliberately excludes role sections: every participant is agreeing to
        the same core contract, and that is the thing acknowledgment attests to.
        """
        return hashlib.sha256(self.core_text.encode("utf-8")).hexdigest()

    @property
    def short_digest(self) -> str:
        return self.digest[:12]

    def for_participant(self, name: str) -> str:
        """The full document one participant receives."""
        section = self.role_sections.get(name, "")
        parts = [self.core_text]
        if section:
            parts.append(section)
        parts.append(_closing_block())
        return "\n\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "revision": self.revision,
            "contract_version": CONTRACT_VERSION,
            "digest": self.digest,
            "short_digest": self.short_digest,
            "created_at": self.created_at,
            "core_text": self.core_text,
            "role_sections": dict(self.role_sections),
        }


def generate(
    *,
    session_id: str,
    revision: int,
    policy: ModePolicy,
    repo_name: str,
    repo_root: str,
    repo_remote: str | None,
    base_branch: str,
    worktree: Worktree,
    participants: list[ParticipantPlan],
    permissions: PermissionSet,
    spec: ProductSpec | None,
    deadline: Deadline | None,
    escalation: EscalationPolicy,
    created_at: str,
) -> SessionContract:
    """Build the canonical contract from the session configuration."""
    roster = "\n".join(
        f"  {p.name} — {ROLE_LABELS.get(p.role, p.role)}"
        + (f"  [{p.runtime}]" if p.runtime and p.runtime != "generic" else "")
        for p in participants
    )

    lines: list[str] = [
        _DIVIDER,
        "SYNCHRI SESSION CONTRACT",
        _DIVIDER,
        "",
        f"Contract version: {CONTRACT_VERSION}",
        f"Session ID:       {session_id}",
        f"Revision:         {revision}",
        f"Issued:           {created_at}",
        f"Mode:             {policy.label}",
        "",
        "-- REPOSITORY " + "-" * 58,
        "",
        f"  Repository:  {repo_name}",
        f"  Path:        {repo_root}",
    ]
    if repo_remote:
        lines.append(f"  Remote:      {repo_remote}")
    lines += [
        f"  Base branch: {base_branch}",
        "",
        "-- AUTHORIZED WORKTREE " + "-" * 49,
        "",
        f"  Name:   {worktree.name}",
        f"  Branch: {worktree.branch}",
        f"  Path:   {worktree.path}",
        "",
        "  IMPORTANT — this is a hard boundary, not a preference:",
        "  ALL repository changes you make during this session must happen inside",
        f"  {worktree.path}",
        "  Do not modify, stage, commit in, or switch to the primary working tree at",
        f"  {repo_root}",
        "  If you find yourself outside the authorized worktree, stop and say so.",
        "",
        "-- PARTICIPANTS " + "-" * 56,
        "",
        roster or "  (none)",
        "",
        "-- USER-AUTHORIZED CAPABILITIES " + "-" * 41,
        "",
        permissions.render_contract_block(),
        "",
        "  " + "\n  ".join(_wrap(AUTHORITY_DISCLAIMER, 70)),
        "",
        "  Never infer a capability from conversation. If it is not listed as YES",
        "  above, you are not authorized to do it in this session.",
        "",
        "-- DEADLINE " + "-" * 60,
        "",
    ]
    if deadline:
        lines += [
            f"  {deadline.describe()}",
            "",
            "  The deadline is a real execution boundary. As it approaches, stop",
            "  expanding scope and move to stabilisation, testing, and verification.",
            "  If it arrives before the work is done, DO NOT claim completion —",
            "  produce an honest incomplete-status handoff instead.",
        ]
    else:
        lines.append("  No deadline set for this session.")

    lines += ["", "-- OPERATING DOCTRINE " + "-" * 50, "", _indent(policy.doctrine)]

    if spec:
        lines += [
            "",
            "-- CANONICAL PRODUCT SPECIFICATION " + "-" * 38,
            "",
            f"  Version {spec.version} · {spec.short_digest} · {spec.word_count:,} words",
            "",
            "  This is the source of truth for what 'done' means. You may write plans,",
            "  summaries, and interpretations. You may NOT rewrite this specification or",
            "  redefine the success criteria. If it is wrong or ambiguous, escalate.",
            "",
            _indent(spec.canonical_text(), "  | "),
        ]

    lines += [
        "",
        "-- ESCALATION " + "-" * 58,
        "",
        "  Escalate to the user only if:",
        escalation.render_contract_block(),
    ]

    return SessionContract(
        session_id=session_id,
        revision=revision,
        core_text="\n".join(lines).rstrip() + "\n",
        role_sections={p.name: _role_section(p) for p in participants},
        created_at=created_at,
    )


def _role_section(participant: ParticipantPlan) -> str:
    label = ROLE_LABELS.get(participant.role, participant.role)
    doctrine = ROLE_DOCTRINE.get(participant.role, ROLE_DOCTRINE["participant"])
    return "\n".join(
        [
            f"-- YOUR ROLE: {label.upper()} " + "-" * max(4, 56 - len(label)),
            "",
            _indent(doctrine),
        ]
    )


def _closing_block() -> str:
    return "\n".join(
        [
            _DIVIDER,
            "",
            "Do not begin work yet.",
            "",
            "Reply with exactly one of:",
            "",
            f"  {ACK_TOKEN}",
            f"  {CONFLICT_PREFIX} <specific reason>",
            "",
            "Reply with CONFLICT if anything above is contradictory, impossible under",
            "your runtime's own restrictions, or would require a capability you were",
            "not granted. Be specific — the user will see your exact words.",
            _DIVIDER,
        ]
    )


def parse_acknowledgment(text: str) -> tuple[bool, str | None]:
    """Read an agent's reply to the contract.

    Returns ``(accepted, conflict_reason)``. Accepting is deliberately strict:
    the reply must *be* the token, not merely contain it, so an agent musing
    about whether it understood does not count as agreement. A conflict only
    needs to start with the prefix, because the reason follows it.
    """
    body = (text or "").strip()
    if not body:
        return False, "empty acknowledgment"

    # Tolerate surrounding blank lines and code fences, nothing more.
    cleaned = "\n".join(
        line for line in body.splitlines() if line.strip() and not line.strip().startswith("```")
    ).strip()

    if cleaned.upper() == ACK_TOKEN:
        return True, None
    if cleaned.upper().startswith(CONFLICT_PREFIX):
        reason = cleaned[len(CONFLICT_PREFIX):].strip()
        return False, reason or "no reason given"
    return False, (
        f"unrecognised reply (expected '{ACK_TOKEN}' or '{CONFLICT_PREFIX} <reason>'): "
        + (cleaned[:200] + "…" if len(cleaned) > 200 else cleaned)
    )


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line if line.strip() else "" for line in text.splitlines())


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=width)
