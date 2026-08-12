# Instructions to give a coding agent

Paste this into an agent's session, substituting `<NAME>`, `<INVITE-TOKEN>`, and
`<ROOM-ID>`. `aidapter create-room --agents <NAME>,...` prints the exact join command for
each agent, so the first line below is usually copy-paste ready. This is the v0.1
integration surface: the agent participates because it can run shell commands, with no
SDK and no provider plumbing.

---

You are participating in an AIDapter room together with other coding agents and a human.
AIDapter is a local CLI that lets us address each other directly instead of the human
copying messages between us.

**Join once:**

```bash
aidapter join <INVITE-TOKEN> --name <NAME>
```

The invite is single-use, bound to your name, and expires — so run it once and do not
retry with the same token. If it fails as expired or already used, ask the human for a
fresh one (`aidapter invite --name <NAME>`). Your participant credential is stored in a
local session file, so later commands need only `--as <NAME>`.

**Then loop:**

1. **Wait for your turn.**
   ```bash
   aidapter wait --as <NAME> --timeout 600 --json
   ```
   Branch on the exit code:

   | Exit | Meaning | What to do |
   |---|---|---|
   | 0 | It is your turn | The JSON `request` field holds the message addressed to you |
   | 10 | Timed out | Run `wait` again |
   | 12 | Room is awaiting human input | Stop and tell the human |
   | 11 | Room stopped | Stop entirely |
   | 13 | You were removed | Stop entirely |

2. **Do the actual work** described in the request, with your normal tools. Honor
   anything in the request's `constraints` array, and look at anything in
   `artifact_references`.

3. **Report back into the room.**
   ```bash
   aidapter send --from <NAME> --type response --status complete \
     -m "<your findings>" \
     --claim "<the assertion you are making>" \
     --evidence "<what supports it>" \
     --confidence 0.7 \
     --artifact "<file or commit you touched>"
   ```
   - Add `--to <name>` when you need a specific participant to act next. This creates a
     **blocking turn**: everyone else waits until they answer.
   - Add `--handoff-to <name>` when you are simply passing the baton rather than making
     a request.
   - Add `--task <task_id>` and `--status complete` to close a task that was assigned to
     you.

4. **Pass when you have nothing material to add** — do not send an empty or filler
   message:
   ```bash
   aidapter pass --as <NAME> --reason "no concerns with this change"
   ```

5. **Use the shared memory ledger** for anything that should outlive the scrollback:
   ```bash
   aidapter memory --room <ROOM-ID>                            # read shared context
   aidapter memory add decisions "..." --as <NAME>              # record a decision
   aidapter memory add constraints "..." --as <NAME>            # record a constraint
   aidapter memory add open_issues "..." --as <NAME>            # record an open issue
   aidapter memory add disagreements "..." --as <NAME>          # record a disagreement
   ```
   The ledger is not the chat log. Put durable conclusions there, not conversation.

6. **Catch up on what you missed** with `aidapter read --room <ROOM-ID> --tail 20`.

**Rules:**

- Never send unless `wait` or `turn` says it is your turn. Exit code **7** means you do
  not hold the floor — go back to `wait`.
- The human outranks everyone and can interrupt at any moment. If they redirect you,
  follow the new instruction.
- Do not loop with another agent indefinitely. The room enforces a limit on consecutive
  agent turns, but you should yield to the human before it fires.
- Address a specific participant only when you genuinely need them to act — a targeted
  message blocks everyone else.
