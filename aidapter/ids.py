"""Identifier generation and validation.

All identifiers are unguessable by construction: a short type prefix plus 128
bits of ``secrets`` entropy.  Nothing in AIDapter uses a sequential or
timestamp-derived identifier as a secret.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

# urlsafe base64 alphabet, minus padding.  Deliberately excludes "." so that a
# compound join token ("<room_id>.<secret>") can be split unambiguously.
_ID_BODY = r"[A-Za-z0-9_-]{16,64}"

ID_PATTERNS = {
    "room": re.compile(rf"^room_{_ID_BODY}$"),
    "part": re.compile(rf"^part_{_ID_BODY}$"),
    "msg": re.compile(rf"^msg_{_ID_BODY}$"),
    "turn": re.compile(rf"^turn_{_ID_BODY}$"),
    "task": re.compile(rf"^task_{_ID_BODY}$"),
    "evt": re.compile(rf"^evt_{_ID_BODY}$"),
    "invite": re.compile(rf"^invite_{_ID_BODY}$"),
}

#: Participant names are used in transcripts and CLI flags, never in paths.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def new_id(prefix: str, entropy_bytes: int = 12) -> str:
    """Return a new prefixed identifier with at least 96 bits of entropy."""
    if prefix not in ID_PATTERNS:
        raise ValueError(f"unknown id prefix: {prefix}")
    return f"{prefix}_{secrets.token_urlsafe(entropy_bytes)}"


def is_valid_id(prefix: str, value: object) -> bool:
    pattern = ID_PATTERNS.get(prefix)
    if pattern is None or not isinstance(value, str):
        return False
    return bool(pattern.match(value))


def new_secret(entropy_bytes: int = 32) -> str:
    """Return a 256-bit secret suitable for a room join token or participant key."""
    return secrets.token_urlsafe(entropy_bytes)


def utc_now() -> str:
    """Timestamps are ISO-8601, UTC, millisecond precision, always suffixed 'Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
