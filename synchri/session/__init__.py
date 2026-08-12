"""Session startup: modes, worktrees, permissions, contracts, gates.

The session layer sits above the room. A room owns messaging, the turn queue and
shared memory; a session owns the operating contract those messages happen
under -- which repository, which isolated worktree, which agents in which roles,
what they are authorized to do, what "done" means, and by when.
"""

from .contract import ACK_TOKEN, CONFLICT_PREFIX, SessionContract, parse_acknowledgment
from .deadline import Deadline
from .draft import STEPS, SessionDraft
from .escalation import EscalationPolicy
from .gates import Gate, GateStatus, summarize
from .manager import SessionManager, SessionRecord, SessionStatus
from .modes import KNOWN_RUNTIMES, ParticipantPlan, Role, SessionMode, list_modes, policy_for
from .permissions import CATALOG, Decision, PermissionDenied, PermissionSet
from .spec import ProductSpec
from .worktree import RepoStatus, Worktree, inspect_repository

__all__ = [
    "ACK_TOKEN",
    "CATALOG",
    "CONFLICT_PREFIX",
    "Deadline",
    "Decision",
    "EscalationPolicy",
    "Gate",
    "GateStatus",
    "KNOWN_RUNTIMES",
    "ParticipantPlan",
    "PermissionDenied",
    "PermissionSet",
    "ProductSpec",
    "RepoStatus",
    "Role",
    "STEPS",
    "SessionContract",
    "SessionDraft",
    "SessionManager",
    "SessionMode",
    "SessionRecord",
    "SessionStatus",
    "Worktree",
    "inspect_repository",
    "list_modes",
    "parse_acknowledgment",
    "policy_for",
    "summarize",
]
