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
    assert [m["mode"] for m in boot["modes"]] == ["long_horizon", "planning"]
    assert any(c["key"] == "git.push" for g in boot["permissions"] for c in g["capabilities"])
    assert boot["sessions"] == [] and "workspace" in boot
    assert boot["github"]["authenticated"] is False
    runtimes = {runtime["key"]: runtime for runtime in boot["runtimes"]}
    assert runtimes["claude_code"]["connection_test_available"] is True
    assert runtimes["copilot"]["connection_test_available"] is True
    assert runtimes["claude_code"]["tool_permissions"]["state"] in {"pass", "fail"}
    assert "/permissions" in runtimes["claude_code"]["tool_permissions"]["instructions"]
    assert boot["runtime_connection_tests"]["claude_code"]["state"] == "not_connected"
    assert boot["appearance"] == {"theme": ""}


def test_an_unverifiable_runtime_cannot_start_an_impossible_connection_test(ui):
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(ui, "/api/runtimes/connect", {"runtime": "gemini"})
    payload = json.loads(exc.value.read())
    assert payload["error"]["code"] == "validation_error"
    assert "cannot be marked connected" in payload["error"]["message"]


def test_connection_ui_distinguishes_readiness_from_connected_state():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert "SETUP CHECKLIST" in source
    assert "CONNECTION TEST RESULTS" in source
    assert "Not connected yet" in source
    assert "Testing connection" in source
    assert "Connection failed" in source
    assert "Connection verified" in source
    assert "The protected canary intentionally used no tools." in source
    assert "tool permissions still need setup" in source
    assert "One step remains: run the protected connection test." in source
    assert "Nothing you can do locally will verify this connection" in source
    assert "A previously verified part of this connection changed" in source
    assert source.count("renderAgentConnections(") == 3  # definition + first-run and regular homes
    assert 'button.textContent = "Connection verified"' in source
    assert 'if (S.view === "home") await home();' in source
    assert "Re-run connection test" not in source


def test_theme_persists_across_ephemeral_loopback_origins(ui):
    selected = call(ui, "/api/appearance", {"theme": "sage"})
    assert selected == {"appearance": {"theme": "sage"}}
    assert call(ui, "/api/bootstrap")["appearance"]["theme"] == "sage"

    # A desktop relaunch gets a different loopback port (a different browser
    # origin). The server must still inject the database-backed choice before
    # first paint instead of relying on origin-scoped localStorage.
    second, url = create_server(ui["broker"], port=0)
    thread = threading.Thread(target=second.serve_forever, daemon=True)
    thread.start()
    try:
        assert second.server_address[1] != ui["server"].server_address[1]
        with urllib.request.urlopen(url, timeout=20) as response:
            body = response.read().decode()
        assert 'const savedTheme = "sage";' in body
    finally:
        second.shutdown()
        second.server_close()

    cleared = call(ui, "/api/appearance", {"theme": ""})
    assert cleared == {"appearance": {"theme": ""}}
    assert call(ui, "/api/bootstrap")["appearance"]["theme"] == ""


def test_theme_endpoint_rejects_values_that_cannot_be_injected(ui):
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(ui, "/api/appearance", {"theme": 'sage";alert(1)//'})
    assert json.loads(exc.value.read())["error"]["code"] == "validation_error"

    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert "localStorage" not in source
    assert 'api("appearance", {theme:key})' in source


def test_the_client_uses_native_updates_only_from_the_packaged_desktop_app():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert "window.__TAURI__?.core?.invoke" in source
    assert 'nativeInvoke("check_for_update")' in source
    assert 'nativeInvoke("install_update")' in source
    assert "Download the signed Synchri update" in source
    assert "Check for updates" in source
    assert "Install Synchri in Applications" in source
    assert "move_to_applications" in source


def test_the_client_uses_github_app_device_sign_in_without_a_cli_dependency():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert 'api("github/connect", {})' in source
    assert 'api("github/poll", {request_id: login.request_id})' in source
    assert "Enter this code in GitHub" in source
    assert 'nativeInvoke("open_github_url", {url: destination})' in source
    assert "GitHub CLI" not in source


def test_the_client_refreshes_a_stale_github_sign_in_click_instead_of_reauthorizing():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()

    assert 'if (login.state === "connected")' in source
    assert "is already signed in." in source


def test_the_client_browses_github_without_the_broken_installation_page():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()

    assert 'id="github-account"' in source
    assert "Create your Synchri profile" in source
    assert "Your repositories are loading now." in source
    assert "openRepositoryAccess" not in source
    assert "installations/new" not in source
    assert "Repository link or local folder" in source
    assert "Paste a GitHub URL, owner/repository" in source
    assert "GITHUB ACCOUNT" in source
    assert "Disconnect GitHub" in source


def test_session_dashboard_offers_a_bounded_whole_session_restart():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert 'id="c-restart">Restart session' in source
    assert 'runSessionControl(restart, "restart")' in source
    assert "Conversation, contract, worktree, and files stay exactly where they are." in source


