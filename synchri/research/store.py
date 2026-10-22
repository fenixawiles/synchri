"""The per-run, append-only, hash-chained event store (spec §6, D5).

One SQLite file per run, outside ``SYNCHRI_HOME``, with exactly two tables:
``research_events`` (the hash-chained log) and ``research_blobs``
(content-addressed exact bytes). Database triggers refuse UPDATE and DELETE on
both. The triggers stop accidents and honest writers; anyone with write access
to the file can still drop them, which is why the hash chain exists and
:meth:`RunStore.verify` recomputes it (tamper evidence, not tamper proofing).

Failure handling is explicit (LOG-03): any failed write raises
:class:`~.errors.StoreWriteError`, and the store marks itself *failed* so that
every later write also refuses. Nothing in this module swallows an exception.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..ids import new_secret, utc_now
from ..storage import db
from . import RESEARCH_SCHEMA_VERSION
from .canonical import ZERO_HASH, canonical_json, sha256_hex
from .errors import EventSchemaError, StoreIntegrityError, StoreWriteError
from .events import CATALOG

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS research_events (
    run_id         TEXT    NOT NULL,
    seq            INTEGER NOT NULL,
    event_id       TEXT    NOT NULL UNIQUE,
    event_type     TEXT    NOT NULL,
    schema_version INTEGER NOT NULL,
    created_at     TEXT    NOT NULL,
    monotonic_ns   INTEGER NOT NULL,
    actor          TEXT    NOT NULL,
    trial_id       TEXT,
    slot           TEXT,
    round          INTEGER,
    invocation_id  TEXT,
    attempt        INTEGER,
    payload        TEXT    NOT NULL,
    prev_hash      TEXT    NOT NULL,
    hash           TEXT    NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS research_blobs (
    sha256      TEXT    PRIMARY KEY,
    size        INTEGER NOT NULL,
    media_type  TEXT    NOT NULL,
    content     BLOB    NOT NULL,
    created_at  TEXT    NOT NULL
);
CREATE TRIGGER IF NOT EXISTS research_events_no_update BEFORE UPDATE ON research_events
    BEGIN SELECT RAISE(ABORT, 'research_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS research_events_no_delete BEFORE DELETE ON research_events
    BEGIN SELECT RAISE(ABORT, 'research_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS research_blobs_no_update BEFORE UPDATE ON research_blobs
    BEGIN SELECT RAISE(ABORT, 'research_blobs is append-only'); END;
CREATE TRIGGER IF NOT EXISTS research_blobs_no_delete BEFORE DELETE ON research_blobs
    BEGIN SELECT RAISE(ABORT, 'research_blobs is append-only'); END;
"""

REQUIRED_TRIGGERS = (
    "research_events_no_update",
    "research_events_no_delete",
    "research_blobs_no_update",
    "research_blobs_no_delete",
)

#: The columns that, with the payload, define an event's identity.
_ROW_COLUMNS = (
    "run_id", "seq", "event_id", "event_type", "schema_version", "created_at",
    "monotonic_ns", "actor", "trial_id", "slot", "round", "invocation_id",
    "attempt", "payload", "prev_hash",
)

ACTORS = ("orchestrator", "relay", "evaluator")


def event_hash(row: dict) -> str:
    """SHA-256 over the canonical serialization of every column but ``hash``."""
    material = {name: row[name] for name in _ROW_COLUMNS}
    material["payload"] = json.loads(row["payload"])
    return sha256_hex(canonical_json(material))


@dataclass
class VerifyReport:
    ok: bool
    events: int
    blobs_checked: int
    chain_head: str
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "events": self.events, "blobs_checked": self.blobs_checked,
            "chain_head": self.chain_head, "problems": list(self.problems),
        }


