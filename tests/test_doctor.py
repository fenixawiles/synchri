"""The two-tier connection doctor: passive probes, the consented connection
test, durable connection records, and their invalidation."""

from __future__ import annotations

import json
import shlex
import sys
import tempfile
import threading

import pytest

from synchri.errors import ValidationError
from synchri.runner import doctor
from synchri.runner.stream_events import ClaudeStreamParser, CodexStreamParser


# ----------------------------------------------------------------------
# a fake provider CLI
# ----------------------------------------------------------------------

FAKE_CLI = """\
import json, os, re, sys

args = sys.argv[1:]
if "--version" in args:
    print("fake-cli " + os.environ.get("FAKE_CLI_VERSION", "1.2.3"))
    raise SystemExit(0)

cwd_file = os.environ.get("FAKE_CLI_CWD_FILE")
if cwd_file:
    with open(cwd_file, "w") as handle:
        handle.write(os.getcwd())

args_file = os.environ.get("FAKE_CLI_ARGS_FILE")
if args_file:
    with open(args_file, "a") as handle:
        handle.write(" ".join(args) + "\\n")

mode = os.environ.get("FAKE_CLI_MODE", "ok")
prompt = args[-1] if args else ""
if mode == "auth-fail":
    sys.stderr.write("Please run /login to authenticate\\n")
    raise SystemExit(1)
if mode == "boom":
    sys.stderr.write("kaboom\\n")
    raise SystemExit(3)

sid = os.environ.get("FAKE_CLI_SESSION_ID", "sess-fake")

if "--resume" in args:
    if os.environ.get("FAKE_CLI_RESUME", "ok") == "reject":
        sys.stderr.write("error: unknown option '--resume'\\n")
        raise SystemExit(2)
    resumed = args[args.index("--resume") + 1]
    token = re.search(r"SYNCHRI-RESUME-OK-[0-9a-f]+", prompt).group(0)
    print(json.dumps({"type": "system", "subtype": "init", "session_id": resumed, "model": "fake"}))
    print(json.dumps({"type": "result", "result": token, "session_id": resumed}))
    raise SystemExit(0)

token = re.search(r"SYNCHRI-CONNECT-OK-[0-9a-f]+", prompt).group(0)
if mode == "no-stream":
    print(token)
    raise SystemExit(0)
init = {"type": "system", "subtype": "init", "model": "fake"}
result = {"type": "result", "result": token}
if sid:
    init["session_id"] = sid
    result["session_id"] = sid
print(json.dumps(init))
print(json.dumps(result))
"""


@pytest.fixture
def fake_cli(tmp_path):
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI)
    return script


@pytest.fixture
def auth_file(tmp_path):
    path = tmp_path / "auth.json"
    path.write_text("{}")
    return path


def make_definition(script, auth_file, *, resume=True, min_version=(1, 0, 0)):
    base = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"
    return {
        "label": "Fake CLI",
        "default_name": "Fake",
        "executable": sys.executable,
        "version_command": base + " --version",
        "min_version": min_version,
        "managed_command": base + " --stream {prompt}",
        "plain_managed_command": base + " {prompt}",
        "suggested_command": base + " {prompt}",
        "stream_format": "claude",
        "resume_command": (base + " --stream --resume {resume_id} {prompt}") if resume else None,
        # The canary's enforced invocations: the fake --sandboxed flag stands
        # in for the real CLIs' no-tools / read-only modes.
        "connection_test_command": base + " --sandboxed --stream {prompt}",
        "plain_connection_test_command": base + " --sandboxed {prompt}",
        "connection_test_resume_command": (
            (base + " --sandboxed --stream --resume {resume_id} {prompt}") if resume else None
        ),
        "auth_indicators": [str(auth_file)],
    }


def checks_by_key(payload):
    return {check["key"]: check for check in payload}


# ----------------------------------------------------------------------
# passive tier
# ----------------------------------------------------------------------


def test_passive_report_is_honest_when_everything_is_present(fake_cli, auth_file):
    report = doctor.passive_report("fake", definition=make_definition(fake_cli, auth_file))
    checks = checks_by_key(report["checks"])
    assert checks["installed"]["state"] == doctor.PASS
    assert checks["version"]["state"] == doctor.PASS
    assert checks["version"]["detail"] == "1.2.3"
    assert checks["capabilities"]["state"] == doctor.PASS
    assert checks["auth"]["state"] == doctor.PASS
    facts = report["facts"]
    assert facts["version"] == "1.2.3"
    assert facts["auth_indication"] is True
    assert facts["executable_path"]
    assert facts["adapter_revision"]


