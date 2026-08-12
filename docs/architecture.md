# Architecture

This document records what AIDapter v0.1 is, and — more importantly — *why*, so that
later changes are made with the original reasoning visible rather than rediscovered.

## 1. Shape of the system

```
                 ┌───────────────────────────────────────────────┐
   claude ──────►│                                               │
   codex  ──────►│   aidapter CLI  (one short-lived process      │
   gemini ──────►│                  per command)                 │
   human  ──────►│                                               │
                 └───────────────────┬───────────────────────────┘
                                     │  Broker (library)
                     ┌───────────────┼────────────────┐
                     ▼               ▼                ▼
             ┌──────────────┐ ┌─────────────┐ ┌────────────────┐
             │ orchestration│ │  semantic   │ │   transcript   │
             │    state     │ │   memory    │ │                │
             │              │ │             │ │                │
             │  SQLite WAL  │ │  memory.md  │ │ messages table │
             │ aidapter.db  │ │  per room   │ │ transcript.jsonl│
             └──────────────┘ └─────────────┘ └────────────────┘
              authoritative     human-editable   chronological
              machine state     shared meaning   record
```

Package layout:

```
aidapter/
  broker/      Broker: the entire orchestration API surface
  cli/         argparse CLI, rendering, session/credential resolution
  models/      enums, entity read-models, the message envelope
  queue/       deterministic scheduler (priority buckets, turns, budget)
  storage/     SQLite connection/schema, row-level DAO, transcript mirror
  memory/      the Markdown ledger: parse, render, mutate
  security/    token generation, hashing, verification
  protocol/    the event-type vocabulary shared with future adapters
  config.py    workspace layout and file permissions
  errors.py    error taxonomy with stable codes and exit codes
```

Dependency direction is one-way: `cli → broker → {queue, storage, memory, security} → models`.
Nothing below the broker knows the CLI exists, which is what makes an HTTP or MCP
façade a wrapper rather than a rewrite.

## 2. Design decisions

### D1. The broker is a library over SQLite, not a daemon

**Decision.** `aidapter start` initializes the workspace and reports state. It launches no
background process and opens no socket. Each CLI invocation constructs a `Broker`,
runs one `BEGIN IMMEDIATE` transaction, commits, and exits.

**Why.** The success criterion for v0.1 — two terminals exchanging targeted messages
through a deterministic queue — needs no networking at all. A daemon would add
process lifecycle, port binding, restart recovery, and stale-socket handling, none of
which buys anything here. SQLite's write lock provides precisely the mutual exclusion
a single-threaded broker loop would: "read the queue, decide, write the queue" is
atomic across processes. And "no listener" is a stronger security posture than "a
listener bound to localhost".

**Cost.** No server push. Agents poll via `aidapter wait`. This is the main thing a
future daemon would fix.

**Revisit when.** A UI needs live updates, rooms need to span machines, or polling
cost becomes measurable.

### D2. Single active speaker, always

**Decision.** At most one participant holds the floor at any moment, whether or not
the current turn is a blocking targeted one.

**Why.** The specification only requires blocking during targeted turns, which implies
concurrent speech is allowed otherwise. That is nondeterministic: message order would
depend on process scheduling. Enforcing a single speaker at all times is strictly
stronger, satisfies the requirement, and makes the whole system reproducible. The
`blocking` flag is still recorded on turns and queue entries — it distinguishes
`blocked_targeted_turn` from `not_your_turn` and appears in provenance.

**Cost.** Two agents cannot chat simultaneously even when it would be harmless.

### D3. The Markdown ledger is authoritative for its own contents

**Decision.** Semantic memory lives in `memory.md` and is parsed → mutated → rewritten
on every change, rather than being rendered from a database table.

**Why.** The requirement says humans can edit it. If the file were a projection of a
table, the next agent write would silently discard hand edits. Making the file
authoritative makes human editing a first-class action. Unknown `##` sections a human
adds are preserved verbatim.

**Cost.** Read-modify-write on a file needs serialization. Ledger writes therefore run
while holding the room's database write lock, so the SQLite lock serializes file
writers too. Tested with concurrent writers.

### D4. Filesystem side effects are deferred until after commit

**Decision.** Transcript appends and automatic ledger entries are queued as callables
and run after the transaction commits. Failures there are returned as `warnings`, not
raised.

**Why.** A rolled-back operation must not leave a stray transcript line or ledger
entry. And conversely, a filesystem hiccup must not make a committed, durable
orchestration operation report failure. The database is authoritative; `aidapter
export` regenerates the mirrors.

**Cost.** A crash between commit and append can leave `transcript.jsonl` one line
short until the next `export`.

