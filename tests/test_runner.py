"""Single-terminal operation: the conductor and the agent-command contract."""

from __future__ import annotations

import json
import sys
import threading

import pytest

from synchri.errors import ValidationError
from synchri.runner import AgentCommand, Conductor, parse_directives

from helpers import make_room


# ----------------------------------------------------------------------
# fake agents
# ----------------------------------------------------------------------


def write_agent(tmp_path, name: str, body: str) -> str:
    """Create a stand-in agent script and return the --agent spec for it."""
    path = tmp_path / f"{name}.py"
    path.write_text(
        "import sys\nprompt = sys.stdin.read()\n" + body,
        encoding="utf-8",
    )
    return f"{name}={sys.executable} {path}"


def conductor_for(room, tmp_path, specs: dict[str, str], **kwargs) -> Conductor:
    agents = {}
    for name, body in specs.items():
        agents[name] = AgentCommand.parse(write_agent(tmp_path, name, body), timeout=30)
    credentials = {name: room.credential(name) for name in agents}
    return Conductor(
        room.broker,
        room.room_id,
        agents,
        credentials,
        room.credential("human"),
        **kwargs,
    )


# ----------------------------------------------------------------------
# directive parsing
# ----------------------------------------------------------------------


def test_directives_are_stripped_from_the_reply():
    body, directives = parse_directives(
        "Found one race in retry.py.\n\nSYNCHRI-HANDOFF: claude\nSYNCHRI-CONFIDENCE: 0.7\n"
    )
    assert body == "Found one race in retry.py."
    assert directives.handoff == "claude"
    assert directives.confidence == 0.7
    assert directives.to is None


def test_only_trailing_directives_count():
    """An agent quoting the convention mid-review must not redirect the room."""
    text = "The docs say to write SYNCHRI-TO: codex at the end.\nBut I am still explaining."
    body, directives = parse_directives(text)
    assert directives.to is None
    assert body == text


def test_pass_directive():
    body, directives = parse_directives("SYNCHRI-PASS")
    assert directives.passed is True
    assert body == ""


def test_target_beats_handoff_when_an_agent_asks_for_both():
    _, directives = parse_directives("x\nSYNCHRI-TO: codex\nSYNCHRI-HANDOFF: gemini")
    assert directives.to == "codex"
    assert directives.handoff is None
    assert directives.warnings


def test_unparseable_confidence_is_ignored_with_a_warning():
    _, directives = parse_directives("x\nSYNCHRI-CONFIDENCE: very high")
    assert directives.confidence is None
    assert directives.warnings


def test_gate_directives_keep_evidence_together_and_request_completion():
    body, directives = parse_directives(
        "Verified the login flow.\n\n"
        "SYNCHRI-GATE: AUTH-01|pass|implemented and checked\n"
        "SYNCHRI-EVIDENCE: src/auth.py validates the session\n"
        "SYNCHRI-TEST: pytest tests/test_auth.py::test_login\n"
        "SYNCHRI-COMMIT: abc123\n"
        "SYNCHRI-COMPLETE\n"
    )
    assert body == "Verified the login flow."
    assert directives.complete_requested is True
    assert directives.gate_updates[0].gate_id == "AUTH-01"
    assert directives.gate_updates[0].status == "pass"
    assert directives.gate_updates[0].tests == ["pytest tests/test_auth.py::test_login"]


def test_approval_directive_is_structured_and_not_shown_in_the_reply():
    body, directives = parse_directives(
        "I need permission to install the package.\n\n"
        "SYNCHRI-TO: human\n"
        "SYNCHRI-STATUS: blocked\n"
        "SYNCHRI-APPROVAL: repo.install_deps|Install the missing development dependency."
    )
    assert body == "I need permission to install the package."
    assert directives.to == "human"
    assert directives.status == "blocked"
    assert directives.approval_capability == "repo.install_deps"
    assert directives.approval_request == "Install the missing development dependency."


def test_gate_directives_are_preserved_when_an_agent_passes():
    body, directives = parse_directives(
        "SYNCHRI-GATE: AUTH-01|in_progress|started the implementation\n"
        "SYNCHRI-EVIDENCE: src/auth.py\n"
        "SYNCHRI-PASS\n"
    )
    assert body == ""
    assert directives.passed is True
    assert directives.gate_updates[0].evidence == ["src/auth.py"]