class RunStore:
    """One run's event log. Thread-safe within a process; safe across
    processes because every append runs inside ``BEGIN IMMEDIATE``."""

    FILENAME = "run.db"

    def __init__(self, path: Path, run_id: str, conn: sqlite3.Connection) -> None:
        self.path = path
        self.run_id = run_id
        self._conn = conn
        self._lock = threading.Lock()
        self.failed: str | None = None

    # -- lifecycle ------------------------------------------------------

    @classmethod
    def create(cls, run_dir: Path, run_id: str | None = None) -> "RunStore":
        run_dir = Path(run_dir)
        path = run_dir / cls.FILENAME
        if path.exists():
            raise StoreWriteError(f"a run store already exists at {path}", path=str(path))
        run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        conn = db.connect(path)
        conn.execute("PRAGMA synchronous=FULL")
        conn.executescript(SCHEMA_SQL)
        return cls(path, run_id or f"run_{new_secret(12)}", conn)

    @classmethod
    def open(cls, run_dir: Path) -> "RunStore":
        path = Path(run_dir) / cls.FILENAME
        if not path.exists():
            raise StoreIntegrityError(f"no run store at {path}", path=str(path))
        conn = db.connect(path)
        conn.execute("PRAGMA synchronous=FULL")
        row = conn.execute("SELECT run_id FROM research_events ORDER BY seq LIMIT 1").fetchone()
        run_id = row["run_id"] if row else None
        if run_id is None:
            raise StoreIntegrityError(f"run store at {path} holds no events", path=str(path))
        return cls(path, run_id, conn)

    def close(self) -> None:
        self._conn.close()

    # -- writes ---------------------------------------------------------

    def _refuse_if_failed(self) -> None:
        if self.failed is not None:
            raise StoreWriteError(
                f"the run store already failed and refuses further writes: {self.failed}",
                path=str(self.path),
            )

    def _fail(self, exc: BaseException, what: str) -> StoreWriteError:
        self.failed = f"{what}: {type(exc).__name__}: {exc}"
        return StoreWriteError(
            f"could not write {what} to the run store: {exc}", path=str(self.path)
        )

    def put_blob(self, data: bytes, media_type: str = "application/octet-stream") -> str:
        """Store exact bytes, content-addressed. Returns the SHA-256 key."""
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("blobs are bytes")
        data = bytes(data)
        digest = sha256_hex(data)
        with self._lock:
            self._refuse_if_failed()
            try:
                with db.transaction(self._conn):
                    self._conn.execute(
                        "INSERT OR IGNORE INTO research_blobs "
                        "(sha256, size, media_type, content, created_at) VALUES (?,?,?,?,?)",
                        (digest, len(data), media_type, data, utc_now()),
                    )
            except sqlite3.Error as exc:
                raise self._fail(exc, "a blob") from exc
        return digest

    def append(
        self,
        event_type: str,
        payload: dict,
        *,
        actor: str = "orchestrator",
        trial_id: str | None = None,
        slot: str | None = None,
        round: int | None = None,
        invocation_id: str | None = None,
        attempt: int | None = None,
    ) -> dict:
        """Validate against the catalog, chain, and append one event."""
        spec = CATALOG.get(event_type)
        if spec is None:
            raise EventSchemaError(f"unknown research event type {event_type!r}")
        if not isinstance(payload, dict):
            raise EventSchemaError(f"{event_type}: payload must be an object")
        missing = [name for name in spec.required if name not in payload]
        if missing:
            raise EventSchemaError(
                f"{event_type}: missing required payload field(s): {', '.join(missing)}",
                missing=missing,
            )
        if actor not in ACTORS and not actor.startswith("operator:"):
            raise EventSchemaError(f"unknown actor {actor!r}")
        try:
            payload_text = canonical_json(payload).decode("utf-8")
        except (TypeError, ValueError) as exc:
            raise EventSchemaError(f"{event_type}: payload is not canonical JSON: {exc}") from exc

        with self._lock:
            self._refuse_if_failed()
            try:
                with db.transaction(self._conn):
                    for name in spec.blobs:
                        ref = payload[name]
                        if ref is None:
                            continue
                        found = self._conn.execute(
                            "SELECT 1 FROM research_blobs WHERE sha256 = ?", (ref,)
                        ).fetchone()
                        if found is None:
                            raise EventSchemaError(
                                f"{event_type}: field {name!r} references a missing blob {ref}"
                            )
                    last = self._conn.execute(
                        "SELECT seq, hash FROM research_events WHERE run_id = ? "
                        "ORDER BY seq DESC LIMIT 1",
                        (self.run_id,),
                    ).fetchone()
                    row = {
                        "run_id": self.run_id,
                        "seq": (last["seq"] + 1) if last else 1,
                        "event_id": f"revt_{new_secret(12)}",
                        "event_type": event_type,
                        "schema_version": RESEARCH_SCHEMA_VERSION,
                        "created_at": utc_now(),
                        "monotonic_ns": time.monotonic_ns(),
                        "actor": actor,
                        "trial_id": trial_id,
                        "slot": slot,
                        "round": round,
                        "invocation_id": invocation_id,
                        "attempt": attempt,
                        "payload": payload_text,
                        "prev_hash": last["hash"] if last else ZERO_HASH,
                    }
                    row["hash"] = event_hash(row)
                    columns = (*_ROW_COLUMNS, "hash")
                    self._conn.execute(
                        f"INSERT INTO research_events ({', '.join(columns)}) "
                        f"VALUES ({', '.join('?' for _ in columns)})",
                        tuple(row[name] for name in columns),
                    )
            except EventSchemaError:
                raise
            except sqlite3.Error as exc:
                raise self._fail(exc, f"event {event_type}") from exc
        return row

    # -- reads ----------------------------------------------------------

    def events(self, event_type: str | None = None) -> list[dict]:
        sql = "SELECT * FROM research_events WHERE run_id = ?"
        args: list = [self.run_id]
        if event_type:
            sql += " AND event_type = ?"
            args.append(event_type)
        rows = self._conn.execute(sql + " ORDER BY seq", args).fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            entry["payload"] = json.loads(entry["payload"])
            out.append(entry)
        return out

    def blob(self, digest: str) -> bytes:
        row = self._conn.execute(
            "SELECT content FROM research_blobs WHERE sha256 = ?", (digest,)
        ).fetchone()
        if row is None:
            raise StoreIntegrityError(f"missing blob {digest}")
        content = bytes(row["content"])
        if sha256_hex(content) != digest:
            raise StoreIntegrityError(f"blob {digest} does not match its content hash")
        return content

    def chain_head(self) -> str:
        row = self._conn.execute(
            "SELECT hash FROM research_events WHERE run_id = ? ORDER BY seq DESC LIMIT 1",
            (self.run_id,),
        ).fetchone()
        return row["hash"] if row else ZERO_HASH

    # -- verification (§6.3 steps 1, 2, 8 for this phase) -----------------

    def verify(self) -> VerifyReport:
        problems: list[str] = []
        triggers = {
            row["name"]
            for row in self._conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
        for name in REQUIRED_TRIGGERS:
            if name not in triggers:
                problems.append(f"append-only trigger missing: {name}")

        rows = [dict(r) for r in self._conn.execute(
            "SELECT * FROM research_events WHERE run_id = ? ORDER BY seq", (self.run_id,)
        )]
        other_runs = self._conn.execute(
            "SELECT COUNT(*) AS n FROM research_events WHERE run_id != ?", (self.run_id,)
        ).fetchone()["n"]
        if other_runs:
            problems.append(f"{other_runs} event(s) belong to a different run_id")

        prev = ZERO_HASH
        referenced: set[str] = set()
        for expected_seq, row in enumerate(rows, start=1):
            label = f"seq {row['seq']} ({row['event_type']})"
            if row["seq"] != expected_seq:
                problems.append(f"{label}: sequence gap, expected seq {expected_seq}")
            if row["prev_hash"] != prev:
                problems.append(f"{label}: prev_hash does not link to the previous event")
            try:
                recomputed = event_hash(row)
            except (ValueError, TypeError) as exc:
                problems.append(f"{label}: payload is not valid JSON ({exc})")
                prev = row["hash"]
                continue
            if recomputed != row["hash"]:
                problems.append(f"{label}: hash mismatch (row content was altered)")
            spec = CATALOG.get(row["event_type"])
            payload = json.loads(row["payload"])
            if spec is None:
                problems.append(f"{label}: unknown event type")
            else:
                for name in spec.required:
                    if name not in payload:
                        problems.append(f"{label}: missing required field {name}")
                for name in spec.blobs:
                    if payload.get(name):
                        referenced.add(payload[name])
            prev = row["hash"]

        checked = 0
        for digest in sorted(referenced):
            row = self._conn.execute(
                "SELECT content FROM research_blobs WHERE sha256 = ?", (digest,)
            ).fetchone()
            checked += 1
            if row is None:
                problems.append(f"referenced blob missing: {digest}")
            elif sha256_hex(bytes(row["content"])) != digest:
                problems.append(f"blob content does not match its key: {digest}")
        return VerifyReport(
            ok=not problems, events=len(rows), blobs_checked=checked,
            chain_head=prev, problems=problems,
        )
