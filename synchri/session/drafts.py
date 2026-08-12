"""Persisted wizard drafts.

An unfinished wizard survives closing the app, and two browser tabs pointed at
the same draft id see the same state. Drafts hold no authority whatsoever:
nothing is created on disk outside this table until "Start session".

Each write bumps a version, which is how the UI knows another tab changed
something. Concurrent edits are last-write-wins -- correct enough for one
person with two tabs, and the version means the other tab notices rather than
silently diverging.
"""

from __future__ import annotations

import json
import re
import sqlite3

from ..errors import ValidationError
from ..ids import utc_now
from ..storage import db

DRAFT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_DRAFTS = 50


def _validate(draft_id: str) -> str:
    if not DRAFT_ID.match(draft_id or ""):
        raise ValidationError(f"invalid draft id: {draft_id!r}")
    return draft_id


def save(conn: sqlite3.Connection, draft_id: str, payload: dict) -> int:
    """Store a draft and return its new version."""
    _validate(draft_id)
    now = utc_now()
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO ui_drafts (draft_id, payload, version, created_at, updated_at) "
            "VALUES (?, ?, 1, ?, ?) "
            "ON CONFLICT(draft_id) DO UPDATE SET payload=excluded.payload, "
            "version=ui_drafts.version + 1, updated_at=excluded.updated_at",
            (draft_id, json.dumps(payload, default=str), now, now),
        )
        # Keep the table from growing without bound if drafts are abandoned.
        conn.execute(
            "DELETE FROM ui_drafts WHERE draft_id NOT IN "
            "(SELECT draft_id FROM ui_drafts ORDER BY updated_at DESC LIMIT ?)",
            (MAX_DRAFTS,),
        )
        row = conn.execute(
            "SELECT version FROM ui_drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
    return int(row["version"]) if row else 1


def load(conn: sqlite3.Connection, draft_id: str) -> tuple[dict, int] | None:
    _validate(draft_id)
    row = conn.execute(
        "SELECT payload, version FROM ui_drafts WHERE draft_id = ?", (draft_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["payload"]), int(row["version"])
    except json.JSONDecodeError:  # pragma: no cover - corrupt row
        return None


def delete(conn: sqlite3.Connection, draft_id: str) -> None:
    _validate(draft_id)
    with db.transaction(conn):
        conn.execute("DELETE FROM ui_drafts WHERE draft_id = ?", (draft_id,))


def versions(conn: sqlite3.Connection) -> dict[str, int]:
    """Every draft's version, for cheap change detection."""
    return {
        row["draft_id"]: int(row["version"])
        for row in conn.execute("SELECT draft_id, version FROM ui_drafts")
    }
