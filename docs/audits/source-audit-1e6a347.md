# Synchri source audit: disagreement, independence, logging, and exposure

**Purpose.** Pre-study audit of Synchri for a planned preregistered study of
multi-architecture LLM coordination and factual reliability.

**Audited commit:** `1e6a3471f07c372fa4aaa1daee14cce87f0b8b50`
("Merge pull request #17 from fenixawiles/codex/fix-desktop-cli-discovery",
committed 2026-09-06 15:07:35 -0500), package version `0.5.3`
(`pyproject.toml`, `synchri/__init__.py:20`).

**Audit date:** 2026-10-22. **No source, test, or configuration file was
modified.** The only additions are this report and an evidence appendix,
`docs/audits/probes/test_audit_probes.py`, which sits outside the suite's
`testpaths` and does not change what `pytest -q` collects.

---

## 0. How to read this report

### Evidence levels

Every claim is tagged with the strongest evidence that supports it:

| Tag | Meaning |
|---|---|
| **VERIFIED (repo test)** | An existing test in `tests/` exercises the property and passed at the audited commit. |
| **DEMONSTRATED (probe Pn)** | An audit probe in `docs/audits/probes/test_audit_probes.py` exercises the property and passed at the audited commit. Probes are executable tests, but they belong to this audit, not to the project's own suite. |
| **SOURCE ONLY** | Read from the code. No executable test shows it. |
| **PROMPT ONLY** | The behavior exists only as text sent to a model. Whether it happens depends on the model doing what the text says. |
| **DOC ONLY** | Stated in documentation or comments, with no supporting code path found. |

A property is called *verified* only when a VERIFIED or DEMONSTRATED test
backs it. Prompt text is evidence of what the system *intends*. It is never
evidence of what the system *does*.

### Enforcement classes (question 2)

- **Programmatic.** Python code accepts, refuses, or routes regardless of
  what the model says.
- **Prompt.** Instructions in the contract, role doctrine, or task text.
- **Model compliance.** The mechanism works only if a model emits a specific
  control line (`SYNCHRI-…`). The code then enforces *consequences* of that
  line, but nothing forces the line to be emitted.

### Runtime and audit environment

| Item | Value |
|---|---|
| Python | 3.13.16 (GCC 13.3.0) |
| SQLite | 3.45.1, FTS5 available |
| pytest | 9.1.1 |
| OS | Linux 6.18.44 x86_64, glibc 2.39 |
| Dependencies | `certifi>=2024.2.2`; dev: `pytest>=7.0` (`pyproject.toml`) |
| Install | `pip install -e ".[dev]"` |
| Repo suite | `pytest -q`: **749 passed** (about 150 s), run twice |
| Audit probes | **20 passed**, under the repository's own pytest config (`filterwarnings = error`) |
| Provider CLIs on the audit host | `claude` present at `/opt/node22/bin/claude`. `codex`, `copilot`, `gemini` absent. **No real provider CLI was invoked.** Every test and probe uses scripted stand-in agents (`tests/test_runner.py:25` `write_agent`). |
| State location | `~/.synchri` or `$SYNCHRI_HOME` (`synchri/config.py:18-22`); DB `synchri.db`; per-room `memory.md`, `transcript.jsonl` |

Configuration constants that bear on the study:

| Constant | Value | Location |
|---|---|---|
| Planning budgets (turns / revisions / minutes), one resume | 24 / 6 / 60 | `synchri/session/planning.py:67-69` |
| Autonomous-turn limit per mode (interactive / long-horizon / audit / planning) | 8 / 200 / 24 / 24 | `synchri/session/modes.py:323,337,348,365` |
| Prompt transcript window | last 12 messages | `synchri/runner/conductor.py:83` |
| Agent timeout | 900 s | `synchri/runner/agent_command.py:28` |
| Captured stdout / stderr cap (oldest lines dropped) | 2,000,000 / 200,000 chars | `synchri/runner/agent_command.py:155-156` |
| Stream telemetry caps | title 160, detail 4,000, command output 1,000 chars; 2,000 events per invocation; **1,000 events kept per room** | `synchri/runner/stream_events.py:28-32` |
| Maintained provider commands | `claude -p --verbose --output-format stream-json {prompt}`; `codex exec --json {prompt}`; `copilot -sp {prompt}` | `synchri/session/modes.py:489,548,580` |
| Planning commands | Claude: `--permission-mode plan --safe-mode --strict-mcp-config --tools "Read,Glob,Grep"`; Codex: `--sandbox read-only` | `synchri/session/modes.py:519-522,566` |

None of the maintained commands sets a model, temperature, seed, or token
limit. Each provider CLI's own default or user configuration decides these,
and Synchri does not record them (§6).

---

## 1. Executive summary

**Verdict: not ready for a preregistered study of inter-model disagreement
or factual reliability as-is.** Synchri has real, programmatically enforced
structure *around* disagreement. But the disagreement itself is optional at
every point. Several properties a study needs are missing or demonstrably
violated:

