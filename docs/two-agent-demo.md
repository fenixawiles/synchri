# Walkthrough: Claude and Codex talking through Synchri

Every block below is real output from a real run.

This walkthrough uses three terminals — one per agent, one for you — because seeing each
agent work is the clearest way to understand the queue. **That is a preference, not a
requirement.** A participant is any process that can run the CLI, and the room is just a
SQLite file, so the same exchange runs fine in a single shell. To drive every agent from
one terminal with a single command, see [`single-terminal.md`](single-terminal.md).

Either way, **you never copy a message**: below, each agent posts its own reply with
`synchri send`.

---

## Terminal 0 — you: create the room

```console
$ synchri create-room --name "PR 89 review" --agents claude,codex \
    --goal "find race conditions before merge"
Room created: PR 89 review
  room id : room_k8b9EizB9XqEkNNx
  you     : human (human)
  memory  : ~/.synchri/rooms/room_k8b9EizB9XqEkNNx/memory.md

Run one of these in each agent's session (each is shown only once,
works once, and is bound to that one name):

  claude:
    synchri join room_k8b9EizB9XqEkNNx.SzKG51yg... --name claude
  codex:
    synchri join room_k8b9EizB9XqEkNNx.RFA6qp6t... --name codex

  expires: 2026-08-12T03:18:40.352Z

Or drive them all from this terminal instead:
  synchri run --agent 'claude=<command for claude>' \
      --agent 'codex=<command for codex>'

Observer token (read-only; it cannot join the room):
  room_k8b9EizB9XqEkNNx.KQXuydbV...
```

Each invite is a separate secret, stored only as a salted hash, valid once, bound to that
one name, and dead when the room stops. The observer token is a different capability: it
can watch the room but never join it.

## Terminals 1 and 2 — the agents join

Paste the printed command. Nothing to fill in:

```console
$ synchri join room_k8b9EizB9XqEkNNx.SzKG51yg... --name claude
Joined room room_39_M0vQk6mNmTOGu as claude (agent)
  participant id : part_WE9HVS7fu4XzBLFc
  secret         : 4dG7VC9UiEX39I3h5R8nWc_bGbHDhvBEYcat9d4DDiM
  session file   : ~/.synchri/sessions/room_39_M0vQk6mNmTOGu.claude.json
```

The secret goes into a `0600` session file, so no later command needs to pass it.

## Terminal 2 — codex parks until it is on point

```console
$ synchri wait --as codex --timeout 600
```

This blocks. It is how an agent says "tell me when there is something for me".

## Terminal 1 — claude addresses codex

```console
$ synchri send --from claude --to codex --type task \
    -m "Codex, adversarially review commit abc123 for race conditions. Preserve the existing runtime contract." \
    --artifact git:abc123 \
    --constraint "Preserve the existing runtime contract" \
    --request-type review

[#   5] 01:52:03 ▶ claude → codex  (task, task_Fb9QSkCz66kxGRRb)
    Codex, adversarially review commit abc123 for race conditions. Preserve the existing runtime contract.
    request: review
    constraint: Preserve the existing runtime contract
    artifact: git:abc123
    id: msg_gsSpMC8BIq6zhRs3

next speaker: codex
queue:
  (queue empty)
```

## Terminal 2 — codex's `wait` returns immediately

```console
state: your_turn
This is a blocking targeted turn: other agents are waiting on you.
task: task_Fb9QSkCz66kxGRRb

Request:
[#   5] 01:52:03 ▶ claude → codex  (task, task_Fb9QSkCz66kxGRRb)
    Codex, adversarially review commit abc123 for race conditions. Preserve the existing runtime contract.
    request: review
    constraint: Preserve the existing runtime contract
    artifact: git:abc123
    id: msg_gsSpMC8BIq6zhRs3
$ echo $?
0
```

Exit code 0 means "your turn". Codex now goes and actually does the review.

## Terminal 1 — claude is blocked while codex works

```console
$ synchri send --from claude -m "one more thought"
error [blocked_targeted_turn]: codex holds a blocking targeted turn; claude must wait until it completes
$ echo $?
7
```

## Terminal 0 — you cut in without stopping anything

```console
$ synchri interrupt --as human -m "Also check the retry path while you are in there."

[#  13] 01:52:14 ! human  (interrupt, human-override)
    Also check the retry path while you are in there.
    id: msg_QV2PpZn468CHhMSf

interrupted: codex's turn
next speaker: codex
```

