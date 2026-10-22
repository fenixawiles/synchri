"""Event-log reconstruction: every invocation rebuilds from the log alone (§6.3).

A synthetic invocation is driven through the real relay (mock upstream), with
the orchestrator's events appended as ``run_invocation`` appends them. The
container itself is exercised by the opt-in conformance suite.
"""

from __future__ import annotations

import http.client
import json
import sqlite3

import pytest

from synchri.research.credentials import CredentialVault
from synchri.research.mock_provider import MockProvider
from synchri.research.reconstruct import reconstruct
from synchri.research.relay import RecordingRelay, Upstream
from synchri.research.store import RunStore

MODEL = "claude-test-model-1"


@pytest.fixture
def setup(tmp_path):
    with MockProvider() as mock:
        store = RunStore.create(tmp_path / "run")
        relay = RecordingRelay(store, CredentialVault.for_tests({"anthropic": "dummy-anthropic-0001"}),
                               {"anthropic": Upstream(mock.base_url)}, tcp_port=0,
                               allow_insecure_upstream=True).start()
        yield store, relay, mock
        relay.stop()


def _invocation(store, relay, *, requests=1, terminal="invocation.exited", bad_model=False):
    grant, token = relay.issue_grant(invocation_id="inv_synthetic_0001", provider="anthropic",
                                     model=MODEL, max_requests=1)
    prompt = store.put_blob(b"What is 2+2?", "text/plain")
    store.append("invocation.prepared", {
        "template_id": "t", "template_version": "1", "template_sha256": "0" * 64,
        "prompt": prompt, "prompt_sha256": prompt, "prompt_chars": 12, "inputs": [],
        "argv": ["cli", "<stdin>"], "cwd": "/work", "env_names": [], "env_value_sha256": {},
        "timeout_s": 10, "budget_before": None,
    }, invocation_id=grant.invocation_id)
    store.append("invocation.isolation", {
        "runtime_kind": "container", "image_ref": "x@sha256:" + "a" * 64, "container_argv": [],
        "mounts": [], "network": {"mode": "none"}, "limits": {}, "cli_binary_sha256": "0" * 64,
        "cli_version": "1",
    }, invocation_id=grant.invocation_id)
    for _ in range(requests):
        conn = http.client.HTTPConnection("127.0.0.1", relay.tcp_port, timeout=10)
        conn.request("POST", "/anthropic/v1/messages", body=json.dumps({
            "model": "other" if bad_model else MODEL, "stream": True, "tools": [],
            "messages": [{"role": "user", "content": "What is 2+2?"}]}),
            headers={"x-api-key": token, "Content-Type": "application/json"})
        conn.getresponse().read()
        conn.close()
    stdout = store.put_blob(b"{}", "application/x-ndjson")
    store.append(terminal, {"returncode": 0, "timed_out": False, "cancelled": False, "wall_s": 1,
                            "monotonic_elapsed_ns": 1, "failure_kind": None},
                 invocation_id=grant.invocation_id)
    store.append("invocation.captured", {
        "stdout": stdout, "stderr": stdout, "stream": stdout, "stdout_truncated": False,
        "final_text": None, "final_text_source": "raw_stdout", "model_reported": None,
        "provider_session_id": None, "usage": None, "canary_scan": "clean", "tool_events_seen": 0,
    }, invocation_id=grant.invocation_id)
    return grant


def test_a_complete_invocation_reconstructs(setup):
    store, relay, _mock = setup
    _invocation(store, relay)
    report = reconstruct(store)
    assert report.ok, report.problems
    [record] = report.invocations
    assert record.prompt == b"What is 2+2?"
    assert len(record.requests) == 1 and b"What is 2+2?" in record.requests[0]
    assert len(record.responses) == 1 and b"MOCK-REPLY-OK" in record.responses[0]
    assert record.terminal == "invocation.exited"
    assert "prompt re-rendering from templates (step 4)" in report.not_yet_applicable


def test_refused_requests_reconstruct_with_their_refusal(setup):
    store, relay, mock = setup
    _invocation(store, relay, requests=2)  # the second exceeds the grant
    report = reconstruct(store)
    assert report.ok, report.problems
    assert len(report.invocations[0].requests) == 2 and len(report.invocations[0].responses) == 1
    assert len(mock.requests) == 1


def test_a_missing_terminal_event_fails_reconstruction(setup):
    store, relay, _mock = setup
    grant = _invocation(store, relay)
    store.append("invocation.failed", {"returncode": 1, "timed_out": False, "cancelled": False,
                                       "wall_s": 1, "monotonic_elapsed_ns": 1, "failure_kind": "x"},
                 invocation_id=grant.invocation_id)
    assert any("2 terminal events" in p for p in reconstruct(store).problems)


def test_an_altered_response_blob_fails_reconstruction(setup):
    store, relay, _mock = setup
    _invocation(store, relay)
    digest = store.events("relay.upstream_response")[0]["payload"]["response"]
    raw = sqlite3.connect(store.path)
    raw.execute("DROP TRIGGER research_blobs_no_update")
    raw.execute("UPDATE research_blobs SET content = ? WHERE sha256 = ?", (b"forged", digest))
    raw.commit()
    raw.close()
    report = reconstruct(store)
    assert not report.ok and not report.chain_ok
    assert any("response 1" in p for p in report.problems)