### D5. Structured envelope, chat-shaped rendering

**Decision.** Messages carry ~26 fields; the renderer shows `content` prominently and
the rest as indented annotations only when present.

**Why.** Agents need structure to act on (`response_status`, `task_id`, `constraints`),
humans need something readable. Both, without a second message type.

### D6. Per-participant secrets with session files

**Decision.** Each participant gets a 256-bit secret at join, stored salted-hashed. The
CLI writes it to a `0600` session file so later commands find it automatically.

**Why.** Room-scoped credentials are what make cross-room isolation and authoritative
revocation possible. But an agent driving this through a shell cannot thread a secret
through every command, and telling it to would guarantee the secret ends up in shell
history. The session file is the ergonomic compromise; it weakens nothing the broker
checks.

### D7. Passes count against the autonomy budget

**Decision.** `pass` increments `consecutive_agent_turns` exactly like a message does.

**Why.** Otherwise the loop breaker has a hole: two agents can pass at each other
indefinitely without ever yielding the room.

### D8. Human interrupt has two distinct shapes

**Decision.** A *targeted* human message clears the waiting queue and promotes the
addressee (a redirect). An *untargeted* one preserves the queue and re-enqueues the
interrupted speaker at direct-address priority (a comment).

**Why.** "Recalculate the response sequence" is ambiguous. Both readings are useful and
they correspond to two things humans actually do — "no, do this instead" versus "keep
going, but note this". Making the distinction syntactic (`--to` or not) keeps it
predictable.

## 3. Data model

`rooms` carries the authoritative room state, including the `seq` counter, the active
speaker, the active turn, and the autonomy budget. `participants` is scoped by
`room_id` with a unique `(room_id, name)` index — removed rows keep their name
reserved so a removed agent's name cannot be silently reused.

`messages` is the transcript. `queue_entries` is the queue, with a partial unique index
enforcing at most one live entry per participant per room — the invariant is a database
constraint, not just application logic. `turns` records every floor grant and how it
ended. `tasks` tracks units of work opened by a targeted `task` message. `events` is the
audit log for transitions that are not messages.

**`rooms.seq` is a per-room monotonic counter shared by messages and events**, so the
two streams interleave into one total order. It is incremented with
`UPDATE ... RETURNING` inside the write transaction, which is what makes it safe under
concurrency.

## 4. Turn lifecycle

```
   idle ──(direct address / handoff / request-floor)──► queued
                                                           │
                                          activate_next (head of queue)
                                                           ▼
                                                        active
                                                           │
             ┌──────────────┬──────────────┬───────────────┴────────────┐
             ▼              ▼              ▼                            ▼
         completed        passed      interrupted                  cancelled
         (send)           (pass)      (human message)          (removal / stop)
             │              │              │                            │
             └──────────────┴──────────────┴────────────────────────────┘
                                     │
                          record_agent_turn → budget check
                                     │
                        exhausted? ──yes──► awaiting_human (agents refused)
                                     │
                                     no
                                     ▼
                              activate_next
```

`_acquire_floor` also permits an agent to *volunteer*: if nobody holds the floor and
nobody is queued, an agent may simply speak. This keeps ordinary conversation from
requiring a ceremony, while the queue still governs the moment anyone is waiting.

## 5. Invariants

These are the properties the test suite defends. Changing any of them is a deliberate
architectural decision, not a refactor.

1. At most one active speaker per room.
2. Only the active speaker, or a human, may append a non-system message.
3. A blocking targeted turn ends only via its target, a human interrupt, a pass, or a stop.
4. Queue order is a total order over `(priority, enqueue_seq)`.
5. Every message and transition records actor, identity, room, task, timestamp, and a monotonic seq.
6. A removed participant cannot mutate room state, even holding a valid secret.
7. No query crosses a room boundary.
8. `consecutive_agent_turns` never exceeds its maximum; the room then requires human input.
9. A stopped room accepts no mutations.
10. No authoritative state lives in process memory.
11. Per-room `seq` is strictly increasing and never reused across messages and events.

## 6. Extension points

- **Transport.** Wrap `Broker` in HTTP/WebSocket or MCP. It has no CLI dependency.
- **Events.** `aidapter/protocol/events.py` is the shared vocabulary; a UI can subscribe
  by polling `events --since <seq>` today and by subscription later.
- **Scheduling.** `aidapter/queue/scheduler.py` is where priority policy lives; the
  buckets are an enum, not scattered constants.
- **Memory.** `LedgerStore` is the only writer of `memory.md`; a summarizer or
  garbage collector plugs in there.
