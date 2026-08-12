"""CLI surface: exit codes, JSON output, and credential resolution.

These run ``main()`` in-process for speed; ``test_concurrency.py`` covers the
genuinely cross-process paths.
"""

from __future__ import annotations

import json

import pytest

from synchri.cli.main import main


@pytest.fixture
def cli(workspace, capsys):
    """Run the CLI against the test workspace and return (code, stdout, stderr)."""

    def run(*args: str):
        code = main(["--home", str(workspace.home), *args])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


@pytest.fixture
def cli_room(cli):
    code, out, _ = cli(
        "--json", "create-room", "--name", "PR 89 Review", "--goal", "no races",
        "--agents", "claude,codex",
    )
    assert code == 0
    created = json.loads(out)
    for invite in created["invites"]:
        assert cli(
            "--json", "join", invite["token"], "--name", invite["participant_name"]
        )[0] == 0
    return created


def test_no_command_prints_help(cli):
    code, out, _ = cli()
    assert code == 0
    assert "Stop being the clipboard" in out


def test_workspace_reports_the_workspace_and_no_daemon(cli, workspace):
    code, out, _ = cli("--json", "workspace")
    payload = json.loads(out)
    assert code == 0
    assert payload["workspace"] == str(workspace.home)
    assert payload["daemon"] is False


def test_create_room_writes_a_session_file_with_owner_only_permissions(cli, workspace):
    code, out, _ = cli("--json", "create-room", "--name", "Room")
    created = json.loads(out)
    assert code == 0
    session_path = workspace.session_path(created["room_id"], "human")
    assert session_path.exists()
    assert session_path.stat().st_mode & 0o777 == 0o600
    stored = json.loads(session_path.read_text(encoding="utf-8"))
    assert stored["secret"] == created["human"]["secret"]
    assert stored["room_token"] == created["observer_token"]


def test_agent_session_files_do_not_store_the_room_join_token(cli, cli_room, workspace):
    stored = json.loads(
        workspace.session_path(cli_room["room_id"], "claude").read_text(encoding="utf-8")
    )
    assert "room_token" not in stored, "an agent session must not carry the room-wide token"


def test_room_defaults_to_the_last_created_room(cli, cli_room):
    code, out, _ = cli("--json", "send", "--from", "claude", "-m", "no --room flag needed")
    assert code == 0
    assert json.loads(out)["message"]["room_id"] == cli_room["room_id"]


def test_secret_is_resolved_from_the_session_file(cli, cli_room):
    code, _, _ = cli("--json", "send", "--room", cli_room["room_id"], "--from", "codex", "-m", "hi")
    assert code == 0


def test_activity_is_a_live_work_note_not_a_message(cli, cli_room):
    room_id = cli_room["room_id"]
    # Give Claude the floor so publishing a note cannot be used to bypass turns.
    assert cli("--json", "send", "--room", room_id, "--from", "human", "--to", "claude", "-m", "inspect this")[0] == 0

    code, out, _ = cli(
        "--json", "activity", "--room", room_id, "--as", "claude", "-m", "Inspecting the startup path"
    )

    assert code == 0
    assert json.loads(out)["activity"]["summary"] == "Inspecting the startup path"
    code, out, _ = cli("--json", "read", "--room", room_id, "--as", "human")
    assert code == 0
    assert [message["content"] for message in json.loads(out)["messages"]] == ["inspect this"]


def test_explicit_bad_secret_beats_the_session_file(cli, cli_room):
    code, _, err = cli(
        "--json", "send", "--room", cli_room["room_id"], "--from", "codex",
        "--secret", "wrong", "-m", "hi",
    )
    assert code == 3
    assert json.loads(err)["error"]["code"] == "auth_error"


