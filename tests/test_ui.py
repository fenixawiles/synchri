"""The local app: server security and the API behind the UI.

The UI is the one component that opens a socket, so its access control gets the
same scrutiny as the room's. Everything else here checks that the browser cannot
do anything the manager would refuse — the UI holds no rules of its own.
"""

from __future__ import annotations

import json
import errno
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest

from synchri.broker import Broker, Credential
from synchri.errors import SynchriError
from synchri.models.envelope import MessageDraft
from synchri.session.manager import SessionManager
from synchri.session.modes import ParticipantPlan
from synchri.ui.server import SynchriUIServer, create_server

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


def test_default_port_conflict_falls_back_to_an_available_loopback_port(workspace, monkeypatch):
    original_init = SynchriUIServer.__init__
    attempts = []

    def busy_once(server, address, *args, **kwargs):
        attempts.append(address)
        if len(attempts) == 1:
            raise OSError(errno.EADDRINUSE, "Address already in use")
        return original_init(server, address, *args, **kwargs)

    monkeypatch.setattr(SynchriUIServer, "__init__", busy_once)
    broker = Broker(workspace)
    try:
        server, url = create_server(broker)
        assert attempts == [("127.0.0.1", 8765), ("127.0.0.1", 0)]
        assert server.server_address[1] != 8765
        assert url.startswith("http://127.0.0.1:")
        server.server_close()
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
    assert [m["mode"] for m in boot["modes"]] == ["long_horizon"]
    assert any(c["key"] == "git.push" for g in boot["permissions"] for c in g["capabilities"])
    assert boot["sessions"] == [] and "workspace" in boot
    assert boot["github"]["authenticated"] is False


def test_the_client_uses_native_updates_only_from_the_packaged_desktop_app():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert "window.__TAURI__?.core?.invoke" in source
    assert 'nativeInvoke("check_for_update")' in source
    assert 'nativeInvoke("install_update")' in source
    assert "Download the signed Synchri update" in source


def test_the_client_uses_github_app_device_sign_in_without_a_cli_dependency():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert 'api("github/connect", {})' in source
    assert 'api("github/poll", {request_id: login.request_id})' in source
    assert "Enter this code in GitHub" in source
    assert "GitHub CLI" not in source


def test_the_client_separates_github_profile_sign_in_from_repository_access():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()

    assert 'id="github-account"' in source
    assert "Create your Synchri profile" in source
    assert "Choose repository access" in source
    assert "openRepositoryAccess" in source


def test_api_keeps_the_github_device_secret_out_of_the_browser(ui, monkeypatch):
    from synchri.session import github_auth

    monkeypatch.setattr(
        github_auth,
        "start_device_authorization",
        lambda: github_auth.DeviceAuthorization(
            device_code="secret-device-code", user_code="WXYZ-1234",
            verification_uri="https://github.com/login/device", expires_at=9_999_999_999, interval=5,
        ),
    )
    seen = []

    def complete(workspace, device_code):
        seen.append((workspace, device_code))
        return {"state": "connected"}

    monkeypatch.setattr(github_auth, "complete_device_authorization", complete)
    started = call(ui, "/api/github/connect", {})

    assert "device_code" not in started
    assert started["request_id"]
    result = call(ui, "/api/github/poll", {"request_id": started["request_id"]})
    assert result == {"state": "connected"}
    assert seen[0][1] == "secret-device-code"


def test_local_repositories_do_not_wait_for_github(ui, monkeypatch):
    from synchri.session import discovery

    monkeypatch.setattr(discovery, "local_repositories", lambda: [{"name": "fast", "path": "/fast"}])
    monkeypatch.setattr(
        discovery, "github_available", lambda: pytest.fail("GitHub lookup ran on the local pass")
    )
    result = call(ui, "/api/repositories?github=0")
    assert result["local"] == [{"name": "fast", "path": "/fast"}]
    assert result["github"] == [] and result["github_available"] is False