def test_the_packaged_engine_includes_the_public_root_store_for_github_tls():
    from pathlib import Path

    build = (Path(__file__).parents[1] / "scripts" / "build_tauri_sidecar.sh").read_text()
    assert "--collect-data certifi" in build


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


def test_api_does_not_restart_github_authorization_for_an_existing_profile(ui, monkeypatch):
    from synchri.session import discovery, github_auth

    monkeypatch.setattr(
        discovery,
        "github_status",
        lambda _workspace: {"authenticated": True, "account": {"login": "fenixawiles"}},
    )
    monkeypatch.setattr(
        github_auth,
        "start_device_authorization",
        lambda: pytest.fail("already-connected account restarted device authorization"),
    )

    result = call(ui, "/api/github/connect", {})

    assert result == {
        "state": "connected",
        "already_connected": True,
        "account": {"login": "fenixawiles"},
    }


def test_local_repositories_do_not_wait_for_github(ui, monkeypatch):
    from synchri.session import discovery

    monkeypatch.setattr(discovery, "local_repositories", lambda: [{"name": "fast", "path": "/fast"}])
    monkeypatch.setattr(
        discovery, "github_available", lambda: pytest.fail("GitHub lookup ran on the local pass")
    )
    result = call(ui, "/api/repositories?github=0")
    assert result["local"] == [{"name": "fast", "path": "/fast"}]
    assert result["github"] == [] and result["github_available"] is False


def test_a_github_api_failure_is_distinguishable_from_missing_repository_access(ui, monkeypatch):
    """A signed-in user with a failing API must not see the silent empty list.

    Before this, an API fault produced the identical payload shape as "the app
    is not installed anywhere", so the chooser told people to grant access they
    may already have granted.
    """
    from synchri.errors import StateError
    from synchri.session import discovery

    signed_in = {
        "installed": True,
        "authenticated": True,
        "account": {"login": "fenixawiles"},
        "message": "GitHub is signed in. Repository access is managed separately.",
    }
    monkeypatch.setattr(discovery, "github_status", lambda workspace=None: signed_in)

    def unavailable(workspace=None):
        raise StateError(
            "GitHub could not load your repositories. Try again in a moment.",
            code="github_unavailable",
        )

    monkeypatch.setattr(discovery, "github_repository_access", unavailable)
    result = call(ui, "/api/repositories")

    assert result["github"] == []
    assert result["github_available"] is False
    assert result["github_access"]["unavailable"] is True
    assert "could not load your repositories" in result["github_access"]["message"]


def test_the_quick_start_surfaces_github_repository_errors_with_a_retry():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert "github_access?.unavailable" in source
    assert 'id="quick-retry-github"' in source


def test_github_api_requests_pin_a_published_api_version():
    """An unsupported X-GitHub-Api-Version makes every API call fail with 400,
    which the chooser used to render as a permanently empty repository list."""
    from synchri.session import github_auth

    assert github_auth.GITHUB_API_VERSION == "2022-11-28"


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
    assert "function startManaged(participants)" in source
    assert 'api("managed/start", payload)' in source
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


def test_the_chat_input_is_a_wrapping_composer():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert '<textarea id="mm"' in source
    assert '<input id="mm"' not in source
    assert 'event.key === "Enter" && !event.shiftKey && !event.isComposing' in source
    assert "Shift+Enter for a new line" in source
    assert "Math.min(input.scrollHeight, 160)" in source
    # The draft/caret rescue across SSE repaints survives the element swap.
    assert "const draftValue = previousInput?.value" in source
    assert "input.setSelectionRange(caretStart, caretEnd)" in source


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


def test_stop_interrupts_the_agreement_phase(ui, repo, tmp_path):
    """Stop during attach/agree must end the run promptly, not after the CLI timeout."""
    import sys

    agent = tmp_path / "slow_agree.py"
    agent.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
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
    call(ui, "/api/managed/start", {"session": session_id})
    for _ in range(300):
        managed = call(ui, f"/api/managed?session={session_id}")["managed"]
        if managed["phase"] == "agreeing":
            break
        time.sleep(0.03)
    assert managed["phase"] == "agreeing", managed

    call(ui, "/api/control", {"session": session_id, "action": "stop"})
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        managed = call(ui, f"/api/managed?session={session_id}")["managed"]
        if managed["phase"] == "stopped" and not managed["alive"]:
            break
        time.sleep(0.05)
    assert managed["phase"] == "stopped", managed
    assert managed["reason"] == "cancelled"
    assert call(ui, f"/api/session?session={session_id}")["status"] == "stopped"


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


def test_file_diff_endpoint_serves_live_per_file_diffs(ui, repo):
    from pathlib import Path

    session_id = _active(ui, repo)
    worktree = call(ui, f"/api/session?session={session_id}")["worktree"]["path"]
    Path(worktree, "README.md").write_text("hi\nlive edit\n", encoding="utf-8")

    r = call(ui, f"/api/diff/file?session={session_id}&path=README.md")
    assert "+live edit" in r["diff"]
    assert r["insertions"] == 1 and r["deletions"] == 0

    with pytest.raises(urllib.error.HTTPError) as exc:
        call(ui, f"/api/diff/file?session={session_id}&path=..%2Fsecrets.txt")
    assert exc.value.code == 400