def test_passive_report_missing_executable_fails_installed_only(fake_cli, auth_file):
    definition = make_definition(fake_cli, auth_file)
    definition["executable"] = "synchri-definitely-not-a-real-cli"
    report = doctor.passive_report("fake", definition=definition)
    checks = checks_by_key(report["checks"])
    assert checks["installed"]["state"] == doctor.FAIL
    assert checks["version"]["state"] == doctor.UNKNOWN


def test_version_below_the_supported_floor_fails(fake_cli, auth_file):
    definition = make_definition(fake_cli, auth_file, min_version=(9, 0, 0))
    report = doctor.passive_report("fake", definition=definition)
    checks = checks_by_key(report["checks"])
    assert checks["version"]["state"] == doctor.FAIL
    assert "supported floor" in checks["version"]["detail"]


def test_absent_auth_file_is_unknown_never_a_red_x(fake_cli, tmp_path):
    definition = make_definition(fake_cli, tmp_path / "never-written.json")
    report = doctor.passive_report("fake", definition=definition)
    checks = checks_by_key(report["checks"])
    assert checks["auth"]["state"] == doctor.UNKNOWN
    # ...but the underlying fact still records "defined and absent", which is
    # what a later True -> False downgrade compares against.
    assert report["facts"]["auth_indication"] is False


# ----------------------------------------------------------------------
# the connection test
# ----------------------------------------------------------------------


def test_connection_test_proves_the_whole_path_including_resume(broker, fake_cli, auth_file):
    definition = make_definition(fake_cli, auth_file)
    outcome = doctor.run_connection_test(broker.conn, "fake", definition=definition)
    assert outcome["state"] == "connected"
    assert outcome["resume"] == doctor.RESUME_VERIFIED
    checks = checks_by_key(outcome["checks"])
    assert checks["launch"]["state"] == doctor.PASS
    assert checks["bootstrap"]["state"] == doctor.PASS
    assert checks["auth"]["state"] == doctor.PASS
    assert checks["structured_output"]["state"] == doctor.PASS

    stored = doctor.load_connection(broker.conn, "fake")
    assert stored["state"] == "connected"
    assert stored["resume"] == doctor.RESUME_VERIFIED
    assert stored["version"] == "1.2.3"

    state = doctor.connection_state(broker.conn, "fake", definition=definition)
    assert state["state"] == "connected"


def test_one_invocation_cannot_prove_resume_without_a_session_id(
    broker, fake_cli, auth_file, monkeypatch
):
    monkeypatch.setenv("FAKE_CLI_SESSION_ID", "")
    outcome = doctor.run_connection_test(
        broker.conn, "fake", definition=make_definition(fake_cli, auth_file)
    )
    assert outcome["state"] == "connected"
    assert outcome["resume"] == doctor.RESUME_UNVERIFIED


def test_a_rejected_resume_invocation_is_recorded_unsupported(
    broker, fake_cli, auth_file, monkeypatch
):
    monkeypatch.setenv("FAKE_CLI_RESUME", "reject")
    outcome = doctor.run_connection_test(
        broker.conn, "fake", definition=make_definition(fake_cli, auth_file)
    )
    assert outcome["state"] == "connected"
    assert outcome["resume"] == doctor.RESUME_UNSUPPORTED


def test_an_adapter_without_resume_is_unsupported_not_failed(broker, fake_cli, auth_file):
    outcome = doctor.run_connection_test(
        broker.conn, "fake", definition=make_definition(fake_cli, auth_file, resume=False)
    )
    assert outcome["state"] == "connected"
    assert outcome["resume"] == doctor.RESUME_UNSUPPORTED


def test_a_reply_without_the_documented_stream_is_never_a_connection(
    broker, fake_cli, auth_file, monkeypatch
):
    """Structured-output parse is one of the proofs — no false green.

    The CLI launches, authenticates, and echoes the sentinel, but as plain
    text instead of its documented stream. That is a degraded pipeline, not
    a connected one.
    """
    monkeypatch.setenv("FAKE_CLI_MODE", "no-stream")
    outcome = doctor.run_connection_test(
        broker.conn, "fake", definition=make_definition(fake_cli, auth_file)
    )
    assert outcome["state"] == "failed"
    checks = checks_by_key(outcome["checks"])
    assert checks["bootstrap"]["state"] == doctor.PASS
    assert checks["structured_output"]["state"] == doctor.FAIL
    stored = doctor.stored_connections(broker.conn).get("fake") or {}
    assert stored.get("state") != "connected"