def test_agents_step_has_a_real_save_action_and_footer_navigation():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert "actions.append(add, save);" in source
    assert "box.append($(`<div class=\"row\"></div>`)).lastChild" not in source
    assert "if (S.step === \"agents\" && S.agentDraft)" in source


def test_every_session_start_routes_to_the_agent_setup_prompts():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert "function showLaunch(result)" in source
    assert "showLaunch(r);" in source
    assert "Start my agents" in source
    assert "function renderExternalSetup(l)" in source
    assert 'api("managed/start", {session:S.session})' in source
    assert "function openLaunch(id)" in source


def test_live_updates_repaint_only_the_chat_surface():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert "if (what.includes(\"sessions\")) {\n          renderSession();" in source
    assert "refreshSessionChrome();" in source
    assert '<div class="message-list">${messages}</div>' in source
    assert "list.scrollTo({ top: list.scrollHeight, behavior: \"smooth\" })" in source


def test_completed_session_is_read_only_and_exposes_its_changelog():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert 'tab !== "changelog" || s.status === "complete"' in source
    assert 'd.session.status !== "active"' in source
    assert "Session complete — the final changelog is available in the rail." in source


def test_an_incidental_non_repository_cwd_is_not_preselected(workspace, tmp_path):
    from synchri.ui.api import Api

    broker = Broker(workspace)
    try:
        api = Api(broker, SessionManager(broker), default_repo=str(tmp_path))
        assert api.bootstrap({}, {})["default_repo"] is None
    finally:
        broker.close()


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


def test_quick_start_returns_paste_ready_agent_setup_and_live_arrival_state(ui, repo):
    result = call(ui, "/api/quick-start", {
        "repo_path": str(repo),
        "goal": "Review the current change together.",
        "agents": [
            {"name": "codex", "runtime": "codex", "role": "primary_builder"},
            {"name": "copilot", "runtime": "copilot", "role": "adversarial_reviewer"},
        ],
    })

    session_id = result["session"]["session_id"]
    launch = result["launch"]
    assert result["session"]["mode"] == "long_horizon"
    assert launch["joined_count"] == 0
    assert launch["agents"][0]["join_command"].startswith("cd ")
    assert "synchri join " in launch["agents"][0]["setup_prompt"]
    assert f"synchri session contract --session {session_id}" in launch["agents"][0]["setup_prompt"]
    assert f"synchri session ack codex --reply UNDERSTOOD --session {session_id}" in launch["agents"][0]["setup_prompt"]
    assert "--watch-messages" in launch["agents"][0]["setup_prompt"]
    assert "do not send status updates" in launch["agents"][0]["setup_prompt"]

    for agent in launch["agents"]:
        token = agent["join_command"].split("synchri join ", 1)[1].split(" --name", 1)[0]
        ui["broker"].join(token, agent["name"])

    refreshed = call(ui, f"/api/launch?session={session_id}")["launch"]
    assert refreshed["joined_count"] == 2
    assert all(agent["joined"] for agent in refreshed["agents"])
    assert not refreshed["ready_to_activate"]

    for name in ("codex", "copilot"):
        call(ui, "/api/ack", {"session": session_id, "participant": name, "reply": "UNDERSTOOD"})
    assert call(ui, f"/api/launch?session={session_id}")["launch"]["ready_to_activate"] is True


def test_external_prompt_uses_the_packaged_helper_not_the_agents_path(ui, repo):
    from synchri.runner.managed import ManagedRunnerRegistry

    helper = "/Applications/Synchri.app/Contents/MacOS/Synchri"
    ui["server"].api.managed = ManagedRunnerRegistry(ui["broker"].workspace, cli_command=helper)
    result = call(ui, "/api/quick-start", {
        "repo_path": str(repo),
        "goal": "Review the repository.",
        "agents": [
            {"name": "codex", "runtime": "codex", "role": "primary_builder"},
            {"name": "copilot", "runtime": "generic", "role": "adversarial_reviewer"},
        ],
    })
    agent = result["launch"]["agents"][0]

    assert f"&& {helper} join " in agent["join_command"]
    assert helper + " session contract" in agent["setup_prompt"]
    assert helper + " wait" in agent["setup_prompt"]