def test_changes_files_endpoint_lists_per_file_cards(ui, repo):
    from pathlib import Path

    session_id = _active(ui, repo)
    worktree = call(ui, f"/api/session?session={session_id}")["worktree"]["path"]
    Path(worktree, "README.md").write_text("hi\nedited\n", encoding="utf-8")
    files = call(ui, f"/api/changes/files?session={session_id}")["files"]
    assert files and files[0]["path"] == "README.md"
    assert files[0]["uncommitted"] is True
    assert "+edited" in files[0]["diff"]


def test_commit_ids_link_to_github_when_the_remote_is_github():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert "function commitUrl(" in source
    assert "/commit/" in source
    assert "data-commit-url" in source
    assert "<th>Date</th>" in source
    # Non-GitHub remotes fall back to plain text, never to a broken link.
    assert 'if (!/^https:\\/\\/github\\.com\\/[^/]+\\/[^/]+$/.test(cleaned)) return null;' in source


def test_the_chat_renders_a_live_feed_with_file_cards():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert "function renderLiveFeed(" in source
    assert '<details class="file-card"' in source
    assert "diff/file?session=" in source
    assert "function clampBlock(" in source
    assert "S.openFileCards" in source
    # Non-streaming runtimes keep the cooperative activity note as fallback.
    assert "function renderActivityNote(" in source


def test_conversation_carries_the_live_event_tail(ui, repo):
    from synchri.storage import dao

    session_id = _active(ui, repo)
    room_id = call(ui, f"/api/session?session={session_id}")["room_id"]
    dao.insert_stream_event(
        ui["broker"].conn, room_id, session_id=session_id, participant="claude",
        invoke_key="inv-1", kind="thinking", title="Reasoning",
        detail="Weighing the options.", payload={"n": 1},
    )
    conversation = call(ui, f"/api/conversation?session={session_id}")
    assert conversation["live_events"][-1]["kind"] == "thinking"
    assert conversation["live_events"][-1]["participant"] == "claude"
    assert conversation["live_events"][-1]["payload"] == {"n": 1}


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


def test_gate_preview_reports_what_the_brief_will_produce(ui):
    r = call(ui, "/api/gates/preview", {"spec": "Acceptance criteria:\n- login works\n- logout works"})
    assert [g["gate_id"] for g in r["gates"]] == ["GATE-01", "GATE-02"]
    assert "2" in r["note"]
    assert r["derivation"] == "acceptance_list"

    fallback = call(ui, "/api/gates/preview", {"spec": "Make it nicer."})
    assert [g["gate_id"] for g in fallback["gates"]] == ["SPEC-01"]
    assert "No explicit acceptance criteria" in fallback["note"]
    assert fallback["derivation"] == "generic_fallback"


def test_gates_carry_their_derivation_to_the_dashboard(ui, repo):
    """The type-time explanation survives: how the gates came to exist is
    persisted at create and served with the gates themselves."""
    session_id = _active(ui, repo)
    gates = call(ui, f"/api/gates?session={session_id}")
    assert gates["derivation"] == "explicit_ids"
    assert "nothing was invented" in gates["derivation_note"]


def test_the_contract_carries_a_human_summary(ui, repo):
    """The cover addresses the human; the verbatim text stays beneath it."""
    session_id = _active(ui, repo)
    contract = call(ui, f"/api/contract?session={session_id}")
    summary = contract["human_summary"]
    assert summary["mode"]
    assert {member["name"] for member in summary["team"]} == {"claude", "codex"}
    assert all(member["role_label"] for member in summary["team"])
    assert "Read the repository" in summary["allowed"]
    assert "Install dependencies" in summary["ask_first"], (
        "ASK capabilities surface as ask-first, in plain labels"
    )
    assert "Force push" in summary["denied"]
    assert "acceptance gate" in summary["done"]
    assert summary["gate_ids"] == ["AUTH-01"]
    assert summary["timebox"]
    assert "UNDERSTOOD" in summary["acknowledgment"]
    # The preflight payload carries the same cover, derived at serve time.
    launch = call(ui, f"/api/launch?session={session_id}")
    assert launch["contract"]["human_summary"]["team"]


def test_the_preflight_handles_mixed_teams():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    # One combined screen: managed rows with real phase indicators, external
    # rows with their collapsed paste fallback — a single external agent no
    # longer forces the whole preflight through the paste wall.
    assert "renderMixedSetup" in source
    assert "agentStatusPill" in source
    assert "managed_by_synchri" in source
    assert "startManaged(managedAgents.map(" in source, (
        "the Start button launches only the managed subset"
    )
    assert "payload.participants = participants" in source
    assert "Begin collaboration" in source
    assert "Manual connection fallback" in source