def test_reply_without_directives_is_untouched():
    body, directives = parse_directives("just a normal answer")
    assert body == "just a normal answer"
    assert (directives.to, directives.handoff, directives.passed) == (None, None, False)


# ----------------------------------------------------------------------
# agent command parsing
# ----------------------------------------------------------------------


def test_agent_spec_parsing():
    agent = AgentCommand.parse("codex=codex exec {prompt}")
    assert agent.name == "codex"
    assert agent.argv == ["codex", "exec", "{prompt}"]
    assert agent.takes_prompt_in_argv is True
    assert agent.build_argv("hello") == ["codex", "exec", "hello"]


def test_agent_without_placeholder_uses_stdin():
    agent = AgentCommand.parse("claude=claude -p")
    assert agent.takes_prompt_in_argv is False


@pytest.mark.parametrize("spec", ["", "no-equals-sign", "=command", "name=", "name='unclosed"])
def test_malformed_agent_specs_are_rejected(spec):
    with pytest.raises(ValidationError):
        AgentCommand.parse(spec)


def test_prompt_is_never_interpreted_as_shell_syntax(tmp_path):
    """Commands run without a shell, so metacharacters in a prompt are inert."""
    script = tmp_path / "echo_arg.py"
    script.write_text("import sys\nprint(sys.argv[1])\n", encoding="utf-8")
    agent = AgentCommand.parse(f"x={sys.executable} {script} {{prompt}}")
    sentinel = tmp_path / "pwned"
    result = agent.invoke(f"hi; touch {sentinel}")

    assert result.ok
    assert result.stdout.strip() == f"hi; touch {sentinel}"
    assert not sentinel.exists(), "the prompt must not reach a shell"


def test_agent_timeout_is_reported_not_raised(tmp_path):
    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    agent = AgentCommand.parse(f"x={sys.executable} {script}", timeout=0.5)
    result = agent.invoke("go")
    assert result.timed_out is True
    assert result.ok is False


def test_cancelled_agent_never_starts_a_new_provider_process(tmp_path):
    agent = AgentCommand.parse("x=definitely-not-a-real-binary-xyz")
    cancelled = threading.Event()
    cancelled.set()
    result = agent.invoke("go", cancel_event=cancelled)
    assert result.cancelled is True
    assert result.stderr == ""


def test_missing_executable_is_reported_not_raised():
    agent = AgentCommand.parse("x=definitely-not-a-real-binary-xyz")
    result = agent.invoke("go")
    assert result.ok is False
    assert "could not run the agent" in result.stderr


# ----------------------------------------------------------------------
# the conductor
# ----------------------------------------------------------------------


def test_conductor_relays_a_targeted_exchange_with_no_human_action(broker, tmp_path):
    """The whole point: an agent's answer reaches the room without a human copying it."""
    room = make_room(broker, "claude", "codex")
    room.send("claude", "review commit abc123", target="codex", message_type="task")

    conductor = conductor_for(
        room,
        tmp_path,
        {
            "codex": "print('Found a race in retry.py:40')\nprint('SYNCHRI-HANDOFF: claude')\n",
            "claude": "print('Fixed it.')\n",
        },
    )
    report = conductor.run(max_turns=5)

    assert report.reason == "idle"
    assert [t["participant"] for t in report.turns] == ["codex", "claude"]
    contents = [m["content"] for m in room.messages()]
    assert contents == ["review commit abc123", "Found a race in retry.py:40", "Fixed it."]


def test_conductor_exits_before_another_turn_when_the_session_is_cancelled(broker, tmp_path):
    room = make_room(broker, "claude")
    room.send("human", "begin", target="claude")
    cancelled = threading.Event()
    cancelled.set()
    conductor = conductor_for(
        room,
        tmp_path,
        {"claude": "print('this must not run')\n"},
        cancel_event=cancelled,
    )
    report = conductor.run()
    assert report.reason == "cancelled"
    assert report.turns == []
    assert [message["content"] for message in room.messages()] == ["begin"]


