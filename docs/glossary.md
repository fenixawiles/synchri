# Glossary

The load-bearing terms, defined the way the app defines them. Each of these
is also available in the interface itself — tap any dotted-underlined term
where it first appears.

## Worktree

A separate working folder git creates from your repository, on its own
branch. The agents make every change inside it, so your primary checkout is
never touched. If a session goes wrong you can delete the worktree and
nothing else has changed; keeping or merging its branch afterwards is your
call.

## Acceptance gate

One concrete thing that must be true before the session can complete —
usually lifted straight from your brief. A gate passes only with recorded
evidence (tests, commits, observations) and sign-off from both the builder
and the reviewer. You can correct, add, or override any gate at any time.

The app also says where a session's gates came from: explicit ids your brief
already carried, the bullets under an acceptance-criteria heading, one
generic fallback gate when nothing parseable was found, or — for a promoted
plan — the approved plan's own acceptance criteria, verbatim.

## Session contract

The written terms every agent must agree to before work begins: the
repository, the authorized worktree, who does what, what they may and may
not do, the brief, and the timebox. Each agent replies `UNDERSTOOD` to the
same text — and if anything material changes later, a new revision goes out
and every agent must agree again. The contract tab leads with a plain-terms
cover; the agents' exact copy stays beneath it.

## Acknowledgment

An agent's `UNDERSTOOD` reply to the current contract revision, matched by
digest. A session cannot activate until every participant — including agents
you run in your own terminals — has acknowledged the same revision.

## Timebox

Optional pacing guidance for the session, never a stop condition. Agents use
it to sequence exploration, implementation, review, and stabilisation; a
session that needs longer continues carefully and reports its state
honestly.

## Planning workspace

A disposable, read-only copy of your repository that planning agents
inspect. It is cloned from one exact commit (the *inspection baseline*) and
has no connection back to your repository, so nothing run inside it can
touch your code. It is thrown away when planning ends; an approved plan
starts the real build from that same inspected commit.

## Room and session

The *room* is the durable conversation underneath: participants, turn queue,
transcript, shared memory. A *session* is the terms that room runs under —
contract, worktree, gates, timebox. The app mostly says "session"; the CLI's
lower-level commands say "room". They refer to the same collaboration.