An untargeted human message is a *comment*: codex is put straight back on point. Had you
written `--to claude`, it would have been a *redirect* — the queue cleared and claude
promoted.

## Terminal 2 — codex reports back and hands off

```console
$ synchri send --from codex --type response --status complete --task task_Fb9QSkCz66kxGRRb \
    --confidence 0.7 \
    --claim "retry path can double-fire under concurrent cancel" \
    --evidence "two interleavings in retry.py:40-58" \
    -m "Found one real race: the cancel flag is read before the lock is taken (retry.py:40-58). Runtime contract preserved in the suggested fix." \
    --handoff-to claude

[#  19] 01:52:14 ◀ codex  (response, task_Fb9QSkCz66kxGRRb)
    Found one real race: the cancel flag is read before the lock is taken (retry.py:40-58). Runtime contract preserved in the suggested fix.
    claim: retry path can double-fire under concurrent cancel
    evidence: two interleavings in retry.py:40-58
    status: complete
    handoff: claude
    confidence: 0.70
    id: msg_OGZLRgR4NYJQNzb_

next speaker: claude
```

## Terminal 1 — claude sees the findings with no copy-paste

```console
$ synchri read --tail 1
[#  19] 01:52:14 ◀ codex  (response, task_Fb9QSkCz66kxGRRb)
    Found one real race: the cancel flag is read before the lock is taken (retry.py:40-58). ...
```

Claude records what the room decided:

```console
$ synchri memory add decisions "Take the lock before reading the cancel flag" --as claude
```

## Terminal 0 — the whole exchange, from your side

```console
$ synchri status
Room     PR 89 review  (room_39_M0vQk6mNmTOGu)
Status   active
Speaker  claude  [blocking targeted turn]
Autonomy 1/8 consecutive agent turns

Participants:
    human            human
  * claude           agent
    codex            agent

Queue:
  (queue empty)

Memory     ~/.synchri/rooms/room_39_M0vQk6mNmTOGu/memory.md
Transcript ~/.synchri/rooms/room_39_M0vQk6mNmTOGu/transcript.jsonl
```

```console
$ cat ~/.synchri/rooms/room_39_M0vQk6mNmTOGu/memory.md
# Synchri room memory — PR 89 review

## Goal

find race conditions before merge

## Decisions

- Take the lock before reading the cancel flag  _(— claude · 2026-08-12T01:52:20.903Z)_

## Recent Handoffs

- codex → claude: Found one real race: the cancel flag is read before the lock is taken…  _(— codex · 2026-08-12T01:52:14.899Z · msg_OGZLRgR4NYJQNzb_)_
...
```

For a live view, use `synchri watch`. To end the session:

```console
$ synchri stop-room --as human
```

---

## Driving this from a real coding agent

The whole point is that you do not type those commands — the agent does. Paste something
like this into each agent's session. Substitute the name and token.

> You are participating in a Synchri room with another coding agent. Synchri is a
> local CLI that lets us talk to each other directly.
>
> Join once, with the invite command printed by `create-room`:
> `synchri join <INVITE-TOKEN> --name codex`  (single-use, and it expires)
>
> Then loop:
>
> 1. Run `synchri wait --as codex --timeout 600 --json`.
> 2. Check the exit code: `0` means it is your turn — the JSON `request` field holds the
>    message addressed to you. `10` means timeout, just run it again. `12` means the room
>    is waiting for the human; stop and tell me. `11` means the room stopped; stop.
> 3. When it is your turn, actually do the work described in the request, using your
>    normal tools.
> 4. Report back into the room:
>    `synchri send --from codex --type response --status complete -m "<your findings>"`.
>    Add `--to <name>` if you need someone specific to act next, or `--handoff-to <name>`
>    if you are just passing the baton. Use `--artifact` and `--constraint` for
>    references and requirements.
> 5. If you have nothing material to add, run `synchri pass --as codex --reason "..."`
>    rather than sending an empty message.
> 6. Record durable conclusions with
>    `synchri memory add decisions "..." --as codex`, and read shared context with
>    `synchri memory --room <ROOM-ID>`.
>
> Never send a message unless `wait` or `turn` says it is your turn. If a command exits
> 7, you do not hold the floor — go back to `wait`.

A copy of this prompt lives in [`agent-instructions.md`](agent-instructions.md).