def test_workflows_are_reusable_and_can_be_renamed_or_deleted(ui):
    workflow = call(ui, "/api/draft/reset", {"draft": "workflow"})
    assert workflow["draft"]["mode"] == "long_horizon"
    call(ui, "/api/draft", {"draft": "workflow", "agents": [
        {"name": "codex", "runtime": "codex", "role": "primary_builder"},
        {"name": "copilot", "runtime": "copilot", "role": "adversarial_reviewer"},
    ], "deadline": "2 hours"})
    saved = call(ui, "/api/preset", {"draft": "workflow", "name": "Steady build"})
    assert saved["presets"][0]["name"] == "Steady build"
    assert saved["presets"][0]["deadline_duration"] == "2 hours"

    renamed = call(ui, "/api/preset/rename", {"name": "Steady build", "new_name": "Ship it"})
    assert [item["name"] for item in renamed["presets"]] == ["Ship it"]
    deleted = call(ui, "/api/preset/delete", {"name": "Ship it"})
    assert deleted["presets"] == []


def test_quick_start_uses_the_saved_workflow_instead_of_reasking_for_agents(ui, repo):
    call(ui, "/api/draft/reset", {"draft": "workflow"})
    call(ui, "/api/draft", {"draft": "workflow", "agents": [
        {"name": "builder", "runtime": "codex", "role": "primary_builder"},
        {"name": "reviewer", "runtime": "copilot", "role": "adversarial_reviewer"},
    ]})
    call(ui, "/api/preset", {"draft": "workflow", "name": "Saved pair"})

    result = call(ui, "/api/quick-start", {
        "repo_path": str(repo), "goal": "Ship the requested change.", "preset": "Saved pair",
    })
    assert [agent["name"] for agent in result["launch"]["agents"]] == ["builder", "reviewer"]


def test_managed_start_attaches_agrees_and_begins_without_pasted_prompts(ui, repo, tmp_path):
    """The local default must prove each real transition, not pretend it happened."""
    import sys

    agent = tmp_path / "managed_agent.py"
    agent.write_text(
        "import sys\n"
        "prompt = sys.argv[1]\n"
        "if 'output exactly UNDERSTOOD' in prompt:\n"
        "    print('UNDERSTOOD')\n"
        "else:\n"
        "    print('I inspected the worktree and have begun.')\n"
        "    print('SYNCHRI-PASS')\n",
        encoding="utf-8",
    )
    command = f"{sys.executable} {agent} {{prompt}}"
    started = call(ui, "/api/quick-start", {
        "repo_path": str(repo),
        "goal": "Inspect the repository.",
        "agents": [{
            "name": "builder", "runtime": "generic", "role": "primary_builder",
            "command": command,
        }, {
            "name": "reviewer", "runtime": "generic", "role": "adversarial_reviewer",
            "command": command,
        }],
    })
    session_id = started["session"]["session_id"]
    launch = started["launch"]
    assert launch["managed"]["readiness"]["available"] is True
    assert launch["agents"][0]["managed_ready"] is True

    call(ui, "/api/managed/start", {"session": session_id})
    for _ in range(100):
        managed = call(ui, f"/api/managed?session={session_id}")["managed"]
        if managed["phase"] in {"waiting", "failed", "needs_attention"}:
            break
        time.sleep(0.03)
    assert managed["phase"] == "waiting", managed
    assert managed["reason"] == "idle"

    refreshed = call(ui, f"/api/launch?session={session_id}")["launch"]
    assert refreshed["agents"][0]["joined"] is True
    assert refreshed["agents"][0]["acknowledged"] is True
    conversation = call(ui, f"/api/conversation?session={session_id}")["messages"]
    assert conversation[0]["metadata"]["source"] == "session_activation"
    assert conversation[-1]["message_type"] == "pass"


