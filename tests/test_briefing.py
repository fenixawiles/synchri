"""Session continuity: repo binding, rediscovery, and the join-time briefing.

The two things that must happen at the jump, without the human arranging them:
the agent learns which working tree the room is about, and it is told where
durable progress belongs — the room ledger for shared conclusions, its own
platform memory for its working context.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from synchri import repo
from synchri.briefing import DEFAULT_MEMORY_NOTE, memory_note_for
from synchri.broker import Broker, Credential
from synchri.cli.main import main

from helpers import make_room


@pytest.fixture
def git_repo(tmp_path):
    """A real git repository to bind rooms to."""
    root = tmp_path / "project"
    root.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": __import__("os").environ.get("PATH", ""),
        "HOME": str(tmp_path),
    }
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, env=env, capture_output=True)
    (root / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=env, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", "init"], check=True, env=env, capture_output=True
    )
    return root


# ----------------------------------------------------------------------
# repo detection
# ----------------------------------------------------------------------


def test_detect_reads_a_real_repository(git_repo):
    info = repo.detect(git_repo)
    assert info.is_git is True
    assert info.root == str(git_repo.resolve())
    assert info.branch == "main"
    assert info.head and len(info.head) == 40
    assert git_repo.name in info.describe()


def test_detect_degrades_gracefully_outside_a_repository(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    info = repo.detect(plain)
    assert info.is_git is False
    assert info.root == str(plain.resolve())
    assert "not a git repository" in info.describe()


def test_a_subdirectory_counts_as_the_same_tree(git_repo):
    nested = git_repo / "src" / "deep"
    nested.mkdir(parents=True)
    assert repo.same_tree(str(git_repo), repo.detect(nested)) is True


def test_a_different_tree_does_not_match(git_repo, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert repo.same_tree(str(git_repo), repo.detect(other)) is False


def test_a_room_with_no_binding_matches_anything(tmp_path):
    assert repo.same_tree(None, repo.detect(tmp_path)) is True


# ----------------------------------------------------------------------
# repo binding and rediscovery
# ----------------------------------------------------------------------


def test_a_room_records_the_repository_it_is_about(broker, git_repo):
    created = broker.create_room("PR 89", workspace_root=str(git_repo))
    assert created["repo"]["root"] == str(git_repo.resolve())
    assert created["repo"]["branch"] == "main"

    status = broker.room_status(
        created["room_id"],
        credential=Credential(created["human"]["name"], created["human"]["secret"]),
    )
    assert status["room"]["workspace_root"] == str(git_repo.resolve())
    assert status["room"]["repo_head"]


def test_rooms_are_rediscoverable_from_the_working_tree(broker, git_repo, monkeypatch):
    created = broker.create_room("Findable", workspace_root=str(git_repo))
    broker.create_room("Unrelated", workspace_root=None)

    monkeypatch.chdir(git_repo)
    found = broker.rooms_for_workspace()
    assert [r["room_id"] for r in found] == [created["room_id"]]


def test_rediscovery_finds_the_newest_active_room(broker, git_repo):
    older = broker.create_room("Older", workspace_root=str(git_repo))
    newer = broker.create_room("Newer", workspace_root=str(git_repo))
    found = broker.rooms_for_workspace(str(git_repo))
    assert found[0]["room_id"] == newer["room_id"]
    assert older["room_id"] in [r["room_id"] for r in found]


def test_stopped_rooms_are_not_rediscovered(broker, git_repo):
    created = broker.create_room("Done", workspace_root=str(git_repo))
    broker.stop_room(
        created["room_id"],
        credential=Credential(created["human"]["name"], created["human"]["secret"]),
    )
    assert broker.rooms_for_workspace(str(git_repo)) == []


def test_a_new_session_resumes_the_room_without_a_room_id(workspace, git_repo, monkeypatch, capsys):
    """The spin-up promise: same repo, new process, same conversation."""
    monkeypatch.chdir(git_repo)

    def cli(*args):
        code = main(["--home", str(workspace.home), *args])
        return code, capsys.readouterr().out

    _, out = cli("--json", "create-room", "--name", "Resumable", "--agents", "codex")
    created = json.loads(out)
    cli("--json", "join", created["invites"][0]["token"], "--name", "codex")
    cli("--json", "send", "--from", "codex", "-m", "progress from session one")

    # Simulate a brand new session: forget the room id entirely.
    (workspace.home / "current_room").unlink()
    code, out = cli("--json", "read")
    assert code == 0
    payload = json.loads(out)
    assert payload["room_id"] == created["room_id"]
    assert [m["content"] for m in payload["messages"]] == ["progress from session one"]


# ----------------------------------------------------------------------
# the briefing
# ----------------------------------------------------------------------


def test_joining_returns_a_briefing_automatically(broker, git_repo):
    created = broker.create_room(
        "PR 89", agents=["codex"], goal="find races", workspace_root=str(git_repo)
    )
    joined = broker.join(created["invites"][0]["token"], "codex")

    briefing = joined["briefing"]
    assert briefing["participant"] == "codex"
    assert briefing["room_id"] == created["room_id"]
    assert briefing["repo"]["root"] == str(git_repo.resolve())
    assert briefing["memory"]["goal"] == "find races"
    assert briefing["returning"] is False
    assert "PERSISTENCE" in briefing["text"]


def test_the_briefing_carries_the_persistence_contract(room):
    briefing = room.broker.briefing(room.room_id, credential=room.credential("codex"))
    note = " ".join(briefing.memory_note.split())  # the note is hard-wrapped

    assert "Synchri durably stores the ROOM" in note
    assert "does NOT store your own reasoning" in note
    assert "synchri memory add decisions" in note
    assert "persistent memory your own platform gives you" in note
    assert "--as codex" in note, "the note is addressed to the participant reading it"


def test_the_persistence_note_is_overridable_per_room(broker):
    created = broker.create_room(
        "Custom", agents=["codex"], memory_note="Write everything to NOTES.md, {name}."
    )
    joined = broker.join(created["invites"][0]["token"], "codex")
    assert joined["briefing"]["memory_note"] == "Write everything to NOTES.md, codex."


def test_a_custom_note_with_stray_braces_is_still_delivered():
    assert memory_note_for("use {curly} braces", "codex") == "use {curly} braces"


def test_the_default_note_is_used_when_a_room_sets_none(room):
    briefing = room.broker.briefing(room.room_id, credential=room.credential("claude"))
    assert briefing.memory_note == DEFAULT_MEMORY_NOTE.format(name="claude")


def test_the_briefing_reconstitutes_shared_memory(room):
    room.broker.memory_write(
        room.room_id,
        credential=room.credential("human"),
        section="decisions",
        value="take the lock before reading the flag",
        mode="add",
    )
    briefing = room.broker.briefing(room.room_id, credential=room.credential("codex"))
    assert any("take the lock" in d for d in briefing.memory["decisions"])
    assert "take the lock" in briefing.render()


def test_a_returning_agent_is_told_what_it_missed(room):
    room.send("codex", "my last word before leaving")
    room.send("claude", "something codex missed")
    room.send("human", "and a human note")

    briefing = room.broker.briefing(room.room_id, credential=room.credential("codex"))
    assert briefing.returning is True
    missed = [m["content"] for m in briefing.missed_messages]
    assert missed == ["something codex missed", "and a human note"]
    assert "WHILE YOU WERE AWAY" in briefing.render()


def test_a_first_time_agent_sees_the_conversation_so_far(room):
    room.send("claude", "earlier context")
    briefing = room.broker.briefing(room.room_id, credential=room.credential("codex"))
    assert briefing.returning is False
    assert "CONVERSATION SO FAR" in briefing.render()


def test_the_briefing_surfaces_a_pending_request(room):
    room.send("claude", "review commit abc123", target="codex", message_type="task")
    briefing = room.broker.briefing(room.room_id, credential=room.credential("codex"))
    assert briefing.your_turn is True
    assert briefing.pending_request["content"] == "review commit abc123"
    assert "IT IS YOUR TURN RIGHT NOW." in briefing.render()


def test_the_briefing_tells_an_idle_agent_to_wait(room):
    briefing = room.broker.briefing(room.room_id, credential=room.credential("codex"))
    assert briefing.your_turn is False
    assert "synchri wait --as codex" in briefing.render()


def test_the_briefing_warns_when_the_agent_is_in_the_wrong_tree(broker, git_repo, tmp_path, monkeypatch):
    created = broker.create_room("Bound", agents=["codex"], workspace_root=str(git_repo))
    elsewhere = tmp_path / "other-project"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    joined = broker.join(created["invites"][0]["token"], "codex")
    mismatch = joined["briefing"]["repo_mismatch"]
    assert mismatch and str(git_repo.resolve()) in mismatch
    assert "!!" in joined["briefing"]["text"]


def test_no_warning_when_the_agent_is_in_the_right_tree(broker, git_repo, monkeypatch):
    created = broker.create_room("Bound", agents=["codex"], workspace_root=str(git_repo))
    monkeypatch.chdir(git_repo)
    joined = broker.join(created["invites"][0]["token"], "codex")
    assert joined["briefing"]["repo_mismatch"] is None


def test_the_briefing_requires_a_participant_identity(room):
    from synchri.errors import AuthError

    with pytest.raises(AuthError):
        room.broker.briefing(room.room_id, credential=Credential(room_token=room.observer_token))


def test_the_briefing_is_room_scoped(broker):
    room_a = make_room(broker, "codex", name="A")
    room_b = make_room(broker, "codex", name="B")
    room_a.send("codex", "only in A")

    briefing = broker.briefing(room_b.room_id, credential=room_b.credential("codex"))
    assert "only in A" not in briefing.render()


def test_briefing_survives_a_restart_and_still_orients(workspace, git_repo):
    """A later session gets the room's durable state back, in full."""
    with Broker(workspace) as first:
        created = first.create_room("Durable", agents=["codex"], workspace_root=str(git_repo))
        joined = first.join(created["invites"][0]["token"], "codex")
        codex = Credential("codex", joined["secret"])
        human = Credential(created["human"]["name"], created["human"]["secret"])
        first.memory_write(
            created["room_id"],
            credential=human,
            section="decisions",
            value="survives the session",
            mode="add",
        )
        first.send(
            created["room_id"],
            credential=codex,
            draft=__import__("synchri").MessageDraft(content="work from session one"),
        )

    # A brand new process, holding only the credential from the session file.
    with Broker(workspace) as second:
        briefing = second.briefing(created["room_id"], credential=codex)
        assert any("survives the session" in d for d in briefing.memory["decisions"])
        assert briefing.repo["root"] == str(git_repo.resolve())
        assert briefing.room_name == "Durable"
        rendered = briefing.render()
        assert "survives the session" in rendered
        assert "PERSISTENCE" in rendered


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def test_join_prints_the_briefing(workspace, capsys, git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)

    def cli(*args):
        code = main(["--home", str(workspace.home), *args])
        return code, capsys.readouterr().out

    _, out = cli("--json", "create-room", "--name", "Briefed", "--agents", "codex",
                 "--goal", "keep the contract")
    created = json.loads(out)
    code, out = cli("join", created["invites"][0]["token"], "--name", "codex")

    assert code == 0
    assert "SYNCHRI BRIEFING" in out
    assert "keep the contract" in out
    assert "PERSISTENCE" in out
    assert str(git_repo.resolve()) in out


