"""Error taxonomy for Synchri.

Every failure an agent can provoke carries a stable machine-readable ``code``
so that a shell-driven agent can branch on it without parsing prose.
"""

from __future__ import annotations


class SynchriError(Exception):
    """Base class for all Synchri failures."""

    code = "error"
    exit_code = 1

    def __init__(self, message: str, code: str | None = None, **details: object) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details

    def to_dict(self) -> dict:
        return {"ok": False, "error": {"code": self.code, "message": self.message, **self.details}}


class ValidationError(SynchriError):
    """The caller supplied something malformed."""

    code = "validation_error"
    exit_code = 2


class AuthError(SynchriError):
    """Bad or missing credentials, or a participant that may no longer act."""

    code = "auth_error"
    exit_code = 3


class NotFoundError(SynchriError):
    """Room, participant, or message does not exist (or is not visible here)."""

    code = "not_found"
    exit_code = 4


class ConflictError(SynchriError):
    """The request collides with existing state (e.g. duplicate participant)."""

    code = "conflict"
    exit_code = 5


class StateError(SynchriError):
    """The room is not in a state that permits this operation."""

    code = "invalid_state"
    exit_code = 6


class TurnError(StateError):
    """The caller does not hold the floor."""

    code = "not_your_turn"
    exit_code = 7


# Kept for library consumers upgrading their import paths.  New code should
# catch SynchriError, which is the public name shown in the documentation.
AidapterError = SynchriError