def test_human_direction_forces_reviewer_after_the_builder(broker, tmp_path):
    room = make_room(broker, "claude", "codex")
    room.send(
        "human",
        "Prioritize auth.",
        target="claude",
        metadata={"human_direction": {"lead": "claude", "reviewer": "codex"}},
    )
    conductor = conductor_for(
        room,
        tmp_path,
        {
            "claude": "print('I will prioritize auth.')\nprint('SYNCHRI-TO: human')\n",
            "codex": "print('I reviewed the auth plan.')\n",
        },
    )

    report = conductor.run(max_turns=2)

    assert [turn["participant"] for turn in report.turns] == ["claude", "codex"]
    messages = room.messages()
    assert messages[-2]["handoff_target"] == "codex"
    assert messages[-1]["sender"] == "codex"
    assert messages[-2]["metadata"]["human_direction_review"] == {"reviewer": "codex"}


def test_human_direction_cannot_be_skipped_with_a_pass(broker, tmp_path):
    room = make_room(broker, "claude", "codex")
    room.send(
        "human",
        "Prioritize auth.",
        target="claude",
        metadata={"human_direction": {"lead": "claude", "reviewer": "codex"}},
    )
    conductor = conductor_for(
        room,
        tmp_path,
        {
            "claude": "print('SYNCHRI-PASS')\n",
            "codex": "print('I reviewed the auth priority.')\n",
        },
    )

    report = conductor.run(max_turns=2)

    assert [turn["participant"] for turn in report.turns] == ["claude", "codex"]
    assert room.messages()[-2]["message_type"] == "pass"
    assert room.messages()[-2]["metadata"]["human_direction_review"] == {"reviewer": "codex"}


def test_human_direction_reaches_the_reviewer_when_the_builder_crashes(broker, tmp_path):
    room = make_room(broker, "claude", "codex")
    room.send(
        "human",
        "Prioritize auth.",
        target="claude",
        metadata={"human_direction": {"lead": "claude", "reviewer": "codex"}},
    )
    conductor = conductor_for(
        room,
        tmp_path,
        {
            "claude": "sys.stderr.write('provider failed')\nsys.exit(3)\n",
            "codex": "print('The builder failed; I reviewed the auth direction and the current state.')\n",
        },
    )

    report = conductor.run(max_turns=2)

    assert [turn["participant"] for turn in report.turns] == ["claude", "codex"]
    assert room.messages()[-2]["response_status"] == "failed"
    assert room.messages()[-2]["handoff_target"] == "codex"


def test_conductor_forwards_the_request_into_the_agent_prompt(broker, tmp_path):
    room = make_room(broker, "claude", "codex")
    room.send(
        "claude",
        "review commit abc123",
        target="codex",
        message_type="task",
        constraints=["Preserve the runtime contract"],
        artifact_references=["git:abc123"],
    )
    # The agent echoes what it was told, so we can assert on it.
    conductor = conductor_for(
        room, tmp_path, {"codex": "print('SAW:' + prompt.replace(chr(10), ' | '))\n"}
    )
    conductor.run(max_turns=1)

    relayed = room.messages()[-1]["content"]
    assert "review commit abc123" in relayed
    assert "Preserve the runtime contract" in relayed
    assert "git:abc123" in relayed
    assert "claude addressed you directly" in relayed


def test_conductor_includes_shared_memory_in_the_prompt(broker, tmp_path):
    room = make_room(broker, "codex", goal="find race conditions")
    room.broker.memory_write(
        room.room_id,
        credential=room.credential("human"),
        section="constraints",
        value="no new dependencies",
        mode="add",
    )
    room.send("human", "codex, take a look", target="codex")

    conductor = conductor_for(room, tmp_path, {"codex": "print('SAW:' + prompt)\n"})
    conductor.run(max_turns=1)
    relayed = room.messages()[-1]["content"]
    assert "find race conditions" in relayed
    assert "no new dependencies" in relayed


def test_conductor_can_omit_memory(broker, tmp_path):
    room = make_room(broker, "codex", goal="secret goal text")
    room.send("human", "go", target="codex")
    conductor = conductor_for(
        room, tmp_path, {"codex": "print('SAW:' + prompt)\n"}, include_memory=False
    )
    conductor.run(max_turns=1)
    assert "secret goal text" not in room.messages()[-1]["content"]