def test_full_canonical_exchange(cli, cli_room):
    room = cli_room["room_id"]

    code, out, _ = cli(
        "--json", "send", "--room", room, "--from", "claude", "--to", "codex",
        "--type", "task", "-m", "Adversarially review commit abc123 for race conditions.",
        "--artifact", "git:abc123", "--constraint", "Preserve the existing runtime contract",
        "--request-type", "review",
    )
    assert code == 0
    sent = json.loads(out)
    assert sent["next_speaker"] == "codex"
    task_id = sent["message"]["task_id"]

    code, out, _ = cli("--json", "turn", "--room", room, "--as", "codex")
    assert json.loads(out)["state"] == "your_turn"

    code, out, _ = cli(
        "--json", "send", "--room", room, "--from", "codex", "--type", "response",
        "--status", "complete", "--task", task_id, "--confidence", "0.7",
        "-m", "Two interleavings look unsafe.", "--claim", "retry path is racy",
    )
    assert code == 0

    code, out, _ = cli("--json", "read", "--room", room)
    messages = json.loads(out)["messages"]
    assert [m["sender"] for m in messages] == ["claude", "codex"]
    assert messages[1]["confidence"] == 0.7


def test_human_readable_transcript_reads_like_chat(cli, cli_room):
    room = cli_room["room_id"]
    cli("--json", "send", "--room", room, "--from", "claude", "-m", "hello codex")
    code, out, _ = cli("read", "--room", room)
    assert code == 0
    assert "claude" in out and "hello codex" in out


def test_pass_from_the_cli(cli, cli_room):
    room = cli_room["room_id"]
    cli("--json", "send", "--room", room, "--from", "claude", "--to", "codex", "--type", "task",
        "-m", "anything?")
    code, out, _ = cli("--json", "pass", "--room", room, "--as", "codex", "--reason", "nothing new")
    assert code == 0
    assert json.loads(out)["held_floor"] is True


def test_interrupt_command_is_human_only(cli, cli_room):
    room = cli_room["room_id"]
    cli("--json", "send", "--room", room, "--from", "claude", "--to", "codex", "--type", "task",
        "-m", "review")

    code, out, _ = cli("--json", "interrupt", "--room", room, "--as", "human",
                       "-m", "check the retry path too")
    assert code == 0
    payload = json.loads(out)
    assert payload["message"]["human_override"] is True
    assert payload["interrupted"]["participant_name"] == "codex"


def test_status_and_participants_render(cli, cli_room):
    room = cli_room["room_id"]
    code, out, _ = cli("status", "--room", room)
    assert code == 0
    assert "Participants:" in out and "claude" in out and "Queue:" in out

    code, out, _ = cli("participants", "--room", room)
    assert code == 0 and "codex" in out


def test_events_command_shows_the_audit_log(cli, cli_room):
    room = cli_room["room_id"]
    cli("--json", "send", "--room", room, "--from", "claude", "-m", "hi")
    code, out, _ = cli("--json", "events", "--room", room)
    assert code == 0
    types = [e["event_type"] for e in json.loads(out)["events"]]
    assert "room.created" in types and "message.sent" in types


def test_memory_commands(cli, cli_room):
    room = cli_room["room_id"]
    code, out, _ = cli("--json", "memory", "show", "--room", room)
    assert code == 0
    assert json.loads(out)["memory"]["goal"] == "no races"

    # Positionals go together, either before the flags or after them.
    code, _, _ = cli("--json", "memory", "set", "current_task", "review commit abc123",
                     "--room", room, "--as", "claude")
    assert code == 0
    code, _, _ = cli("--json", "memory", "--room", room, "--as", "codex",
                     "add", "decisions", "keep the runtime contract")
    assert code == 0

    code, out, _ = cli("--json", "memory", "show", "--room", room)
    memory = json.loads(out)["memory"]
    assert memory["current_task"].startswith("review commit abc123")
    assert "keep the runtime contract" in memory["decisions"][0]

    code, out, _ = cli("memory", "show", "--room", room)
    assert "## Decisions" in out


def test_memory_defaults_to_show(cli, cli_room):
    code, out, _ = cli("memory", "--room", room_id := cli_room["room_id"])
    assert code == 0 and room_id
    assert "## Goal" in out


def test_request_floor_and_queue_rendering(cli, cli_room):
    room = cli_room["room_id"]
    cli("--json", "send", "--room", room, "--from", "claude", "--to", "codex", "--type", "task",
        "-m", "review")
    code, out, _ = cli("--json", "request-floor", "--room", room, "--as", "claude",
                       "--priority", "optional")
    assert code == 0
    assert json.loads(out)["queue"][0]["priority_name"] == "optional"