def test_quick_start_resolves_a_github_reference_before_making_the_room(ui, repo, monkeypatch):
    from synchri.session import discovery

    resolved = {
        "path": str(repo), "name": "proj", "source": "fenixawiles/synchri",
        "cloned": False, "reused": True,
    }
    monkeypatch.setattr(discovery, "resolve_github_repository", lambda reference: resolved)
    result = call(ui, "/api/quick-start", {
        "repo_path": "fenixawiles/synchri",
        "goal": "Review the current change together.",
        "agents": [
            {"name": "codex", "runtime": "codex", "role": "primary_builder"},
            {"name": "copilot", "runtime": "copilot", "role": "adversarial_reviewer"},
        ],
    })

    assert result["repository"] == resolved
    assert result["session"]["repository"]["root"] == str(repo.resolve())


def test_reusing_a_repository_source_creates_distinct_session_worktrees(ui, repo):
    agents = [
        {"name": "codex", "runtime": "codex", "role": "primary_builder"},
        {"name": "copilot", "runtime": "copilot", "role": "adversarial_reviewer"},
    ]
    first = call(ui, "/api/quick-start", {
        "repo_path": str(repo), "goal": "Build the first feature.", "agents": agents,
    })
    second = call(ui, "/api/quick-start", {
        "repo_path": str(repo), "goal": "Build a different feature.", "agents": agents,
    })

    assert first["session"]["repository"]["root"] == second["session"]["repository"]["root"]
    assert first["session"]["worktree"]["path"] != second["session"]["worktree"]["path"]
    assert first["session"]["worktree"]["branch"] != second["session"]["worktree"]["branch"]


def test_existing_worktrees_can_be_selected_for_a_new_session(ui, repo):
    agents = [
        {"name": "codex", "runtime": "codex", "role": "primary_builder"},
        {"name": "copilot", "runtime": "copilot", "role": "adversarial_reviewer"},
    ]
    first = call(ui, "/api/quick-start", {
        "repo_path": str(repo), "goal": "Build the first feature.", "agents": agents,
    })
    choices = call(ui, f"/api/worktrees?repo={repo}")

    assert choices["repository"] == str(repo.resolve())
    assert len(choices["worktrees"]) == 1
    choice = choices["worktrees"][0]
    assert choice["name"] == first["session"]["worktree"]["name"]
    assert choice["path"] == first["session"]["worktree"]["path"]
    assert choice["branch"] == first["session"]["worktree"]["branch"]

    second = call(ui, "/api/quick-start", {
        "repo_path": str(repo), "goal": "Review the existing feature.", "agents": agents,
        "existing_worktree_path": first["session"]["worktree"]["path"],
    })

    assert second["session"]["worktree"]["path"] == first["session"]["worktree"]["path"]
    assert second["session"]["metadata"]["worktree_strategy"] == "existing"


def test_bootstrap_exposes_understandable_permission_profiles(ui):
    profiles = call(ui, "/api/bootstrap")["permission_profiles"]
    by_key = {profile["key"]: profile for profile in profiles}
    assert by_key["important_only"]["label"] == "Ask only if it’s important"
    assert by_key["god_mode"]["warning"]


def test_dashboard_exposes_the_active_turn_for_agent_task_timing(ui, repo):
    started = call(ui, "/api/quick-start", {
        "repo_path": str(repo), "goal": "Build this.",
        "agents": [
            {"name": "codex", "runtime": "codex", "role": "primary_builder"},
            {"name": "copilot", "runtime": "copilot", "role": "adversarial_reviewer"},
        ],
    })
    session_id = started["session"]["session_id"]
    record = started["session"]
    for agent in started["launch"]["agents"]:
        token = agent["join_command"].split("synchri join ", 1)[1].split(" --name", 1)[0]
        ui["broker"].join(token, agent["name"])
        call(ui, "/api/ack", {"session": session_id, "participant": agent["name"], "reply": "UNDERSTOOD"})
    call(ui, "/api/activate", {"session": session_id})

    dashboard = call(ui, f"/api/dashboard?session={session_id}")
    assert dashboard["current_actor"] == "codex"
    assert dashboard["active_turn"]["participant_name"] == "codex"
    assert dashboard["active_turn"]["started_at"]


