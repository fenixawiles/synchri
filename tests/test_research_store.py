"""Research Mode event store: append-only, hash-chained, fail-closed (LOG-01, LOG-03).

Spec VT-09 (append-only, chain, tamper detection, required fields) and VT-12
(injected write failures halt instead of dropping).
"""

from __future__ import annotations

import sqlite3

import pytest

from synchri.research.canonical import ZERO_HASH, sha256_hex
from synchri.research.errors import EventSchemaError, StoreIntegrityError, StoreWriteError
from synchri.research.store import RunStore


def _store(tmp_path):
    return RunStore.create(tmp_path / "run")


def _halt(store, reason="operator"):
    return store.append("relay.halted", {"reason": reason, "detail": "test"}, actor="relay")


def test_events_chain_from_zero_and_verify(tmp_path):
    store = _store(tmp_path)
    first = _halt(store, "one")
    second = _halt(store, "two")
    assert first["seq"] == 1 and first["prev_hash"] == ZERO_HASH
    assert second["seq"] == 2 and second["prev_hash"] == first["hash"]
    report = store.verify()
    assert report.ok, report.problems
    assert report.events == 2 and report.chain_head == second["hash"]


def test_the_database_refuses_update_and_delete(tmp_path):
    store = _store(tmp_path)
    _halt(store)
    digest = store.put_blob(b"exact bytes")
    raw = sqlite3.connect(store.path)
    try:
        for sql in (
            "UPDATE research_events SET payload = '{}'",
            "DELETE FROM research_events",
            "UPDATE research_blobs SET content = x'00'",
            "DELETE FROM research_blobs",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                raw.execute(sql)
    finally:
        raw.close()
    assert store.blob(digest) == b"exact bytes"


def test_tampering_with_dropped_triggers_is_detected(tmp_path):
    """Anyone with file access can drop the triggers; the chain still catches it."""
    store = _store(tmp_path)
    _halt(store, "original")
    _halt(store)
    raw = sqlite3.connect(store.path)
    raw.execute("DROP TRIGGER research_events_no_update")
    raw.execute(
        "UPDATE research_events SET payload = ? WHERE seq = 1",
        ('{"detail":"test","reason":"forged"}',),
    )
    raw.commit()
    raw.close()
    report = store.verify()
    assert not report.ok
    assert any("append-only trigger missing" in p for p in report.problems)
    assert any("seq 1" in p and "hash mismatch" in p for p in report.problems)


def test_a_removed_event_breaks_the_chain(tmp_path):
    store = _store(tmp_path)
    for reason in ("a", "b", "c"):
        _halt(store, reason)
    raw = sqlite3.connect(store.path)
    raw.execute("DROP TRIGGER research_events_no_delete")
    raw.execute("DELETE FROM research_events WHERE seq = 2")
    raw.commit()
    raw.close()
    problems = store.verify().problems
    assert any("sequence gap" in p for p in problems)
    assert any("prev_hash does not link" in p for p in problems)


def test_blobs_are_content_addressed_and_verified(tmp_path):
    store = _store(tmp_path)
    digest = store.put_blob(b"\x00\xffnot utf-8\x80", "application/octet-stream")
    assert digest == sha256_hex(b"\x00\xffnot utf-8\x80")
    assert store.put_blob(b"\x00\xffnot utf-8\x80") == digest, "idempotent by content"
    store.append(
        "isolation.violation_detected",
        {"invocation_id": "inv", "kind": "canary", "locations": ["x"], "evidence": digest},
    )
    assert store.verify().ok
    raw = sqlite3.connect(store.path)
    raw.execute("DROP TRIGGER research_blobs_no_update")
    raw.execute("UPDATE research_blobs SET content = x'01' WHERE sha256 = ?", (digest,))
    raw.commit()
    raw.close()
    assert any("does not match its key" in p for p in store.verify().problems)
    with pytest.raises(StoreIntegrityError):
        store.blob(digest)


def test_catalog_refuses_unknown_types_missing_fields_and_dangling_blobs(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(EventSchemaError, match="unknown research event type"):
        store.append("made.up", {})
    with pytest.raises(EventSchemaError, match="missing required payload field"):
        store.append("relay.halted", {"reason": "x"})
    with pytest.raises(EventSchemaError, match="missing blob"):
        store.append(
            "isolation.violation_detected",
            {"invocation_id": "i", "kind": "canary", "locations": [], "evidence": "0" * 64},
        )
    with pytest.raises(EventSchemaError, match="unknown actor"):
        store.append("relay.halted", {"reason": "x", "detail": ""}, actor="model")
    assert store.verify().events == 0, "refused writes leave no trace"
    assert store.failed is None, "a schema refusal is not a store failure"


def test_a_write_failure_fails_closed_and_stays_failed(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _halt(store)

    class BrokenConnection:
        def __init__(self, real):
            self.real = real

        def __getattr__(self, name):
            return getattr(self.real, name)

        def execute(self, sql, *args):
            if sql.lstrip().upper().startswith("INSERT"):
                raise sqlite3.OperationalError("disk I/O error")
            return self.real.execute(sql, *args)

    real = store._conn
    store._conn = BrokenConnection(real)
    with pytest.raises(StoreWriteError, match="disk I/O error"):
        _halt(store)
    assert store.failed and "disk I/O error" in store.failed
    store._conn = real  # the disk "recovers" ...
    with pytest.raises(StoreWriteError, match="already failed"):
        _halt(store)  # ... but the store stays closed for writes
    with pytest.raises(StoreWriteError, match="already failed"):
        store.put_blob(b"x")
    assert store.verify().events == 1, "nothing was written after the failure"


def test_create_refuses_to_overwrite_and_open_reads_back(tmp_path):
    store = _store(tmp_path)
    _halt(store)
    store.close()
    with pytest.raises(StoreWriteError, match="already exists"):
        RunStore.create(tmp_path / "run")
    reopened = RunStore.open(tmp_path / "run")
    assert reopened.run_id == store.run_id
    assert reopened.events()[0]["payload"]["detail"] == "test"
    assert (tmp_path / "run" / "run.db").stat().st_mode & 0o777 == 0o600
