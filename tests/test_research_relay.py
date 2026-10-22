"""The recording relay: capture-before-forward, credential isolation, refusal.

Spec VT-32 (relay invariants) and VT-33 (credential isolation), plus the
fail-closed behavior of LOG-03 at the relay. Mock upstream, dummy credentials.
"""

from __future__ import annotations

import gzip
import http.client
import json
import socket
import sqlite3
import tempfile
from pathlib import Path

import pytest

from synchri.research.canonical import sha256_hex
from synchri.research.credentials import CredentialVault
from synchri.research.errors import RelayError
from synchri.research.mock_provider import SERVED_MODEL, MockProvider
from synchri.research.relay import RecordingRelay, Upstream, UnixHTTPConnection
from synchri.research.store import RunStore

DUMMY = {"anthropic": "dummy-anthropic-credential-0001", "openai": "dummy-openai-credential-0002"}
MODEL = "claude-test-model-1"


@pytest.fixture
def mock():
    with MockProvider() as provider:
        yield provider


@pytest.fixture
def relay(tmp_path, mock):
    store = RunStore.create(tmp_path / "run")
    vault = CredentialVault.for_tests(DUMMY)
    upstreams = {p: Upstream(mock.base_url) for p in ("anthropic", "openai")}
    r = RecordingRelay(store, vault, upstreams, tcp_port=0, allow_insecure_upstream=True).start()
    yield r
    r.stop()


