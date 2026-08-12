"""AIDapter — a local interoperability layer for coding agents.

Agents that can run shell commands can join a shared, user-controlled room and
address each other directly, instead of the human relaying messages by hand.
"""

from .broker import Broker, Credential
from .config import Workspace, resolve_workspace
from .errors import (
    AidapterError,
    AuthError,
    ConflictError,
    NotFoundError,
    StateError,
    TurnError,
    ValidationError,
)
from .models import MessageDraft, MessageEnvelope

__version__ = "0.1.0"

__all__ = [
    "AidapterError",
    "AuthError",
    "Broker",
    "ConflictError",
    "Credential",
    "MessageDraft",
    "MessageEnvelope",
    "NotFoundError",
    "StateError",
    "TurnError",
    "ValidationError",
    "Workspace",
    "__version__",
    "resolve_workspace",
]
