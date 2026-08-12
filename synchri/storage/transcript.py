"""Durable transcript mirror (layer 3 of 3).

The ``messages`` table is authoritative.  This module additionally appends each
message to ``rooms/<room_id>/transcript.jsonl`` so the conversation survives as
a greppable, tool-agnostic artifact that does not require SQLite to read.

The mirror is written after the transaction commits, so a crash between commit
and append can leave the file one line short.  ``rebuild`` regenerates it from
the database, which is why the database and not the file is the source of truth.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..config import Workspace, append_private, write_private
from ..models.envelope import MessageEnvelope
from . import dao


def append(workspace: Workspace, envelope: MessageEnvelope) -> None:
    """Best-effort append of one message to the room's JSONL transcript."""
    workspace.ensure_room_dir(envelope.room_id)
    append_private(
        workspace.transcript_path(envelope.room_id),
        json.dumps(envelope.to_dict(), ensure_ascii=False, sort_keys=True),
    )


def rebuild(workspace: Workspace, conn: sqlite3.Connection, room_id: str) -> Path:
    """Regenerate the JSONL transcript for a room from authoritative state."""
    workspace.ensure_room_dir(room_id)
    lines = [
        json.dumps(m.to_dict(), ensure_ascii=False, sort_keys=True)
        for m in dao.list_messages(conn, room_id)
    ]
    path = workspace.transcript_path(room_id)
    write_private(path, "\n".join(lines) + ("\n" if lines else ""))
    return path
