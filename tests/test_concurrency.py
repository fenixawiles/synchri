"""Concurrency invariants.

The broker has no daemon, so mutual exclusion comes entirely from SQLite's
write lock.  These tests exercise that rather than assuming it: real threads
with independent connections, and real subprocesses.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

import pytest

from aidapter.broker import Broker
from aidapter.errors import AidapterError
from aidapter.models.envelope import MessageDraft

from helpers import make_room

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_cli(workspace, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "AIDAPTER_HOME": str(workspace.home),
        "PYTHONPATH": REPO_ROOT,
    }
    for name in ("AIDAPTER_ROOM", "AIDAPTER_PARTICIPANT", "AIDAPTER_SECRET", "AIDAPTER_ROOM_TOKEN"):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-m", "aidapter", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"aidapter {' '.join(args)} failed ({result.returncode}):\n{result.stderr}"
        )
    return result


def _create_room(workspace, name: str, agents: str = "claude,codex", *extra: str) -> dict:
    return json.loads(
        _run_cli(
            workspace, "--json", "create-room", "--name", name, "--agents", agents, *extra
        ).stdout
    )


def _join_all(workspace, created: dict) -> None:
    """Redeem every invite the room was created with."""
    for invite in created["invites"]:
        _run_cli(
            workspace, "--json", "join", invite["token"], "--name", invite["participant_name"]
        )


def test_only_one_agent_wins_a_contested_floor(workspace):
    """Ten agents send at once into an idle room; exactly one may hold the floor."""
    with Broker(workspace) as setup:
        agents = [f"agent{i}" for i in range(10)]
        harness = make_room(setup, *agents, max_consecutive_agent_turns=1000)
        room_id = harness.room_id
        credentials = {name: harness.credential(name) for name in agents}

    barrier = threading.Barrier(len(agents))
    outcomes: dict[str, str] = {}
    lock = threading.Lock()

    def attempt(name: str) -> None:
        # A separate Broker per thread means a separate SQLite connection,
        # which is what makes this a real cross-connection contention test.
        with Broker(workspace) as broker:
            barrier.wait(timeout=30)
            try:
                broker.send(
                    room_id,
                    credential=credentials[name],
                    draft=MessageDraft(content=f"{name} speaking"),
                    # Hold the floor so the winner stays identifiable after the
                    # race rather than immediately releasing it.
                    hold_floor=True,
                )
                outcome = "spoke"
            except AidapterError as exc:
                outcome = exc.code
            with lock:
                outcomes[name] = outcome

    threads = [threading.Thread(target=attempt, args=(name,)) for name in agents]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "a send deadlocked"

    winners = [name for name, outcome in outcomes.items() if outcome == "spoke"]
    assert len(winners) == 1, f"exactly one agent may hold the floor, got {outcomes}"

    with Broker(workspace) as broker:
        credential = credentials[winners[0]]
        status = broker.room_status(room_id, credential=credential)
        assert status["active_speaker"] == winners[0]
        messages = broker.read(room_id, credential=credential)["messages"]
        assert [m["sender"] for m in messages] == winners, (
            "only the winner's message may reach the transcript"
        )


def test_concurrent_sequence_numbers_never_collide(workspace):
    """Interleaved writers must not mint the same per-room seq."""
    with Broker(workspace) as setup:
        harness = make_room(setup, "claude", max_consecutive_agent_turns=1000)
        room_id = harness.room_id
        human = harness.credential("human")

    rounds = 12
    threads_count = 4
    barrier = threading.Barrier(threads_count)

    def writer(index: int) -> None:
        with Broker(workspace) as broker:
            barrier.wait(timeout=30)
            for round_index in range(rounds):
                try:
                    broker.send(
                        room_id,
                        credential=human,
                        draft=MessageDraft(content=f"w{index}-{round_index}"),
                    )
                except AidapterError:
                    pass

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
        assert not thread.is_alive()

    with Broker(workspace) as broker:
        messages = broker.read(room_id, credential=human)["messages"]
        seqs = [m["seq"] for m in messages]
        assert len(seqs) == len(set(seqs)), "duplicate sequence numbers"
        assert seqs == sorted(seqs)
        ids = [m["message_id"] for m in messages]
        assert len(ids) == len(set(ids))


def test_concurrent_memory_writes_do_not_lose_entries(workspace):
    """The ledger is a file, so serialize its writers through the room lock."""
    with Broker(workspace) as setup:
        harness = make_room(setup, "claude")
        room_id = harness.room_id
        human = harness.credential("human")

    writers = 4
    per_writer = 5
    barrier = threading.Barrier(writers)

    def writer(index: int) -> None:
        with Broker(workspace) as broker:
            barrier.wait(timeout=30)
            for entry in range(per_writer):
                broker.memory_write(
                    room_id,
                    credential=human,
                    section="open_issues",
                    value=f"issue-{index}-{entry}",
                    mode="add",
                )

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
        assert not thread.is_alive()

    with Broker(workspace) as broker:
        issues = broker.memory_show(room_id, credential=human)["memory"]["open_issues"]
    assert len(issues) == writers * per_writer, "a concurrent ledger write was lost"


def test_two_separate_processes_share_one_room(workspace):
    """The end-to-end claim: two terminals, one room, no shared process."""
    created = _create_room(workspace, "Cross process")
    room_id = created["room_id"]

    _join_all(workspace, created)

    sent = json.loads(
        _run_cli(
            workspace,
            "--json",
            "send",
            "--room",
            room_id,
            "--from",
            "claude",
            "--to",
            "codex",
            "--type",
            "task",
            "-m",
            "review commit abc123",
        ).stdout
    )
    assert sent["next_speaker"] == "codex"

    turn = json.loads(
        _run_cli(workspace, "--json", "turn", "--room", room_id, "--as", "codex").stdout
    )
    assert turn["state"] == "your_turn"
    assert turn["request"]["content"] == "review commit abc123"

    blocked = _run_cli(
        workspace, "--json", "send", "--room", room_id, "--from", "claude", "-m", "me again",
        check=False,
    )
    assert blocked.returncode == 7
    assert json.loads(blocked.stderr)["error"]["code"] == "blocked_targeted_turn"

    _run_cli(
        workspace, "--json", "send", "--room", room_id, "--from", "codex",
        "--type", "response", "--status", "complete", "-m", "no races found",
    )
    transcript = json.loads(_run_cli(workspace, "--json", "read", "--room", room_id).stdout)
    assert [m["content"] for m in transcript["messages"]] == [
        "review commit abc123",
        "no races found",
    ]


def test_wait_in_one_process_unblocks_when_another_process_addresses_it(workspace):
    """``aidapter wait`` is how an agent parks until it is on point."""
    created = _create_room(workspace, "Wait test")
    room_id = created["room_id"]
    _join_all(workspace, created)

    env = {**os.environ, "AIDAPTER_HOME": str(workspace.home), "PYTHONPATH": REPO_ROOT}
    waiter = subprocess.Popen(
        [
            sys.executable, "-m", "aidapter", "--json", "wait",
            "--room", room_id, "--as", "codex", "--timeout", "30", "--poll", "0.1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        _run_cli(
            workspace, "--json", "send", "--room", room_id, "--from", "claude",
            "--to", "codex", "--type", "task", "-m", "your turn",
        )
        stdout, stderr = waiter.communicate(timeout=45)
    except subprocess.TimeoutExpired:  # pragma: no cover - failure path
        waiter.kill()
        pytest.fail("wait did not return after the participant was addressed")

    assert waiter.returncode == 0, stderr
    payload = json.loads(stdout)
    assert payload["state"] == "your_turn"
    assert payload["timed_out"] is False
    assert payload["request"]["content"] == "your turn"


def test_wait_times_out_with_a_distinct_exit_code(workspace):
    created = _create_room(workspace, "Timeout test", "codex")
    _join_all(workspace, created)
    result = _run_cli(
        workspace, "--json", "wait", "--room", created["room_id"], "--as", "codex",
        "--timeout", "1", "--poll", "0.1", check=False,
    )
    assert result.returncode == 10, result.stderr
    assert json.loads(result.stdout)["timed_out"] is True


def test_hard_stop_from_another_process_ends_a_wait(workspace):
    created = _create_room(workspace, "Stop test", "codex")
    room_id = created["room_id"]
    _join_all(workspace, created)

    env = {**os.environ, "AIDAPTER_HOME": str(workspace.home), "PYTHONPATH": REPO_ROOT}
    waiter = subprocess.Popen(
        [
            sys.executable, "-m", "aidapter", "--json", "wait",
            "--room", room_id, "--as", "codex", "--timeout", "30", "--poll", "0.1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        _run_cli(workspace, "--json", "stop-room", "--room", room_id, "--as", "human")
        stdout, stderr = waiter.communicate(timeout=45)
    except subprocess.TimeoutExpired:  # pragma: no cover - failure path
        waiter.kill()
        pytest.fail("a hard stop must end a waiting participant's loop")

    assert waiter.returncode == 11, stderr
    assert json.loads(stdout)["state"] == "stopped"
