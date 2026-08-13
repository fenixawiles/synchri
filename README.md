# Synchri

**Synchri is an open-source local interoperability layer that lets coding agents communicate through a shared, user-controlled room instead of requiring the user to manually relay messages between them.**

*Stop being the clipboard between your coding agents.*

> **Status: v0.2, early prototype.** Synchri is a local app with a deterministic broker, persistent state, a native macOS interface, and managed local launches for supported agent tools. Nothing here is affiliated with or endorsed by Anthropic, OpenAI, GitHub, or Google.

---

## The idea

You are running Claude Code in one terminal and Codex in another. Claude finishes a change and you want Codex to review it adversarially. Today you copy Claude's message, paste it into Codex, wait, copy the findings back, and paste them into Claude. You are the transport layer.

The insight Synchri exploits is that these agents already share a runtime surface: **your machine and your shell.** An agent that can run `git status` can run `synchri send`. That is enough to let them talk to each other directly, with you watching and able to cut in at any moment.

```
Claude ──► synchri send --to codex --type task -m "Review commit abc123 for race conditions."
                    │
                    ▼
             ┌──────────────┐
             │  Synchri    │   deterministic queue · persistent state · provenance
             │  room        │
             └──────────────┘
                    │
Codex  ◄── synchri wait   (blocks until it is on point, then returns the request)
Codex  ──► synchri send --type response --status complete -m "Two interleavings look unsafe: ..."
                    │
Claude ◄── synchri read   (sees the findings; you never touched the clipboard)
```

You watch the whole exchange with `synchri watch`, and interrupt with `synchri interrupt` whenever you want.

**You never copy or paste a message in either mode.** The agent's own answer goes into the room, because either the agent writes it there itself or the conductor captures it:

| Mode | How the reply reaches the room | Terminals |
|---|---|---|
| **Attached** | The agent runs `synchri send` itself after doing the work | one per agent, by preference — not by requirement |
| **Conducted** | `synchri run` invokes each agent's command and posts its stdout | **one, total** |

A participant is any *process* that can run the CLI. The room is a SQLite file: there is no tty affinity anywhere in the design, so "one terminal per agent" is a way to watch them, never a constraint. See [`docs/single-terminal.md`](docs/single-terminal.md).

## The app

The signed macOS app opens Synchri in its own window; `synchri ui` remains the
developer and automation path that opens the same interface in a browser. Both
use the same mission-control dashboard: gates, tests, diff, commits, worktree,
memory, raw event log, and each agent's acknowledgment. It is the **only** part
of Synchri that opens a socket — loopback-only, token-gated per launch, and
opt-in. See
[`docs/ui.md`](docs/ui.md).

## Sessions

`synchri start` walks you through one short wizard — mode, repository, isolated
worktree, agents and roles, permissions, spec, optional timebox — then generates a single
**session contract** that every agent must acknowledge with `UNDERSTOOD` before any
work begins.

```bash
synchri start
```

Agents work in a dedicated git worktree (`synchri-lh-amber-fox-4821`), never in your
primary tree. Permissions are explicit toggles with conservative defaults, and Synchri's
grant is a ceiling that never overrides your provider, OS, or repo host. Completion
requires evidence, not agreement. A timebox guides pacing but never stops a good
session or claims "done". Activation gives the Primary Builder the opening task
automatically; the reviewer follows its handoff. Full detail: [`docs/sessions.md`](docs/sessions.md).

## Install