def test_conversation_ui_contains_task_states_timebox_resolution_and_inline_decisions():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert "Planning the next step. This can take a few minutes." in source
    assert "data-task-started" in source
    assert "Add 30 min" in source and "agent work will not stop" in source
    assert "Approve</button>" in source and "Deny</button>" in source
    assert "Here’s how to continue" in source


def test_the_ui_cannot_activate_before_acknowledgment(ui, repo):
    _ready_draft(ui, repo)
    session_id = call(ui, "/api/start", {"draft": "d"})["session"]["session_id"]

    with pytest.raises(urllib.error.HTTPError) as exc:
        call(ui, "/api/activate", {"session": session_id})
    assert json.loads(exc.value.read())["error"]["code"] == "awaiting_acknowledgment"

    launch = call(ui, f"/api/launch?session={session_id}")["launch"]
    for agent in launch["agents"]:
        token = agent["join_command"].split("synchri join ", 1)[1].split(" --name", 1)[0]
        ui["broker"].join(token, agent["name"])
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


def _active_with_credentials(ui, repo):
    _ready_draft(ui, repo)
    started = call(ui, "/api/start", {"draft": "d"})
    session_id = started["session"]["session_id"]
    launch = call(ui, f"/api/launch?session={session_id}")["launch"]
    credentials = {}
    for agent in launch["agents"]:
        token = agent["join_command"].split("synchri join ", 1)[1].split(" --name", 1)[0]
        joined = ui["broker"].join(token, agent["name"])
        credentials[agent["name"]] = Credential(agent["name"], secret=joined["secret"])
    for agent in ("claude", "codex"):
        call(ui, "/api/ack", {"session": session_id, "participant": agent, "reply": "UNDERSTOOD"})
    call(ui, "/api/activate", {"session": session_id})
    return session_id, credentials


def _active(ui, repo):
    return _active_with_credentials(ui, repo)[0]


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


def test_human_reply_returns_to_the_agent_waiting_on_permission(ui, repo):
    session_id, credentials = _active_with_credentials(ui, repo)
    session = call(ui, f"/api/session?session={session_id}")

    # Claude finishes its active turn by requesting a human decision. That
    # leaves a targeted human turn active, so the UI must not fall back to the
    # primary-builder default when the human answers.
    ui["broker"].send(
        session["room_id"],
        credential=credentials["claude"],
        draft=MessageDraft(
            content="I need approval before I can continue.",
            message_type="response",
            response_status="blocked",
            target="human",
        ),
    )

    result = call(ui, "/api/message", {"session": session_id, "content": "Approved."})
    assert result["routed_to"] == "claude"
    assert result["message"]["target"] == "claude"
    assert result["next_speaker"] == "claude"


def test_inline_approval_grants_only_the_named_ask_capability_and_routes_the_reply(ui, repo):
    session_id, credentials = _active_with_credentials(ui, repo)
    session = call(ui, f"/api/session?session={session_id}")
    ui["broker"].send(
        session["room_id"],
        credential=credentials["claude"],
        draft=MessageDraft(
            content="I need permission to install the package.",
            message_type="response",
            response_status="blocked",
            target="human",
            metadata={
                "approval_capability": "repo.install_deps",
                "approval_request": "Install the missing development dependency.",
            },
        ),
    )

    result = call(ui, "/api/approval", {
        "session": session_id,
        "target": "claude",
        "approved": True,
        "capability": "repo.install_deps",
        "detail": "Install the missing development dependency.",
    })

    assert result["routed_to"] == "claude"
    assert "external controls still apply" in result["message"]["content"]
    manager = SessionManager(ui["broker"])
    assert manager.check_permission(session_id, "repo.install_deps") is True