1. **Disagreement is never required.** A reviewer can reach PLAN-READY on
   the first draft with zero objections (probe P1; also the repository's
   own `tests/test_planning.py:308`). A reviewer can pass with no content
   (P8). The plan-reviewer prompt tells the model *not* to manufacture
   objections (`synchri/session/modes.py:238-240`).
2. **A recorded disagreement can be overridden.** A gate the reviewer
   reported `fail` completes once the builder reports `pass`, because gate
   status is last-writer-wins and "sign-off" means any non-empty assessment
   string (P5; `synchri/session/gates.py:71-72`,
   `synchri/session/manager.py:1000-1016`).
3. **No independent initial responses.** Every mode is sequential: one
   model drafts, the other sees that draft. No step collects blind,
   independent answers before models communicate (§5).
4. **Logging is incomplete for reproducibility.** Prompts and raw replies
   are not persisted (P9). Model identifiers are captured only from the
   Claude stream's `init` event and never for Codex (P11, P12). Inference
   parameters are not recorded at all. Stream telemetry is pruned to 1,000
   events per room (P13). Gate assessments are overwritten (P6). Session
   audit writes fail silently (P14). The conductor's "idle" stop is not
   logged (P7b).
5. **Conditions and reference answers can reach the models.** The roster
   tells each model the other's runtime (P15). The idea/spec reaches every
   model verbatim, so a reference answer placed there is exposed (P16).
   Managed agents are told where Synchri's state lives and can read the
   whole database (P17). That database holds the human's room secret, so
   any process that can read it can post as the human (P18).
