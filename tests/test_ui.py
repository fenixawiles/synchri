"""The local app: server security and the API behind the UI.

The UI is the one component that opens a socket, so its access control gets the
same scrutiny as the room's. Everything else here checks that the browser cannot
do anything the manager would refuse — the UI holds no rules of its own.
"""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from synchri.broker import Broker
from synchri.errors import SynchriError
from synchri.session.manager import SessionManager
from synchri.session.modes import ParticipantPlan
from synchri.ui.server import create_server

SPEC = "Build it.\n\n## Acceptance\n- AUTH-01 login works"


def _git(root, *args):
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com",
        "PATH": __import__("os").environ.get("PATH", ""), "HOME": str(root),
    }
    subprocess.run(["git", "-C", str(root), *args], check=True, env=env, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@e.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


@pytest.fixture
def ui(workspace):
    """A running server on an ephemeral loopback port."""
    broker = Broker(workspace)
    server, url = create_server(broker, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield {"base": base, "token": server.token, "url": url, "broker": broker, "server": server}
    server.shutdown()
    server.server_close()
    broker.close()


def call(ui, path, body=None, *, token="__default__", headers=None):
    request = urllib.request.Request(
        f"{ui['base']}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
    )
    request.add_header("Content-Type", "application/json")
    if token is not None:
        request.add_header("X-Synchri-Token", ui["token"] if token == "__default__" else token)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode())


# ----------------------------------------------------------------------
# access control
# ----------------------------------------------------------------------


def test_the_server_binds_loopback_only(ui):
    assert ui["server"].server_address[0] == "127.0.0.1"
    assert ui["url"].startswith("http://127.0.0.1:")


def test_binding_a_public_address_is_refused(workspace):
    broker = Broker(workspace)
    try:
        with pytest.raises(SynchriError) as exc:
            create_server(broker, host="0.0.0.0", port=0)
        assert exc.value.code == "refused_bind"
        assert "--allow-remote" in exc.value.message
    finally:
        broker.close()


def test_the_api_requires_the_token(ui):
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(ui, "/api/bootstrap", token=None)
    assert exc.value.code == 401


def test_a_wrong_token_is_rejected(ui):
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(ui, "/api/bootstrap", token="not-the-token")
    assert exc.value.code == 401


def test_cross_origin_requests_are_rejected(ui):
    """A page on another site must not be able to drive the session."""
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(ui, "/api/bootstrap", headers={"Origin": "http://evil.example"})
    assert exc.value.code == 401


def test_the_index_without_a_token_explains_rather_than_serving_the_app(ui):
    with urllib.request.urlopen(f"{ui['base']}/", timeout=20) as response:
        body = response.read().decode()
    assert "needs the launch link" in body
    assert "__SYNCHRI_TOKEN__" not in body


def test_the_launch_url_serves_the_app_and_sets_a_cookie(ui):
    with urllib.request.urlopen(ui["url"], timeout=20) as response:
        body = response.read().decode()
        assert "synchri_token=" in (response.headers.get("Set-Cookie") or "")
        assert "SameSite=Strict" in response.headers.get("Set-Cookie")
        assert "frame-ancestors 'none'" in response.headers.get("Content-Security-Policy")
    assert "<title>Synchri</title>" in body
    assert "__SYNCHRI_TOKEN__" not in body, "the placeholder must be substituted"


def test_the_page_references_no_external_origin(ui):
    with urllib.request.urlopen(ui["url"], timeout=20) as response:
        body = response.read().decode()
    for scheme in ("http://", "https://"):
        for line in body.splitlines():
            if scheme in line and "127.0.0.1" not in line:
                pytest.fail(f"page reaches outside: {line.strip()[:120]}")


def test_unknown_routes_are_not_found(ui):
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(ui, "/api/nope")
    assert exc.value.code == 404


def test_broker_errors_come_back_as_structured_json(ui):
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(ui, "/api/dashboard?session=sess_" + "z" * 20)
    assert exc.value.code == 400
    assert json.loads(exc.value.read())["error"]["code"] == "not_found"


# ----------------------------------------------------------------------
# the wizard, driven as the browser drives it
# ----------------------------------------------------------------------


def test_bootstrap_gives_the_app_everything_it_needs(ui):
    boot = call(ui, "/api/bootstrap")
    assert [m["mode"] for m in boot["modes"]] == ["interactive", "long_horizon", "review_audit"]
    assert any(c["key"] == "git.push" for g in boot["permissions"] for c in g["capabilities"])
    assert boot["sessions"] == [] and "workspace" in boot


def test_the_draft_reports_problems_until_it_is_ready(ui, repo):
    call(ui, "/api/draft/reset", {"draft": "d"})
    state = call(ui, "/api/draft", {"draft": "d", "mode": "long_horizon"})
    assert not state["ready"] and state["problems"]

    state = call(ui, "/api/draft", {"draft": "d", "repo_path": str(repo)})
    assert state["repo_status"]["name"] == "proj"

    state = call(ui, "/api/draft", {"draft": "d", "agents": [
        {"name": "claude", "runtime": "claude_code", "role": "primary_builder"},
        {"name": "codex", "runtime": "codex", "role": "adversarial_reviewer"},
    ]})
    state = call(ui, "/api/draft", {"draft": "d", "spec": SPEC})
    assert [g["gate_id"] for g in state["detected_gates"]] == ["AUTH-01"]

    state = call(ui, "/api/draft", {"draft": "d", "deadline": "6 hours"})
    assert state["ready"] and state["problems"] == []
    assert state["summary"]["deadline"].startswith("duration: 6 hours")


def test_starting_from_the_ui_creates_a_worktree_and_a_contract(ui, repo):
    _ready_draft(ui, repo)
    result = call(ui, "/api/start", {"draft": "d"})
    session = result["session"]

    assert session["status"] == "awaiting_ack"
    assert session["worktree"]["path"] != session["repository"]["root"]
    assert result["contract"]["revision"] == 1
    assert "SYNCHRI SESSION CONTRACT" in result["contract"]["core_text"]


def test_the_ui_cannot_activate_before_acknowledgment(ui, repo):
    _ready_draft(ui, repo)
    session_id = call(ui, "/api/start", {"draft": "d"})["session"]["session_id"]

    with pytest.raises(urllib.error.HTTPError) as exc:
        call(ui, "/api/activate", {"session": session_id})
    assert json.loads(exc.value.read())["error"]["code"] == "awaiting_acknowledgment"

    for agent in ("claude", "codex"):
        call(ui, "/api/ack", {"session": session_id, "participant": agent, "reply": "UNDERSTOOD"})
    assert call(ui, "/api/activate", {"session": session_id})["status"] == "active"


def test_a_conflict_is_surfaced_to_the_ui(ui, repo):
    _ready_draft(ui, repo)
    session_id = call(ui, "/api/start", {"draft": "d"})["session"]["session_id"]
    call(ui, "/api/ack", {"session": session_id, "participant": "claude", "reply": "UNDERSTOOD"})
    call(ui, "/api/ack", {"session": session_id, "participant": "codex",
                          "reply": "CONFLICT: push not granted"})

    contract = call(ui, f"/api/contract?session={session_id}")
    assert contract["acknowledgments"]["conflicts"][0]["reason"] == "push not granted"

    dashboard = call(ui, f"/api/dashboard?session={session_id}")
    assert dashboard["user_intervention_required"] is True


# ----------------------------------------------------------------------
# the dashboard tabs
# ----------------------------------------------------------------------


def _ready_draft(ui, repo, mode="long_horizon"):
    call(ui, "/api/draft/reset", {"draft": "d"})
    call(ui, "/api/draft", {"draft": "d", "mode": mode, "repo_path": str(repo),
                            "spec": SPEC, "deadline": "6 hours", "name": "UI session",
                            "agents": [
                                {"name": "claude", "runtime": "claude_code", "role": "primary_builder"},
                                {"name": "codex", "runtime": "codex", "role": "adversarial_reviewer"}]})


def _active(ui, repo):
    _ready_draft(ui, repo)
    session_id = call(ui, "/api/start", {"draft": "d"})["session"]["session_id"]
    for agent in ("claude", "codex"):
        call(ui, "/api/ack", {"session": session_id, "participant": agent, "reply": "UNDERSTOOD"})
    call(ui, "/api/activate", {"session": session_id})
    return session_id


def test_the_dashboard_carries_every_panel(ui, repo):
    session_id = _active(ui, repo)
    dashboard = call(ui, f"/api/dashboard?session={session_id}")

    assert dashboard["status"] == "active"
    assert dashboard["time_remaining"] and dashboard["phase"] == "exploration"
    assert dashboard["gates"]["summary"]["required"] == 1
    assert dashboard["changes"]["commits"] == 0
    assert dashboard["worktree_present"] is True
    assert dashboard["user_intervention_required"] is False


def test_the_human_can_speak_from_the_ui(ui, repo):
    session_id = _active(ui, repo)
    call(ui, "/api/message", {"session": session_id, "content": "focus on auth first",
                              "interrupt": True})
    conversation = call(ui, f"/api/conversation?session={session_id}")
    assert conversation["messages"][-1]["content"] == "focus on auth first"
    assert conversation["messages"][-1]["sender"] == "human"


def test_tests_and_changes_tabs_read_real_state(ui, repo):
    from pathlib import Path

    from synchri.session import worktree as worktree_module

    session_id = _active(ui, repo)
    tree = Path(call(ui, f"/api/session?session={session_id}")["worktree"]["path"])
    (tree / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tree / "test_x.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    worktree_module.git(tree, "add", "-A")
    worktree_module.git(tree, "-c", "user.email=a@e.com", "-c", "user.name=a",
                        "commit", "-qm", "add tests")

    result = call(ui, "/api/tests/run", {"session": session_id})
    assert result["passed"] == 1 and result["green"] is True

    changes = call(ui, f"/api/changes?session={session_id}")
    assert changes["commits"] == 1 and changes["recent"][0]["subject"] == "add tests"
    assert "test_x.py" in call(ui, f"/api/diff?session={session_id}")["diff"]


def test_memory_and_raw_tabs_expose_underlying_state(ui, repo):
    session_id = _active(ui, repo)
    assert "## Goal" in call(ui, f"/api/memory?session={session_id}")["markdown"]
    types = [e["event_type"] for e in call(ui, f"/api/events?session={session_id}")["events"]]
    assert "session.created" in types and "session.activated" in types


def test_gates_can_be_updated_from_the_ui(ui, repo):
    session_id = _active(ui, repo)
    call(ui, "/api/gate", {"session": session_id, "gate_id": "AUTH-01", "status": "pass",
                           "evidence": ["tests/test_auth.py::test_login"]})
    gates = call(ui, f"/api/gates?session={session_id}")
    assert gates["gates"][0]["status"] == "pass"
    assert "AUTH-01 has no builder sign-off" in gates["summary"]["blockers"]


# ----------------------------------------------------------------------
# human controls
# ----------------------------------------------------------------------


def test_pause_and_resume_from_the_ui(ui, repo):
    session_id = _active(ui, repo)
    call(ui, "/api/control", {"session": session_id, "action": "pause"})
    call(ui, "/api/control", {"session": session_id, "action": "resume"})


def test_changing_permissions_forces_re_acknowledgment(ui, repo):
    session_id = _active(ui, repo)
    permissions = call(ui, f"/api/session?session={session_id}")["permissions"]
    permissions["git.push"] = "allow"

    dashboard = call(ui, "/api/control", {"session": session_id, "action": "permissions",
                                          "permissions": permissions})
    assert dashboard["session"]["status"] == "awaiting_ack"
    assert dashboard["session"]["contract_revision"] == 2
    assert dashboard["acknowledgments"]["accepted"] == []


def test_stopping_from_the_ui_ends_the_session(ui, repo):
    session_id = _active(ui, repo)
    call(ui, "/api/control", {"session": session_id, "action": "stop", "reason": "changed my mind"})
    session = call(ui, f"/api/session?session={session_id}")
    assert session["status"] == "stopped" and session["ended_reason"] == "changed my mind"


def test_presets_can_be_saved_from_the_wizard(ui, repo):
    _ready_draft(ui, repo)
    result = call(ui, "/api/preset", {"draft": "d", "name": "UI Preset"})
    assert result["presets"][0]["name"] == "UI Preset"
    assert "spec" not in result["presets"][0]