def test_resolved_permission_request_does_not_capture_later_human_direction(ui, repo):
    session_id, credentials = _active_with_credentials(ui, repo)
    session = call(ui, f"/api/session?session={session_id}")
    ui["broker"].send(
        session["room_id"],
        credential=credentials["claude"],
        draft=MessageDraft(
            content="I need approval.",
            message_type="response",
            response_status="blocked",
            target="human",
        ),
    )
    call(ui, "/api/message", {"session": session_id, "content": "Approved."})
    ui["broker"].pass_turn(
        session["room_id"], credential=credentials["claude"], reason="approval applied"
    )

    result = call(ui, "/api/message", {"session": session_id, "content": "Now prioritize auth."})

    assert result["routed_to"] == "claude"
    assert result["message"]["metadata"]["human_direction"]["reviewer"] == "codex"


def test_new_human_direction_starts_with_builder_even_when_reviewer_has_the_floor(ui, repo):
    session_id, credentials = _active_with_credentials(ui, repo)
    session = call(ui, f"/api/session?session={session_id}")

    # Move the conversation to the reviewer. A fresh human direction must not
    # accidentally go to whoever happened to be working at that moment.
    ui["broker"].send(
        session["room_id"],
        credential=credentials["claude"],
        draft=MessageDraft(content="Please review.", message_type="task", target="codex"),
    )
    result = call(ui, "/api/message", {"session": session_id, "content": "Prioritize auth."})

    assert result["routed_to"] == "claude"
    direction = result["message"]["metadata"]["human_direction"]
    assert direction == {"lead": "claude", "reviewer": "codex"}
    assert result["next_speaker"] == "claude"


def test_an_explicit_human_recipient_keeps_control_of_the_route(ui, repo):
    session_id = _active(ui, repo)
    result = call(
        ui,
        "/api/message",
        {"session": session_id, "content": "Review this directly.", "target": "codex"},
    )

    assert result["routed_to"] == "codex"
    assert "human_direction" not in result["message"]["metadata"]


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
    paused = call(ui, "/api/control", {"session": session_id, "action": "pause"})
    assert paused["session"]["status"] == "paused"
    resumed = call(ui, "/api/control", {"session": session_id, "action": "resume"})
    assert resumed["session"]["status"] == "active"


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


def test_completing_from_the_ui_closes_the_room_and_exposes_the_changelog(ui, repo):
    session_id = _active(ui, repo)
    call(ui, "/api/gate", {
        "session": session_id,
        "gate_id": "AUTH-01",
        "status": "pass",
        "evidence": ["tests/test_auth.py::test_login"],
        "builder_assessment": "implemented",
        "reviewer_assessment": "reviewed independently",
    })

    dashboard = call(ui, "/api/control", {"session": session_id, "action": "complete"})
    assert dashboard["session"]["status"] == "complete"
    changelog = call(ui, f"/api/changelog?session={session_id}")
    assert "# Synchri final changelog" in changelog["markdown"]
    assert "AUTH-01" in changelog["markdown"]


def test_presets_can_be_saved_from_the_wizard(ui, repo):
    _ready_draft(ui, repo)
    result = call(ui, "/api/preset", {"draft": "d", "name": "UI Preset"})
    assert result["presets"][0]["name"] == "UI Preset"
    assert "spec" not in result["presets"][0]


# ----------------------------------------------------------------------
# persisted drafts: an unfinished wizard survives, and tabs stay in step
# ----------------------------------------------------------------------


