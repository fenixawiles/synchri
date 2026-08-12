"""Secret handling for room join tokens and participant keys.

Threat model for v0.1 is deliberately modest and stated plainly in
``docs/security.md``: the adversary is a *confused or misbehaving local agent*,
not another OS user.  What we defend is room scoping — an agent that holds a
credential for room A must not be able to act in room B, and a removed
participant must not be able to keep acting anywhere.

Secrets are 256-bit random values, so a single salted SHA-256 is sufficient;
there is no low-entropy password to protect against offline brute force, and a
slow KDF would add latency to every single CLI call.  This is the one place
that reasoning matters, so it is written down rather than assumed.
"""

from __future__ import annotations

import hashlib
import hmac

from ..errors import ValidationError
from ..ids import is_valid_id, new_secret

TOKEN_SEPARATOR = "."


def hash_secret(secret: str, salt: str) -> str:
    """Salted SHA-256 of a high-entropy secret, hex encoded."""
    return hashlib.sha256(f"{salt}{TOKEN_SEPARATOR}{secret}".encode("utf-8")).hexdigest()


def new_salt() -> str:
    return new_secret(16)


def verify_secret(secret: str, salt: str, expected_hash: str) -> bool:
    """Constant-time secret comparison."""
    if not isinstance(secret, str) or not secret:
        return False
    return hmac.compare_digest(hash_secret(secret, salt), expected_hash)


def compose_join_token(room_id: str, secret: str) -> str:
    """Build the shareable join token.

    The token embeds the room id so ``aidapter join <token>`` needs no other
    argument.  Identifiers use the urlsafe-base64 alphabet, which excludes
    ``.``, so the split is unambiguous.
    """
    return f"{room_id}{TOKEN_SEPARATOR}{secret}"


def split_join_token(token: str) -> tuple[str | None, str]:
    """Split a compound token into ``(room_id, secret)``.

    A bare secret (no embedded room id) yields ``(None, secret)``; the caller
    must then know which room it is talking about.
    """
    if not isinstance(token, str) or not token.strip():
        raise ValidationError("join token must be a non-empty string")
    token = token.strip()
    room_id, separator, secret = token.partition(TOKEN_SEPARATOR)
    if separator and is_valid_id("room", room_id) and secret:
        return room_id, secret
    return None, token


def room_id_from_reference(reference: str) -> str | None:
    """Extract a room id from either a bare room id or a compound join token."""
    if is_valid_id("room", reference):
        return reference
    room_id, _ = split_join_token(reference)
    return room_id