For a normal Mac user, download and open **Synchri.app** from
[`synchri.com/download`](https://synchri.com/download). It bundles its own
runtime: no Python, package manager, PATH setup, Synchri account, or API key.
Choose a repository and the local coding tools you already use, then click
**Start agents**. Synchri verifies that each managed tool joined and accepted
the contract before work starts. Later releases appear in the app itself and
install through the native, signature-verified macOS updater—no fresh download
or app replacement is required.

The Python package is a developer and automation channel, not the primary
onboarding flow. It requires Python 3.10+ and has no runtime dependencies:

```bash
pipx install synchri
synchri ui
```

If anything looks off, run `synchri doctor`. It changes nothing; it tells you
which local agent tools Synchri can manage and which need the external prompt.
The complete distribution and release model is in
[`docs/distribution.md`](docs/distribution.md).

## Quickstart

```bash
# 1. You: create a room and pre-invite the agents. This prints one ready-to-run
#    join command per agent -- each single-use, name-bound, and expiring.
synchri create-room --name "PR 89 review" --agents claude,codex \
  --goal "find race conditions before merge"

# 2. Paste each printed command into that agent's session. It looks like:
synchri join room_k8b9Ei….SzKG51yg… --name claude

# 3. Claude addresses Codex directly. This creates a blocking turn.
synchri send --from claude --to codex --type task \
  -m "Adversarially review commit abc123 for race conditions." \
  --artifact git:abc123 \
  --constraint "Preserve the existing runtime contract"

# 4. Codex parks until it is on point, does the work, and answers into the room.
synchri wait --as codex
synchri send --from codex --type response --status complete \
  -m "Two interleavings look unsafe: ..." --confidence 0.7

# 5. You watch and can cut in at any time.
synchri watch
synchri interrupt --as human -m "also check the retry path" --to codex
synchri stop-room --as human
```

Or drive the whole thing from **one** terminal, with the agents' own commands:

```bash
synchri run \
  --agent 'claude=claude -p {prompt}' \
  --agent 'codex=codex exec {prompt}' \
  --start claude --turns 6
```

`synchri run` watches the room, invokes whichever managed agent holds the floor, feeds it the pending request, and posts its stdout back. The commands are yours — Synchri still has no built-in knowledge of any provider. Details in [`docs/single-terminal.md`](docs/single-terminal.md); the step-by-step walkthrough with real output, plus the prompt to give an agent driving itself, is in [`docs/two-agent-demo.md`](docs/two-agent-demo.md).

## Session persistence

A room is not a chat that vanishes when the terminal closes. There is no in-memory
broker, so **everything Synchri owns is durable from the first write**: transcript,
shared memory ledger, queue, turns, tasks, identities, and the full audit trail — all
of it survives the session and any restart.

What Synchri deliberately does *not* own is an agent's internal reasoning and working
context. Shadowing that for every provider would be a worse job than the providers do
themselves. So two things happen automatically at the jump, with nothing for you to set up:

1. **The room is bound to the repository it was created in** — root, branch, HEAD,
   origin. A later session in that repo finds the room with no room id anywhere:

   ```console
   $ rm ~/.synchri/current_room     # forget which room it was
   $ cd ~/projects/thing/src/deep    # even from a subdirectory
   $ synchri status
   Room     PR 89 review  (room_XHdZVKbuy1ibvSAI)
   ```

2. **Every arrival gets a briefing** — printed by `join`, re-fetchable any time with
   `synchri briefing --as codex`. It carries the bound repo (and warns loudly if the
   agent is in a different working tree), the whole shared memory ledger, what that
   participant missed since it last spoke, the request waiting for it, and the
   persistence contract: *put shared conclusions in the room ledger, put your own
   resumption notes in whatever memory your platform gives you.*

That last line is overridable per room with `--memory-note`. Full detail, including
where to put what and what the limits are: [`docs/persistence.md`](docs/persistence.md).

## Architecture

Synchri keeps three kinds of state deliberately separate, because collapsing them is how this class of system rots.

| Layer | Where it lives | What it is | Authority |
|---|---|---|---|
| **Orchestration state** | SQLite (`~/.synchri/synchri.db`) | rooms, participants, queue, turns, tasks, audit events | authoritative machine state |
| **Semantic memory** | `rooms/<room_id>/memory.md` | goal, current task, decisions, constraints, open issues, handoffs, artifacts, disagreements | durable shared understanding; human-editable |
| **Transcript** | `messages` table, mirrored to `rooms/<room_id>/transcript.jsonl` | the chronological conversation | the record of what was said |

The Markdown ledger is **not** queue state. The queue never reads it. It exists so a human and an agent can both open the same file and see what the room has actually decided, independent of scrollback.

### There is no daemon

`synchri start` initializes the workspace and prints its state. It does **not** launch a background process and does **not** open a socket. The broker is a library over a WAL-mode SQLite file: every command opens the database, runs one `BEGIN IMMEDIATE` transaction, commits, and exits.

This is a real trade-off, made deliberately:

- **Gained:** no orphaned processes, no port to bind, no restart-recovery path, no state that only exists in RAM. SQLite's write lock provides exactly the mutual exclusion a broker loop would — two agents cannot both become the active speaker, and this is tested with real concurrent processes.
- **Given up:** no server push. Agents poll with `synchri wait`, which costs a cheap read every 500ms.

The core is transport-agnostic. A future HTTP/WebSocket façade or MCP adapter wraps the same `Broker` class without redesigning anything.

Full detail, including the design decisions and their reasoning: [`docs/architecture.md`](docs/architecture.md).

## Queue semantics

Priority order, lowest number first:

| Priority | Bucket | Set by |
|---|---|---|
| 0 | Human intervention | a human message — bypasses the queue entirely |
| 1 | Explicitly addressed participant | `--to <name>` |
| 2 | Model-to-model handoff target | `--handoff-to <name>` |
| 3 | Existing queued participants | `synchri request-floor` |
| 4 | Optional, has not yet responded | `synchri request-floor --priority optional` |

Rules:

- **One active speaker at a time.** v0.1 enforces this always, which is strictly stronger than the "blocking targeted turn" requirement and removes a whole class of nondeterminism.
- **Direct addressing moves the target to the head** and makes the turn **blocking**: other agents get `blocked_targeted_turn` (exit code 7) if they try to speak.
- **When the targeted participant finishes**, an explicit `--handoff-to` enqueues that participant at handoff priority; otherwise the previous queue order resumes.
- **Ties inside a bucket are first-come-first-served**, ordered by the room's monotonic sequence counter — no wall-clock dependence.
- **Participants may `pass`**, which is recorded as an attributed message. Silence and "nothing to add" are different things and the room should be able to tell them apart.
- **A human message interrupts** the current turn and recalculates the sequence. A *targeted* human message clears the waiting queue (a redirect); an *untargeted* one preserves it and lets the interrupted speaker resume (a comment).
- **Loops are bounded.** After `--max-agent-turns` consecutive agent turns (default 8) the room enters `awaiting_human` and agents are refused until a human speaks. Passes count toward this budget, so two agents cannot pass at each other forever.
- **The human can always hard-stop.** `stop-room` is terminal, cancels the active turn, clears the queue, closes open tasks, and cannot be undone.

The normative description, including a worked example, is in [`docs/queue-semantics.md`](docs/queue-semantics.md).

## Security model

Proportionate to the actual threat, and honest about what it is not.

**The adversary this defends against is a confused or misbehaving local agent, not another OS user.** Everything runs as you, on your machine.

What is enforced:

- Room ids and every token are 128/256-bit `secrets` values. Nothing sequential, nothing timestamp-derived.
- Only salted SHA-256 hashes of tokens and participant secrets are stored. The plaintext is shown once. (A high-entropy random secret does not need a slow KDF; there is no password to brute-force.)
- **Joining and reading are separate capabilities.** The observer token can read a room but can never join it. Entering requires an **invite**, which is bound to one participant name, is **single-use**, and **expires** (1h default, `--invite-ttl`). Minting a replacement supersedes the old one.
- **Ending the room ends every grant to enter it.** `stop-room` revokes all pending invites, so a token left in terminal scrollback stops working.
- Every participant is scoped to one room. A credential minted in room A has no authority in room B, even with an identical participant name.
- No query in the data layer can return another room's rows; every one takes an explicit `room_id`.
- **Removal is authoritative.** A removed participant keeps a syntactically valid secret and is still refused, because status is checked separately from the credential.
- The workspace is `0700` and every file in it is `0600`.
- Room ids are validated against a strict pattern before touching the filesystem. Room names and participant names never become path components.
- **No network listener exists.** There is nothing to bind, and a test asserts the broker never calls `socket.bind`.
- No provider credentials are stored, because v0.1 needs none.

What is **not** claimed: this is not multi-tenant, not multi-user, and not hardened against a local attacker who can already read your home directory. See [`docs/security.md`](docs/security.md).

## CLI

```
synchri start                             initialize the workspace, show its state
synchri create-room --name "PR 89" --agents claude,codex
                                           create a room; prints a join command per agent
synchri rooms [--here]                    list rooms (--here: bound to this repo)
synchri briefing --as codex               re-orient: repo, memory, what you missed
synchri join <invite-token> --name codex  redeem an invite and take an identity

synchri invite --as human --name gemini   mint another single-use invite
synchri invites                           list invites and their status
synchri revoke-invite --as human --name gemini

synchri send --from claude --to codex --type task -m "..."
synchri read [--follow] [--tail N]        the transcript
synchri watch                             live human view: status + transcript
synchri turn --as codex                   is it my turn? (no blocking)
synchri wait --as codex [--timeout 300]   block until it is
synchri pass --as codex --reason "..."    nothing material to add
synchri request-floor --as gemini         ask for a turn without being addressed

synchri run --agent 'codex=codex exec {prompt}' [--start claude] [--turns 6]
                                           drive managed agents from this terminal

synchri status                            room state, participants, queue
synchri participants
synchri events                            the state-transition audit log
synchri provenance --message <id>         full provenance for one message
synchri export                            rebuild transcript.jsonl from the database

synchri memory                            print the shared ledger
synchri memory set goal "..." --as claude
synchri memory add decisions "..." --as codex
synchri memory remove open_issues 2 --as human

synchri interrupt --as human -m "..." [--to codex]
synchri pause-room --as human
synchri resume-room --as human
synchri stop-room --as human
synchri remove --as human --participant codex
synchri config --as human --max-agent-turns 12
```

Every command takes `--json`. Exit codes are stable so an agent can branch without parsing prose:

| Code | Meaning |
|---|---|
| 0 | success |
| 2 | validation error |
| 3 | auth error |
| 4 | not found |
| 5 | conflict (e.g. duplicate participant) |
| 6 | invalid room state (paused, stopped, awaiting human) |
| 7 | not your turn / blocked by a targeted turn |
| 10–13 | `wait` outcomes: timeout, room stopped, awaiting human, removed |
| 11–15 | `run` outcomes: room stopped, awaiting human, paused, unmanaged speaker |

Credentials resolve from `--secret`, then `$SYNCHRI_SECRET`, then the `0600` session file written by `join`. The room defaults to `$SYNCHRI_ROOM`, then the last room created.

## Message model

Messages are structured envelopes, not bare strings, but the transcript still reads like chat — the extra fields are annotations, and most messages use few of them.

`message_id` · `room_id` · `seq` · `sender` · `sender_participant_id` · `target` · `target_participant_id` · `timestamp` · `message_type` · `task_id` · `goal` · `content` · `artifact_references` · `constraints` · `claim` · `evidence` · `request_type` · `response_status` · `confidence` · `handoff_target` · `in_reply_to` · `turn_id` · `was_targeted` · `human_override` · `resulted_in_handoff` · `metadata`

Every message and every state transition can answer: who sent it, under which participant identity, when, in which room, for what task, replying to what, whether it was targeted, whether a human overrode it, whether it caused a handoff, and which artifacts it referenced.

## Current limitations

Stated plainly, because the point of a prototype is knowing what it does not do yet.

- **Polling, not push.** `wait` polls every 500ms. Fine for two or three agents on one machine; not a design for scale.
- **No UI.** The data model was built so a group-chat UI can read `status`, `read`, `events`, and `memory` without core changes, but none exists.
- **No provider integrations.** Agents participate by running shell commands, or by being invoked as one via `synchri run --agent 'name=your command'`. There is no MCP server, no provider adapter, no SDK, and no knowledge of any specific agent baked in.
- **Attached agents must cooperate.** Nothing forces a self-driving agent to call `wait` before speaking or to honor a blocking turn — the broker refuses out-of-turn writes, but an agent that never polls simply never participates. `synchri run` sidesteps this by driving the turn loop itself.
- **Conducted agents must be non-interactive.** `run` needs a prompt-in / answer-on-stdout invocation. An agent that only works as an interactive REPL has to be driven in attached mode instead.
- **Single machine.** No remote rooms, no multi-user rooms, no authentication beyond local secrets.
- **The dashboard stream is per-tab.** Each open tab holds a thread and a SQLite connection — fine locally, not a fan-out design.
- **The ledger is append-oriented.** Agents add entries; nothing summarizes or garbage-collects them except a rolling cap on handoffs.
- **Room rediscovery is per working tree.** Two clones of the same repo at different paths are different rooms.
- **Agent-side persistence is advisory.** The briefing tells each agent to save its own context; nothing enforces that it does.
- **`wait` holds no lock.** Between `wait` returning "your turn" and your `send`, a human can interrupt. That is intentional — the human outranks you — but it means `wait` returning success is not a guarantee your send will land.
- **No message editing or deletion.** The transcript is append-only.

## Future adapter strategy

Deliberately not built yet, and not to be started without an explicit decision:

1. **MCP server wrapper** — expose `send` / `wait` / `read` / `memory` as MCP tools so agents that speak MCP skip the shell.
2. **Provider-specific adapters** — thin shims that teach a particular agent harness the Synchri conventions.
3. **Read-only HTTP/WebSocket observer** on `127.0.0.1`, for a group-chat UI.
4. **Richer artifact references** — resolve `git:abc123` into a real diff the room can display.

Each of these wraps the existing `Broker` class. None of them requires changing the queue, the storage layer, or the message model.

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

The test suite treats queue semantics and room isolation as core invariants, and exercises concurrency with real threads and real subprocesses rather than assuming it works.

## License

Apache-2.0.
