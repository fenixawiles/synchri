"""``synchri`` command line interface.

The CLI is the integration surface for v0.1: an agent participates in a room
because it can run shell commands, with no SDK and no provider plumbing.  Every
command therefore accepts ``--json`` and returns a meaningful exit code, so an
agent can branch on the outcome without parsing prose.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from typing import Callable

from .. import __version__
from ..broker import Broker, Credential
from ..config import (
    DEFAULT_INVITE_TTL_SECONDS,
    DEFAULT_MAX_CONSECUTIVE_AGENT_TURNS,
    resolve_workspace,
)
from ..errors import SynchriError, ValidationError
from ..memory.ledger import SECTIONS
from ..models.enums import MessageType, ResponseStatus, TurnState
from ..models.envelope import MessageDraft
from ..runner import AgentCommand, Conductor
from ..runner.conductor import (
    STOP_AWAITING_HUMAN,
    STOP_ROOM_PAUSED,
    STOP_ROOM_STOPPED,
    STOP_UNMANAGED_SPEAKER,
)
from ..session.modes import runtime_catalog
from . import render, session, session_cli

#: ``wait`` exit codes, so a shell loop can branch without parsing output.
WAIT_EXIT_CODES = {
    TurnState.YOUR_TURN.value: 0,
    TurnState.STOPPED.value: 11,
    TurnState.AWAITING_HUMAN.value: 12,
    TurnState.REMOVED.value: 13,
}
WAIT_TIMEOUT_EXIT = 10
WAIT_MESSAGE_EXIT = 14

#: ``run`` exit codes: why the conductor handed control back to you.
RUN_EXIT_CODES = {
    STOP_ROOM_STOPPED: 11,
    STOP_AWAITING_HUMAN: 12,
    STOP_ROOM_PAUSED: 14,
    STOP_UNMANAGED_SPEAKER: 15,
}

RUN_REASON_HELP = {
    STOP_AWAITING_HUMAN: "the room hit its autonomy limit and wants your input",
    STOP_ROOM_STOPPED: "the room was stopped",
    STOP_ROOM_PAUSED: "the room is paused",
    STOP_UNMANAGED_SPEAKER: "the floor belongs to someone this terminal does not drive",
    "turn_limit": "reached the --turns limit",
    "idle": "nobody is queued to speak",
}


# ----------------------------------------------------------------------
# argument parsing
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="synchri",
        description=(
            "Local interoperability layer for coding agents. "
            "Stop being the clipboard between your coding agents."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    parser.add_argument("--home", help="workspace directory (default: $SYNCHRI_HOME or ~/.synchri)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--version", action="version", version=f"synchri {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def command(name: str, help_text: str, **kwargs) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, description=help_text, **kwargs)

    # -- the app ----------------------------------------------------------
    ui = command("ui", "Open Synchri in your browser (local, loopback-only).")
    ui.add_argument("--port", type=int, default=8765, help="0 picks a free port")
    ui.add_argument("--host", default="127.0.0.1", help="loopback unless --allow-remote")
    ui.add_argument(
        "--repo",
        help="repository to preselect (default: the directory where you run synchri ui)",
    )
    ui.add_argument("--no-open", action="store_true", help="do not open a browser")
    ui.add_argument(
        "--allow-remote",
        action="store_true",
        help="bind a non-loopback address (exposes session control to your network)",
    )

    # -- session startup ------------------------------------------------
    session_cli.add_parsers(sub, command)

    # -- lifecycle ------------------------------------------------------
    command("workspace", "Initialize the local workspace and show its state.")
    command("doctor", "Check that Synchri and local coding-agent tools are ready.")

    create = command("create-room", "Create a room and its owning human participant.")
    create.add_argument("--name", required=True, help="human-readable room name")
    create.add_argument("--human-name", default="human", help="name for your own identity")
    create.add_argument("--goal", help="seed the memory ledger with the room goal")
    create.add_argument(
        "--agents",
        action="append",
        default=[],
        metavar="NAME[,NAME...]",
        help="pre-invite these agents and print a ready-to-run join command for each",
    )
    create.add_argument(
        "--invite-ttl",
        type=int,
        default=DEFAULT_INVITE_TTL_SECONDS,
        metavar="SECONDS",
        help="how long the invites stay valid (0 = until the room is stopped)",
    )
    create.add_argument(
        "--repo",
        dest="workspace_root",
        help="working tree this room is about (default: the repository you are in)",
    )
    create.add_argument(
        "--memory-note",
        help="override what joining agents are told about where to persist progress",
    )
    create.add_argument(
        "--max-agent-turns",
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_AGENT_TURNS,
        help="consecutive agent turns allowed before the room waits for you",
    )

    rooms = command("rooms", "List rooms in this workspace.")
    rooms.add_argument(
        "--here", action="store_true", help="only rooms bound to the repository you are in"
    )

    join = command("join", "Redeem an invite and join a room.")
    join.add_argument("reference", help="the invite token (or a bare room id when --rejoin-ing)")
    join.add_argument("--name", required=True, help="participant name, e.g. claude or codex")
    join.add_argument("--token", help="invite token, if the reference was a bare room id")
    join.add_argument("--rejoin", metavar="SECRET", help="re-attach to an existing identity")
    join.add_argument("--meta", help="JSON object of participant metadata")

    invite = command("invite", "Mint a single-use, expiring invite for one participant.")
    _add_room_actor(invite)
    invite.add_argument("--name", required=True, help="the participant this invite is bound to")
    invite.add_argument("--kind", choices=["agent", "human"], default="agent")
    invite.add_argument(
        "--ttl",
        type=int,
        default=DEFAULT_INVITE_TTL_SECONDS,
        metavar="SECONDS",
        help="validity window (0 = until the room is stopped)",
    )

    invites = command("invites", "List this room's invites and their status.")
    _add_room_reader(invites)

    revoke = command("revoke-invite", "Revoke a pending invite (human only).")
    _add_room_actor(revoke)
    revoke.add_argument("--name", required=True)

    # -- messaging ------------------------------------------------------
    send = command("send", "Send a message into a room.")
    _add_room_actor(send, actor_flags=("--from", "--as"))
    send.add_argument("--to", dest="target", help="address a participant directly (blocking turn)")
    send.add_argument("--message", "-m", dest="content", default="", help="message body")
    send.add_argument("--stdin", action="store_true", help="read the body from standard input")
    send.add_argument(
        "--type",
        dest="message_type",
        choices=[m.value for m in MessageType],
        default=MessageType.CHAT.value,
    )
    send.add_argument("--task", dest="task_id", help="task id this message belongs to")
    send.add_argument("--goal", help="goal this message serves")
    send.add_argument("--artifact", action="append", default=[], help="artifact reference (repeatable)")
    send.add_argument("--constraint", action="append", default=[], help="constraint (repeatable)")
    send.add_argument("--claim", help="the assertion being made")
    send.add_argument("--evidence", help="what supports the claim")
    send.add_argument("--request-type", help="free-form request kind, e.g. review or implement")
    send.add_argument("--status", dest="response_status", choices=[s.value for s in ResponseStatus])
    send.add_argument("--confidence", type=float, help="0.0-1.0")
    send.add_argument("--handoff-to", help="defer to this participant when finished")
    send.add_argument("--reply-to", help="message id this replies to")
    send.add_argument("--meta", help="JSON object of extra metadata")
    send.add_argument("--hold", action="store_true", help="keep the floor for a follow-up message")

    interrupt = command("interrupt", "Human interrupt: cut in and optionally redirect.")
    _add_room_actor(interrupt)
    interrupt.add_argument("--message", "-m", dest="content", required=True)
    interrupt.add_argument("--to", dest="target", help="redirect the room to this participant")

    brief = command(
        "briefing",
        "Print the orientation briefing: repo, shared memory, what you missed, "
        "and where durable progress belongs.",
    )
    _add_room_actor(brief)
    brief.add_argument(
        "--full", action="store_true", help="include the whole conversation, not just what you missed"
    )

    read = command("read", "Show the room transcript.")
    _add_room_reader(read)
    read.add_argument("--since", type=int, default=0, help="only messages after this seq")
    read.add_argument("--limit", type=int, help="maximum messages to return")
    read.add_argument("--tail", type=int, help="show only the last N messages")
    read.add_argument("--follow", "-f", action="store_true", help="stream new messages")
    read.add_argument("--poll", type=float, default=1.0, help="follow poll interval in seconds")

    watch = command("watch", "Live view of the room for the human: status plus new messages.")
    _add_room_reader(watch)
    watch.add_argument("--poll", type=float, default=1.0)

    # -- turns ----------------------------------------------------------
    turn = command("turn", "Report whether it is your turn right now.")
    _add_room_actor(turn)

    wait = command("wait", "Block until it is your turn.")
    _add_room_actor(wait)
    wait.add_argument("--timeout", type=float, default=300.0)
    wait.add_argument("--poll", type=float, default=0.5)
    wait.add_argument(
        "--watch-messages",
        action="store_true",
        help="also return when the room receives a message (it is not permission to speak)",
    )

    passer = command("pass", "Decline the turn: nothing material to add.")
    _add_room_actor(passer)
    passer.add_argument("--reason", help="optional short reason, recorded in the transcript")

    activity = command(
        "activity",
        "Share a short public work note without sending a response or moving the queue.",
    )
    _add_room_actor(activity, actor_flags=("--from", "--as"))
    activity.add_argument("--message", "-m", dest="summary", help="short public work note")
    activity.add_argument("--clear", action="store_true", help="remove your current work note")
    activity.add_argument(
        "--ttl", type=int, default=900, metavar="SECONDS", help="how long the note stays visible"
    )

    floor = command("request-floor", "Ask for a turn without being addressed.")
    _add_room_actor(floor)
    floor.add_argument("--priority", choices=["queued", "optional"], default="queued")

    # -- single-terminal driver ------------------------------------------
    run = command(
        "run",
        "Drive several agents' turns from this one terminal.",
        epilog=(
            "  synchri run --agent 'claude=claude -p {prompt}' \\\n"
            "               --agent 'codex=codex exec {prompt}' \\\n"
            "               --start claude --turns 6\n\n"
            "Each --agent is a command YOU supply; Synchri has no built-in knowledge\n"
            "of any provider. The prompt is substituted for {prompt}, or piped to the\n"
            "command's stdin when the placeholder is absent. Commands run without a\n"
            "shell, so prompt text is never interpreted as shell syntax.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run.add_argument("--room", help="room id (default: $SYNCHRI_ROOM or the last room created)")
    run.add_argument("--token", dest="room_token", help="room join token, for observer reads")
    run.add_argument(
        "--agent",
        action="append",
        default=[],
        metavar="NAME=COMMAND",
        help="a managed participant and the command that speaks for it (repeatable)",
    )
    run.add_argument("--start", help="give the first turn to this participant if the room is idle")
    run.add_argument("--turns", type=int, help="stop after this many agent turns")
    run.add_argument(
        "--agent-timeout", type=float, default=900.0, help="seconds allowed per agent invocation"
    )
    run.add_argument("--cwd", help="working directory for agent commands")
    run.add_argument(
        "--context-messages", type=int, default=12, help="recent messages included in the prompt"
    )
    run.add_argument("--no-memory", action="store_true", help="omit the memory ledger from prompts")
    run.add_argument("--quiet", action="store_true", help="only print the final summary")

    # -- inspection -----------------------------------------------------
    status = command("status", "Show room state, participants, and the queue.")
    _add_room_reader(status)

    participants = command("participants", "List participants.")
    _add_room_reader(participants)

    events = command("events", "Show the state-transition audit log.")
    _add_room_reader(events)
    events.add_argument("--since", type=int, default=0)
    events.add_argument("--limit", type=int)

    prov = command("provenance", "Show the full provenance record for one message.")
    _add_room_reader(prov)
    prov.add_argument("--message", required=True, help="message id")

    why = command("why", "Ask the recorded development history a question.")
    why.add_argument("question", help='e.g. "Why was persistent authentication adopted?"')
    why.add_argument("--session", help="scope to one session id (default: every session)")

    export = command("export", "Rebuild transcript.jsonl from authoritative state.")
    _add_room_reader(export)

    # -- memory ---------------------------------------------------------
    # Deliberately flat positionals rather than sub-subcommands: argparse
    # subparsers reset shared flags like --room to their defaults, and a bare
    # "synchri memory --room X" should just print the ledger.
    memory = command(
        "memory",
        "Read or update the shared semantic memory ledger.",
        epilog=(
            "  synchri memory --room <id>\n"
            "  synchri memory set goal 'ship the fix' --as claude\n"
            "  synchri memory add decisions 'keep the runtime contract' --as codex\n"
            "  synchri memory remove open_issues 2 --as human\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    memory.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=["show", "set", "add", "remove"],
        help="default: show",
    )
    memory.add_argument("section", nargs="?", help=f"one of: {', '.join(sorted(SECTIONS))}")
    memory.add_argument("value", nargs="?", help="entry text, or the entry number for 'remove'")
    memory.add_argument("--message", dest="message_id", help="message id this entry came from")
    _add_room_reader(memory)

    # -- human controls -------------------------------------------------
    for name, help_text in (
        ("pause-room", "Pause the room: agents stop, you keep control."),
        ("resume-room", "Resume a paused room."),
        ("stop-room", "Hard stop. Terminal and authoritative."),
    ):
        control = command(name, help_text)
        _add_room_actor(control)

    remove = command("remove", "Remove a participant from the room (human only).")
    _add_room_actor(remove)
    remove.add_argument("--participant", required=True, help="name of the participant to remove")

    config = command("config", "Change room configuration (human only).")
    _add_room_actor(config)
    config.add_argument("--max-agent-turns", type=int, required=True)

    return parser


def _add_room_actor(parser: argparse.ArgumentParser, actor_flags: tuple[str, ...] = ("--as",)) -> None:
    parser.add_argument("--room", help="room id (default: $SYNCHRI_ROOM or the last room created)")
    parser.add_argument(
        *actor_flags,
        dest="actor",
        help="acting participant name (default: $SYNCHRI_PARTICIPANT)",
    )
    parser.add_argument("--secret", help="participant secret (default: session file or $SYNCHRI_SECRET)")


def _add_room_reader(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--room", help="room id (default: $SYNCHRI_ROOM or the last room created)")
    parser.add_argument("--as", dest="actor", help="read as this participant")
    parser.add_argument("--secret", help="participant secret")
    parser.add_argument("--token", dest="room_token", help="room join token, for observer reads")


EPILOG = """\
typical flow
  synchri ui                          open the app in your browser
  synchri start                       the session wizard: mode, repo, worktree,
                                      agents, permissions, spec, deadline, contract
  synchri session dashboard           mission control for a running session