def test_briefing_command_can_be_refetched(workspace, capsys, git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)

    def cli(*args):
        code = main(["--home", str(workspace.home), *args])
        return code, capsys.readouterr().out

    _, out = cli("--json", "create-room", "--name", "Refetch", "--agents", "codex")
    created = json.loads(out)
    cli("--json", "join", created["invites"][0]["token"], "--name", "codex")

    code, out = cli("briefing", "--as", "codex")
    assert code == 0
    assert "SYNCHRI BRIEFING" in out

    code, out = cli("--json", "briefing", "--as", "codex")
    assert json.loads(out)["participant"] == "codex"


def test_rooms_here_filters_by_repository(workspace, capsys, git_repo, tmp_path, monkeypatch):
    def cli(*args):
        code = main(["--home", str(workspace.home), *args])
        return code, capsys.readouterr().out

    monkeypatch.chdir(git_repo)
    cli("--json", "create-room", "--name", "In repo")
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    cli("--json", "create-room", "--name", "Elsewhere")

    monkeypatch.chdir(git_repo)
    _, out = cli("--json", "rooms", "--here")
    names = [r["name"] for r in json.loads(out)["rooms"]]
    assert names == ["In repo"]


def test_create_room_reports_the_binding(workspace, capsys, git_repo, monkeypatch):
    monkeypatch.chdir(git_repo)
    code = main(["--home", str(workspace.home), "create-room", "--name", "Bound"])
    out = capsys.readouterr().out
    assert code == 0
    assert str(git_repo.resolve()) in out
    assert "bound to that working tree" in out


def test_conducted_agents_receive_the_persistence_note(broker, tmp_path):
    """A conducted agent is told the same thing an attached one is."""
    from synchri.runner import AgentCommand, Conductor

    room = make_room(broker, "codex")
    room.send("human", "codex, go", target="codex")

    script = tmp_path / "echo.py"
    script.write_text("import sys\nprint('SAW:' + sys.stdin.read())\n", encoding="utf-8")
    conductor = Conductor(
        broker,
        room.room_id,
        {"codex": AgentCommand.parse(f"codex={sys.executable} {script}", timeout=30)},
        {"codex": room.credential("codex")},
        room.credential("human"),
    )
    conductor.run(max_turns=1)

    relayed = room.messages()[-1]["content"]
    assert "synchri memory add decisions" in relayed
    assert "your own platform" in relayed
