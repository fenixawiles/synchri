"""The relay as a separate process: grants over an owner-only control socket (ISO-06).

The orchestrator side (:class:`RelayClient`) never holds a credential; it gets
per-invocation tokens and credential fingerprints from the relay, and its
leak checks work from those fingerprints alone.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from synchri.research.canonical import sha256_hex
from synchri.research.container import check_cli_environment
from synchri.research.credentials import CredentialVault
from synchri.research.errors import IsolationError, RelayError
from synchri.research.mock_provider import MockProvider
from synchri.research.reconstruct import reconstruct
from synchri.research.relay import RecordingRelay, RelayClient, UnixHTTPConnection, Upstream
from synchri.research.store import RunStore

DUMMY = "dummy-anthropic-control-0001"
MODEL = "claude-test-model-1"
ALLOWED_ENV = frozenset({"PATH", "HOME", "ANTHROPIC_API_KEY"})


@pytest.fixture
def sock_dir():
    path = Path(tempfile.mkdtemp(prefix="srr-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _post(data_socket: Path, token: str, model: str = MODEL):
    conn = UnixHTTPConnection(str(data_socket), timeout=10)
    try:
        conn.request("POST", "/anthropic/v1/messages", body=json.dumps({
            "model": model, "stream": True, "tools": [],
            "messages": [{"role": "user", "content": "What is 2+2?"}]}),
            headers={"x-api-key": token, "Content-Type": "application/json"})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


@pytest.fixture
def in_process(tmp_path, sock_dir):
    with MockProvider() as mock:
        store = RunStore.create(tmp_path / "run")
        relay = RecordingRelay(store, CredentialVault.for_tests({"anthropic": DUMMY}),
                               {"anthropic": Upstream(mock.base_url)},
                               socket_path=sock_dir / "relay.sock",
                               control_socket_path=sock_dir / "control.sock",
                               allow_insecure_upstream=True).start()
        yield relay, RelayClient(sock_dir / "control.sock", sock_dir / "relay.sock"), mock
        relay.stop()


def test_the_control_socket_is_owner_only(in_process, sock_dir):
    assert stat.S_IMODE((sock_dir / "control.sock").stat().st_mode) == 0o600


def test_a_client_grant_authorizes_exactly_its_invocation(in_process, sock_dir):
    relay, client, mock = in_process
    grant, token = client.issue_grant(invocation_id="inv_ctl_0001", provider="anthropic", model=MODEL)
    assert token.startswith("srt_") and grant.invocation_id == "inv_ctl_0001"
    assert _post(sock_dir / "relay.sock", token)[0] == 200
    assert mock.requests[0]["auth_sha256"] == sha256_hex(DUMMY.encode())
    assert _post(sock_dir / "relay.sock", token)[0] == 403, "the default grant allows one request"
    [issued] = relay.store.events("relay.grant_issued")
    assert issued["payload"]["grant_id"] == grant.grant_id
    assert token not in json.dumps(relay.store.events())


def test_status_shares_fingerprints_never_values(in_process):
    _relay, client, _mock = in_process
    assert client.credential_fingerprints() == {"anthropic": sha256_hex(DUMMY.encode())}
    assert client.halted is None
    conn = UnixHTTPConnection(client.control_socket)
    conn.request("GET", "/status")
    assert DUMMY.encode() not in conn.getresponse().read()
    conn.close()


@pytest.mark.parametrize("payload", [{"provider": "anthropic"}, {"invocation_id": "i", "provider": "nope",
                                                                  "model": MODEL}])
def test_bad_grant_requests_are_refused(in_process, payload):
    _relay, client, _mock = in_process
    with pytest.raises(RelayError):
        client._call("POST", "/grants", payload)


def test_a_remote_relay_keeps_its_own_forbidden_markers(in_process):
    _relay, client, _mock = in_process
    with pytest.raises(RelayError):
        client.issue_grant(invocation_id="i", provider="anthropic", model=MODEL, forbidden_markers=())


def test_unknown_control_paths_are_refused(in_process):
    _relay, client, _mock = in_process
    with pytest.raises(RelayError):
        client._call("GET", "/grants")


def test_fingerprints_alone_catch_a_credential_in_the_cli_environment():
    env = {"PATH": "/usr/bin", "HOME": "/home/slot", "ANTHROPIC_API_KEY": DUMMY}
    with pytest.raises(IsolationError):
        check_cli_environment(env, ALLOWED_ENV, "ANTHROPIC_API_KEY",
                              forbidden_fingerprints={sha256_hex(DUMMY.encode())})
    env["ANTHROPIC_API_KEY"] = "srt_relay-token"
    check_cli_environment(env, ALLOWED_ENV, "ANTHROPIC_API_KEY",
                          forbidden_fingerprints={sha256_hex(DUMMY.encode())})


def _wait_for(path: Path, proc: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if proc.poll() is not None:
            raise AssertionError(f"relay exited early: {proc.stderr.read()}")
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.05)


def test_the_relay_cli_serves_an_orchestrator_in_another_process(tmp_path, sock_dir):
    """The credential lives only in the relay subprocess; this process never loads it."""
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"anthropic": DUMMY}))
    credentials.chmod(0o600)
    run_dir = tmp_path / "run"
    with MockProvider() as mock:
        proc = subprocess.Popen(
            [sys.executable, "-m", "synchri", "research", "relay", "--run-dir", str(run_dir),
             "--credentials", str(credentials), "--socket", str(sock_dir / "relay.sock"),
             "--control-socket", str(sock_dir / "control.sock"), "--provider", "anthropic",
             "--anthropic-upstream", mock.base_url, "--allow-loopback-http-upstream"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, "SYNCHRI_HOME": str(tmp_path / "never-created")},
        )
        try:
            _wait_for(sock_dir / "control.sock", proc)
            client = RelayClient(sock_dir / "control.sock", sock_dir / "relay.sock")
            _grant, token = client.issue_grant(invocation_id="inv_remote_0001",
                                               provider="anthropic", model=MODEL)
            assert _post(sock_dir / "relay.sock", token)[0] == 200
            # The orchestrator appends to the same store concurrently.
            store = RunStore.open(run_dir)
            prompt = store.put_blob(b"What is 2+2?", "text/plain")
            store.append("invocation.prepared", {
                "template_id": "t", "template_version": "1", "template_sha256": "0" * 64,
                "prompt": prompt, "prompt_sha256": prompt, "prompt_chars": 12, "inputs": [],
                "argv": [], "cwd": "/work", "env_names": [], "env_value_sha256": {},
                "timeout_s": 10, "budget_before": None,
            }, invocation_id="inv_remote_0001")
        finally:
            proc.send_signal(signal.SIGTERM)
            out, err = proc.communicate(timeout=20)
    assert proc.returncode == 0, err
    assert mock.requests[0]["auth_sha256"] == sha256_hex(DUMMY.encode())
    assert not (sock_dir / "control.sock").exists()
    assert not (tmp_path / "never-created").exists()
    store = RunStore.open(run_dir)
    assert store.verify().ok
    kinds = [e["event_type"] for e in store.events()]
    assert kinds[0] == "relay.started" and "invocation.prepared" in kinds and kinds[-1] == "relay.halted"
    assert DUMMY not in json.dumps(store.events()) and DUMMY not in out + err
    rebuilt = reconstruct(store)
    assert rebuilt.invocations[0].requests and rebuilt.invocations[0].responses


def test_loopback_http_is_the_only_insecure_upstream_allowed(tmp_path, sock_dir):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"anthropic": DUMMY}))
    credentials.chmod(0o600)
    proc = subprocess.run(
        [sys.executable, "-m", "synchri", "research", "relay", "--run-dir", str(tmp_path / "run"),
         "--credentials", str(credentials), "--socket", str(sock_dir / "relay.sock"),
         "--provider", "anthropic", "--anthropic-upstream", "http://example.com",
         "--allow-loopback-http-upstream"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "SYNCHRI_HOME": str(tmp_path / "never-created")},
    )
    assert proc.returncode == 16 and "loopback only" in proc.stderr
    assert not (tmp_path / "run").exists()