room-level commands (a session drives these for you)
  synchri create-room --name "PR 89 review" --agents claude,codex
  # paste the printed command into each agent's session; each is single-use
  synchri join <invite-token> --name claude
  synchri send --from claude --to codex --type task \\
      -m "Adversarially review commit abc123 for race conditions." \\
      --artifact git:abc123 --constraint "preserve the existing runtime contract"
  synchri wait --as codex            # blocks until codex is on point
  synchri send --from codex --type response --status complete -m "Findings: ..."
  synchri read                       # the human watches
  synchri interrupt --as human -m "hold on, check the retry path too" --to codex

exit codes
  0 ok   2 validation   3 auth   4 not found   5 conflict   6 invalid state   7 not your turn
  wait: 0 your turn   10 timeout   11 room stopped   12 awaiting human   13 removed
"""


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    handler: Callable[[argparse.Namespace, Broker], int] | None = HANDLERS.get(args.command)
    if handler is None:  # pragma: no cover - argparse rejects unknown commands
        parser.error(f"unknown command {args.command!r}")
        return 2

    workspace = resolve_workspace(args.home)
    broker: Broker | None = None
    try:
        broker = Broker(workspace)
        return handler(args, broker)
    except SynchriError as exc:
        _emit_error(args, exc)
        return exc.exit_code
    except BrokenPipeError:  # pragma: no cover - e.g. piping into head
        return 0
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return 130
    finally:
        if broker is not None:
            broker.close()


def _emit_error(args: argparse.Namespace, exc: SynchriError) -> None:
    if getattr(args, "json", False):
        print(render.as_json(exc.to_dict()), file=sys.stderr)
    else:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)


def _out(args: argparse.Namespace, payload: object, text: str) -> int:
    print(render.as_json(payload) if args.json else text)
    return 0


def _credential(args: argparse.Namespace, broker: Broker, room_id: str) -> Credential:
    return session.resolve_credential(
        broker.workspace,
        room_id,
        getattr(args, "actor", None),
        getattr(args, "secret", None),
        getattr(args, "room_token", None),
    )


def _room(args: argparse.Namespace, broker: Broker) -> str:
    return session.resolve_room(broker.workspace, getattr(args, "room", None), broker)


def _metadata(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"--meta must be valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError("--meta must be a JSON object")
    return value


# ----------------------------------------------------------------------
# handlers
# ----------------------------------------------------------------------


def cmd_start(args: argparse.Namespace, broker: Broker) -> int:
    rooms = broker.list_rooms()
    payload = {
        "workspace": str(broker.workspace.home),
        "database": str(broker.workspace.db_path),
        "rooms": len(rooms),
        "active_rooms": sum(1 for r in rooms if r["status"] == "active"),
        "current_room": session.get_current_room(broker.workspace),
        "daemon": False,
        "note": (
            "Synchri v0.1 has no background process and opens no network socket. "
            "State lives in the SQLite workspace below and every command is a short-lived "
            "transaction against it."
        ),
    }
    text = (
        f"Synchri {__version__} workspace ready\n"
        f"  workspace : {payload['workspace']}\n"
        f"  database  : {payload['database']}\n"
        f"  rooms     : {payload['rooms']} ({payload['active_rooms']} active)\n"
        f"  current   : {payload['current_room'] or '(none)'}\n"
        f"\n{payload['note']}"
    )
    return _out(args, payload, text)


def cmd_create_room(args: argparse.Namespace, broker: Broker) -> int:
    result = broker.create_room(
        args.name,
        human_name=args.human_name,
        goal=args.goal,
        max_consecutive_agent_turns=args.max_agent_turns,
        agents=args.agents,
        invite_ttl_seconds=args.invite_ttl,
        workspace_root=args.workspace_root,
        memory_note=args.memory_note,
    )
    record = session.SessionRecord(
        room_id=result["room_id"],
        participant=result["human"]["name"],
        participant_id=result["human"]["participant_id"],
        secret=result["human"]["secret"],
        kind="human",
        room_name=result["name"],
        room_token=result["observer_token"],
    )
    result["session_file"] = session.save(broker.workspace, record)
    session.set_current_room(broker.workspace, result["room_id"])
    return _out(args, result, _room_created_summary(result))


def _room_created_summary(result: dict) -> str:
    repo = result.get("repo") or {}
    lines = [
        f"Room created: {result['name']}",
        f"  room id : {result['room_id']}",
        f"  you     : {result['human']['name']} ({result['human']['kind']})",
        f"  repo    : {repo.get('root', '(unknown)')}"
        + (f" [{repo.get('branch')}]" if repo.get("branch") else " (not a git repository)"),
        f"  memory  : {result['memory_path']}",
        "",
        "This room is bound to that working tree: running synchri there again finds",
        "it without a room id.",
        "",
    ]
    if result["invites"]:
        lines.append("Run one of these in each agent's session (each is shown only once,")
        lines.append("works once, and is bound to that one name):")
        lines.append("")
        for invite in result["invites"]:
            lines.append(f"  {invite['participant_name']}:")
            lines.append(f"    {invite['command']}")
        lines.append("")
        lines.append(f"  expires: {_expiry_text(result['invites'][0]['expires_at'])}")
        managed = " \\\n      ".join(
            f"--agent '{invite['participant_name']}=<command for {invite['participant_name']}>'"
            for invite in result["invites"]
        )
        lines.extend(
            [
                "",
                "Or drive them all from this terminal instead:",
                f"  synchri run {managed}",
            ]
        )
    else:
        lines.append("Invite an agent with:")
        lines.append(f"  synchri invite --room {result['room_id']} --name codex")
    lines.extend(
        [
            "",
            "Observer token (read-only; it cannot join the room):",
            f"  {result['observer_token']}",
        ]
    )
    return "\n".join(lines)


def _expiry_text(expires_at: str | None) -> str:
    return expires_at if expires_at else "never (lives until the room is stopped)"


def cmd_invite(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    invite = broker.create_invite(
        room_id,
        args.name,
        credential=_credential(args, broker, room_id),
        kind=args.kind,
        ttl_seconds=args.ttl,
    )
    text = (
        f"Invite for {invite['participant_name']} ({invite['kind']}), "
        f"single-use, expires: {_expiry_text(invite['expires_at'])}\n\n"
        f"  {invite['command']}"
    )
    if invite.get("superseded_invite_id"):
        text += "\n\n(the previous invite for this name no longer works)"
    return _out(args, invite, text)


def cmd_invites(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    found = broker.list_invites(room_id, credential=_credential(args, broker, room_id))
    lines = [f"{'NAME':<16} {'KIND':<6} {'STATUS':<9} EXPIRES"]
    for invite in found:
        lines.append(
            f"{invite['participant_name']:<16} {invite['kind']:<6} {invite['status']:<9} "
            f"{invite['expires_at'] or '-'}"
        )
    return _out(args, {"invites": found}, "\n".join(lines) if found else "(no invites)")


def cmd_revoke_invite(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    result = broker.revoke_invite(
        room_id, args.name, credential=_credential(args, broker, room_id)
    )
    return _out(args, result, f"revoked the invite for {result['revoked']}")


def cmd_rooms(args: argparse.Namespace, broker: Broker) -> int:
    rooms = (
        broker.rooms_for_workspace(active_only=False) if args.here else broker.list_rooms()
    )
    return _out(args, {"rooms": rooms}, render.rooms_table(rooms))


def cmd_join(args: argparse.Namespace, broker: Broker) -> int:
    result = broker.join(
        args.reference,
        args.name,
        token=args.token,
        rejoin_secret=args.rejoin,
        metadata=_metadata(args.meta),
    )
    record = session.SessionRecord(
        room_id=result["room_id"],
        participant=result["name"],
        participant_id=result["participant_id"],
        secret=result["secret"],
        kind=result["kind"],
        room_name=result.get("room_name", ""),
    )
    result["session_file"] = session.save(broker.workspace, record)
    verb = "Re-attached to" if result.get("rejoined") else "Joined"
    text = (
        f"{verb} room {result['room_id']} as {result['name']} ({result['kind']})\n"
        f"  participant id : {result['participant_id']}\n"
        f"  secret         : {result['secret']}\n"
        f"  session file   : {result['session_file']}\n\n"
        "Subsequent commands find this secret automatically, e.g.\n"
        f"  synchri wait --as {result['name']}\n"
        "\n" + "=" * 72 + "\n"
    )
    briefing = result.get("briefing") or {}
    return _out(args, result, text + (briefing.get("text") or ""))


def cmd_send(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    content = args.content
    if args.stdin:
        content = sys.stdin.read()
    draft = MessageDraft(
        content=content,
        message_type=args.message_type,
        target=args.target,
        goal=args.goal,
        task_id=args.task_id,
        artifact_references=list(args.artifact or []),
        constraints=list(args.constraint or []),
        claim=args.claim,
        evidence=args.evidence,
        request_type=args.request_type,
        response_status=args.response_status,
        confidence=args.confidence,
        handoff_target=args.handoff_to,
        in_reply_to=args.reply_to,
        metadata=_metadata(args.meta),
    )
    result = broker.send(
        room_id,
        credential=_credential(args, broker, room_id),
        draft=draft,
        hold_floor=args.hold,
    )
    return _out(args, result, _send_summary(result))


def _send_summary(result: dict) -> str:
    message = result["message"]
    lines = [render.message_line(message), ""]
    if result.get("interrupted"):
        lines.append(f"interrupted: {result['interrupted']['participant_name']}'s turn")
    if result.get("cleared_queue_entries"):
        lines.append(f"cleared {result['cleared_queue_entries']} queued turn(s)")
    if result.get("awaiting_human"):
        lines.append("autonomy limit reached: the room is now waiting for human input")
    if result.get("floor_held"):
        lines.append("floor held: send again to continue, or 'synchri pass' to release")
    lines.append(f"next speaker: {result.get('next_speaker') or '(nobody)'}")
    lines.append("queue:")
    lines.append(render.queue_block(result.get("queue") or []))
    for warning in result.get("warnings") or []:
        lines.append(f"warning: {warning}")
    return "\n".join(lines)


def cmd_interrupt(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    draft = MessageDraft(
        content=args.content,
        message_type=MessageType.INTERRUPT.value,
        target=args.target,
    )
    result = broker.send(room_id, credential=_credential(args, broker, room_id), draft=draft)
    return _out(args, result, _send_summary(result))


def cmd_briefing(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    briefing = broker.briefing(
        room_id,
        credential=_credential(args, broker, room_id),
        max_missed=10_000 if args.full else 30,
    )
    return _out(args, briefing.to_dict(), briefing.render())


def cmd_read(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    credential = _credential(args, broker, room_id)
    result = broker.read(
        room_id, credential=credential, since_seq=args.since, limit=args.limit, tail=args.tail
    )
    if not args.follow:
        return _out(args, result, render.transcript(result["messages"]))

    # In follow mode the backlog is emitted in the same shape as the stream that
    # follows it: one JSON object per message, or the rendered transcript.
    if args.json:
        for message in result["messages"]:
            print(render.as_json(message))
    else:
        print(render.transcript(result["messages"]))
    since = _max_seq(result["messages"], args.since)
    return _follow(args, broker, room_id, credential, since, args.poll)


def _follow(
    args: argparse.Namespace,
    broker: Broker,
    room_id: str,
    credential: Credential,
    since: int,
    poll: float,
) -> int:
    """Stream new messages until the room stops or the user interrupts."""
    while True:
        time.sleep(max(0.1, poll))
        result = broker.read(room_id, credential=credential, since_seq=since)
        for message in result["messages"]:
            print(render.as_json(message) if args.json else render.message_line(message) + "\n")
        since = _max_seq(result["messages"], since)
        if result["room_status"] == "stopped":
            print("(room stopped)", file=sys.stderr)
            return 0


def _max_seq(messages: list[dict], fallback: int) -> int:
    return max([m["seq"] for m in messages], default=fallback)


def cmd_watch(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    credential = _credential(args, broker, room_id)
    status = broker.room_status(room_id, credential=credential)
    print(render.room_status(status))
    print("\n--- transcript (live) ---\n")
    result = broker.read(room_id, credential=credential)
    print(render.transcript(result["messages"]))
    return _follow(args, broker, room_id, credential, _max_seq(result["messages"], 0), args.poll)


def cmd_turn(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    status = broker.turn_status(room_id, credential=_credential(args, broker, room_id))
    _out(args, status, render.turn_status(status))
    return 0


def cmd_wait(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    credential = _credential(args, broker, room_id)
    status = broker.wait_for_turn(
        room_id,
        credential=credential,
        timeout=args.timeout,
        poll_interval=args.poll,
        wake_on_message=args.watch_messages,
    )
    # `wait` is the standard entry point for an attached coding agent. Start
    # the live, non-addressable trail here so the UI never depends on the agent
    # remembering a second command before it begins working.
    if status["state"] == TurnState.YOUR_TURN.value and credential.participant:
        request = status.get("request") or {}
        subject = request.get("sender")
        summary = (
            f"Reading {subject}'s request and mapping the next step."
            if subject
            else "Beginning the assigned turn and inspecting the work."
        )
        try:
            status["activity"] = broker.publish_activity(
                room_id, credential=credential, summary=summary
            )["activity"]
        except SynchriError:
            # A human is allowed to interrupt between the final wait poll and
            # the note write. The truthful turn status remains more useful
            # than turning a successful wait into an error in that race.
            pass
    _out(args, status, render.turn_status(status))
    if status.get("timed_out"):
        return WAIT_TIMEOUT_EXIT
    if status.get("wake_reason") == "room_message":
        return WAIT_MESSAGE_EXIT
    return WAIT_EXIT_CODES.get(status["state"], 0)


def cmd_pass(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    result = broker.pass_turn(
        room_id, credential=_credential(args, broker, room_id), reason=args.reason
    )
    text = (
        f"{result['message']['sender']} passed"
        + (" (held the floor)" if result["held_floor"] else " (gave up a queued turn)")
        + f"\nnext speaker: {result.get('next_speaker') or '(nobody)'}\nqueue:\n"
        + render.queue_block(result.get("queue") or [])
    )
    return _out(args, result, text)


def cmd_activity(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    credential = _credential(args, broker, room_id)
    if args.clear:
        if args.summary:
            raise ValidationError("use either --clear or --message, not both")
        result = broker.clear_activity(room_id, credential=credential)
        return _out(args, result, "live work note cleared" if result["cleared"] else "no live work note")
    if not args.summary:
        raise ValidationError("--message is required unless --clear is supplied")
    result = broker.publish_activity(
        room_id,
        credential=credential,
        summary=args.summary,
        ttl_seconds=args.ttl,
    )
    activity = result["activity"]
    return _out(args, result, f"live work note published for {activity['name']}: {activity['summary']}")


def cmd_request_floor(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    result = broker.request_floor(
        room_id, credential=_credential(args, broker, room_id), priority=args.priority
    )
    return _out(
        args,
        result,
        f"queued at {result['priority']} priority\n" + render.queue_block(result["queue"]),
    )


def cmd_run(args: argparse.Namespace, broker: Broker) -> int:
    """Run a room's agent turns from this terminal."""
    room_id = _room(args, broker)
    agents = {}
    for spec in args.agent:
        agent = AgentCommand.parse(spec, timeout=args.agent_timeout, cwd=args.cwd)
        agents[agent.name] = agent
    if not agents:
        raise ValidationError(
            "give at least one --agent, e.g. --agent 'codex=codex exec {prompt}'"
        )

    credentials = {
        name: session.resolve_credential(broker.workspace, room_id, name) for name in agents
    }
    observer = session.resolve_credential(
        broker.workspace, room_id, None, room_token=args.room_token
    )
    if not (observer.room_token or observer.participant):
        # Fall back to reading as one of the managed agents.
        observer = next(iter(credentials.values()))

    if args.start:
        _start_turn(broker, room_id, args.start, credentials)

    def report_event(event: str, payload: dict) -> None:
        if args.quiet or args.json:
            return
        if event == "agent.invoking":
            print(f"→ {payload['participant']} thinking…", flush=True)
        elif event == "agent.returned" and payload.get("timed_out"):
            print(f"  {payload['participant']} timed out", flush=True)

    conductor = Conductor(
        broker,
        room_id,
        agents,
        credentials,
        observer,
        context_messages=args.context_messages,
        include_memory=not args.no_memory,
        on_event=report_event,
    )
    report = conductor.run(max_turns=args.turns)

    if not args.json and not args.quiet:
        print()
        result = broker.read(room_id, credential=observer, tail=max(1, report.turn_count * 2))
        print(render.transcript(result["messages"]))
        print()
    _out(args, report.to_dict(), _run_summary(report))
    return RUN_EXIT_CODES.get(report.reason, 0)


