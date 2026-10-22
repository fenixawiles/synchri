"""Research Mode failures.

Every failure carries a stable ``code`` (as the rest of Synchri does), and
the write-path failures are distinct types so callers can fail closed on
them explicitly rather than catching everything (LOG-03).
"""

from __future__ import annotations

from ..errors import SynchriError


class ResearchError(SynchriError):
    """Base class for Research Mode failures."""

    code = "research_error"
    exit_code = 10


class StoreWriteError(ResearchError):
    """An event or blob could not be durably written. The caller must halt."""

    code = "research_store_write_failed"
    exit_code = 11


class StoreIntegrityError(ResearchError):
    """The event log failed verification (broken chain, missing blob, ...)."""

    code = "research_store_integrity"
    exit_code = 12


class EventSchemaError(ResearchError):
    """An event is unknown to the catalog or misses a required field."""

    code = "research_event_schema"
    exit_code = 13


class CredentialError(ResearchError):
    """A provider credential is missing, unsafe to read, or misplaced."""

    code = "research_credential"
    exit_code = 14


class IsolationError(ResearchError):
    """A container specification would weaken isolation (mount, env, network)."""

    code = "research_isolation"
    exit_code = 15


class RelayError(ResearchError):
    """The recording relay cannot serve (halted, misconfigured, unreachable)."""

    code = "research_relay"
    exit_code = 16
