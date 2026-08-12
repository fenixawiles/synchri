# Queue semantics

The queue is the part of Synchri that has to be exactly right. This document is the
normative description; `synchri/queue/scheduler.py` is the implementation and
`tests/test_queue.py` is the enforcement.

## Priority buckets

Ordering is a total order over `(priority, enqueue_seq)`. Lower priority number wins.
`enqueue_seq` is the room's monotonic counter, so ties inside a bucket are
first-come-first-served with no dependence on wall-clock time or process scheduling.

| # | Bucket | How you get into it |
|---|---|---|
| 0 | `HUMAN` | Reserved. Humans never actually queue — they bypass it (see below). |
| 1 | `DIRECT_ADDRESS` | Someone sent you a message with `--to <you>`. |
| 2 | `HANDOFF` | Someone finished a turn with `--handoff-to <you>`. |
| 3 | `QUEUED` | You ran `synchri request-floor`. |
| 4 | `OPTIONAL` | You ran `synchri request-floor --priority optional`. |

A participant holds **at most one live queue entry**, enforced by a partial unique index
in the schema. Being addressed again while already waiting *promotes* the existing entry
if the new priority is stronger, and refreshes its position within the new bucket —
being addressed directly is a fresh ask, not a resumption. A weaker or equal priority
leaves the entry where it is, preserving FIFO fairness.

## The floor

**At most one participant holds the floor at any moment.** This is stronger than the
requirement that targeted turns block, and it is what makes the system deterministic.

An agent may write only when it holds the floor. Three ways to get it:

1. **Promotion** — it is at the head of the queue and the room activates it.
2. **Volunteering** — nobody holds the floor and nobody is queued, so an agent that
   sends simply takes it. Ordinary conversation needs no ceremony.
3. **Holding** — `send --hold` keeps the floor for a follow-up message.

Everyone else gets an error:

| Situation | Error code | Exit |
|---|---|---|
| Someone holds a *blocking* targeted turn | `blocked_targeted_turn` | 7 |
| Someone holds an ordinary turn | `not_your_turn` | 7 |
| You are not at the head of a non-empty queue | `not_your_turn` | 7 |
| Room hit its autonomy limit | `awaiting_human` | 6 |
| Room is paused | `room_paused` | 6 |
| Room is stopped | `room_stopped` | 6 |

## Targeted turns are blocking

`send --to codex` does three things atomically: appends the message, ends the sender's
turn, and enqueues `codex` at `DIRECT_ADDRESS` with `blocking = true`. When codex is
activated, every other agent reporting in via `turn` or `wait` sees `blocked`, and any
attempt to write is refused with `blocked_targeted_turn`.

If the message is `--type task`, a `task` row is opened, assigned to the target, and
threaded onto every subsequent message that carries its `task_id`. The task closes when
the assignee replies with `--status complete`, or is marked `passed` if they pass.

## Completing a turn

When the floor-holder sends without `--hold`, the turn ends and one of the following
happens:

- **`--to <name>`** → that participant is enqueued at `DIRECT_ADDRESS`, blocking.
- **`--handoff-to <name>`** → that participant is enqueued at `HANDOFF`, blocking, the
  handoff is written to the audit log, and a line is appended to the memory ledger's
  *Recent Handoffs*.
- **Neither** → nothing new is enqueued; the pre-existing queue resumes in its own order.

Note that a handoff *enqueues* rather than force-activating. If a direct address is
already pending, it outranks the handoff — that is what the priority table means.

## PASS

`synchri pass` is a real, attributed message of type `pass` in the transcript. A room
should be able to distinguish "nothing material to add" from "still working".

- Passing **while holding the floor** ends the turn as `passed`, closes any associated
  task as `passed`, and promotes the next queued participant.
- Passing **while merely queued** gives up the queue slot without disturbing whoever
  holds the floor.

Passes count against the autonomy budget. Without that, two agents can pass at each
other forever.

## Human priority

A human message never queues. It always:

1. **Interrupts** the active turn, which is recorded with status `interrupted`.
2. **Resets** the autonomy budget to zero and clears `awaiting_human`.
3. **Recalculates** the sequence, in one of two shapes:

| Shape | Command | Effect |
|---|---|---|
| **Redirect** | `interrupt -m "..." --to codex` | Clears the waiting queue and promotes codex. The old queue is stale relative to the new instruction. |
| **Comment** | `interrupt -m "..."` | Preserves the queue and re-enqueues the interrupted speaker at `DIRECT_ADDRESS`, so they can finish what they were asked. |

Humans can also `pause-room` (agents refused, human still acts, active turn preserved),
`resume-room`, `remove` a participant, `config --max-agent-turns`, and `stop-room`.

**`stop-room` is terminal.** It cancels the active turn, clears the queue, cancels open
tasks, and refuses every subsequent mutation from every participant including the human
who issued it. It cannot be resumed, and it survives a restart because it is persisted
state, not a process flag.

## Loop control

Each completed agent turn — message or pass — increments `consecutive_agent_turns`. On
reaching `max_consecutive_agent_turns` (default 8, `--max-agent-turns` at creation,
`synchri config` later) the room sets `awaiting_human`:

- agents are refused with `awaiting_human` (exit 6)
- `wait` returns immediately with state `awaiting_human` (exit 12)
- **pending queue entries survive** — nothing is lost, the room simply stops advancing
- any human message resets the counter and the room resumes

This is the loop breaker. `tests/test_queue.py::test_ping_pong_between_agents_terminates`
drives two agents addressing each other in a loop and asserts the room hands itself back.

## Worked example

Four participants: `human`, `claude`, `codex`, `gemini`.

| Step | Action | Floor | Queue |
|---|---|---|---|
| 1 | `claude: send --hold "starting"` | claude | — |
| 2 | `gemini: request-floor` | claude | gemini (p3) |
| 3 | `claude: send --to codex --type task` | **codex** (blocking) | gemini (p3) |
| 4 | `gemini: send` → refused `blocked_targeted_turn` | codex | gemini (p3) |
| 5 | `human: interrupt -m "note this"` | codex (resumed) | gemini (p3) |
| 6 | `codex: send --type response --handoff-to gemini` | gemini | — |
| 7 | `gemini: pass` | — | — |

At step 5 the human comment interrupted codex and put it straight back at
`DIRECT_ADDRESS`, ahead of gemini, because an untargeted human message is a comment and
not a redirect. Had the human written `--to gemini`, step 5 would instead have cleared
the queue and promoted gemini.