def test_empty_agent_output_becomes_a_pass(broker, tmp_path):
    room = make_room(broker, "claude", "codex")
    room.send("claude", "anything to add?", target="codex", message_type="task")

    conductor = conductor_for(room, tmp_path, {"codex": "pass\n"})
    report = conductor.run(max_turns=2)

    assert report.turns[0]["status"] == "passed"
    assert room.messages()[-1]["message_type"] == "pass"


def test_explicit_pass_directive(broker, tmp_path):
    room = make_room(broker, "claude", "codex")
    room.send("claude", "anything?", target="codex", message_type="task")
    conductor = conductor_for(room, tmp_path, {"codex": "print('SYNCHRI-PASS')\n"})
    report = conductor.run(max_turns=2)
    assert report.turns[0]["status"] == "passed"


def test_a_failing_agent_reports_into_the_room_and_releases_the_floor(broker, tmp_path):
    room = make_room(broker, "claude", "codex")
    room.send("claude", "review", target="codex", message_type="task")

    conductor = conductor_for(
        room, tmp_path, {"codex": "sys.stderr.write('boom\\n')\nsys.exit(3)\n"}
    )
    report = conductor.run(max_turns=2)

    assert report.turns[0]["status"] == "failed"
    failure = room.messages()[-1]
    assert failure["sender"] == "codex"
    assert failure["response_status"] == "failed"
    assert "exit 3" in failure["content"] and "boom" in failure["content"]
    assert room.active_speaker() is None, "a crashed agent must not hold the floor forever"


def test_conductor_stops_at_the_autonomy_limit_instead_of_looping(broker, tmp_path):
    """Two agents that keep addressing each other must still hand the room back."""
    room = make_room(broker, "claude", "codex", max_consecutive_agent_turns=4)
    room.send("claude", "codex, start", target="codex", message_type="task")

    conductor = conductor_for(
        room,
        tmp_path,
        {
            "codex": "print('your turn')\nprint('SYNCHRI-TO: claude')\n",
            "claude": "print('no, your turn')\nprint('SYNCHRI-TO: codex')\n",
        },
    )
    report = conductor.run(max_turns=50)

    assert report.reason == "awaiting_human"
    assert report.turn_count < 50, "the autonomy budget, not --turns, must stop the loop"
    assert room.status()["autonomy"]["awaiting_human"] is True


def test_conductor_respects_its_own_turn_limit(broker, tmp_path):
    room = make_room(broker, "claude", "codex", max_consecutive_agent_turns=100)
    room.send("claude", "codex, start", target="codex", message_type="task")
    conductor = conductor_for(
        room,
        tmp_path,
        {
            "codex": "print('a')\nprint('SYNCHRI-TO: claude')\n",
            "claude": "print('b')\nprint('SYNCHRI-TO: codex')\n",
        },
    )
    report = conductor.run(max_turns=3)
    assert report.reason == "turn_limit"
    assert report.turn_count == 3


def test_conductor_yields_when_an_unmanaged_participant_holds_the_floor(broker, tmp_path):
    room = make_room(broker, "claude", "codex")
    room.send("codex", "claude, over to you", target="claude", message_type="task")

    # Only codex is managed here; claude is driven from some other terminal.
    conductor = conductor_for(room, tmp_path, {"codex": "print('hi')\n"})
    report = conductor.run(max_turns=5)

    assert report.reason == "unmanaged_speaker"
    assert report.turn_count == 0
    assert room.active_speaker() == "claude", "the room state is untouched"


def test_conductor_stops_when_the_room_is_stopped(broker, tmp_path):
    room = make_room(broker, "codex")
    room.broker.stop_room(room.room_id, credential=room.credential("human"))
    conductor = conductor_for(room, tmp_path, {"codex": "print('hi')\n"})
    report = conductor.run(max_turns=5)
    assert report.reason == "room_stopped"
    assert report.turn_count == 0


def test_conductor_stops_when_the_room_is_paused(broker, tmp_path):
    room = make_room(broker, "codex")
    room.send("human", "codex, look", target="codex")
    room.broker.pause_room(room.room_id, credential=room.credential("human"))
    conductor = conductor_for(room, tmp_path, {"codex": "print('hi')\n"})
    assert conductor.run(max_turns=5).reason == "room_paused"


