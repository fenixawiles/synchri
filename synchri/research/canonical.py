"""Canonical JSON and hashing — the byte-level identity of every record."""

from __future__ import annotations

import hashlib
import json


def canonical_json(value) -> bytes:
    """Sorted keys, no insignificant whitespace, UTF-8, no NaN (spec §6.1)."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


ZERO_HASH = "0" * 64