def _post(relay, provider, path, body, token, *, extra_headers=None, raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", relay.tcp_port, timeout=10)
    headers = {"Content-Type": "application/json", **(extra_headers or {})}
    if provider == "anthropic":
        headers["x-api-key"] = token
    else:
        headers["Authorization"] = f"Bearer {token}"
    payload = raw if raw is not None else json.dumps(body).encode()
    conn.request("POST", f"/{provider}{path}", body=payload, headers=headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def _anthropic_body(model=MODEL, tools=(), text="What is 2+2?"):
    return {"model": model, "max_tokens": 100, "stream": True, "tools": list(tools),
            "system": [{"type": "text", "text": "Answer."}],
            "messages": [{"role": "user", "content": text}]}


def test_forwarding_records_exact_bytes_and_injects_only_the_real_credential(relay, mock):
    grant, token = relay.issue_grant(invocation_id="inv-1", provider="anthropic", model=MODEL)
    body = json.dumps(_anthropic_body(), separators=(",", ":")).encode()
    status, data = _post(relay, "anthropic", "/v1/messages?beta=true", None, token, raw=body)
    assert status == 200 and b"MOCK-REPLY-OK" in data

    [upstream] = mock.requests
    assert upstream["path"] == "/v1/messages?beta=true"
    assert upstream["raw"] == body, "forwarded byte for byte"
    assert upstream["auth_sha256"] == sha256_hex(DUMMY["anthropic"].encode()), "real credential injected"
    assert upstream["auth_sha256"] != sha256_hex(token.encode()), "relay token never forwarded"

    received = relay.store.events("relay.request_received")[-1]["payload"]
    assert received["decision"] == "forward" and received["model"] == MODEL
    assert received["headers"]["x-api-key"] == "<redacted>"
    assert relay.store.blob(received["request"]) == body
    response = relay.store.events("relay.upstream_response")[-1]["payload"]
    assert response["status"] == 200 and response["served_model"] == SERVED_MODEL
    assert response["usage"]["message_start"]["input_tokens"] == 11
    assert relay.store.blob(response["response"]) == data, "client got exactly what was recorded"
    assert relay.store.verify().ok


def test_no_credential_or_token_value_is_ever_stored(relay, mock):
    _grant, token = relay.issue_grant(invocation_id="inv-2", provider="openai", model="gpt-test")
    body = {"model": "gpt-test", "input": [], "tools": [], "stream": True}
    assert _post(relay, "openai", "/v1/responses", body, token)[0] == 200
    raw = sqlite3.connect(relay.store.path)
    everything = b"".join(
        (v.encode() if isinstance(v, str) else bytes(v))
        for table in ("research_events", "research_blobs")
        for row in raw.execute(f"SELECT * FROM {table}")
        for v in row if isinstance(v, (str, bytes))
    )
    raw.close()
    for secret in (*DUMMY.values(), token):
        assert secret.encode() not in everything
    grant_event = relay.store.events("relay.grant_issued")[-1]["payload"]
    assert grant_event["token_sha256"] == sha256_hex(token.encode())
    started = relay.store.events("relay.started")[0]["payload"]
    assert started["credential_fingerprints"]["openai"] == sha256_hex(DUMMY["openai"].encode())


@pytest.mark.parametrize(
    "case, provider, path, body_kwargs, expected",
    [
        ("wrong model", "anthropic", "/v1/messages", {"model": "claude-other"}, "model_mismatch"),
        ("disallowed tool", "anthropic", "/v1/messages",
         {"tools": [{"name": "Bash", "input_schema": {}}]}, "tool_not_allowed:Bash"),
        ("injected cwd", "anthropic", "/v1/messages",
         {"text": "# Environment\n - Primary working directory: /home/x"},
         "injected_marker:Primary working directory"),
        ("injected date", "anthropic", "/v1/messages",
         {"text": "Today's date is 2026-10-22."}, "injected_marker:Today's date is"),
        ("unknown route", "anthropic", "/v1/messages/count_tokens", {}, "route_not_allowed"),
    ],
)
def test_policy_violations_are_recorded_and_never_forwarded(
    relay, mock, case, provider, path, body_kwargs, expected
):
    _grant, token = relay.issue_grant(invocation_id=f"inv-{case}", provider=provider, model=MODEL)
    status, data = _post(relay, provider, path, _anthropic_body(**body_kwargs), token)
    assert status == 403 and b"research relay refused" in data
    assert mock.requests == [], f"{case}: must not reach the provider"
    rejected = relay.store.events("relay.request_rejected")[-1]["payload"]
    assert expected in rejected["reasons"]
    received = relay.store.events("relay.request_received")[-1]["payload"]
    assert received["decision"] == "reject", "the refused request is still recorded verbatim"


def test_codex_allowlist_admits_only_request_user_input(relay, mock):
    grant, token = relay.issue_grant(
        invocation_id="inv-codex", provider="openai", model="gpt-test",
        allowed_tools=("request_user_input",), max_requests=2,
    )
    ok = {"model": "gpt-test", "input": [], "tools": [{"type": "function", "name": "request_user_input"}]}
    assert _post(relay, "openai", "/v1/responses", ok, token)[0] == 200
    bad = {**ok, "tools": [{"type": "function", "name": "exec_command"}]}
    assert _post(relay, "openai", "/v1/responses", bad, token)[0] == 403
    assert len(mock.requests) == 1
    assert "tool_not_allowed:exec_command" in relay.store.events("relay.request_rejected")[-1]["payload"]["reasons"]


def test_the_grant_request_limit_blocks_hidden_retries(relay, mock):
    _grant, token = relay.issue_grant(invocation_id="inv-limit", provider="anthropic", model=MODEL)
    assert _post(relay, "anthropic", "/v1/messages", _anthropic_body(), token)[0] == 200
    status, _ = _post(relay, "anthropic", "/v1/messages", _anthropic_body(), token)
    assert status == 403
    assert len(mock.requests) == 1, "a second (retry) request never reaches the provider"
    assert "request_limit_exceeded" in relay.store.events("relay.request_rejected")[-1]["payload"]["reasons"]


def test_unknown_token_and_expired_grant_are_refused(relay, mock):
    status, _ = _post(relay, "anthropic", "/v1/messages", _anthropic_body(), "srt_not-issued")
    assert status == 403
    assert relay.store.events("relay.request_rejected")[-1]["payload"]["reasons"] == ["unknown_or_missing_token"]
    _grant, token = relay.issue_grant(invocation_id="inv-exp", provider="anthropic", model=MODEL, ttl_s=-1)
    assert _post(relay, "anthropic", "/v1/messages", _anthropic_body(), token)[0] == 403
    assert "grant_expired" in relay.store.events("relay.request_rejected")[-1]["payload"]["reasons"]
    assert mock.requests == []


def test_gzip_bodies_are_inspected_and_forwarded_unchanged(relay, mock):
    _grant, token = relay.issue_grant(invocation_id="inv-gz", provider="anthropic", model=MODEL)
    raw = gzip.compress(json.dumps(_anthropic_body()).encode())
    status, _ = _post(relay, "anthropic", "/v1/messages", None, token, raw=raw,
                      extra_headers={"Content-Encoding": "gzip"})
    assert status == 200 and mock.requests[0]["raw"] == raw
    _grant, token = relay.issue_grant(invocation_id="inv-br", provider="anthropic", model=MODEL)
    status, _ = _post(relay, "anthropic", "/v1/messages", None, token, raw=b"\x00",
                      extra_headers={"Content-Encoding": "br"})
    assert status == 403
    assert "encoding_unsupported" in relay.store.events("relay.request_rejected")[-1]["payload"]["reasons"]


def test_a_store_failure_halts_the_relay_before_anything_is_forwarded(relay, mock):
    _grant, token = relay.issue_grant(invocation_id="inv-fail", provider="anthropic", model=MODEL)
    relay.store.failed = "simulated disk failure"
    status, _ = _post(relay, "anthropic", "/v1/messages", _anthropic_body(), token)
    assert status == 503 and mock.requests == [], "an unrecordable request is never forwarded"
    assert relay.halted and "store_write_failed" in relay.halted
    status, _ = _post(relay, "anthropic", "/v1/messages", _anthropic_body(), token)
    assert status == 503, "a halted relay stays halted"
    with pytest.raises(RelayError, match="halted"):
        relay.issue_grant(invocation_id="inv-after", provider="anthropic", model=MODEL)


def test_upstream_failures_are_recorded(tmp_path):
    store = RunStore.create(tmp_path / "run")
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()  # nothing listens here now
    relay = RecordingRelay(
        store, CredentialVault.for_tests(DUMMY), {"anthropic": Upstream(f"http://127.0.0.1:{port}")},
        tcp_port=0, allow_insecure_upstream=True,
    ).start()
    try:
        _grant, token = relay.issue_grant(invocation_id="inv-dead", provider="anthropic", model=MODEL)
        status, _ = _post(relay, "anthropic", "/v1/messages", _anthropic_body(), token)
        assert status == 502
        error = store.events("relay.upstream_error")[-1]["payload"]
        assert error["error_kind"] in {"ConnectionRefusedError", "ConnectionError", "OSError"}
    finally:
        relay.stop()


def test_construction_refuses_insecure_upstreams_and_missing_credentials(tmp_path):
    store = RunStore.create(tmp_path / "run")
    with pytest.raises(RelayError, match="must be https"):
        RecordingRelay(store, CredentialVault.for_tests(DUMMY),
                       {"anthropic": Upstream("http://api.example.test")}, tcp_port=0)
    with pytest.raises(Exception, match="no openai credential"):
        RecordingRelay(store, CredentialVault.for_tests({"anthropic": DUMMY["anthropic"]}),
                       {"openai": Upstream("https://api.example.test")}, tcp_port=0)


def test_the_relay_serves_a_unix_socket(tmp_path, mock):
    store = RunStore.create(tmp_path / "run")
    short = Path(tempfile.mkdtemp(prefix="srr-", dir="/tmp"))
    sock_path = short / "relay.sock"
    relay = RecordingRelay(store, CredentialVault.for_tests(DUMMY),
                           {"anthropic": Upstream(mock.base_url)}, socket_path=sock_path,
                           allow_insecure_upstream=True).start()
    try:
        assert oct(sock_path.stat().st_mode & 0o777) == "0o666"
        _grant, token = relay.issue_grant(invocation_id="inv-unix", provider="anthropic", model=MODEL)
        conn = UnixHTTPConnection(str(sock_path))
        conn.request("POST", "/anthropic/v1/messages", body=json.dumps(_anthropic_body()),
                     headers={"x-api-key": token, "Content-Type": "application/json"})
        assert conn.getresponse().status == 200
        conn.close()
    finally:
        relay.stop()
    assert not sock_path.exists()
    short.rmdir()
    too_long = tmp_path / ("x" * 120) / "relay.sock"
    with pytest.raises(RelayError, match="too long"):
        RecordingRelay(RunStore.create(tmp_path / "run2"), CredentialVault.for_tests(DUMMY),
                       {"anthropic": Upstream(mock.base_url)}, socket_path=too_long,
                       allow_insecure_upstream=True).start()