6. **Shared workspace.** All participants in a session use one worktree as
   their working directory (P19). `CLAUDE.md` ("each in an isolated git
   worktree") can be read as one worktree per agent; the code gives one per
   session.

---

## 2. Q1 — Mechanisms that initiate, require, encourage, or record disagreement

### 2.1 Planning Mode objection loop (the primary mechanism)

| ID | Mechanism | What it does | Implementation |
|---|---|---|---|
| M1 | Typed objections | Reviewer emits `SYNCHRI-OBJECTION: blocking\|nonblocking\|advisory\|<text>`. Parsed into `Directives.objections`; malformed kinds become warnings. | Parser `synchri/runner/agent_command.py:433-444`; persisted by `planning._raise` `synchri/session/planning.py:474-492`; role check (reviewer only) `planning.py:426-429`; table `synchri/storage/schema.sql:519-535` |
| M2 | Forks | `SYNCHRI-FORK: <choice and both sides>` records a `fork` objection. | Parser `agent_command.py:452-456`; `planning.py:420-424` (**no role check**: either role may fork, P4) |
| M3 | Blocking objections and forks prevent PLAN-READY | `approve` is refused while any `blocking` or `fork` objection is open. | `BLOCKING_KINDS` `planning.py:82`; `open_blockers` `planning.py:222-226`; refusal `planning.py:575-582` |
| M4 | Review verdict | `SYNCHRI-PLAN-REVIEW: approve\|…` or `revise\|…`. Only the reviewer may close; only after revision ≥ 1. | Parser `agent_command.py:421-432`; `planning.py:449-460`; `_close_review` `planning.py:574-645` |
| M5 | Planner dispositions | `SYNCHRI-OBJECTION-RESOLVED: <id>\|<disposition>` marks an objection resolved. Planner only. | Parser `agent_command.py:445-451`; `planning.py:395-418` |
| M6 | Automatic re-review routing | Every submission sends the reviewer a task: "Review it adversarially … Raise findings with SYNCHRI-OBJECTION lines". A `revise` sends the planner the open items. | `_submit` `planning.py:557-570`; `_close_review` revise branch `planning.py:622-645`; `_send_task` `planning.py:648-661` |
| M7 | Forced reviewer turn after a "human direction" task | Tasks sent with `metadata.human_direction` force the named reviewer to take the next turn, whatever control lines the lead emits, even on a pass. | `_send_task` sets the marker `planning.py:653`; enforced in `Broker._send_as_agent` `synchri/broker/broker.py:833-844` and `Broker.pass_turn` `broker.py:975-983,1024-1034`; resolver `broker.py:1817-1840` |
| M8 | Invalidation re-opens review | A human edit, a captured consideration, a human waiver, or repository drift returns the plan to review. Drift also auto-raises a `blocking` objection authored by `synchri`. | `reopen` `planning.py:767-793`; `note_capture` `planning.py:861-890`; `waive_objection` `planning.py:796-858`; drift `planning.py:1093-1103,1234-1279` (auto-objection `1259-1265`) |
| M9 | Blocking items cannot cross approval | Human approval re-checks open blockers. | `planning.py:1081-1088` |
| M10 | Residual concerns carried forward | Open nonblocking/advisory objections and resolved findings are serialized into the coordination spec. | `_spec_from_plan` `planning.py:1422-1444` |
| M11 | Budgets end the loop | 24 turns / 6 revisions / 60 minutes → `needs_human_resolution`, one resume. | `count_turn` `planning.py:669-693`; `_exhaust` `planning.py:708-722`; `resume_budget` `planning.py:725-759`; revision check `planning.py:544-551` |

### 2.2 Long-horizon (coordination) mode

| ID | Mechanism | Implementation |
|---|---|---|
| M12 | Adversarial Reviewer doctrine: "falsify the builder's claims, not … agree"; "'Looks good to me' is not evidence" | `synchri/session/modes.py:146-172` |
| M13 | Builder doctrine: request "explicit … adversarial review" | `modes.py:132-141` |
| M14 | Mode doctrine: "Completion requires evidence, not agreement … both the builder and the reviewer have signed off" | `modes.py:260-263` |
| M15 | Gate dual sign-off: completion requires `pass`, evidence, and both a builder and a reviewer assessment | `Gate.is_signed_off` / `blocks_completion` `synchri/session/gates.py:71-87`; role→slot mapping `synchri/session/manager.py:1009-1015`; completion check `manager.py:1240-1245` |
| M16 | Forced reviewer turn after human input (UI) or after an agent replacement | `synchri/ui/api.py:903-913`; `synchri/runner/managed.py:754-760`; phase tasks `manager.py:1373-1395` |

### 2.3 Other mechanisms

| ID | Mechanism | Implementation |
|---|---|---|
| M17 | Contract acknowledgment: each agent replies exactly `UNDERSTOOD` or `CONFLICT: <reason>`; any conflict blocks activation | Closing block `synchri/session/contract.py:213-230`; parser `contract.py:233-258`; `acknowledge` `manager.py:514-559`; activation refusal `manager.py:619-630`. This is agent-vs-contract disagreement, not inter-model disagreement. |
| M18 | Dropbox dual evaluation: both main roles approve or decline each side task; any non-approval declines, both rationales kept | `synchri/session/dropbox.py:582-~700`; prompt `dropbox.py:800-824`. The second evaluator **sees the first's verdict** ("evaluations so far", `dropbox.py:846-850`), so the two evaluations are not independent. |
| M19 | Escalation rules `requirement_conflict` and `deadlock` ("after N review cycles", default 3) | `synchri/session/escalation.py:42-45,54`. **Prompt only:** rendered into the contract (`escalation.py:84-86`, `contract.py:184-190`). `deadlock_cycles` is never computed anywhere; no code detects deadlock. |
| M20 | Memory-ledger section "Unresolved Disagreements" | `synchri/memory/ledger.py:38,60-62`. **No automatic writer.** Populated only if a participant or human writes it (`broker.py:1395-1437`). |
| M21 | Deliberative timeline: read-time classification of objections, conflicts, resolutions, residual concerns | `synchri/session/deliberation.py:94-357` (objections `144-165`, contract conflicts `207-213`, residuals `324-350`). Derived index only, never a second record (`deliberation.py:1-18`). |

---

## 3. Q2 — How each mechanism is enforced

| Mechanism | Initiation | Consequence once initiated | Evidence |
|---|---|---|---|
| M1 objections | **Model compliance** (reviewer must emit the line) | **Programmatic** (typed, persisted, role-checked) | VERIFIED `tests/test_planning.py:204` (`test_the_loop_planner_first_review_revision_ready`), `:471` |
| M2 forks | Model compliance | Programmatic (blocks READY) | VERIFIED `tests/test_planning.py:289`; no role check DEMONSTRATED P4 |
| M3 blockers prevent READY | n/a | **Programmatic** | VERIFIED `tests/test_planning.py:233-241` |
| M4 verdict | Model compliance | Programmatic (role and revision checks) | VERIFIED `tests/test_planning.py:213-217` |
| M5 dispositions | Model compliance | Programmatic role check; **no reviewer concurrence** | DEMONSTRATED P3 |
| M6 re-review routing | **Programmatic** (task sent on every submit/revise) | Prompt (the content of the request) | VERIFIED `tests/test_planning.py:229-230,248-250` |
| M7 forced reviewer turn | **Programmatic** | Reviewer gets the floor; *what it says* is model compliance | VERIFIED `tests/test_runner.py:421,447,471`, `tests/test_queue.py:192` |
| M8 invalidation | Programmatic (human/drift triggered) | Programmatic | VERIFIED `tests/test_planning.py:305,330,791` |
| M9 approval check | n/a | Programmatic | VERIFIED `tests/test_planning.py:725` |
| M11 budgets | Programmatic | Programmatic | VERIFIED `tests/test_planning.py:384,412` |
| M12–M14 doctrine | **Prompt only** | — | No test checks model behavior. Text presence only. |
| M15 dual sign-off | Programmatic presence check | **Does not encode a verdict**: reviewer FAIL is overridable | VERIFIED presence `tests/test_session_startup.py:837`; override DEMONSTRATED P5 |
| M16 forced review after human input | Programmatic | Model compliance for content | VERIFIED `tests/test_ui.py:1054`, `tests/test_runner.py:421` |
| M17 contract conflict | Model compliance | Programmatic (blocks activation) | VERIFIED `tests/test_session_startup.py:522,539`, `tests/test_ui.py:836` |
| M18 dropbox evaluation | Programmatic request | Programmatic disposition rule; not blind | VERIFIED `tests/test_dropbox.py:259,288,334,533` |
| M19 deadlock / conflict escalation | **Prompt only** | None automatic | SOURCE ONLY (no detector exists) |
| M20 disagreement ledger | Model/human compliance | None | SOURCE ONLY |
| M21 timeline | Programmatic (derived) | — | VERIFIED `tests/test_deliberation.py:65,118` |

**Long-horizon routing gap.** The session's opening task to the builder
carries no `human_direction` marker (`manager.py:719-727`; DEMONSTRATED P7).
The reviewer's first challenge therefore depends on the builder emitting
`SYNCHRI-TO`/`SYNCHRI-HANDOFF`. If it does not, the room idles: nobody is
scheduled and the conductor stops with `idle` (DEMONSTRATED P7b). In
Planning Mode, by contrast, submitting a revision programmatically wakes the
reviewer (M6).

---

## 4. Q4 — Can models agree without challenge, bypass requirements, or end early?

| Question | Answer | Evidence |
|---|---|---|
| Agree without challenging? | **Yes.** `approve` on revision 1 with no objections reaches PLAN-READY. | DEMONSTRATED P1; the repo test `tests/test_planning.py:308` does exactly this |
| Prompt encouragement to agree | "Do not manufacture objections to seem thorough — when remaining concerns are redundant or low-value, say so and approve." | `modes.py:238-240`; "stop at the minimum sufficiently defensible plan" `modes.py:313-314`, `planning.py:754-756` |
| Pass without content? | **Yes.** `SYNCHRI-PASS` or an empty reply becomes an attributed `pass` message. | DEMONSTRATED P8; `conductor.py:337-360`; `broker.py:947-1059` |
| Reviewer objects before the independent draft exists? | **Yes.** An objection at revision 0 is accepted; "reviewer waits" is prompt text (`modes.py:221-222`). The queue makes it unlikely in managed runs (only the planner is tasked at activation, `manager.py:685-698`). | DEMONSTRATED P2 |
| Planner dismisses a blocking objection? | **Yes, unilaterally.** Resolution needs no reviewer agreement and no new revision. The reviewer's later `approve` is the only check. | DEMONSTRATED P3 |
| Override a reviewer FAIL on a gate? | **Yes.** Builder `pass` after reviewer `fail` completes the session. | DEMONSTRATED P5 |
| Reduce review to a formality in long-horizon mode? | **Yes.** Completion checks gate fields, not review content or a review turn (`manager.py:1240-1245`, `1553-1568`). One reviewer turn containing any non-empty assessment string, even a FAIL, satisfies the reviewer sign-off. | DEMONSTRATED P5 |
| End discussion early? | Several routes: idle (no handoff) P7b; the autonomy limit (`scheduler.py:227-246`, VERIFIED `tests/test_runner.py:650`); planning budgets (VERIFIED `tests/test_planning.py:384`); `approve` closes the loop (P1); the circuit breaker after failures (`conductor.py:180-193`). | as cited |
| Bypass via direct writes? | Models with shell access can run the `synchri` CLI with any `--as <participant>`: the secret is loaded from the local session file without checking who is asking (`synchri/cli/session.py:145-148`). They can also read the human secret from the DB and speak as the human (P18), or self-report escalations (`synchri/cli/session_cli.py:580-584`, `raised_by` is self-declared). | P17, P18 DEMONSTRATED; CLI `--as` SOURCE ONLY |

---

## 5. Q5 — Independent initial responses

**Not preserved as a design property. No mode collects blind, independent
initial answers from more than one model.**

- **Planning Mode.** The planner's revision 1 is produced first and stored
  verbatim (`planning.py:513-518`; DEMONSTRATED P10; VERIFIED
  `tests/test_planning.py:494`, `:552`). Ordering is programmatic in the
  managed path: only the planner is tasked at activation (`manager.py:685-698`),
  and the reviewer is tasked only on submission (`planning.py:557-570`). The
  reviewer never produces an independent answer. Its first act is to review
  the planner's text, which is delivered in full in its prompt
  (`planning.py:929-932`). The reviewer is not technically barred from
  speaking first (P2).
- **Long-horizon mode.** The builder opens; the reviewer sees the builder's
  output through the shared transcript (`conductor.py:556-565`), the shared
  memory ledger (`conductor.py:550-554`), and the **shared worktree** (P19;
  `managed.py:949-952`).
- **Dropbox evaluation.** The second evaluator sees the first verdict
  (`dropbox.py:846-850`).
- **Pre-communication snapshot.** None exists. Prompts are not persisted
  (P9), so what each model had seen at a given point cannot be reconstructed
  exactly. It can be approximated from the transcript, the ledger file, and
  plan revisions, but the ledger is mutable and its history is not kept
  (§6.3).

---

## 6. Q6 — Persistence of messages, revisions, identifiers, parameters, termination

### 6.1 What is persisted

| Record | Where | Notes | Evidence |
|---|---|---|---|
| Room messages (post-parse body, sender, target, handoff, confidence, response status, metadata) | `messages` `schema.sql:120-147`; JSONL mirror `synchri/storage/transcript.py:23-29` | Only `task_id` is ever updated afterwards (`broker.py:1935`). The JSONL is best-effort and rebuildable. | VERIFIED `tests/test_provenance.py:19,125,139,150,176` |
| Event log (turns, handoffs, passes, interrupts, plan milestones, gate status, session end) | `events` `schema.sql:210-219` | Shares a monotonic per-room seq with messages | VERIFIED `tests/test_provenance.py:102,125` |
| Plan revisions (full body, author, summary) | `session_plan_revisions` `schema.sql:507-515` | Immutable, primary key `(session_id, revision)` | DEMONSTRATED P10; VERIFIED `tests/test_planning.py:412,779` |
| Objections with disposition, resolver, revisions | `session_plan_objections` `schema.sql:519-535` | Updated in place (status/disposition); no history of edits | VERIFIED `tests/test_planning.py:204` |
| Contract revisions and acknowledgments (raw reply ≤ 20,000 chars) | `schema.sql:280-301`; `manager.py:526-541` | A new revision **deletes** acknowledgments at ≥ that revision (`manager.py:477-480`) | VERIFIED `tests/test_session_startup.py:539` |
| Stream telemetry (tool, command, thinking excerpts) | `agent_stream_events` `schema.sql:354-366` | Clipped; **pruned to 1,000 per room** (`stream_events.py:503`, `dao.py:661-669`) | VERIFIED `tests/test_stream_ingest.py:211,343`; DEMONSTRATED P13 |
| Usage (tokens, cost, duration, `model`) | `agent_turn_usage` `schema.sql:373-390` | Only when the stream reports usage | VERIFIED `tests/test_stream_ingest.py:211,384` |
| Participant runtime and custom command | `session_participants` `schema.sql:267-276` | The resolved maintained command line is **not** stored per turn | SOURCE ONLY |
| Session end (status, reason, timestamp) | `sessions.ended_*`, `session.ended` event | `manager.py:2002-2020` | VERIFIED `tests/test_session_startup.py:837` (completion event) |

### 6.2 What is not persisted, or not reliably

| Gap | Detail | Evidence |
|---|---|---|
| **Prompts** | Built per turn (`conductor.py:517-631`), sent, and discarded. Only `prompt_chars` reaches the in-memory event callback (`conductor.py:235`). | DEMONSTRATED P9 |
| **Raw model output** | Control lines are stripped from the stored body (`agent_command.py:354-363,474`); the plan block is stripped (`:340-349`). Stream output is replaced by the parser's reconstructed final text (`agent_command.py:209-221`). Output over 2,000,000 chars loses its head (`:483-512`). | DEMONSTRATED P9 |
| **Model identifier** | No `model` on `messages`. `agent_turn_usage.model` comes only from Claude's stream `system/init` event (`stream_events.py:172-180,239`). The Codex parser never sets it (`stream_events.py:293-300,454`). Plain-stdout and custom commands record nothing. | DEMONSTRATED P11, P12 |
| **Inference parameters** | Never passed (`modes.py:489,548,580`) and never recorded. Provider CLI version is stored only per runtime at connection test time (`runtime_connections.version` `schema.sql:453-468`), not per session or turn. | SOURCE ONLY |
| **Gate assessment history** | `builder_assessment`/`reviewer_assessment` are overwritten. The `session.gate_updated` event carries status and actor, not text (`manager.py:962-966`). | DEMONSTRATED P6 |
| **Memory-ledger history** | `set` overwrites and `remove` deletes. The `memory.updated` event omits the value (`broker.py:1430-1436`). | SOURCE ONLY |
| **Conductor termination reasons** | `idle`, `turn_limit`, `agent_failed`, `cancelled`, and similar live in `ConductorReport` and in-memory `ManagedRun` state (`managed.py:380-392,629-642`), not in the event log. Budget exhaustion, the autonomy limit, and session end *are* logged. | DEMONSTRATED P7b (idle) |
| **Audit-write failures** | `SessionManager._log` swallows all exceptions (`manager.py:2054-2055`). `StreamRecorder` silently disables itself on any storage error (`stream_events.py:504-505,526-527`). | DEMONSTRATED P14 |
| **Deletion** | `delete_session` removes the session, room (cascade: messages, events, plans, objections), and usage rows (`manager.py:1656-1662`). | SOURCE ONLY |
| **Planning warnings** | Ignored or refused directives (e.g. a refused approval) are returned as warnings in the conductor report and are not persisted, except for `plan_criteria_refused` (`planning.py:605-609`). | SOURCE ONLY |

---

## 7. Q7 — Exposure of experimental conditions or reference answers

| Channel | What a model can see | Evidence |
|---|---|---|
| Contract roster | Every participant's name, role label, and **runtime tag** (e.g. `[codex]`, `[claude_code]`) | `contract.py:100-104,140-142`; DEMONSTRATED P15 |
| Contract header | Session ID, mode label, repo path, worktree path, timestamps | `contract.py:106-138` |
| Canonical spec / planning idea | Rendered verbatim to **every** participant: the spec in the contract (`contract.py:170-182`), the idea in the planning panel (`planning.py:916-923`) and the opening task (`manager.py:696-697`) | DEMONSTRATED P16 |
| Memory ledger | Seeded with the first line of the idea/spec as the room goal (`manager.py:302-305`); rendered to every prompt (`conductor.py:550-554`) | SOURCE ONLY |
| Environment | `SYNCHRI_HOME`, `SYNCHRI_ROOM`, `SYNCHRI_PARTICIPANT`, `SYNCHRI_CLI` exported to every managed agent (`managed.py:954-961`) | DEMONSTRATED P17 |
| Synchri database and files | Same OS user, so readable by any agent with shell or file-read tools. Contains every session, the transcript, plan revisions, objections, the human's secret and invite tokens (`manager.py:339-343`). Planning Claude keeps `Read/Glob/Grep`, and Codex's read-only sandbox still permits reads; whether they can reach `~/.synchri` depends on the provider sandbox and was **not tested**. | DEMONSTRATED P17, P18 (via a scripted process); provider-specific reachability **UNTESTED** |
| Other agents' identities | `synchri send --as <other>` loads the other's saved secret (`cli/session.py:145-148`). The broker refuses an agent posting without the floor, but impersonation of whoever holds the floor, or of the human (P18), is not prevented. | P18 DEMONSTRATED; `--as` impersonation SOURCE ONLY |
| Cross-session retrieval | `deliberation.search` with no session spans all sessions (`deliberation.py:560-566`), reachable through the CLI/API | SOURCE ONLY |

**Consequence for the study.** No field holds data hidden from models. Any
experimental condition, condition label, or reference answer stored in
Synchri state, or written into the idea/spec, should be assumed visible to
participating models. Blinding must be enforced outside Synchri: a separate
OS user or container per model, no reference material in `SYNCHRI_HOME`,
and neutral participant names.

---

## 8. Q8 — Test coverage and tests still required

### 8.1 Existing coverage, summarized

Well covered: the planning state machine (role checks, blockers, forks,
budgets, invalidation, approval) in `tests/test_planning.py`. Forced reviewer
routing in `tests/test_runner.py:421-471` and `tests/test_queue.py:192`.
Contract conflict blocking in `tests/test_session_startup.py:522-539`.
Transcript/event provenance in `tests/test_provenance.py`. Usage and stream
recording in `tests/test_stream_ingest.py`. Dropbox evaluation in
`tests/test_dropbox.py`.

**Not covered by any repo test:** that disagreement occurs or is required;
that a reviewer verdict is binding on gates; prompt and raw-output
persistence; model and parameter capture for every runtime; blinding and
exposure; independence of initial responses; logging under failure; any
behavior of real provider CLIs.

### 8.2 Additional tests a preregistered study would need

Several of these describe behavior that does not exist yet. They would fail
today, and are listed as acceptance tests for the required changes.

| # | Test | Property | Today |
|---|---|---|---|
| T1 | A configurable minimum-challenge rule: `approve` is refused unless the reviewer recorded ≥ k objections (or an explicit "no-objection attestation") on the current revision | Required disagreement | Fails (P1) |
| T2 | A reviewer `fail` on a gate cannot be cleared except by the reviewer (or a recorded human waiver) | Binding verdicts | Fails (P5) |
| T3 | Objection resolution requires reviewer concurrence | No unilateral dismissal | Fails (P3) |
| T4 | Reviewer actions before revision 1 are refused | Draft independence | Fails (P2) |
| T5 | An "independent round" phase: each model answers with no access to the others' outputs (separate cwd, no transcript, no ledger), answers persisted before any exchange, and a test asserts no prompt in that phase contains another model's text | Independent initial responses | Not implemented |
| T6 | Every invocation persists the exact prompt, raw stdout/stderr, return code, timing, and a hash | Full logging | Fails (P9) |
| T7 | Every invocation persists the resolved command line, CLI version, model ID, and inference parameters, for each runtime including Codex | Identifiers and parameters | Fails (P11) |
| T8 | Telemetry is not pruned (or is archived) in study mode | Complete logs | Fails (P13) |
| T9 | Audit-write failure aborts the turn (fail closed) in study mode | Log integrity | Fails (P14) |
| T10 | Every conductor stop reason is written to the event log | Termination events | Fails (P7b) |
| T11 | Gate assessments and ledger edits are versioned (append-only) | Revision history | Fails (P6) |
| T12 | Neutral roster: no runtime tags or architecture-revealing names in any prompt | Condition blinding | Fails (P15) |
| T13 | Reference-answer canary: a marker in a designated hidden field never appears in any prompt, file, or env reachable by an agent | Answer blinding | No hidden field exists (P16) |
| T14 | An agent process cannot read `SYNCHRI_HOME` or other participants' or the human's secrets (separate OS user or container) | Isolation and identity integrity | Fails (P17, P18) |
| T15 | Per-agent worktrees, if architecture independence matters | Workspace independence | Fails (P19) |
| T16 | Long-horizon: the reviewer is guaranteed a turn after the builder's opening and before any completion | Required review | Fails (P7) |
| T17 | Live-provider integration tests (real `claude`, `codex`) asserting the planning sandbox flags hold and the stream parser still extracts final text and model | Real-runtime behavior | Not present; every existing test uses scripted agents |
| T18 | Deadlock detection after N review cycles raises a programmatic escalation | Termination policy | Not implemented (prompt only) |

---

## 9. Traceability matrix

Status codes: **V** = verified by repo test, **D** = demonstrated by audit
probe, **S** = source inspection only, **P** = prompt only,
**✗** = demonstrated absent or violated.

| # | Claimed / required property | Intended by (doc / prompt) | Implementation evidence | Test evidence | Status |
|---|---|---|---|---|---|
| 1 | Reviewer can raise typed objections | `modes.py:231-234` | `agent_command.py:433-444`; `planning.py:426-433,474-492` | `tests/test_planning.py:204,471` | V |
| 2 | Only the reviewer raises objections; only the planner submits and resolves | `planning.py:375-377` | `planning.py:396-397,427-429,437-438,451-452` | `tests/test_planning.py:213-217,252-260` | V |
| 3 | Forks are restricted to a role | none ("either records a fork", `planning.py:376`) | `planning.py:420-424` (no check) | probe P4 | D (unrestricted) |
| 4 | Open blocking/fork objections prevent PLAN-READY | `planning.py:7-10`; `modes.py:306-310` | `planning.py:575-582` | `tests/test_planning.py:233-241,289` | V |
| 5 | Blocking objections cannot cross approval | `planning.py:1077-1080` | `planning.py:1081-1088` | `tests/test_planning.py:725` | V |
| 6 | Reviewer is woken on every submission | `modes.py:306-308` | `planning.py:557-570` | `tests/test_planning.py:229-230,494` | V |
| 7 | Reviewer turn forced after human-directed work | `modes.py:142-145,170-172` | `broker.py:833-844,975-983,1817-1840` | `tests/test_runner.py:421,447,471`; `tests/test_queue.py:192`; `tests/test_ui.py:1054` | V |
| 8 | Disagreement is required before agreement | none | none | probe P1; `tests/test_planning.py:308` | ✗ |
| 9 | Reviewer must contribute content | `modes.py:168` ("'Looks good to me' is not evidence") | `conductor.py:337-360` accepts pass/empty | probe P8 | ✗ (P only) |
| 10 | Adversarial stance of reviewer | `modes.py:146-172,216-240` | none | none (model behavior untested) | P |
| 11 | Encouragement to approve and stop | `modes.py:238-240,313-314`; `planning.py:754-756` | n/a | text only | P |
| 12 | Objection resolution needs reviewer concurrence | implied by "review closure" `modes.py:308-309` | `planning.py:395-418` (planner only) | probe P3 | ✗ |
| 13 | Gate PASS requires both builder and reviewer sign-off | `gates.py:3-5`; `modes.py:260-263` | `gates.py:71-87`; `manager.py:1009-1015` | `tests/test_session_startup.py:837,866` | V (presence only) |
| 14 | Reviewer FAIL is binding | `modes.py:139-140` ("Do not mark a gate PASS on your own assessment alone") | status last-writer-wins `manager.py:1001-1002,953-956` | probe P5 | ✗ |
| 15 | Contract conflict blocks activation | `contract.py:225-227` | `manager.py:619-630` | `tests/test_session_startup.py:522,539` | V |
| 16 | Deadlock / requirement-conflict escalation | `escalation.py:42-45` | none (never computed) | none | P |
| 17 | Disagreements recorded in ledger | `ledger.py:6,38` | no automatic writer | none | S (manual only) |
| 18 | Planner's first draft precedes any reviewer input | `modes.py:193-194,221-222,306-308` | queue: `manager.py:685-698`; no refusal of early objections `planning.py:426-433` | `tests/test_planning.py:208-217` (opening targets planner); probe P2 (early objection accepted) | D (partial) |
| 19 | Independent initial responses from each model | none | none | probe P19 (shared cwd); §5 | ✗ |
| 20 | Initial draft preserved verbatim | `schema.sql:505-506` | `planning.py:513-518` | `tests/test_planning.py:552`; probe P10 | V |
| 21 | All messages persisted | `schema.sql:1-8` | `broker.py:1842-1900`; `transcript.py:23-29` | `tests/test_provenance.py:125,139,176` | V (post-parse body) |
| 22 | Raw outputs persisted | none | stripped `agent_command.py:340-363` | probe P9 | ✗ |
| 23 | Prompts persisted | none | built and discarded `conductor.py:517-631` | probe P9 | ✗ |
| 24 | Model identifier persisted | `schema.sql:371-379` | Claude init only `stream_events.py:172-180`; Codex never | probes P11, P12; `tests/test_stream_ingest.py:211` | ✗ partial (Claude streaming only) |
| 25 | Inference parameters persisted | none | none | none | ✗ (S) |
| 26 | Telemetry retained in full | `schema.sql:349-353` ("these rows persist") | pruned `stream_events.py:503`, `dao.py:661-669` | `tests/test_stream_ingest.py:343`; probe P13 | ✗ |
| 27 | Revisions of assessments retained | `manager.py:985-986` (merge, not replace, for evidence) | assessments overwritten `manager.py:1009-1016` | probe P6 | ✗ |
| 28 | Termination events logged | partial | `session.ended` `manager.py:2019`; `autonomy.limit_reached` `broker.py:884-890`; `plan_budget_exhausted` `planning.py:722`; conductor stops not logged | `tests/test_planning.py:384`; `tests/test_runner.py:650`; probe P7b | D (partial) |
| 29 | Audit log never silently drops | none (comment says the opposite, `manager.py:2054`) | exceptions swallowed | probe P14 | ✗ |
| 30 | Runtime/architecture hidden from peers | none | roster tags `contract.py:100-104` | probe P15 | ✗ |
| 31 | Reference answers hidden from models | none | no hidden field; idea/spec rendered to all | probe P16 | ✗ |
| 32 | Agents isolated from Synchri state and secrets | `docs/security.md` (not re-verified here) | env export `managed.py:954-961`; secrets in DB `manager.py:339-343` | probes P17, P18 | ✗ |
| 33 | Each agent works in an isolated worktree | `CLAUDE.md` ("each in an isolated git worktree"; ambiguous) | one worktree per session `managed.py:949-952` | probe P19 | ✗ (per-session, not per-agent) |
| 34 | Planning agents are read-only | `planning.py:12-32`; `modes.py:506-522,563-566` | command flags; HEAD restore `planning.py:121-154` | `tests/test_planning.py:429,1161,1230` (flags and HEAD restore; scripted agents) | V (flags) / untested against real CLIs |
| 35 | Long-horizon reviewer always gets a turn after the builder's opening | `modes.py:247-250` | opening task lacks `human_direction` `manager.py:719-727` | probes P7, P7b | ✗ |
| 36 | Dropbox evaluations are independent | `dropbox.py:810` ("two" evaluations) | prior verdict shown `dropbox.py:846-850` | `tests/test_dropbox.py:334` (disposition rule only) | ✗ (S) |

---

## 10. Limitations of this audit

- **No real provider was run.** Every conclusion about model behavior is a
  conclusion about what the orchestrator permits or requires, not about what
  Claude or Codex actually do. Provider sandbox reachability of
  `~/.synchri` is untested.
- **The frontend was not audited in depth.** `synchri/ui/static/app.html`
  was not reviewed beyond grep hits. UI-initiated actions route through
  `synchri/ui/api.py`, which was sampled.
- **Retired modes are out of scope.** The legacy interactive and review-audit
  modes remain in `POLICIES` (`modes.py:317-354`) but are not offered for new
  sessions (`modes.py:387`). They are covered only where they share code.
- **Line numbers are for commit `1e6a347` only.**
- **Static findings are labelled.** Findings marked SOURCE ONLY are
  inferences from reading code and should be confirmed with a test before
  they are relied on.

## 11. Reproducing this audit

```bash
git checkout 1e6a3471f07c372fa4aaa1daee14cce87f0b8b50
pip install -e ".[dev]"
pytest -q                                                      # 749 passed
python -m pytest -q -p no:cacheprovider docs/audits/probes/test_audit_probes.py   # 20 passed
```

The probe file is self-describing: each probe's name and docstring state the
property it demonstrates, and the probe IDs (P1–P19, including P7b) match
the citations above.
