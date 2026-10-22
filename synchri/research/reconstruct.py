"""Rebuild each invocation from the event log alone (spec §6.3, this phase).

Applies the reconstruction steps that exist in the foundation phase:

* chain and blob integrity (via :meth:`RunStore.verify`, steps 1–2);
* per invocation: the prompt bytes match their recorded hash; a grant was
  issued for it; every relay request and response is present as exact bytes
  matching its recorded hash, with request indexes gap-free from 1; every
  forwarded request has exactly one upstream outcome; refusals match their
  request records; and exactly one terminal event exists (step 8).

Steps that depend on later phases (schedule regeneration, prompt re-rendering
from templates, response parsing, derived data, scores) are not applicable yet
and are reported as such, not as passes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .canonical import sha256_hex
from .errors import StoreIntegrityError
from .store import RunStore

NOT_YET_APPLICABLE = (
    "schedule regeneration (step 3)",
    "prompt re-rendering from templates (step 4)",
    "visibility checks (step 5)",
    "response re-parsing (step 6)",
    "derived data and scores (step 7)",
)


@dataclass
class InvocationRecord:
    invocation_id: str
    prompt: bytes | None
    requests: list[bytes] = field(default_factory=list)
    responses: list[bytes] = field(default_factory=list)
    terminal: str | None = None
    problems: list[str] = field(default_factory=list)


@dataclass
class ReconstructionReport:
    ok: bool
    chain_ok: bool
    invocations: list[InvocationRecord]
    problems: list[str]
    not_yet_applicable: tuple[str, ...] = NOT_YET_APPLICABLE

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "chain_ok": self.chain_ok, "problems": self.problems,
            "invocations": [
                {"invocation_id": r.invocation_id, "terminal": r.terminal,
                 "requests": len(r.requests), "responses": len(r.responses),
                 "prompt_bytes": len(r.prompt) if r.prompt is not None else None,
                 "problems": r.problems}
                for r in self.invocations
            ],
            "not_yet_applicable": list(self.not_yet_applicable),
        }


def _blob(store: RunStore, digest: str | None, label: str, problems: list[str]) -> bytes | None:
    if digest is None:
        problems.append(f"{label}: no blob reference")
        return None
    try:
        data = store.blob(digest)
    except StoreIntegrityError as exc:
        problems.append(f"{label}: {exc.message}")
        return None
    if sha256_hex(data) != digest:
        problems.append(f"{label}: content does not match its recorded hash")
    return data


def reconstruct(store: RunStore) -> ReconstructionReport:
    chain = store.verify()
    problems = list(chain.problems)
    events = store.events()
    grants = {e["payload"]["grant_id"]: e for e in events if e["event_type"] == "relay.grant_issued"}
    invocation_ids = []
    for e in events:
        if e["event_type"] == "invocation.prepared" and e["invocation_id"] not in invocation_ids:
            invocation_ids.append(e["invocation_id"])

    records = []
    for invocation_id in invocation_ids:
        mine = [e for e in events if e["invocation_id"] == invocation_id]
        record = InvocationRecord(invocation_id=invocation_id, prompt=None)
        prepared = [e for e in mine if e["event_type"] == "invocation.prepared"]
        if len(prepared) != 1:
            record.problems.append(f"{len(prepared)} invocation.prepared events (expected 1)")
        else:
            payload = prepared[0]["payload"]
            record.prompt = _blob(store, payload["prompt"], "prompt", record.problems)
            if record.prompt is not None and sha256_hex(record.prompt) != payload["prompt_sha256"]:
                record.problems.append("prompt bytes do not match prompt_sha256")
        my_grants = [g for g in grants.values() if g["invocation_id"] == invocation_id]
        if len(my_grants) != 1:
            record.problems.append(f"{len(my_grants)} relay grants for this invocation (expected 1)")
        if not any(e["event_type"] == "invocation.isolation" for e in mine):
            record.problems.append("no invocation.isolation record")

        received = [e for e in mine if e["event_type"] == "relay.request_received"]
        indexes = [e["payload"]["request_index"] for e in received]
        if indexes != list(range(1, len(indexes) + 1)):
            record.problems.append(f"relay request indexes are not gap-free from 1: {indexes}")
        outcomes = {}
        for e in mine:
            if e["event_type"] in ("relay.upstream_response", "relay.upstream_error", "relay.request_rejected"):
                outcomes.setdefault(e["payload"]["request_index"], []).append(e)
        for e in received:
            payload = e["payload"]
            index = payload["request_index"]
            data = _blob(store, payload["request"], f"request {index}", record.problems)
            if data is not None:
                record.requests.append(data)
                if sha256_hex(data) != payload["request_sha256"]:
                    record.problems.append(f"request {index} bytes do not match request_sha256")
            if payload["grant_id"] not in grants:
                record.problems.append(f"request {index} names an unissued grant")
            found = outcomes.get(index, [])
            kinds = sorted(o["event_type"] for o in found)
            if payload["decision"] == "reject":
                if kinds != ["relay.request_rejected"]:
                    record.problems.append(f"rejected request {index} has outcomes {kinds}")
            elif len(found) != 1 or kinds[0] == "relay.request_rejected":
                record.problems.append(f"forwarded request {index} has outcomes {kinds}")
            for outcome in found:
                if outcome["event_type"] == "relay.upstream_response":
                    body = _blob(store, outcome["payload"]["response"], f"response {index}", record.problems)
                    if body is not None:
                        record.responses.append(body)
                        if sha256_hex(body) != outcome["payload"]["response_sha256"]:
                            record.problems.append(f"response {index} bytes do not match response_sha256")

        terminals = [e["event_type"] for e in mine if e["event_type"] in ("invocation.exited", "invocation.failed")]
        if len(terminals) != 1:
            record.problems.append(f"{len(terminals)} terminal events (expected exactly 1)")
        else:
            record.terminal = terminals[0]
        captured = [e for e in mine if e["event_type"] == "invocation.captured"]
        if len(captured) != 1:
            record.problems.append(f"{len(captured)} invocation.captured events (expected 1)")
        else:
            _blob(store, captured[0]["payload"]["stdout"], "stdout", record.problems)
        records.append(record)
        problems.extend(f"{invocation_id}: {p}" for p in record.problems)

    return ReconstructionReport(ok=not problems, chain_ok=chain.ok, invocations=records,
                                problems=problems)