def test_an_unfinished_wizard_survives_a_restart(workspace, repo):
    """Closing the app must not lose a half-filled wizard."""
    first = Broker(workspace)
    try:
        api = __import__("synchri.ui.api", fromlist=["Api"]).Api(first, SessionManager(first))
        api.update_draft({}, {"draft": "d", "mode": "long_horizon", "repo_path": str(repo),
                              "spec": SPEC, "deadline": "6 hours", "name": "half done"})
    finally:
        first.close()

    second = Broker(workspace)
    try:
        api = __import__("synchri.ui.api", fromlist=["Api"]).Api(second, SessionManager(second))
        state = api.get_draft({"draft": "d"}, {})
        assert state["draft"]["mode"] == "long_horizon"
        assert state["draft"]["repo_path"] == str(repo.resolve())
        assert state["draft"]["spec"] == SPEC
        assert state["draft"]["name"] == "half done"
        # No agents were chosen before the "restart", so the wizard reopens
        # knowing exactly what is still missing.
        assert state["ready"] is False
        assert any("agent" in p for p in state["problems"])
    finally:
        second.close()


def test_two_tabs_on_one_draft_see_the_same_state(ui, repo):
    """The second tab must not diverge from the first."""
    call(ui, "/api/draft/reset", {"draft": "shared"})
    first = call(ui, "/api/draft", {"draft": "shared", "mode": "long_horizon"})
    second = call(ui, f"/api/draft?draft=shared")
    assert second["draft"]["mode"] == "long_horizon"
    assert second["version"] == first["version"]

    # Tab A edits; tab B sees a new version and the new value.
    edited = call(ui, "/api/draft", {"draft": "shared", "repo_path": str(repo)})
    seen = call(ui, "/api/draft?draft=shared")
    assert seen["version"] == edited["version"] > first["version"]
    assert seen["draft"]["repo_path"] == str(repo.resolve())


def test_drafts_are_isolated_by_id(ui, repo):
    call(ui, "/api/draft", {"draft": "one", "mode": "long_horizon"})
    call(ui, "/api/draft", {"draft": "two", "mode": "review_audit"})
    assert call(ui, "/api/draft?draft=one")["draft"]["mode"] == "long_horizon"
    assert call(ui, "/api/draft?draft=two")["draft"]["mode"] == "review_audit"


def test_starting_a_session_clears_its_draft(ui, repo):
    _ready_draft(ui, repo)
    assert call(ui, "/api/draft?draft=d")["draft"]["mode"] == "long_horizon"
    call(ui, "/api/start", {"draft": "d"})
    assert call(ui, "/api/draft?draft=d")["draft"]["mode"] == "long_horizon"


def test_a_permission_edit_does_not_corrupt_the_draft_id(ui, repo):
    """Regression: the permissions loop once shadowed the draft key."""
    call(ui, "/api/draft", {"draft": "perm", "mode": "long_horizon"})
    call(ui, "/api/draft", {"draft": "perm", "permissions": {"git.push": "allow"}})
    reloaded = call(ui, "/api/draft?draft=perm")
    assert reloaded["draft"]["permissions"]["git.push"] == "allow"
    assert reloaded["draft"]["mode"] == "long_horizon", "the draft was saved under its own id"


def test_a_restored_draft_keeps_an_elapsed_timebox(workspace, repo):
    from synchri.session.draft import SessionDraft

    state = {
        "mode": "long_horizon",
        "repo_path": str(repo),
        "deadline": {"ends_at": "2000-01-01T00:00:00.000Z",
                     "started_at": "1999-01-01T00:00:00.000Z", "source": "fixed"},
    }
    restored = SessionDraft.from_state(state)
    assert restored.deadline is not None
    assert restored.mode == "long_horizon"


def test_a_restored_draft_tolerates_a_vanished_repository(tmp_path):
    from synchri.session.draft import SessionDraft

    restored = SessionDraft.from_state(
        {"mode": "long_horizon", "repo_path": str(tmp_path / "deleted")}
    )
    assert restored.repo_path is None, "reopen the step rather than fail to load"
    assert restored.mode == "long_horizon"


def test_draft_ids_are_validated(ui):
    with pytest.raises(urllib.error.HTTPError):
        call(ui, "/api/draft", {"draft": "../escape", "mode": "long_horizon"})


# ----------------------------------------------------------------------
# server-sent events: the client stops polling
# ----------------------------------------------------------------------