def test_teaching_surfaces_ship_in_the_page():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    # Tap-for-definition terms: one dictionary, four load-bearing entries,
    # rendered as printed text with a dotted rule — never a chip.
    assert "const DEFINITIONS" in source
    for key in ("worktree: {", "gate: {", "contract: {", '"planning workspace": {'):
        assert key in source
    assert "showDefinition" in source and "defTerm(" in source
    assert ".term{" in source
    # Errors: honest titles, and an error carrying a way forward stays put.
    assert "That didn’t work" in source
    assert "error-dismiss" in source
    assert 'resolution?.kind === "edit_permissions"' in source
    assert "error-open-plan" in source
    # The permissions dialog is backed by the existing control endpoint and
    # says what saving means before the user commits to it.
    assert "showPermissionsDialog" in source
    assert 'action: "permissions"' in source
    assert "must reply UNDERSTOOD to the updated terms" in source
    # The contract tab leads with the human cover; the exact copy stays.
    assert "human_summary" in source
    assert "The agents' exact copy:" in source
    # Gate provenance reaches both the Gates tab and the rail tooltip.
    assert "derivation_note" in source and "derivationHints" in source


def test_gates_can_be_added_from_the_gates_panel(ui, repo):
    session_id = _active(ui, repo)
    gates = call(ui, "/api/gate", {"session": session_id,
                                   "add": {"gate_id": "perf-01", "description": "p95 under 200ms"}})
    ids = [g["gate_id"] for g in gates["gates"]]
    assert "PERF-01" in ids
    assert "AUTH-01" in ids, "adding must not wipe the existing gates"
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(ui, "/api/gate", {"session": session_id,
                               "add": {"gate_id": "PERF-01", "description": "duplicate"}})
    assert exc.value.code == 400


def test_quick_start_validates_the_worktree_strategy(ui, repo):
    base = {
        "repo_path": str(repo),
        "goal": "Do the thing.",
        "agents": [
            {"name": "a", "runtime": "generic", "role": "primary_builder", "command": "true {prompt}"},
            {"name": "b", "runtime": "generic", "role": "adversarial_reviewer", "command": "true {prompt}"},
        ],
    }
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(ui, "/api/quick-start", {**base, "worktree_strategy": "existing"})
    assert "existing worktree" in json.loads(exc.value.read())["error"]["message"]

    with pytest.raises(urllib.error.HTTPError):
        call(ui, "/api/quick-start", {**base, "worktree_strategy": "new",
                                      "existing_worktree_path": "/tmp/somewhere"})

    started = call(ui, "/api/quick-start", {**base, "worktree_strategy": "new"})
    assert started["session"]["session_id"]


def test_agent_names_derive_from_the_runtime_selector(ui):
    result = call(ui, "/api/draft", {"draft": "naming", "agents": [
        {"runtime": "codex", "role": "primary_builder"},
        {"runtime": "codex", "role": "adversarial_reviewer"},
        {"runtime": "claude_code", "role": "verifier"},
    ]})
    assert [agent["name"] for agent in result["draft"]["agents"]] == ["Codex", "Codex-2", "Claude"]


def test_the_updater_rests_on_check_for_updates():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert 'button.textContent = "All up to date!";' in source
    assert "· Up to date" not in source
    # Startup checks are silent and once per app load; only an actionable
    # outcome (an available update) may replace the resting state.
    assert "S.updateChecked" in source
    assert "checkForUpdate({silent:true})" in source
    assert '["available", "move_to_applications"].includes(result.status) ? result : null' in source
    assert "S.updateRevert = window.setTimeout" in source


def test_every_theme_defines_the_complete_token_set():
    import re
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()

    def tokens(block):
        return set(re.findall(r"--[a-z0-9-]+(?=\s*:)", block))

    root = re.search(r"\n:root\{(.*?)\n\}", source, re.S)
    themed = tokens(root.group(1)) - {"--radius", "--radius-s", "--sans", "--mono"}
    themes = dict(re.findall(r':root\[data-theme="([a-z]+)"\]\{(.*?)\n\}', source, re.S))
    assert set(themes) == {
        "daylight", "midnight", "sage",
        "ember", "copper", "solar", "harbor", "iris", "orchid",
    }
    for name, block in themes.items():
        missing = themed - tokens(block)
        assert not missing, f"theme {name} is missing tokens: {sorted(missing)}"
        assert "color-scheme" in block, f"theme {name} must set color-scheme"

    # The System-follows-OS-light block must stay byte-identical to Daylight.
    system_light = re.search(
        r"@media \(prefers-color-scheme:light\)\{:root:not\(\[data-theme\]\)\{(.*?)\n\}\}",
        source, re.S,
    )
    assert system_light is not None
    assert system_light.group(1).strip() == themes["daylight"].strip()

    # Every palette is offered in the picker.
    for key in ("terminal", "midnight", "ember", "copper", "orchid",
                "daylight", "sage", "solar", "harbor", "iris"):
        assert f'{{key: "{key}"' in source


def test_home_leads_with_workflows_and_keeps_sessions_compact():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert '<button class="primary" id="new-workflow">+ New workflow</button>' in source
    assert 'class="wf-grid"' in source
    assert "Recent sessions" in source
    assert "S.showAllSessions ? list : list.slice(0, 6)" in source
    assert "Show all ${list.length} sessions" in source


