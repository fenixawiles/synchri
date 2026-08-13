"""``synchri start`` and the session commands.

The wizard here is a *terminal driver* for :class:`SessionDraft`, not the wizard
itself. Every rule — which steps exist, what each needs, what blocks starting —
lives in the draft, so a desktop GUI can present the same flow without
reimplementing any of it. This module only asks questions and prints answers.

Every step is also reachable by flag, so the whole flow is scriptable and
testable without a tty.
"""

from __future__ import annotations

import argparse
import json
import sys

from ..broker import Broker
from ..errors import ValidationError
from ..session import presets as presets_module
from ..session.draft import SessionDraft
from ..session.escalation import EscalationPolicy
from ..session.manager import SessionManager
from ..session.modes import KNOWN_RUNTIMES, ParticipantPlan, list_modes, policy_for
from ..session.permissions import BY_KEY, Decision
from . import render, session as session_files


# ----------------------------------------------------------------------
# parser
# ----------------------------------------------------------------------


def add_parsers(sub, command) -> None:
    start = command(
        "start",
        "Start a new Synchri session: mode, repository, isolated worktree, agents, "
        "permissions, spec, optional timebox, contract.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "  synchri start                       walk through the wizard\n"
            "  synchri start --preset 'Safe Build' --repo . --spec-file spec.md \\\n"
            "      --deadline '10 hours' --yes     non-interactive\n"
        ),
    )
    start.add_argument("--mode", choices=[m["mode"] for m in list_modes()])
    start.add_argument("--repo", help="repository path (default: the one you are in)")
    start.add_argument("--base-branch")
    start.add_argument("--worktree-name")
    start.add_argument("--worktree-parent")
    start.add_argument(
        "--agent",
        action="append",
        default=[],
        metavar="NAME[:RUNTIME[:ROLE]]",
        help="add an agent, e.g. --agent claude:claude_code:primary_builder (repeatable)",
    )
    start.add_argument(
        "--allow", action="append", default=[], metavar="CAP", help="grant a capability"
    )
    start.add_argument(
        "--deny", action="append", default=[], metavar="CAP", help="refuse a capability"
    )
    start.add_argument(
        "--ask", action="append", default=[], metavar="CAP", help="require approval first"
    )
    start.add_argument("--spec", help="product specification text")
    start.add_argument("--spec-file", help="read the specification from a file")
    start.add_argument("--deadline", help="optional timebox duration, e.g. '10 hours'")
    start.add_argument("--deadline-at", help="optional timebox end, e.g. '2026-08-12T08:00'")
    start.add_argument("--name", help="session name")
    start.add_argument("--preset", help="load a saved preset")
    start.add_argument("--save-preset", metavar="NAME", help="save this configuration as a preset")
    start.add_argument(
        "--yes", "-y", action="store_true", help="skip prompts; fail if anything is missing"
    )
    start.add_argument(
        "--dry-run", action="store_true", help="review only; do not create anything"
    )

    session = command("session", "Inspect and control sessions.")
    session.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=[
            "list", "show", "contract", "ack", "activate", "dashboard",
            "gates", "complete", "stop", "restore", "permissions", "escalate",
        ],
    )
    session.add_argument("value", nargs="?", help="participant name for 'ack', gate id for 'gates'")
    session.add_argument("--session", dest="session_id", help="session id (default: the newest)")
    session.add_argument("--reply", help="acknowledgment text for 'ack'")
    session.add_argument("--as", dest="actor", help="participant acting")
    session.add_argument("--rule", help="escalation rule key")
    session.add_argument("--detail", default="", help="escalation detail")
    session.add_argument("--set", dest="set_status", help="new gate status")
    session.add_argument("--evidence", action="append", default=[])
    session.add_argument(
        "--assessment",
        help="your evidence-backed assessment for a gate; recorded under your session role",
    )
    session.add_argument("--test", action="append", default=[], help="test evidence for a gate")
    session.add_argument("--commit", action="append", default=[], help="commit evidence for a gate")
    session.add_argument("--reason", help="reason, for stop")

    preset = command("preset", "Save and reuse session configurations.")
    preset.add_argument("action", nargs="?", default="list", choices=["list", "show", "delete"])
    preset.add_argument("name", nargs="?")


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _prompt(question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{question}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or (default or "")


def _choose(question: str, options: list[tuple[str, str]], default: int = 0) -> str:
    print(f"\n{question}")
    for index, (_, label) in enumerate(options, start=1):
        print(f"  {index}. {label}")
    while True:
        raw = _prompt("Choose", str(default + 1))
        try:
            index = int(raw) - 1
            if 0 <= index < len(options):
                return options[index][0]
        except ValueError:
            pass
        print("  Pick one of the numbers above.")


def _parse_agent(spec: str) -> ParticipantPlan:
    parts = [p.strip() for p in spec.split(":")]
    if not parts or not parts[0]:
        raise ValidationError(f"could not read agent {spec!r}; expected NAME[:RUNTIME[:ROLE]]")
    name = parts[0]
    runtime = parts[1] if len(parts) > 1 and parts[1] else "generic"
    role = parts[2] if len(parts) > 2 and parts[2] else "participant"
    if runtime not in KNOWN_RUNTIMES:
        raise ValidationError(
            f"unknown runtime {runtime!r}; expected one of {sorted(KNOWN_RUNTIMES)}"
        )
    return ParticipantPlan(name=name, runtime=runtime, role=role)


def _apply_permission_flags(draft: SessionDraft, args) -> None:
    for keys, decision in (
        (args.allow, Decision.ALLOW),
        (args.ask, Decision.ASK),
        (args.deny, Decision.DENY),
    ):
        for key in keys:
            if key not in BY_KEY:
                raise ValidationError(
                    f"unknown capability {key!r}; run 'synchri session permissions' to list them"
                )
            draft.set_permission(key, decision)


def build_draft(args, broker: Broker) -> SessionDraft:
    """Assemble a draft from flags, a preset, and (if allowed) prompts."""
    draft = (
        SessionDraft.from_preset(presets_module.load(broker.workspace, args.preset))
        if args.preset
        else SessionDraft()
    )
    interactive = not args.yes and sys.stdin.isatty()

    # 1. mode
    if args.mode:
        draft.set_mode(args.mode)
    elif not draft.mode:
        if not interactive:
            raise ValidationError("--mode is required (or run without --yes to be prompted)")
        modes = [(m["mode"], f"{m['label']} — {m['summary']}") for m in list_modes()]
        draft.set_mode(_choose("How should this session run?", modes))

    # 2. repository
    repo = args.repo
    if not repo and interactive:
        repo = _prompt("\nRepository path", ".")
    draft.set_repository(repo or ".", args.base_branch)
    status = draft.repo_status()
    if not args.json:
        print(f"\n  Repository: {status.name}  ({status.root})")
        print(f"  Branch:     {draft.base_branch}"
              + (f"   Remote: {status.remote}" if status.remote else ""))
        if status.is_dirty:
            print(
                f"  Note:       {len(status.dirty_files)} uncommitted change(s) in your "
                "working tree.\n              Synchri will not touch them — agents work "
                "in an isolated copy."
            )

    # 3. worktree
    draft.set_worktree(args.worktree_name, args.worktree_parent)

    # 4. agents
    if args.agent:
        draft.set_agents([_parse_agent(spec) for spec in args.agent])
    elif not draft.participants:
        if not interactive:
            raise ValidationError("at least one --agent is required")
        draft.set_agents(_prompt_for_agents(draft))

    # 5. permissions
    _apply_permission_flags(draft, args)
    if interactive and not args.allow and not args.deny and not args.ask:
        _prompt_for_permissions(draft)

    # 6. spec
    text = args.spec
    if args.spec_file:
        text = open(args.spec_file, encoding="utf-8").read()
    if text:
        draft.set_spec(text)
    elif draft.policy.requires_spec and interactive:
        print("\nWhat should the agents build? (paste Markdown; end with a line containing only .)")
        lines = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line.strip() == ".":
                break
            lines.append(line)
        if lines:
            draft.set_spec("\n".join(lines))

    # 7. deadline
    if args.deadline:
        draft.set_deadline_duration(args.deadline)
    elif args.deadline_at:
        draft.set_deadline_at(args.deadline_at)
    elif draft.policy.requires_deadline and interactive:
        draft.set_deadline_duration(_prompt("\nDeadline (e.g. '10 hours')", "8 hours"))

    if args.name:
        draft.name = args.name
    return draft


def _prompt_for_agents(draft: SessionDraft) -> list[ParticipantPlan]:
    runtimes = [(k, v["label"]) for k, v in KNOWN_RUNTIMES.items()]
    defaults = list(draft.policy.default_roles) or ["participant"]
    chosen: list[ParticipantPlan] = []
    minimum = draft.policy.min_agents
    while True:
        index = len(chosen)
        if index >= minimum:
            if _prompt("\nAdd another agent? (y/N)", "n").lower() not in {"y", "yes"}:
                break
        runtime = _choose(f"Agent {index + 1} — which coding agent?", runtimes)
        name = _prompt("  Name it (this is how the others address it)", runtime.split("_")[0])
        role = defaults[index] if index < len(defaults) else "participant"
        chosen.append(ParticipantPlan(name=name, runtime=runtime, role=role))
        print(f"  {name}: {role.replace('_', ' ')}")
    return chosen


def _prompt_for_permissions(draft: SessionDraft) -> None:
    print("\nWhat may the agents do? Defaults are conservative.")
    for group in draft.permissions.grouped():
        allowed = [c["label"] for c in group["capabilities"] if c["decision"] == "allow"]
        print(f"  {group['group']}: {', '.join(allowed) if allowed else '(nothing)'}")
    if _prompt("\nChange any of these? (y/N)", "n").lower() not in {"y", "yes"}:
        return
    for group in draft.permissions.grouped():
        for capability in group["capabilities"]:
            marker = {"allow": "YES", "ask": "ASK", "deny": "NO"}[capability["decision"]]
            risk = f"  [{capability['risk']}]" if capability["risk"] != "low" else ""
            answer = _prompt(f"  {capability['label']}{risk} (yes/ask/no)", marker.lower())
            mapping = {"yes": "allow", "y": "allow", "ask": "ask",
                       "no": "deny", "n": "deny", "allow": "allow", "deny": "deny"}
            if answer.lower() in mapping:
                draft.set_permission(capability["key"], mapping[answer.lower()])


# ----------------------------------------------------------------------
# commands
# ----------------------------------------------------------------------


def cmd_start(args: argparse.Namespace, broker: Broker) -> int:
    draft = build_draft(args, broker)
    summary = draft.summary()

    if not args.json:
        print("\n" + render_review(summary))

    if summary["problems"]:
        if args.json:
            print(render.as_json(summary))
        else:
            print("\nCannot start yet:")
            for problem in summary["problems"]:
                print(f"  - {problem}")
        return 2

    if args.save_preset:
        presets_module.save(broker.workspace, args.save_preset, draft.to_preset())
        if not args.json:
            print(f"\nSaved preset {args.save_preset!r}")

    if args.dry_run:
        if args.json:
            print(render.as_json({**summary, "dry_run": True}))
        else:
            print("\n(dry run — nothing was created)")
        return 0

    if not args.yes and sys.stdin.isatty():
        if _prompt("\nStart this session? (Y/n)", "y").lower() in {"n", "no"}:
            print("Cancelled. Nothing was created.")
            return 0

    manager = SessionManager(broker)
    record = manager.create(
        name=draft.name or "Synchri session",
        mode=draft.mode,
        repo_root=draft.repo_path,
        base_branch=draft.base_branch,
        participants=draft.participants,
        permissions=draft.permissions,
        spec=draft.spec,
        deadline=draft.deadline,
        escalation=draft.escalation,
        worktree_parent=draft.worktree_parent,
        worktree_name=draft.worktree_name,
    )
    document = manager.issue_contract(record.session_id, reason="initial contract")
    # Re-read: issuing the contract moved the session to awaiting_ack, and the
    # record captured above predates that.
    record = manager.get(record.session_id)
    payload = {
        "session": record.to_dict(),
        "contract": document.to_dict(),
        "next_steps": _next_steps(record, document),
    }
    if args.json:
        print(render.as_json(payload))
    else:
        print(render_created(record, document))
    return 0


def _next_steps(record, document) -> list[str]:
    steps = []
    for invite in record.metadata.get("invites", []):
        steps.append(invite["command"])
    steps.append(f"synchri session contract --session {record.session_id}")
    steps.append(
        f"synchri session ack <agent> --reply UNDERSTOOD --session {record.session_id}"
    )
    steps.append(f"synchri session activate --session {record.session_id}")
    return steps


def render_review(summary: dict) -> str:
    repo = summary["repository"]
    lines = [
        "=" * 68,
        "REVIEW SESSION",
        "=" * 68,
        "",
        f"  Mode         {summary['mode']}",
        f"               {summary['mode_summary']}",
        "",
        f"  Repository   {repo['name']}  ({repo['path']})",
        f"  Base branch  {repo['branch']}",
        f"  Worktree     {summary['worktree']['name']}",
        f"               {summary['worktree']['explanation']}",
        "",
        "  Agents",
    ]
    for agent in summary["agents"]:
        lines.append(f"    {agent['name']:<14} {agent['role_label']}  [{agent['runtime']}]")
    lines.append("")
    lines.append("  Permissions")
    for group in summary["permissions"]:
        for capability in group["capabilities"]:
            mark = {"allow": "YES", "ask": "ASK", "deny": "NO "}[capability["decision"]]
            if capability["decision"] == "allow" or capability["risk"] in {"high", "destructive"}:
                lines.append(f"    {capability['label']:<32} {mark}")
    lines += [
        "",
        f"  Product spec {summary['spec']}",
        f"  Deadline     {summary['deadline']}",
        "",
        "  Escalate only on:",
    ]
    lines += [f"    - {rule}" for rule in summary["escalation"][:6]]
    if len(summary["escalation"]) > 6:
        lines.append(f"    ... and {len(summary['escalation']) - 6} more")
    for warning in summary["warnings"]:
        lines.append(f"\n  ! {warning}")
    lines.append("=" * 68)
    return "\n".join(lines)


def render_created(record, document) -> str:
    lines = [
        "",
        f"Session created: {record.name}",
        f"  session id : {record.session_id}",
        f"  mode       : {record.policy.label}",
        f"  worktree   : {record.worktree_name}",
        f"  path       : {record.worktree_path}",
        f"  contract   : revision {document.revision} ({document.short_digest})",
        "",
        "Agents work only inside that worktree. Your primary working tree is untouched.",
        "",
        "The session is NOT active yet — every agent must acknowledge the contract first.",
        "",
        "1. Give each agent its join command:",
    ]
    for invite in record.metadata.get("invites", []):
        lines.append(f"     {invite['command']}")
    lines += [
        "",
        "2. Send each one the contract, then record the reply:",
        f"     synchri session contract --session {record.session_id}",
        f"     synchri session ack claude --reply UNDERSTOOD --session {record.session_id}",
        "",
        "3. Once everyone has agreed:",
        f"     synchri session activate --session {record.session_id}",
    ]
    return "\n".join(lines)


def _resolve_session(manager: SessionManager, args) -> str:
    if args.session_id:
        return args.session_id
    sessions = manager.list_sessions()
    if not sessions:
        raise ValidationError("no sessions yet; run 'synchri start'")
    return sessions[0].session_id


def cmd_session(args: argparse.Namespace, broker: Broker) -> int:
    manager = SessionManager(broker)
    action = args.action or "show"

    if action == "list":
        sessions = manager.list_sessions()
        payload = {"sessions": [s.to_dict() for s in sessions]}
        text = "\n".join(
            f"{s.session_id:<26} {s.status:<14} {s.policy.label:<26} {s.name}" for s in sessions
        )
        return _out(args, payload, text or "(no sessions)")

    if action == "permissions":
        from ..session.permissions import PermissionSet

        groups = PermissionSet.defaults().grouped()
        text = "\n".join(
            f"{c['key']:<22} {c['default']:<6} {c['risk']:<12} {c['label']}"
            for g in groups
            for c in g["capabilities"]
        )
        return _out(args, {"capabilities": groups}, text)

    session_id = _resolve_session(manager, args)

    def participant_credential(actor: str):
        """Require a saved, valid room identity for an agent-only action.

        ``--as`` is not treated as a cosmetic attribution label when an agent
        is updating durable progress or closing a room.  The named participant
        must have joined this exact room and present the secret created at
        join time.
        """
        record = manager.get(session_id)
        if not record.room_id:
            raise ValidationError("this session has no room identity")
        credential = session_files.resolve_credential(
            broker.workspace, record.room_id, actor
        )
        if not credential.secret:
            raise ValidationError(
                f"{actor!r} has not joined this room on this machine; join before reporting progress"
            )
        broker.turn_status(record.room_id, credential=credential)
        return credential

    if action == "show":
        record = manager.get(session_id)
        return _out(args, record.to_dict(), render_session(record))
    if action == "contract":
        document = manager.current_contract(session_id)
        who = args.actor
        text = document.for_participant(who) if who else document.core_text
        return _out(args, document.to_dict(), text)
    if action == "ack":
        participant = args.value or args.actor
        if not participant:
            raise ValidationError("say which participant is acknowledging")
        result = manager.acknowledge(session_id, participant, args.reply or "")
        state = manager.acknowledgment_state(session_id)
        text = (
            f"{participant}: {'UNDERSTOOD' if result['accepted'] else 'CONFLICT'}"
            + (f" — {result['conflict']}" if result["conflict"] else "")
            + f"\naccepted: {', '.join(state['accepted']) or '(none)'}"
            + f"\nwaiting : {', '.join(state['waiting']) or '(none)'}"
        )
        return _out(args, {**result, "state": state}, text)
    if action == "activate":
        record = manager.activate(session_id)
        return _out(
            args,
            record.to_dict(),
            f"Session ACTIVE. Agents may begin, in {record.worktree_path}",
        )
    if action == "dashboard":
        data = manager.dashboard(session_id)
        return _out(args, data, render_dashboard(data))
    if action == "gates":
        if args.value and args.set_status:
            if args.actor:
                participant_credential(args.actor)
                gate = manager.report_gate(
                    session_id,
                    args.value,
                    actor=args.actor,
                    status=args.set_status,
                    assessment=args.assessment,
                    evidence=args.evidence,
                    tests=args.test,
                    commits=args.commit,
                )
            else:
                gate = manager.update_gate(
                    session_id,
                    args.value,
                    status=args.set_status,
                    **({"evidence": args.evidence} if args.evidence else {}),
                    **({"tests": args.test} if args.test else {}),
                    **({"commits": args.commit} if args.commit else {}),
                )
            return _out(args, gate.to_dict(), gate.render())
        gates = manager.gates(session_id)
        from ..session.gates import summarize

        report = summarize(gates)
        text = "\n".join(g.render() for g in gates) or "(no gates defined)"
        text += f"\n\n{report['satisfied']}/{report['required']} required gates satisfied"
        for blocker in report["blockers"]:
            text += f"\n  blocked: {blocker}"
        return _out(args, {"gates": [g.to_dict() for g in gates], "summary": report}, text)
    if action == "complete":
        if args.actor:
            participant_credential(args.actor)
            manager.complete_by_agent(session_id, args.actor)
        else:
            manager.complete(session_id)
        report = manager.handoff_report(session_id)
        return _out(args, report, render_handoff(report))
    if action == "stop":
        record = manager.stop(session_id, args.reason or "stopped by the user")
        report = manager.handoff_report(session_id)
        return _out(args, report, render_handoff(report))
    if action == "restore":
        data = manager.restore(session_id)
        return _out(args, data, render_restore(data))
    if action == "escalate":
        if not args.rule:
            raise ValidationError("--rule is required")
        result = manager.escalate(session_id, args.rule, args.detail, raised_by=args.actor)
        return _out(args, result, f"escalated: {result['rule']}")
    raise ValidationError(f"unknown action {action!r}")  # pragma: no cover


def cmd_preset(args: argparse.Namespace, broker: Broker) -> int:
    action = args.action or "list"
    if action == "list":
        found = presets_module.list_presets(broker.workspace)
        text = "\n".join(
            f"{p.get('name'):<28} {policy_for(p['mode']).label:<28} "
            f"{len(p.get('agents') or [])} agent(s)"
            for p in found
        )
        return _out(args, {"presets": found}, text or "(no presets saved)")
    if not args.name:
        raise ValidationError("name the preset")
    if action == "show":
        preset = presets_module.load(broker.workspace, args.name)
        return _out(args, preset, render.as_json(preset))
    presets_module.delete(broker.workspace, args.name)
    return _out(args, {"deleted": args.name}, f"deleted preset {args.name!r}")


# ----------------------------------------------------------------------
# rendering
# ----------------------------------------------------------------------


def render_session(record) -> str:
    lines = [
        f"Session   {record.name}  ({record.session_id})",
        f"Status    {record.status}",
        f"Mode      {record.policy.label}",
        f"Repo      {record.repo_name}  ({record.repo_root})",
        f"Worktree  {record.worktree_name}",
        f"          {record.worktree_path}",
        f"Contract  revision {record.contract_revision}",
        "",
        "Participants:",
    ]
    for plan in record.participants:
        lines.append(f"  {plan.name:<14} {plan.role}")
    if record.deadline:
        lines += ["", f"Deadline  {record.deadline.describe()}",
                  f"Remaining {record.deadline.remaining_text()}"]
    return "\n".join(lines)


def render_dashboard(data: dict) -> str:
    session = data["session"]
    gates = data["gates"]["summary"]
    lines = [
        "=" * 60,
        f"  {session['name']}",
        "=" * 60,
        f"  Session status     {data['status'].upper()}",
    ]
    if data["time_remaining"]:
        lines.append(f"  Time remaining     {data['time_remaining']}   ({data['phase']})")
    lines += [
        f"  Repository         {session['repository']['name']}",
        f"  Worktree           {session['worktree']['name'] if session['worktree'] else '(none)'}",
        f"  Current actor      {data['current_actor'] or '(nobody)'}",
        f"  Spec gates         {gates['satisfied']} / {gates['required']} passed",
        f"  Open blockers      {len(data['open_blockers'])}",
        f"  Escalations        {len(data['open_escalations'])}",
        f"  User intervention  {'REQUIRED' if data['user_intervention_required'] else 'not required'}",
    ]
    if data["acknowledgments"] and not data["acknowledgments"]["all_accepted"]:
        state = data["acknowledgments"]
        lines += ["", f"  Waiting on: {', '.join(state['waiting']) or '(none)'}"]
        for conflict in state["conflicts"]:
            lines.append(f"  CONFLICT {conflict['participant']}: {conflict['reason']}")
    for blocker in data["open_blockers"][:5]:
        lines.append(f"  blocked: {blocker}")
    return "\n".join(lines)


def render_handoff(report: dict) -> str:
    lines = [
        f"Session {report['status'].upper()} — {report['reason_stopped']}",
        "",
        f"  Complete           {'yes' if report['complete'] else 'NO'}",
        f"  Branch             {report['branch']}",
        f"  Head               {report['head'] or '(unknown)'}",
        f"  Worktree           {report['worktree']}",
        f"  Gates satisfied    {', '.join(report['gates']['satisfied']) or '(none)'}",
        f"  Gates unmet        {', '.join(report['gates']['unmet']) or '(none)'}",
        f"  Unverified         {', '.join(report['gates']['unverified']) or '(none)'}",
    ]
    for blocker in report["open_blockers"]:
        lines.append(f"  blocker: {blocker}")
    lines += ["", f"  Recommended next: {report['recommended_next_action']}"]
    if report.get("final_changelog"):
        lines.append(f"  Final changelog    {report['final_changelog']}")
    return "\n".join(lines)


def render_restore(data: dict) -> str:
    session = data["session"]
    lines = [
        f"Session {session['name']} ({session['session_id']})",
        f"  Status before restart : {session['status']}",
        f"  Worktree present      : {'yes' if data['worktree_present'] else 'NO'}",
        f"  Repository valid      : {'yes' if data['repository_valid'] else 'NO'}",
        f"  Deadline expired      : {'yes' if data['deadline_expired'] else 'no'}",
        f"  Contract revision     : {data['contract_revision']}",
        "",
        "  Participants must reconnect: " + ", ".join(data["participants_must_reconnect"]),
    ]
    for problem in data["problems"]:
        lines.append(f"  ! {problem}")
    lines.append("")
    lines.append(
        "  Resumable." if data["resumable"] else "  Not resumable without fixing the above."
    )
    lines.append("  Nothing was resumed automatically.")
    return "\n".join(lines)


def _out(args, payload, text: str) -> int:
    print(render.as_json(payload) if args.json else text)
    return 0