def read_events(url, token, *, count, timeout=25):
    """Collect `count` SSE frames, or as many as arrive before the timeout."""
    request = urllib.request.Request(url)
    request.add_header("X-Synchri-Token", token)
    request.add_header("Accept", "text/event-stream")
    collected, current = [], {}
    response = urllib.request.urlopen(request, timeout=timeout)
    try:
        for raw in response:
            line = raw.decode().rstrip("\n")
            if line.startswith("event:"):
                current["event"] = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                current["data"] = json.loads(line.split(":", 1)[1].strip())
            elif line == "" and current:
                collected.append(current)
                current = {}
                if len(collected) >= count:
                    break
    finally:
        response.close()
    return collected


def test_the_stream_announces_itself_then_pushes_changes(ui, repo):
    session_id = _active(ui, repo)
    url = f"{ui['base']}/api/stream?session={session_id}"
    received = []
    error = []

    def listen():
        try:
            received.extend(read_events(url, ui["token"], count=2))
        except Exception as exc:  # pragma: no cover - reported by the assert
            error.append(exc)

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    threading.Event().wait(1.0)  # let the stream take its first fingerprint

    # Something the dashboard shows changes.
    call(ui, "/api/message", {"session": session_id, "content": "a new message"})
    thread.join(timeout=25)

    assert not error, error
    assert received[0]["event"] == "ready"
    assert received[1]["event"] == "changed"
    assert "conversation" in received[1]["data"]["what"]


def test_the_stream_reports_session_changes(ui, repo):
    session_id = _active(ui, repo)
    url = f"{ui['base']}/api/stream?session={session_id}"
    received, error = [], []

    def listen():
        try:
            received.extend(read_events(url, ui["token"], count=2))
        except Exception as exc:  # pragma: no cover
            error.append(exc)

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    threading.Event().wait(1.0)
    # Changing permissions issues a new contract revision, which is a change to
    # the session record itself. (Pausing changes the *room*, and surfaces as a
    # conversation change instead.)
    permissions = call(ui, f"/api/session?session={session_id}")["permissions"]
    permissions["git.push"] = "allow"
    call(ui, "/api/control", {"session": session_id, "action": "permissions",
                              "permissions": permissions})
    thread.join(timeout=25)

    assert not error, error
    assert "sessions" in received[1]["data"]["what"]


def test_the_stream_reports_a_draft_edited_in_another_tab(ui, repo):
    """This is what keeps two tabs in step."""
    url = f"{ui['base']}/api/stream"
    received, error = [], []

    def listen():
        try:
            received.extend(read_events(url, ui["token"], count=2))
        except Exception as exc:  # pragma: no cover
            error.append(exc)

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    threading.Event().wait(1.0)
    call(ui, "/api/draft", {"draft": "default", "mode": "long_horizon"})
    thread.join(timeout=25)

    assert not error, error
    assert "drafts" in received[1]["data"]["what"]


def test_the_stream_requires_the_token(ui):
    with pytest.raises(urllib.error.HTTPError) as exc:
        read_events(f"{ui['base']}/api/stream", "wrong-token", count=1, timeout=10)
    assert exc.value.code == 401


def test_the_client_uses_eventsource_and_never_polls_the_api(ui):
    with urllib.request.urlopen(ui["url"], timeout=20) as response:
        page = response.read().decode()
    assert "new EventSource(" in page
    assert "setInterval(() => api" not in page, "the dashboard must be push-driven, not polled"
    assert "setInterval(tick, 1000)" in page, "the local timebox clock updates once a second"


def test_a_quiet_stream_stays_open(ui, repo):
    """No change means no event -- but the connection must not be dropped."""
    _active(ui, repo)
    request = urllib.request.Request(f"{ui['base']}/api/stream")
    request.add_header("X-Synchri-Token", ui["token"])
    response = urllib.request.urlopen(request, timeout=10)
    try:
        first = response.readline().decode()
        assert first.startswith("event: ready")
        assert not response.closed
    finally:
        response.close()
