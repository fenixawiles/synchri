# Persistence and session continuity

## What survives, and who owns it

The most common misreading of AIDapter is that a room is a chat that disappears
when you close the terminal. It is not. Everything AIDapter owns is durable from
the first write, because there is no in-memory broker for state to live in.

| Thing | Where it lives | Survives the session? | Owner |
|---|---|---|---|
| Transcript | `messages` table + `transcript.jsonl` | **yes** | AIDapter |
| Shared memory ledger | `rooms/<id>/memory.md` | **yes** | AIDapter |
| Queue, turns, tasks, active speaker | SQLite | **yes** | AIDapter |
| Provenance and audit events | SQLite | **yes** | AIDapter |
| Participant identities and invites | SQLite | **yes** | AIDapter |
| **An agent's own reasoning and working context** | its own platform | **only if the agent saves it** | **the agent** |

That last row is the deliberate boundary. AIDapter is not a memory system for
Claude Code or Codex, and trying to be one would mean shadowing every agent's
internal state — badly, and differently for each. Instead the room carries the
*shared* conclusions, and each agent is told to keep its *own* resumption notes
wherever its platform already puts them.

## The two things that happen automatically at the jump

Neither requires the human to arrange anything.

### 1. The room is bound to a repository

`create-room` records the working tree it was run in — root, branch, HEAD, and
origin — so the room knows what it is about.

That gives rediscovery. A later session, in the same repo, with no room id
anywhere, finds the room:

```console
$ rm ~/.aidapter/current_room      # forget everything about which room it was
$ cd ~/projects/thing/src/deep     # even from a subdirectory
$ aidapter status
Room     PR 89 review  (room_XHdZVKbuy1ibvSAI)
Status   active
Speaker  codex  [blocking targeted turn]
```

Room resolution order is: `--room`, then `$AIDAPTER_ROOM`, then **the newest
active room bound to the repo you are standing in**, then the last room created.
Stopped rooms are not rediscovered, so finishing a room does not haunt the next
one. From an unrelated repo, nothing is found — and it says so rather than
guessing.

Bind explicitly with `create-room --repo <path>`; list what is here with
`aidapter rooms --here`.

### 2. Every arrival gets a briefing

`join` returns and prints a briefing; `aidapter briefing --as <name>` re-fetches
it at any time. It contains:

- who it is, who else is in the room, room status, autonomy budget
- **the repository the room is bound to** — and a loud warning if the agent is
  running in a different working tree
- **the shared memory ledger**, reconstituted in full
- **what it missed** — everything since that participant's own last message, so
  a returning agent gets "while you were away" rather than the whole history
- the request addressed to it right now, if any
- **the persistence contract** (below)

This is the part that makes resuming work: an agent that has lost all of its own
context can run one command and be oriented from durable state.

## The persistence contract

Every agent is told this on joining, and again in every conducted turn:

> AIDapter durably stores the ROOM: the transcript, the shared memory ledger, the
> queue and all provenance survive this session and any restart. It does NOT store
> your own reasoning, plans, or working context — that is yours to keep.
>
> So, as you work:
>   * Record durable, shared conclusions in the room ledger, where the other
>     agents and the human can all see them:
>         `aidapter memory add decisions   "..." --as <name>`
>         `aidapter memory add constraints "..." --as <name>`
>         `aidapter memory add open_issues "..." --as <name>`
>         `aidapter memory set current_task "..." --as <name>`
>   * Record whatever YOU need in order to resume later in whatever persistent
>     memory your own platform gives you (your project memory file, session notes,
>     scratch files in the repo). AIDapter deliberately does not manage that.
>   * Re-read this briefing at any time with:  `aidapter briefing --as <name>`

Override it per room with `create-room --memory-note "..."` — `{name}` is
substituted with the participant's name. Use that to point agents at a
convention your project already has.

## Where to put what

A rule of thumb worth telling agents:

- **Room ledger** — anything another participant or the human would need:
  decisions, constraints, open issues, disagreements, the current task.
- **The agent's own memory** — anything only that agent needs to pick its own
  work back up: its plan, its place in a refactor, what it has already ruled out.
- **The repo** — anything that should outlive the room entirely. Code, and notes
  you would commit.

## Limitations

- **Rediscovery is per working tree.** Two clones of the same repo at different
  paths are different rooms. Matching on the origin remote instead would merge
  them; that is a judgement call left open.
- **The repo check is advisory.** A mismatch is a loud warning in the briefing,
  not a refusal. AIDapter does not control what an agent edits, so blocking the
  join would be security theatre; telling the agent plainly is the honest move.
- **HEAD is recorded at creation, not tracked.** The room does not notice
  subsequent commits. Reference specific commits with `--artifact git:<sha>`.
- **Nothing enforces that an agent actually saves its context.** The briefing
  tells it to. Whether it does is up to the agent — the same cooperative
  assumption the rest of the protocol makes.
- **The ledger is not summarized.** It grows until someone prunes it
  (`aidapter memory remove <section> <n>`); only handoffs roll off automatically.
