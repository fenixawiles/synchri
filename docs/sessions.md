# Sessions: startup and the operating contract

A **room** (the original layer) owns messaging: the turn queue, the transcript,
shared memory. A **session** sits above it and owns the *terms* those messages
happen under — which repository, which isolated worktree, which agents in which
roles, what they are allowed to do, what "done" means, and by when.

Nothing about the room changed to add sessions. `sessions.room_id` is the link.

## The flow

```
synchri start
   │
   ├─ 1  mode          interactive · long horizon · review/audit
   ├─ 2  repository    validated: exists, is git, base branch known, not mid-merge
   ├─ 3  worktree      created for you, named synchri-lh-amber-fox-4821
   ├─ 4  agents        who, which runtime, which role
   ├─ 5  permissions   explicit toggles, conservative defaults
   ├─ 6  spec          what to build (canonical, hashed, immutable)
   ├─ 7  deadline      duration or fixed time
   ├─ 8  review        one screen, no jargon
   ├─ 9  contract      generated once, same core for every agent
   ├─ 10 acknowledge   UNDERSTOOD or CONFLICT: <reason>
   └─ 11 activate      only when all agree
```

Every step is a flag too, so the whole thing is scriptable:

```bash
synchri start --yes --mode long_horizon --repo . \
  --agent claude:claude_code:primary_builder \
  --agent codex:codex:adversarial_reviewer \
  --spec-file spec.md --deadline "10 hours"
```

`--dry-run` shows the review screen and creates nothing.

## Modes

| Mode | Autonomy | Requires | Notes |
|---|---|---|---|
| **Interactive Collaboration** | 8 agent turns before yielding | — | you stay in the room |
| **Long Horizon Development** | 200 | spec + deadline + 2 agents | agents keep going; finishing a turn is not a reason to ping you |
| **Review / Audit** | 24 | spec (the criteria) | forces push, merge, deploy, destructive OFF regardless of what you ticked |

A mode may *narrow* your permissions. It can never widen them. Adding a mode is
a new entry in `POLICIES`, not a redesign.

## The isolated worktree

**Agents never work in your primary tree.** Every session gets a dedicated git
worktree on its own branch, cut before the session can activate, placed *beside*
the repo (`../.synchri-worktrees-<repo>/`) so session changes never appear as
untracked files in your checkout.

Names are readable — `synchri-lh-amber-fox-4821` — because you will see them in
`git worktree list` and need to know what they are. Collisions retry silently.

If your primary tree is dirty, Synchri says so and leaves it completely alone.

## Permissions

Grouped toggles with three states: **YES**, **ASK FIRST**, **NO**. ASK is not a
grant — `check_permission` raises for it, instructing the agent to escalate.

Defaults: read/edit/test/build/lint/commit/branch/review-PR are YES; install
deps is ASK; push, force push, rebase, reset, delete branch, create/merge/close
PR, CI and infra config, deploy and destructive commands are all NO.

**Synchri's grant is a ceiling, never an override.** Every contract carries this
verbatim:

> These permissions are the user's session-level authorization only. They do not
> override restrictions imposed by your runtime, your provider, the operating
> system, the repository host, available credentials, branch protections, or any
> other applicable policy. Where those are stricter, they win. Where Synchri says
> NO, this session forbids the action even if you are technically able to
> perform it.

## The contract

One document, generated from every choice you made, hashed, versioned. Every
participant gets the **same core text**; only a role section differs. That makes
"both agents agreed to the same terms" checkable rather than hopeful.

Generation is deterministic — same inputs, byte-identical output — so the digest
is a real identity.

**Acknowledgment is strict.** `UNDERSTOOD` must be the whole reply, not a word
inside a sentence: an agent musing "I understood the contract and will begin"
does not count. `CONFLICT: <reason>` blocks activation and shows you the reason.

Change a permission, the spec, the deadline, or the roster and you get a new
revision; every prior acknowledgment is void and everyone agrees again. Terms
never drift mid-session without anyone noticing.

## Completion

Agreement is not evidence. A gate reaches PASS only with cited evidence (tests,
commits, artifacts) *and* both builder and reviewer sign-off. A gate that cannot
be checked is UNVERIFIED, never optimistically passed.

```
AUTH-01  [PASS]  users can log in
    evidence: tests/test_auth.py::test_login
    commits: 0811b23
    builder: implemented
    reviewer: verified independently
```

`complete()` refuses with the concrete blocker until every required gate is
satisfied.

## Deadlines

A real boundary, not metadata. Phases shift as it approaches: exploration →
implementation → stabilisation → freeze. When it passes the session becomes
`timed_out` and produces an honest handoff — branch, head, gates satisfied,
gates unmet, blockers, worktree, recommended next action, reason stopped. It
**never** claims completion.

## Restart

Everything is on disk. On restart, `synchri session restore` reports what was
active, whether the worktree still exists, whether the repo is still valid,
whether the deadline passed while you were away, and who must reconnect. It
**resumes nothing** — no mutating action restarts on its own.

## Presets

`--save-preset "Claude + Codex Safe Build"` stores mode, agents, roles,
permissions and escalation policy. It never stores the spec, deadline, worktree,
or session id — those are what make a session *this* session.

## Hard invariants (enforced in code, not documented at)

| # | Invariant | Where |
|---|---|---|
| 1 | No development before the worktree exists | `activate()` validates |
| 2 | No development before unanimous acknowledgment of the current revision | `activate()` |
| 3 | Permissions are read from state, never inferred | `check_permission()` |
| 4 | The primary tree is never the mutation target | `worktree.validate()` |
| 5 | Human interruption outranks the queue | broker |
| 6 | A deadline never causes a false completion claim | `expire()` / `complete()` |
| 7 | Agreement alone is not evidence | `Gate.blocks_completion()` |
| 8 | Agents cannot rewrite spec gates | spec is hashed; edits version it |
| 9 | Meaningful actions retain provenance | room event log |
| 10 | Raw state stays inspectable | `session show/contract/gates/dashboard` |
| 11 | Synchri never claims to override provider permissions | contract text |
| 12 | Destructive actions default off | `CATALOG` defaults |
| 13 | Removed participants cannot mutate | broker |
| 14 | The UI prevents mistakes rather than documenting them | `SessionDraft` validation |