def test_pause_resume_stop_from_the_cli(cli, cli_room):
    room = cli_room["room_id"]
    assert cli("--json", "pause-room", "--room", room, "--as", "human")[0] == 0
    code, _, err = cli("--json", "send", "--room", room, "--from", "claude", "-m", "hi")
    assert code == 6 and json.loads(err)["error"]["code"] == "room_paused"

    assert cli("--json", "resume-room", "--room", room, "--as", "human")[0] == 0
    assert cli("--json", "send", "--room", room, "--from", "claude", "-m", "hi")[0] == 0

    assert cli("--json", "stop-room", "--room", room, "--as", "human")[0] == 0
    code, _, err = cli("--json", "send", "--room", room, "--from", "claude", "-m", "hi")
    assert code == 6 and json.loads(err)["error"]["code"] == "room_stopped"


def test_remove_requires_human_authority(cli, cli_room):
    room = cli_room["room_id"]
    code, _, err = cli("--json", "remove", "--room", room, "--as", "claude", "--participant", "codex")
    assert code == 3
    assert json.loads(err)["error"]["code"] == "human_required"

    assert cli("--json", "remove", "--room", room, "--as", "human", "--participant", "codex")[0] == 0
    code, _, err = cli("--json", "send", "--room", room, "--from", "codex", "-m", "still here")
    assert code == 3


def test_config_changes_the_autonomy_limit(cli, cli_room):
    room = cli_room["room_id"]
    code, out, _ = cli("--json", "config", "--room", room, "--as", "human", "--max-agent-turns", "2")
    assert code == 0
    assert json.loads(out)["autonomy"]["max_consecutive_agent_turns"] == 2


def test_export_rebuilds_artifacts(cli, cli_room):
    room = cli_room["room_id"]
    cli("--json", "send", "--room", room, "--from", "claude", "-m", "hi")
    code, out, _ = cli("--json", "export", "--room", room)
    assert code == 0
    assert json.loads(out)["message_count"] == 1


def test_provenance_command(cli, cli_room):
    room = cli_room["room_id"]
    code, out, _ = cli("--json", "send", "--room", room, "--from", "claude", "-m", "hi")
    message_id = json.loads(out)["message"]["message_id"]
    code, out, _ = cli("--json", "provenance", "--room", room, "--message", message_id)
    assert code == 0
    assert json.loads(out)["message"]["message_id"] == message_id


def test_rooms_listing(cli, cli_room):
    code, out, _ = cli("--json", "rooms")
    assert code == 0
    assert cli_room["room_id"] in [r["room_id"] for r in json.loads(out)["rooms"]]


def test_missing_room_is_a_validation_error(cli, workspace):
    code, _, err = cli("--json", "read")
    assert code == 2
    assert json.loads(err)["error"]["code"] == "validation_error"


def test_unknown_room_exit_code(cli, cli_room):
    code, _, err = cli("--json", "read", "--room", "room_" + "z" * 20, "--token", "x")
    assert code == 4
    assert json.loads(err)["error"]["code"] == "not_found"


def test_bad_metadata_json_is_rejected(cli, cli_room):
    code, _, err = cli(
        "--json", "send", "--room", cli_room["room_id"], "--from", "claude",
        "-m", "hi", "--meta", "not json",
    )
    assert code == 2


def test_duplicate_join_exit_code(cli, cli_room):
    code, _, err = cli("--json", "join", cli_room["observer_token"], "--name", "claude")
    assert code == 5
    assert json.loads(err)["error"]["code"] == "duplicate_participant"


def test_env_vars_supply_room_and_identity(cli, cli_room, monkeypatch):
    monkeypatch.setenv("SYNCHRI_ROOM", cli_room["room_id"])
    monkeypatch.setenv("SYNCHRI_PARTICIPANT", "claude")
    code, out, _ = cli("--json", "send", "-m", "sent with no flags")
    assert code == 0
    assert json.loads(out)["message"]["sender"] == "claude"