def _start_turn(broker: Broker, room_id: str, name: str, credentials: dict) -> None:
    """Hand the opening turn to a participant, if nobody already holds it."""
    credential = credentials.get(name) or session.resolve_credential(
        broker.workspace, room_id, name
    )
    status = broker.turn_status(room_id, credential=credential)
    if status["state"] in (TurnState.YOUR_TURN.value, TurnState.WAITING.value):
        return
    broker.request_floor(room_id, credential=credential)


def _run_summary(report) -> str:
    lines = [
        f"ran {report.turn_count} agent turn(s); stopped: {report.reason}",
        RUN_REASON_HELP.get(report.reason, ""),
    ]
    for turn in report.turns:
        detail = turn.get("status") or f"skipped ({turn.get('skipped')})"
        arrow = f" → {turn['target']}" if turn.get("target") else ""
        arrow = arrow or (f" ⇢ {turn['handoff_target']}" if turn.get("handoff_target") else "")
        lines.append(f"  {turn['participant']}: {detail}{arrow}")
    for warning in report.warnings:
        lines.append(f"  warning: {warning}")
    return "\n".join(line for line in lines if line)


def cmd_status(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    status = broker.room_status(room_id, credential=_credential(args, broker, room_id))
    return _out(args, status, render.room_status(status))


def cmd_participants(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    people = broker.list_participants(room_id, credential=_credential(args, broker, room_id))
    text = "\n".join(
        f"{p['name']:<16} {p['kind']:<6} {p['status']:<8} joined {p['joined_at']}" for p in people
    )
    return _out(args, {"participants": people}, text or "(no participants)")


def cmd_events(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    result = broker.events(
        room_id, credential=_credential(args, broker, room_id), since_seq=args.since, limit=args.limit
    )
    text = "\n".join(
        f"[#{e['seq']:>4}] {render.short_time(e['created_at'])} {e['event_type']:<26} "
        f"{e['actor_name'] or '-':<12} {json.dumps(e['payload'], ensure_ascii=False)}"
        for e in result["events"]
    )
    return _out(args, result, text or "(no events)")


def cmd_why(args: argparse.Namespace, broker: Broker) -> int:
    """Retrieval over the deliberative record; the report layer builds on it."""
    from ..session.deliberation import search
    from ..session.manager import SessionManager

    manager = SessionManager(broker)
    result = search(manager, args.question, session_id=args.session)
    lines = [
        f"engine: {result['engine']} · {len(result['evidence'])} evidence item(s)"
    ]
    for item in result["evidence"]:
        origin = " · ".join(
            str(part) for part in (
                item["kind"], item.get("actor"), (item.get("at") or "")[:19],
                None if args.session else item.get("session_id"),
            ) if part
        )
        marker = " (context)" if item.get("context") else ""
        lines.append(f"\n[{item['ref']}]{marker} {origin}\n  {item['excerpt']}")
    if not result["evidence"]:
        lines.append("nothing in the recorded history matched that question")
    return _out(args, result, "\n".join(lines))


def cmd_provenance(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    result = broker.provenance(
        room_id, args.message, credential=_credential(args, broker, room_id)
    )
    return _out(args, result, render.as_json(result))


def cmd_export(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    result = broker.export(room_id, credential=_credential(args, broker, room_id))
    return _out(
        args,
        result,
        f"exported {result['message_count']} messages\n"
        f"  transcript : {result['transcript_path']}\n"
        f"  memory     : {result['memory_path']}",
    )


def cmd_memory(args: argparse.Namespace, broker: Broker) -> int:
    action = args.action or "show"
    room_id = _room(args, broker)
    credential = _credential(args, broker, room_id)

    if action != "show" and not args.section:
        raise ValidationError(f"'memory {action}' needs a section, e.g. 'memory {action} goal ...'")
    if action != "show" and args.value is None:
        raise ValidationError(f"'memory {action}' needs a value")

    if action == "show":
        result = broker.memory_show(room_id, credential=credential)
    elif action == "remove":
        try:
            index = int(args.value)
        except ValueError as exc:
            raise ValidationError("'memory remove' takes an entry number, e.g. 'remove goal 2'") from exc
        result = broker.memory_remove(
            room_id, credential=credential, section=args.section, index=index
        )
    else:
        result = broker.memory_write(
            room_id,
            credential=credential,
            section=args.section,
            value=args.value,
            mode=action,
            message_id=args.message_id,
        )
    return _out(args, result, render.memory_block(result))


def cmd_pause(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    status = broker.pause_room(room_id, credential=_credential(args, broker, room_id))
    return _out(args, status, render.room_status(status))


def cmd_resume(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    status = broker.resume_room(room_id, credential=_credential(args, broker, room_id))
    return _out(args, status, render.room_status(status))


def cmd_stop(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    status = broker.stop_room(room_id, credential=_credential(args, broker, room_id))
    return _out(args, status, render.room_status(status))


def cmd_remove(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    result = broker.remove_participant(
        room_id, args.participant, credential=_credential(args, broker, room_id)
    )
    return _out(args, result, f"removed {result['removed']} ({result['participant_id']})")


def cmd_config(args: argparse.Namespace, broker: Broker) -> int:
    room_id = _room(args, broker)
    status = broker.set_room_config(
        room_id,
        credential=_credential(args, broker, room_id),
        max_consecutive_agent_turns=args.max_agent_turns,
    )
    return _out(args, status, render.room_status(status))


def cmd_ui(args: argparse.Namespace, broker: Broker) -> int:
    from ..ui.server import serve

    serve(
        broker,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
        allow_remote=args.allow_remote,
        default_repo=args.repo or os.getcwd(),
    )
    return 0


def cmd_doctor(args: argparse.Namespace, broker: Broker) -> int:
    """A single, no-surprises readiness report for a new local install."""
    git_path = shutil.which("git")
    runtimes = runtime_catalog()
    payload = {
        "synchri_version": __version__,
        "workspace": str(broker.workspace.home),
        "git": {"ready": bool(git_path), "path": git_path},
        "runtimes": runtimes,
        "managed_ready": [runtime["key"] for runtime in runtimes if runtime["managed"]],
    }
    lines = [f"Synchri {__version__}", f"Workspace  ready  {broker.workspace.home}"]
    lines.append("Git        ready" if git_path else "Git        missing — install Git before starting a room")
    lines.extend(["", "Local agents"])
    for runtime in runtimes:
        state = "ready" if runtime["managed"] else ("external" if runtime["installed"] else "not found")
        lines.append(f"  {runtime['label']:<22} {state:<10} {runtime['detail']}")
    lines.extend(
        [
            "",
            "Next: open Synchri and choose a repository:",
            "  synchri ui",
        ]
    )
    return _out(args, payload, "\n".join(lines))


HANDLERS: dict[str, Callable[[argparse.Namespace, Broker], int]] = {
    "ui": cmd_ui,
    "start": session_cli.cmd_start,
    "session": session_cli.cmd_session,
    "preset": session_cli.cmd_preset,
    "workspace": cmd_start,
    "doctor": cmd_doctor,
    "create-room": cmd_create_room,
    "rooms": cmd_rooms,
    "join": cmd_join,
    "invite": cmd_invite,
    "invites": cmd_invites,
    "revoke-invite": cmd_revoke_invite,
    "send": cmd_send,
    "interrupt": cmd_interrupt,
    "briefing": cmd_briefing,
    "read": cmd_read,
    "watch": cmd_watch,
    "turn": cmd_turn,
    "wait": cmd_wait,
    "pass": cmd_pass,
    "activity": cmd_activity,
    "request-floor": cmd_request_floor,
    "run": cmd_run,
    "status": cmd_status,
    "participants": cmd_participants,
    "events": cmd_events,
    "provenance": cmd_provenance,
    "why": cmd_why,
    "export": cmd_export,
    "memory": cmd_memory,
    "pause-room": cmd_pause,
    "resume-room": cmd_resume,
    "stop-room": cmd_stop,
    "remove": cmd_remove,
    "config": cmd_config,
}


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