def test_permission_profiles_show_their_selected_state():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert 'class="choice ${on ? "on" : ""}" data-profile=' in source
    assert "function inferPermissionProfile(" in source
    assert "S.workflowProfile = profile.key;" in source
    # Fine-tuning any capability clears the named-profile highlight.
    assert "S.workflowProfile = null;" in source
    assert ".choice:active{transform:translateY(1px)}" in source


def test_the_workflow_editor_has_no_manual_name_field():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert 'placeholder="agent name"' not in source
    assert "runs as ${esc(agent.name)}" in source
    assert "const deriveNames" in source


def test_worktree_choice_is_an_explicit_selection():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert 'data-workspace="new"' in source
    assert "workspace_choice: null" in source
    assert "choose a workspace" in source
    assert "worktree_strategy:q.workspace_choice" in source
    # The old buried default-to-new select is gone.
    assert "quick-worktree" not in source


def test_gates_can_be_updated_from_the_ui(ui, repo):
    session_id = _active(ui, repo)
    call(ui, "/api/gate", {"session": session_id, "gate_id": "AUTH-01", "status": "pass",
                           "evidence": ["tests/test_auth.py::test_login"]})
    gates = call(ui, f"/api/gates?session={session_id}")
    assert gates["gates"][0]["status"] == "pass"
    # A human marking a gate PASS is an acceptance decision: the missing
    # sign-offs are recorded as the user's own, so the gate stops blocking.
    assert gates["gates"][0]["builder_assessment"] == "Accepted by the user"
    assert gates["gates"][0]["reviewer_assessment"] == "Accepted by the user"
    assert gates["gates"][0]["evidence"] == ["tests/test_auth.py::test_login"]
    assert gates["summary"]["blockers"] == []


def test_human_pass_with_no_evidence_records_the_acceptance(ui, repo):
    session_id = _active(ui, repo)
    call(ui, "/api/gate", {"session": session_id, "gate_id": "AUTH-01", "status": "pass"})
    gates = call(ui, f"/api/gates?session={session_id}")
    assert gates["gates"][0]["evidence"] == ["Accepted by the user from the Gates panel"]
    assert gates["summary"]["complete"] is True


def test_agent_gate_reports_do_not_inherit_the_human_acceptance(ui, repo):
    session_id = _active(ui, repo)
    call(ui, "/api/gate", {"session": session_id, "gate_id": "AUTH-01", "status": "pass",
                           "actor": "claude"})
    gates = call(ui, f"/api/gates?session={session_id}")
    assert gates["gates"][0]["builder_assessment"] is None
    assert "AUTH-01 is marked pass with no evidence" in gates["summary"]["blockers"]


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


