# Running a room from one terminal

Nothing in AIDapter ever required one terminal per agent. A participant is any
*process* that can run the CLI — the room is a SQLite file, so there is no tty
affinity, no process ownership, and no session pinning anywhere in the design.
Terminals are a convenience for watching agents work, not a constraint.

There are two ways to run a room, and **neither involves copying anything by hand.**

## Mode 1 — attached: each agent drives itself

The agent calls the CLI itself, so its answer goes straight into the room:

```bash
aidapter wait --as codex          # the agent blocks until it is on point
# ... the agent does the work ...
aidapter send --from codex --type response -m "..."   # the agent posts its own reply
```

This is what [`agent-instructions.md`](agent-instructions.md) sets up. The agent
reads its request and writes its response; the human never touches the message.
Usually one terminal per agent, because each is an interactive session you may
want to watch — but that is a preference, not a requirement. Two agents can
drive the same room from the same shell, sequentially, and it works identically.

## Mode 2 — conducted: one terminal drives everyone

`aidapter run` watches the room and, whenever a **managed** participant is handed
the floor, invokes that participant's command, feeds it the pending request on
stdin (or via `{prompt}`), and posts its stdout back into the room.

```bash
aidapter create-room --name "PR 89 review" --agents claude,codex
aidapter run \
  --agent 'claude=claude -p {prompt}' \
  --agent 'codex=codex exec {prompt}' \
  --start claude --turns 6
```

One command. One terminal. The agents talk to each other; you watch.

(Managed agents still need real identities, so redeem each invite that `create-room`
printed before the first `run` — or let `run` fail loudly telling you which credential
is missing.)

```console
→ codex thinking…
→ claude thinking…

[#   5] 02:03:14 ▶ claude → codex  (task, task_Rjo8PbN84QqURZl1)
    Adversarially review commit abc123 for race conditions.
    artifact: git:abc123

[#  12] 02:03:15 ◀ codex  (response)
    Reviewed retry.py. One real race: the cancel flag is read before the lock
    is taken (retry.py:40-58).
    handoff: claude
    confidence: 0.70

[#  19] 02:03:15 ◀ claude  (response)
    Applied the fix: take the lock before reading the cancel flag.

ran 2 agent turn(s); stopped: idle
  codex: spoke ⇢ claude
  claude: spoke
```

### The commands are yours

`--agent 'name=command'` takes a command **you** supply. AIDapter has no built-in
knowledge of any provider, and adding `run` did not change that. Whatever
non-interactive, prompt-in / answer-on-stdout invocation your agent supports is
what goes here.

- If the command contains `{prompt}`, the prompt is substituted there.
- Otherwise the prompt is written to the command's stdin.
- Commands run **without a shell** (`shlex`-split argv, `shell=False`), so prompt
  text can never be interpreted as shell syntax. A test asserts this.

### What the agent is told

Each invocation gets a generated prompt containing: who it is, who else is in the
room, the request addressed to it (with constraints and artifact references), the
shared memory ledger, the recent transcript, and the reply convention. Tune it
with `--context-messages N` and `--no-memory`.

### How an agent steers the room

Everything the agent prints becomes its message. It may end its output with
control lines, which are stripped from the visible message:

| Line | Effect |
|---|---|
| `AIDAPTER-TO: <name>` | Address that participant directly — a blocking turn |
| `AIDAPTER-HANDOFF: <name>` | Hand the baton over without a demand |
| `AIDAPTER-PASS` | Nothing material to add |
| `AIDAPTER-STATUS: complete\|partial\|blocked\|failed` | Set the response status |
| `AIDAPTER-CONFIDENCE: 0.0-1.0` | Set confidence |

Only *trailing* control lines count, so an agent that quotes the convention in the
middle of a review does not accidentally redirect the room.

### The conductor has no authority

It asks the broker who is on point and does what it is told. Every priority,
blocking, and loop-limit decision stays in the queue. Concretely:

- It **stops at the autonomy limit** rather than looping. Two agents that keep
  addressing each other hand the room back to you, exactly as in attached mode.
- It **yields when an unmanaged participant holds the floor** — if the human or an
  agent from another terminal is on point, `run` exits and says so.
- It **stops on pause and on hard stop.**
- A **crashed, timed-out, or missing** agent posts a `failed` response and releases
  the floor, rather than wedging the room.
- Killing `aidapter run` changes no room state. Restart it, or take over manually.

### Exit codes

| Code | Reason |
|---|---|
| 0 | `idle` (nobody queued) or `turn_limit` |
| 11 | the room was stopped |
| 12 | the room hit its autonomy limit and wants you |
| 14 | the room is paused |
| 15 | the floor belongs to someone this terminal does not drive |

## Mixing the modes

They compose, because both are just participants writing to the same room. You can
drive Codex with `aidapter run --agent 'codex=…'` in one terminal while Claude Code
sits in another driving itself in attached mode — the conductor exits with code 15
when Claude holds the floor, so give the conductor `--turns` or re-run it, or
simply manage both agents in one conductor.

You can always watch from a third terminal with `aidapter watch`, and cut in with
`aidapter interrupt --as human -m "..."` at any moment in either mode.