def test_the_canary_runs_in_a_fresh_directory_outside_any_repository(
    broker, fake_cli, auth_file, tmp_path, monkeypatch
):
    cwd_file = tmp_path / "canary-cwd.txt"
    monkeypatch.setenv("FAKE_CLI_CWD_FILE", str(cwd_file))
    doctor.run_connection_test(
        broker.conn, "fake", definition=make_definition(fake_cli, auth_file, resume=False)
    )
    import os

    recorded = cwd_file.read_text()
    assert "synchri-connect-" in recorded
    # Compare fully resolved paths: on macOS the child's cwd resolves the
    # /var -> /private/var symlink while gettempdir() reports the short form.
    temp_root = os.path.realpath(tempfile.gettempdir())
    assert os.path.realpath(recorded).startswith(temp_root + os.sep) or (
        os.path.realpath(recorded) == temp_root
    )
    # The sandbox is disposable: it is gone once the test finishes.
    assert not os.path.exists(recorded)


def test_a_cancelled_connection_test_stores_nothing(broker, fake_cli, auth_file):
    cancel = threading.Event()
    cancel.set()
    outcome = doctor.run_connection_test(
        broker.conn,
        "fake",
        definition=make_definition(fake_cli, auth_file),
        cancel_event=cancel,
    )
    assert outcome["state"] == "cancelled"
    assert doctor.load_connection(broker.conn, "fake") is None


def test_a_failed_invocation_stores_a_failed_record(broker, fake_cli, auth_file, monkeypatch):
    monkeypatch.setenv("FAKE_CLI_MODE", "boom")
    definition = make_definition(fake_cli, auth_file)
    outcome = doctor.run_connection_test(broker.conn, "fake", definition=definition)
    assert outcome["state"] == "failed"
    assert doctor.load_connection(broker.conn, "fake")["state"] == "failed"
    assert doctor.connection_state(broker.conn, "fake", definition=definition)["state"] == "failed"


def test_a_sign_in_problem_is_classified_as_auth_not_launch(
    broker, fake_cli, auth_file, monkeypatch
):
    monkeypatch.setenv("FAKE_CLI_MODE", "auth-fail")
    outcome = doctor.run_connection_test(
        broker.conn, "fake", definition=make_definition(fake_cli, auth_file)
    )
    assert outcome["state"] == "failed"
    checks = checks_by_key(outcome["checks"])
    assert checks["auth"]["state"] == doctor.FAIL
    assert "sign-in" in checks["auth"]["detail"]


def test_the_canary_only_runs_under_the_adapters_enforced_command(
    broker, fake_cli, auth_file, tmp_path, monkeypatch
):
    """The temp directory is defense-in-depth; the enforced invocation is the
    guard. No enforced command means no consented connection test at all."""
    args_file = tmp_path / "canary-args.log"
    monkeypatch.setenv("FAKE_CLI_ARGS_FILE", str(args_file))
    doctor.run_connection_test(
        broker.conn, "fake", definition=make_definition(fake_cli, auth_file)
    )
    # The multi-line sentinel prompt spreads each record over several lines;
    # each invocation's argv record starts with its flags.
    heads = [
        line for line in args_file.read_text().splitlines()
        if line.startswith("--sandboxed")
    ]
    assert len(heads) == 2, "launch canary plus resume canary, both enforced"
    assert any("--resume" in line for line in heads)

    unavailable = make_definition(fake_cli, auth_file)
    unavailable["connection_test_command"] = None
    with pytest.raises(ValidationError) as exc:
        doctor.run_connection_test(broker.conn, "fake", definition=unavailable)
    assert "unavailable" in str(exc.value)