def test_restarting_a_session_relaunches_only_managed_supervision(ui, repo):
    session_id = _active(ui, repo)
    SessionManager(ui["broker"]).record_participant_failure(
        session_id, "claude", "failed", "permission prompt could not be answered"
    )
    before = call(ui, f"/api/session?session={session_id}")

    class _RestartRegistry:
        def __init__(self):
            self.restarted = []

        def readiness(self, record):
            return {"available": True, "agents": []}

        def restart(self, record):
            self.restarted.append(record.session_id)
            return {"phase": "resuming", "alive": True}

    registry = _RestartRegistry()
    original_registry = ui["server"].api.managed
    ui["server"].api.managed = registry
    dashboard = call(ui, "/api/control", {"session": session_id, "action": "restart"})
    after = call(ui, f"/api/session?session={session_id}")

    assert registry.restarted == [session_id]
    assert dashboard["session"]["status"] == "active"
    assert dashboard["participant_states"]["claude"]["state"] == "active"
    assert dashboard["participant_states"]["claude"]["failures"] == 0
    assert after["room_id"] == before["room_id"]
    assert after["worktree"]["path"] == before["worktree"]["path"]
    assert after["contract_revision"] == before["contract_revision"]

    ui["server"].api.managed = original_registry
    call(ui, "/api/control", {"session": session_id, "action": "pause"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(ui, "/api/control", {"session": session_id, "action": "restart"})
    assert "Only an active session" in json.loads(exc.value.read())["error"]["message"]


class _RecordingRegistry:
    """Stands in for the managed runner registry to observe cancel ordering."""

    def __init__(self):
        self.cancelled = []

    def cancel(self, session_id, reason=""):
        self.cancelled.append((session_id, reason))


def test_sessions_can_be_renamed_and_deleted_from_the_ui(ui, repo):
    session_id = _active(ui, repo)
    renamed = call(ui, "/api/session/rename", {"session": session_id, "name": "Sharper name"})
    assert renamed["session"]["name"] == "Sharper name"

    with pytest.raises(urllib.error.HTTPError) as exc:
        call(ui, "/api/session/delete", {"session": session_id})
    assert json.loads(exc.value.read())["error"]["code"] == "session_not_finished"

    call(ui, "/api/control", {"session": session_id, "action": "stop"})
    result = call(ui, "/api/session/delete", {"session": session_id})
    assert result["deleted"] == session_id
    assert result["sessions"] == []
    assert "worktree kept" in result["worktree_note"], "an unpushed session branch must survive"
    with pytest.raises(urllib.error.HTTPError):
        call(ui, f"/api/session?session={session_id}")


def test_session_rows_offer_rename_and_delete():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert "showDeleteSessionDialog" in source
    assert "Stop the session first" in source
    assert 'api("session/rename"' in source
    assert 'api("session/delete"' in source
    assert "Remote branches are never touched." in source


def test_restart_agent_resets_state_and_resolves_the_escalation(ui, repo):
    from synchri.session.manager import SessionManager

    session_id = _active(ui, repo)
    manager = SessionManager(ui["broker"])
    manager.record_participant_failure(session_id, "claude", "failed", "exit 1")
    manager.record_participant_failure(session_id, "claude", "dropped",
                                       "dropped after 2 consecutive failed turns")
    manager.escalate(session_id, "agent_failed", "claude dropped", raised_by="claude")
    assert call(ui, f"/api/dashboard?session={session_id}")["user_intervention_required"] is True

    class _RestartStub:
        def __init__(self):
            self.restarted = []

        def restart_participant(self, record, name):
            # Participant-scoped: the restart names exactly one agent and
            # never touches the rest of the team.
            self.restarted.append((record.session_id, name))
            return {"phase": "resuming"}

    stub = _RestartStub()
    ui["server"].api.managed = stub

    dashboard = call(ui, "/api/control", {"session": session_id, "action": "restart_agent",
                                          "participant": "claude"})
    assert dashboard["participant_states"]["claude"]["state"] == "active"
    assert dashboard["participant_states"]["claude"]["failures"] == 0
    assert dashboard["user_intervention_required"] is False
    assert stub.restarted == [(session_id, "claude")]

    with pytest.raises(urllib.error.HTTPError) as exc:
        call(ui, "/api/control", {"session": session_id, "action": "restart_agent",
                                  "participant": "ghost"})
    assert exc.value.code == 400


def test_refused_completion_leaves_the_agent_team_untouched(ui, repo):
    session_id = _active(ui, repo)
    registry = _RecordingRegistry()
    ui["server"].api.managed = registry

    with pytest.raises(urllib.error.HTTPError) as exc:
        call(ui, "/api/control", {"session": session_id, "action": "complete"})
    assert json.loads(exc.value.read())["error"]["code"] == "gates_unsatisfied"
    # The refusal must not have signalled the agents to stop.
    assert registry.cancelled == []

    dashboard = call(ui, "/api/control", {"session": session_id, "action": "complete",
                                          "force": True})
    assert dashboard["session"]["status"] == "complete"
    assert "waived" in dashboard["session"]["ended_reason"]
    assert [entry[0] for entry in registry.cancelled] == [session_id]


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


def test_the_session_package_downloads_as_a_zip_with_no_secrets(ui, repo):
    import io
    import zipfile

    from synchri.session.manager import SessionManager

    session_id = _active(ui, repo)
    call(ui, "/api/message", {"session": session_id, "content": "note for the record",
                              "interrupt": True})
    call(ui, "/api/gate", {"session": session_id, "gate_id": "AUTH-01", "status": "pass"})
    call(ui, "/api/control", {"session": session_id, "action": "complete"})

    request = urllib.request.Request(f"{ui['base']}/api/package?session={session_id}")
    request.add_header("X-Synchri-Token", ui["token"])
    with urllib.request.urlopen(request, timeout=20) as response:
        disposition = response.headers.get("Content-Disposition") or ""
        content_type = response.headers.get("Content-Type")
        data = response.read()
    assert content_type == "application/zip"
    assert 'filename="synchri-' in disposition
    assert data[:4] == b"PK\x03\x04"

    archive = zipfile.ZipFile(io.BytesIO(data))
    names = set(archive.namelist())
    assert {"session.md", "transcript.md", "transcript.jsonl", "final-changelog.md",
            "gates.md", "usage-summary.md", "usage.json", "commits.md", "diff.patch"} <= names
    assert "note for the record" in archive.read("transcript.md").decode()
    assert "No usage was recorded" in archive.read("usage-summary.md").decode()
    assert "AUTH-01" in archive.read("gates.md").decode()

    # No secret material may leak into any member: metadata carries invite
    # tokens and the human's room secret, and none of it belongs in a file
    # meant to be shared.
    record = SessionManager(ui["broker"]).get(session_id)
    secrets_to_check = [record.metadata["human"]["secret"]]
    secrets_to_check += [invite["token"] for invite in record.metadata.get("invites", [])]
    blob = b"".join(archive.read(name) for name in names)
    for secret in secrets_to_check:
        assert secret and secret.encode() not in blob


def test_the_package_requires_a_finished_session(ui, repo):
    session_id = _active(ui, repo)
    request = urllib.request.Request(f"{ui['base']}/api/package?session={session_id}")
    request.add_header("X-Synchri-Token", ui["token"])
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=20)
    assert exc.value.code == 400
    assert json.loads(exc.value.read())["error"]["code"] == "session_not_finished"


def test_the_package_can_be_saved_next_to_the_room_artifacts(ui, repo):
    import io
    import zipfile
    from pathlib import Path

    session_id = _active(ui, repo)
    call(ui, "/api/control", {"session": session_id, "action": "stop"})
    saved = call(ui, "/api/package/save", {"session": session_id})
    path = Path(saved["path"])
    assert path.name == "session-package.zip"
    data = path.read_bytes()
    assert data[:4] == b"PK\x03\x04"
    archive = zipfile.ZipFile(io.BytesIO(data))
    # A stopped session's record is honest about not being verified-complete.
    assert "ended without a verified completion" in archive.read("final-changelog.md").decode()


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


# ----------------------------------------------------------------------
# join assembly: durable phases, partial joins, retry-only-the-failed
# ----------------------------------------------------------------------

GOOD_AGENT = """\
import sys
prompt = sys.argv[1]
if 'output exactly UNDERSTOOD' in prompt:
    with open({count!r}, 'a') as handle:
        handle.write('ack\\n')
    print('UNDERSTOOD')
else:
    print('Inspected the worktree.')
    print('SYNCHRI-PASS')
"""

FAILING_AGENT = """\
import sys
prompt = sys.argv[1]
if 'output exactly UNDERSTOOD' in prompt:
    with open({count!r}, 'a') as handle:
        handle.write('ack\\n')
sys.stderr.write('provider sign-in expired')
raise SystemExit(1)
"""


def _wait_managed_phase(ui, session_id, phases, timeout=20):
    deadline = time.monotonic() + timeout
    managed = {}
    while time.monotonic() < deadline:
        managed = call(ui, f"/api/managed?session={session_id}")["managed"]
        if managed["phase"] in phases and not managed["alive"]:
            return managed
        time.sleep(0.05)
    raise AssertionError(f"managed run never reached {phases}: {managed}")


def test_partial_join_keeps_the_room_inactive_and_retries_only_the_failed_agent(
    ui, repo, tmp_path
):
    import sys

    builder_count = tmp_path / "builder-acks.log"
    reviewer_count = tmp_path / "reviewer-acks.log"
    builder_script = tmp_path / "builder.py"
    reviewer_script = tmp_path / "reviewer.py"
    builder_script.write_text(GOOD_AGENT.format(count=str(builder_count)), encoding="utf-8")
    reviewer_script.write_text(FAILING_AGENT.format(count=str(reviewer_count)), encoding="utf-8")

    started = call(ui, "/api/quick-start", {
        "repo_path": str(repo),
        "goal": "Inspect the repository.",
        "agents": [{
            "name": "builder", "runtime": "generic", "role": "primary_builder",
            "command": f"{sys.executable} {builder_script} {{prompt}}",
        }, {
            "name": "reviewer", "runtime": "generic", "role": "adversarial_reviewer",
            "command": f"{sys.executable} {reviewer_script} {{prompt}}",
        }],
    })
    session_id = started["session"]["session_id"]

    call(ui, "/api/managed/start", {"session": session_id})
    managed = _wait_managed_phase(ui, session_id, {"needs_attention"})
    assert "reviewer" in managed["detail"]

    # The room stays inactive; the ready agent is attached but receives no work.
    assert call(ui, f"/api/session?session={session_id}")["status"] == "awaiting_ack"
    launch = call(ui, f"/api/launch?session={session_id}")["launch"]
    phases = {agent["name"]: agent for agent in launch["agents"]}
    assert phases["builder"]["join_phase"] == "ready"
    assert phases["builder"]["acknowledged"] is True
    assert phases["reviewer"]["join_phase"] == "failed"
    assert "sign-in expired" in phases["reviewer"]["join_detail"]

    # Fix the failed agent and retry: only the reviewer is re-invoked for the
    # agreement — the builder's acknowledgment stands.
    reviewer_script.write_text(GOOD_AGENT.format(count=str(reviewer_count)), encoding="utf-8")
    call(ui, "/api/managed/start", {"session": session_id})
    _wait_managed_phase(ui, session_id, {"waiting"})

    assert builder_count.read_text().count("ack") == 1
    assert reviewer_count.read_text().count("ack") == 2
    assert call(ui, f"/api/session?session={session_id}")["status"] == "active"
    launch = call(ui, f"/api/launch?session={session_id}")["launch"]
    assert all(agent["join_phase"] == "ready" for agent in launch["agents"])


def test_launch_payload_reports_runtime_connection_state(ui, repo):
    from synchri.runner import doctor

    doctor._save_connection(ui["broker"].conn, {
        "runtime": "codex",
        "state": "connected",
        "executable_path": "/usr/local/bin/codex",
        "version": "1.0.0",
        "adapter_revision": "abc",
        "auth_indication": True,
        "resume": doctor.RESUME_UNSUPPORTED,
        "checks": [],
        "detail": "connected",
    })
    result = call(ui, "/api/quick-start", {
        "repo_path": str(repo),
        "goal": "Review the current change together.",
        "agents": [
            {"name": "codex", "runtime": "codex", "role": "primary_builder"},
            {"name": "copilot", "runtime": "copilot", "role": "adversarial_reviewer"},
        ],
    })
    agents = {agent["name"]: agent for agent in result["launch"]["agents"]}
    assert agents["codex"]["connected"] is True
    assert agents["copilot"]["connected"] is False

    boot = call(ui, "/api/bootstrap")
    assert boot["runtime_connections"]["codex"]["state"] == "connected"


# ----------------------------------------------------------------------
# the redesigned shell
# ----------------------------------------------------------------------


def test_the_shell_is_a_global_collapsible_sidebar():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert '<aside id="sidebar"' in source
    assert 'id="side-toggle"' in source and 'id="side-reveal"' in source
    assert "S.sidebarCollapsed" in source
    assert "function applySidebar(" in source and "function toggleSidebar(" in source
    # Collapse removes the sidebar from layout and the accessibility tree.
    assert ".shell.collapsed #sidebar{display:none}" in source
    assert 'aria-controls="sidebar"' in source
    # The old top header chrome is gone; its controls live in the sidebar.
    assert "<header>" not in source
    assert 'id="nav-new"' in source and 'id="github-account"' in source
    assert 'id="theme-menu"' in source and 'id="app-update"' in source


def test_the_sidebar_becomes_the_session_rail_without_double_chrome():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    assert "function renderSideContext(" in source
    assert "function renderSessionRail(" in source
    # The one global sidebar swaps its browsing lists for session telemetry.
    assert 'if (S.view === "session" && S.dash)' in source
    assert "renderSessionRail(box);" in source
    assert 'id="session-rail"' not in source
    assert "session-rail-side" not in source
    assert 'id="rail-toggle"' not in source
    # Browsing shows workflows and the compact recent-session list.
    assert "Show all ${list.length} sessions" in source
    assert "S.showAllSessions ? list : list.slice(0, 6)" in source


def test_the_chrome_is_neutral_ink_with_semantic_color():
    import re
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    # Primary actions are inverted ink, not a hue; success needs an explicit
    # modifier instead of being the pill default.
    assert "button.primary{background:var(--btn);" in source
    assert ".pill.ok{color:var(--ok)}" in source
    root = re.search(r"\n:root\{(.*?)\n\}", source, re.S).group(1)
    for token in ("--btn:", "--btn-ink:", "--ok:", "--ok-soft:", "--ok-line:", "--frame:"):
        assert token in root
    # The blinking terminal cursor is retired along with the terminal look.
    assert "cursorblink" not in source
    # Keep the persisted theme key stable while presenting the calmer name.
    assert '{key: "terminal", label: "Graphite"' in source
    assert 'label: "Terminal"' not in source


def test_the_new_session_flow_asks_progressively():
    from pathlib import Path

    source = (Path(__file__).parents[1] / "synchri" / "ui" / "static" / "app.html").read_text()
    for question in ("What are we working on?", "Where should the agents work?",
                     "What do you want done?", "Who should work on it?", "Ready."):
        assert question in source
    assert "Help me make a plan" in source
    # A saved workflow is recommended, never required: the recommended team
    # can launch a build directly, and planning routes through the draft API.
    assert "RECOMMENDED_BUILD_TEAM" in source and "RECOMMENDED_PLAN_TEAM" in source
    assert 'planDraft: `quick-plan-${crypto.randomUUID()}`' in source
    assert 'const draftId = q.planDraft;' in source
    assert 'await api("draft", {draft: draftId, mode: "planning"' in source
    assert 'await api("start", {draft: draftId})' in source
    assert 'role="radiogroup" aria-label="Session intent"' in source
    assert 'role="radio" aria-checked="${planIntent}"' in source
    # Changing the repository invalidates the repository-scoped answers.
    assert "S.quick.workspace_choice = null;\n  S.quick.editStep = null;" in source
    # The timebox is segmented pills, and raw connection prompts stay tucked
    # inside a collapsed fallback drawer.
    assert "TIMEBOX_CHOICES" in source and '["2 hours", "2h"]' in source
    assert "data-tb=" in source
    assert "Manual connection fallback" in source


def test_quick_plan_drafts_are_isolated_between_browser_flows(ui, repo):
    agents = [
        {"runtime": "claude_code", "role": "planner"},
        {"runtime": "codex", "role": "plan_reviewer"},
    ]
    # Interleave the writes exactly as two browser windows can. A shared key
    # would make window A start window B's idea and leave B with no draft.
    for draft_id, idea in (("quick-plan-window-a", "Idea from window A."),
                           ("quick-plan-window-b", "Idea from window B.")):
        call(ui, "/api/draft/reset", {"draft": draft_id})
        call(ui, "/api/draft", {"draft": draft_id, "mode": "planning",
                                "repo_path": str(repo), "spec": idea, "agents": agents})

    started_a = call(ui, "/api/start", {"draft": "quick-plan-window-a"})
    started_b = call(ui, "/api/start", {"draft": "quick-plan-window-b"})
    for started, idea in ((started_a, "Idea from window A."),
                          (started_b, "Idea from window B.")):
        session_id = started["session"]["session_id"]
        session = call(ui, f"/api/session?session={session_id}")
        plan = call(ui, f"/api/plan?session={session_id}")
        assert session["mode"] == "planning"
        assert [p["name"] for p in session["participants"]] == ["Claude", "Codex"]
        assert plan["idea"] == idea