def test_conductor_requires_a_credential_for_every_managed_agent(broker, tmp_path):
    room = make_room(broker, "codex")
    agent = AgentCommand.parse(write_agent(tmp_path, "gemini", "print('hi')\n"))
    with pytest.raises(ValidationError):
        Conductor(
            room.broker,
            room.room_id,
            {"gemini": agent},
            {},
            room.credential("human"),
        )


def test_conductor_requires_at_least_one_agent(broker):
    room = make_room(broker, "codex")
    with pytest.raises(ValidationError):
        Conductor(room.broker, room.room_id, {}, {}, room.credential("human"))


def test_conductor_directives_can_open_a_targeted_task(broker, tmp_path):
    room = make_room(broker, "claude", "codex")
    room.send("human", "claude, kick off", target="claude")
    conductor = conductor_for(
        room,
        tmp_path,
        {"claude": "print('codex, please review')\nprint('SYNCHRI-TO: codex')\n"},
    )
    report = conductor.run(max_turns=1)

    assert report.turns[0]["target"] == "codex"
    assert room.messages()[-1]["message_type"] == "task"
    assert room.active_speaker() == "codex"


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def test_run_command_drives_a_room_from_one_terminal(workspace, tmp_path, capsys):
    from synchri.cli.main import main

    def cli(*args):
        code = main(["--home", str(workspace.home), *args])
        return code, capsys.readouterr().out

    _, out = cli("--json", "create-room", "--name", "One terminal", "--agents", "claude,codex")
    created = json.loads(out)
    for invite in created["invites"]:
        cli("--json", "join", invite["token"], "--name", invite["participant_name"])
    cli("--json", "send", "--from", "claude", "--to", "codex", "--type", "task", "-m", "review")

    spec = write_agent(tmp_path, "codex", "print('found a race')\n")
    code, out = cli("--json", "run", "--agent", spec, "--turns", "2")
    report = json.loads(out)

    assert code == 0
    assert report["turns_run"] == 1
    assert report["turns"][0]["status"] == "spoke"

    _, out = cli("--json", "read")
    assert [m["content"] for m in json.loads(out)["messages"]] == ["review", "found a race"]


def test_run_command_reports_awaiting_human_with_a_distinct_exit_code(workspace, tmp_path, capsys):
    from synchri.cli.main import main

    def cli(*args):
        code = main(["--home", str(workspace.home), *args])
        return code, capsys.readouterr().out

    _, out = cli(
        "--json", "create-room", "--name", "Limit", "--max-agent-turns", "2",
        "--agents", "claude,codex",
    )
    created = json.loads(out)
    for invite in created["invites"]:
        cli("--json", "join", invite["token"], "--name", invite["participant_name"])
    cli("--json", "send", "--from", "claude", "--to", "codex", "--type", "task", "-m", "go")

    specs = [
        write_agent(tmp_path, "codex", "print('a')\nprint('SYNCHRI-TO: claude')\n"),
        write_agent(tmp_path, "claude", "print('b')\nprint('SYNCHRI-TO: codex')\n"),
    ]
    code, out = cli("--json", "run", "--agent", specs[0], "--agent", specs[1], "--turns", "20")
    assert code == 12
    assert json.loads(out)["reason"] == "awaiting_human"


def test_run_start_flag_gives_an_idle_room_its_first_turn(workspace, tmp_path, capsys):
    from synchri.cli.main import main

    def cli(*args):
        code = main(["--home", str(workspace.home), *args])
        return code, capsys.readouterr().out

    _, out = cli("--json", "create-room", "--name", "Kickoff", "--agents", "claude")
    created = json.loads(out)
    invite = created["invites"][0]
    cli("--json", "join", invite["token"], "--name", "claude")

    spec = write_agent(tmp_path, "claude", "print('starting the review')\n")
    code, out = cli("--json", "run", "--agent", spec, "--start", "claude", "--turns", "1")
    assert code == 0
    assert json.loads(out)["turns_run"] == 1

    _, out = cli("--json", "read")
    assert [m["content"] for m in json.loads(out)["messages"]] == ["starting the review"]


def test_run_rejects_a_malformed_agent_spec(workspace, capsys):
    from synchri.cli.main import main

    code = main(["--home", str(workspace.home), "--json", "create-room", "--name", "x"])
    capsys.readouterr()
    code = main(["--home", str(workspace.home), "--json", "run", "--agent", "nonsense"])
    assert code == 2