def test_real_adapters_declare_enforced_canary_commands():
    from synchri.session.modes import KNOWN_RUNTIMES

    claude = KNOWN_RUNTIMES["claude_code"]
    for key in ("connection_test_command", "plain_connection_test_command",
                "connection_test_resume_command"):
        assert "--permission-mode plan" in claude[key]
        assert "--safe-mode" in claude[key]
        assert "--strict-mcp-config" in claude[key]
        assert '--tools ""' in claude[key], "the canary needs no tools at all"
    codex = KNOWN_RUNTIMES["codex"]
    assert "--sandbox read-only" in codex["connection_test_command"]
    assert "--sandbox read-only" in codex["plain_connection_test_command"]
    # Copilot has no verified enforcement flag: no canary command at all.
    assert KNOWN_RUNTIMES["copilot"].get("connection_test_command") is None


# ----------------------------------------------------------------------
# invalidation
# ----------------------------------------------------------------------


def test_a_version_change_invalidates_the_connection(broker, fake_cli, auth_file, monkeypatch):
    definition = make_definition(fake_cli, auth_file)
    doctor.run_connection_test(broker.conn, "fake", definition=definition)
    monkeypatch.setenv("FAKE_CLI_VERSION", "2.0.0")
    state = doctor.connection_state(broker.conn, "fake", definition=definition)
    assert state["state"] == "reconnect_needed"
    assert any("version changed" in reason for reason in state["reasons"])


def test_an_executable_move_invalidates_the_connection(broker, fake_cli, auth_file):
    definition = make_definition(fake_cli, auth_file)
    doctor.run_connection_test(broker.conn, "fake", definition=definition)
    broker.conn.execute(
        "UPDATE runtime_connections SET executable_path = '/somewhere/else/cli' WHERE runtime = 'fake'"
    )
    state = doctor.connection_state(broker.conn, "fake", definition=definition)
    assert state["state"] == "reconnect_needed"
    assert any("moved" in reason for reason in state["reasons"])


def test_an_adapter_change_invalidates_the_connection(broker, fake_cli, auth_file):
    definition = make_definition(fake_cli, auth_file)
    doctor.run_connection_test(broker.conn, "fake", definition=definition)
    changed = dict(definition)
    changed["managed_command"] = definition["managed_command"] + " --extra-flag"
    state = doctor.connection_state(broker.conn, "fake", definition=changed)
    assert state["state"] == "reconnect_needed"
    assert any("adapter" in reason for reason in state["reasons"])


def test_losing_the_cached_sign_in_invalidates_the_connection(broker, fake_cli, auth_file):
    definition = make_definition(fake_cli, auth_file)
    doctor.run_connection_test(broker.conn, "fake", definition=definition)
    auth_file.unlink()
    state = doctor.connection_state(broker.conn, "fake", definition=definition)
    assert state["state"] == "reconnect_needed"
    assert any("sign-in" in reason for reason in state["reasons"])


def test_an_unknown_auth_indication_never_downgrades(broker, fake_cli, tmp_path):
    # The adapter defines no indicators: the fact is None at test time and
    # None later — nothing to compare, nothing to downgrade.
    definition = make_definition(fake_cli, tmp_path / "unused.json")
    definition["auth_indicators"] = []
    doctor.run_connection_test(broker.conn, "fake", definition=definition)
    state = doctor.connection_state(broker.conn, "fake", definition=definition)
    assert state["state"] == "connected"


# ----------------------------------------------------------------------
# background tester + parser session ids
# ----------------------------------------------------------------------


def test_the_background_tester_reports_phases_and_a_durable_outcome(
    workspace, broker, fake_cli, auth_file
):
    tester = doctor.ConnectionTester(workspace)
    tester.start("fake", definition=make_definition(fake_cli, auth_file))
    status = tester.wait("fake", timeout=60)
    assert status["phase"] == "done"
    assert status["result"]["state"] == "connected"
    assert doctor.load_connection(broker.conn, "fake")["state"] == "connected"


def test_parsers_capture_the_provider_session_id():
    claude = ClaudeStreamParser()
    claude.feed(json.dumps({"type": "system", "subtype": "init", "session_id": "abc", "model": "m"}))
    assert claude.provider_session_id == "abc"

    codex = CodexStreamParser()
    codex.feed(json.dumps({"type": "thread.started", "thread_id": "th_1"}))
    assert codex.provider_session_id == "th_1"

    legacy = CodexStreamParser()
    legacy.feed(json.dumps({"id": "1", "msg": {"type": "session_configured", "session_id": "s0"}}))
    assert legacy.provider_session_id == "s0"
