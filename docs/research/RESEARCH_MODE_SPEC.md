# Research Mode — formal specification (draft for review)

| | |
|---|---|
| **Status** | Revision 2 (amended). Amendments PC-01 to PC-13 from `docs/research/PROVIDER_FEASIBILITY.md` §8 are applied; each is traced in `docs/research/SPEC_CHANGELOG.md` and marked *[PC-nn]* where it lands. PC-14 is an undecided option (§12.1); PC-15 is a separate production security issue outside this specification. No experimental data has been collected. |
| **Baseline commit** | `1e6a3471f07c372fa4aaa1daee14cce87f0b8b50` (Synchri 0.5.3) |
| **Primary input** | `docs/audits/source-audit-1e6a347.md` (cited below as "Audit §n", audit probes as "P*n*", audit test proposals as "T*n*") |
| **Audience** | Study investigators, preregistration reviewers, implementers |
| **Date** | 2026-10-22 (revision 1); 2026-10-22 (revision 2) |
| **Evidence inputs** | Source audit (above); provider feasibility investigation `docs/research/PROVIDER_FEASIBILITY.md` (cited as "Feasibility §n") |

---

## 0. Scope, vocabulary, and conventions

### 0.1 What Research Mode is

Research Mode is a dedicated, CLI-only execution path for controlled
experiments. Each experiment sends a fixed set of factual items to one or
more language-model participants under preregistered conditions. It records
every input and output needed to reconstruct the run, and scores responses
against reference answers that participants can never reach.

It is **not** a new kind of coding session. It does not use rooms, the turn
queue, contracts, the memory ledger, worktrees, Planning Mode objections,
gates, or the dropbox (decision D1, §2).

### 0.2 Non-goals

- Changing any existing mode's behavior.
- A UI. The pilot is driven from the CLI; `app.html` is untouched.
- Hypothesis testing. The pilot validates the instrument; its data is
  excluded from confirmatory analysis (§11.3).
- Tool-using or open-book tasks. The pilot is closed-book factual answering.
- Forcing models to disagree or to agree (§7).

### 0.3 Requirement status vocabulary

Every requirement carries exactly one status:

| Status | Meaning |
|---|---|
| **SUPPORTED** | Existing code can be reused as-is for this requirement. Supporting evidence is cited, and its type is stated. |
| **MODIFY** | New code, or a change to existing code, is required. |
| **UNKNOWN** | Whether the requirement can be met depends on facts not yet established. Usually this is provider CLI behavior that only a live test can show. |

A requirement may be MODIFY *and* depend on an UNKNOWN. Where that happens,
both are stated.

### 0.4 Evidence vocabulary

This specification keeps four kinds of evidence separate and never treats
one as another *[PC-02]*:

| Code | Evidence type | What it can establish | What it cannot establish |
|---|---|---|---|
| **I** | Implementation evidence: source code, configuration, prompt text | What the system is built to do | That it does so at runtime; anything about model behavior |
| **A** | Automated test evidence: executable tests with scripted stand-in agents (as in `tests/test_runner.py:25` `write_agent`) | That orchestrator logic behaves as specified, deterministically | Anything about real provider CLIs or real models |
| **H** | Harness evidence *[PC-02]*: the real, pinned provider CLI binary run against a recording **mock** provider API, with scripted replies (including forged tool calls and injected failures) | Client-enforced properties: the exact request the CLI sends, which tools it declares, whether it executes a forged tool call, what it persists, how it retries | Anything about model behavior or server-side behavior |
| **L** | Live-provider evidence: tests that invoke real provider CLIs and models on the pilot host | That a specific CLI version or model behaves as required under the probes used, at the time of testing | That it always behaves so; behavior under unprobed inputs; behavior after provider-side changes |

"Verified" in this document means the stated evidence types have passed. A
property that needs **L** evidence is never called verified on **I**, **A**,
or **H** evidence alone. **H** evidence may verify client-enforced properties
(Feasibility §2); it never verifies model or server behavior *[PC-02]*.

### 0.5 Requirement ID prefixes

`PRO` protocol · `ISO` isolation and access control · `LOG` logging and
reconstruction · `CFG` configuration and prompt versioning · `BUD`
inference budget · `EVL` evaluation · `PLT` platform constraints.

---

## 1. Research objectives and required experimental capabilities

### 1.1 Objectives

The study asks whether communication between heterogeneous LLM
architectures changes three outcomes relative to controls:

| ID | Objective | Outcome the instrument must measure |
|---|---|---|
| **O1** | Factual reliability | Correctness of each participant's response against a hidden reference, at every round |
| **O2** | Hallucination | Rate of confident, non-abstaining incorrect answers. The exact operational definition is fixed in the preregistration; the instrument records answer, abstention, and confidence so that any reasonable definition can be computed (EVL-03). |
| **O3** | Output inconsistency | Disagreement of final answers across replicate runs of the same item and condition, and across paraphrases of the same item |

The instrument must make it possible to attribute effects to
**communication** (versus extra inference alone) and to **heterogeneity**
(versus same-architecture communication). The conditions in §1.2 exist for
that purpose.

### 1.2 Conditions the instrument must support

These are condition *types* the system must be able to run. The study
design (which ones, how many replicates, which models) is a preregistration
decision, not part of this specification.

| Condition type | Slots | Rounds after R0 | Visibility | Isolates |
|---|---|---|---|---|
| `solo` | 1 model | 0 | none | Single-pass baseline per model |
| `self_revision` | 1 model | K | own previous responses only | Extra inference without communication (compute-matched control for the two below) |
| `homogeneous` | 2 instances of the same model | K | peers' previous-round responses | Communication without heterogeneity |
| `heterogeneous` | 2 different architectures | K | peers' previous-round responses | Communication with heterogeneity |

A single trial runs one item under one condition with one replicate index.
K, replicates, and models are manifest parameters (CFG-01).

### 1.3 Required capabilities

| ID | Capability | Requirements |
|---|---|---|
| CAP-01 | Collect and seal independent initial responses before any communication | PRO-03 |
| CAP-02 | Fixed, symmetric, orchestrator-authored communication rounds | PRO-02, PRO-04, PRO-05, PRO-06 |
| CAP-03 | Run all four condition types from one frozen manifest with seeded, blocked randomization | CFG-01, CFG-07, BUD-06 |
| CAP-04 | Keep reference answers and condition labels unreachable by participants | ISO-04, ISO-05, ISO-07, ISO-09 |
| CAP-05 | Isolate participants from each other, from Synchri state, and from host configuration | ISO-01, ISO-02, ISO-03, ISO-06, ISO-08 |
| CAP-06 | Record a tamper-evident, append-only, complete event history | LOG-01 to LOG-07, LOG-09 |
| CAP-07 | Reconstruct and verify any run from its record alone | LOG-08 |
| CAP-08 | Pin and attest model, CLI version, and inference parameters | CFG-02 to CFG-05, CFG-08 |
| CAP-09 | Version every prompt template and render deterministically | CFG-06 |
| CAP-10 | Account for and bound inference cost per invocation, trial, condition, and run | BUD-01 to BUD-05 |
| CAP-11 | Score every round's response against references, separately from generation | EVL-01 to EVL-06 |

---

## 2. Architectural decisions

These decisions shape every later section. Each rests on audit findings.

**D1. A separate orchestration path, not a new room mode.** Research Mode
gets its own package (`synchri/research/`), its own per-run store, and its
own CLI subcommand. It reuses a small set of low-level components
(subprocess invocation, stream parsers, runtime resolution, version
probing, SQLite helpers). It does not use the broker, rooms, the queue,
contracts, the memory ledger, worktrees, Planning Mode, gates, or the
dropbox.

*Rationale.* Every one of those components puts content into prompts or
state that a controlled study must be able to exclude: the roster with
runtime tags (P15), the spec or idea sent verbatim to all (P16), the
transcript tail and memory ledger (Audit §5), a shared worktree (P19),
state paths in the environment (P17), and room secrets in the database
(P18). They also carry role doctrine that pushes models toward
disagreement (Audit M12) or toward agreement (`modes.py:238-240`).
Retrofitting blinding into the room path would change production behavior
and touch most of the platform. A narrow parallel path is smaller and
cannot regress existing modes (PLT-01).

*Alternative rejected:* a new `research` entry in `POLICIES`
(`modes.py:317-380`). It would inherit the contract, roster, ledger, and
queue routing that the audit shows are exposure channels.

**D2. Symmetric, synchronous rounds.** All participants in a trial are
peers with identical prompt templates. Round *k* prompts are built only
from responses sealed at the end of round *k−1*. No participant ever sees a
response from its own round. There is no builder or reviewer, and no
speaking order.

*Rationale.* The audit found that every existing mode is sequential, which
makes independent responses impossible (Audit §5). The dropbox shows the
second evaluator the first's verdict (Audit M18). Sequential designs
confound communication with anchoring and order effects.

**D3. Disagreement is measured, never required.** The orchestrator neither
requires objections nor rewards agreement. It computes disagreement
between sealed responses as derived data (PRO-12).

*Rationale.* The audit's proposed minimum-challenge rule (T1) would create
artificial disagreement, which contradicts the study's purpose. The
existing prompts both push toward disagreement (adversarial doctrine,
Audit M12) and away from it ("Do not manufacture objections",
`modes.py:238-240`). Both are rejected for Research Mode. See §7.

**D4. OS-level isolation from the pilot onward** *[PC-04]*. Every
invocation runs in its own container (or, where containers are unavailable,
as a distinct OS user), so the operating system denies access to run data,
references, other slots, and host files regardless of what a model or CLI
attempts. Provider-level tool disablement, a scrubbed environment, and an
empty working directory remain required, as **defense in depth inside** that
boundary. The former "Tier 1" (client-side controls only) is retired as a
pilot option; "Tier 2" in the rest of this document means this decision.

*Rationale.* Same-user file protection cannot stop an agent that has file or
shell tools (P17, P18). A vendor restriction can be weaker than its name:
Codex's `--sandbox read-only` read a protected file outside the working
directory and sent it to the API (Feasibility §4.1, finding 1, O-H).
Container and OS-user isolation confined both CLIs identically, with every
CLI-level protection switched off (Feasibility §4.6, O-OS), at the cost of a
stock Docker daemon and one small image. The open question this decision
previously left to reviewers (whether Tier 1 suffices for a pilot) is
therefore resolved in favor of Tier 2.

**D5. One store per run, outside `SYNCHRI_HOME`.** Each run writes to its
own directory containing an append-only SQLite event log and a
content-addressed blob store. References live in a separate location that
the generation path never learns (ISO-04).

*Rationale.* Mixing research data into `synchri.db` exposes it to every
other session and to cross-session search (`deliberation.py:560-566`).
`delete_session` cascades deletes through room data (`manager.py:1656-1662`).
A per-run store can be frozen, hashed, and archived as a unit.

**D6. A trusted recording relay between every CLI and its provider**
*[PC-01, PC-06]*. The relay is the participant's only network channel. It
holds the dedicated provider credentials; it forwards each request to the
provider; and it appends the exact request bytes, response bytes, status,
timing, served model, and usage to the run store before and after
forwarding. It enforces the run's request invariants (model, tool
definitions, injected-context markers, request count) and refuses anything
else, fail-closed. The CLI receives only a per-invocation relay token in
place of a provider credential.

*Rationale.* Both CLIs add content their output never shows (identity lines,
environment context, skill lists, tool definitions), both retry internally,
Codex reports no served model at all, and Claude's `init.model` reports only
the requested one (Feasibility §4.2, §4.7, O-H). Both CLIs also expose their
provider credential to tool subprocesses (Feasibility §4.5, O-H). Capturing
at the network boundary, with the real credential held only there, is the
only observed way to meet LOG-02, CFG-02, and ISO-06 together.

---

## 3. Mapping from audit findings to requirements

"Disposition" states what Research Mode does about each finding. **N/A by
design** means the component involved is not used by Research Mode (D1).
The mapping then names the requirement that keeps the underlying problem
from reappearing.

### 3.1 Disagreement and protocol findings

| # | Audit finding | Evidence | Disposition | Requirement(s) |
|---|---|---|---|---|
| F01 | Disagreement is never required; approve with zero objections reaches PLAN-READY | P1; `tests/test_planning.py:308` | Kept as a property: Research Mode also does not require disagreement. It measures it instead. The T1 minimum-challenge rule is **rejected**. | PRO-12, §7 |
| F02 | Plan-reviewer prompt discourages objections ("Do not manufacture objections") | `modes.py:238-240` | Prompt not used. Neutral templates under review. | PRO-02, CFG-06, §7.3 |
| F03 | Adversarial-reviewer doctrine instructs falsification | Audit M12, `modes.py:146-172` | Role doctrine not used. Symmetric peers. | PRO-02 |
| F04 | A participant may pass or reply empty | P8; `conductor.py:337-360` | Models get no pass mechanism; every slot must respond every round. Empty responses are recorded as `missing`. | PRO-06, PRO-08 |
| F05 | Reviewer may act before the independent draft exists | P2 | Replaced by a sealed independent round. | PRO-03 |
| F06 | Planner resolves blocking objections unilaterally | P3 | N/A by design (no objection loop). | PRO-01 |
| F07 | Fork has no role check | P4 | N/A by design. | PRO-01 |
| F08 | Gate FAIL overridden by later PASS (last-writer-wins) | P5 | N/A by design (no gates). All state is append-only events; nothing is overwritten. | LOG-01 |
| F09 | Long-horizon reviewer turn depends on the builder handing off | P7 | N/A by design. The orchestrator schedules every invocation. | PRO-04 |
| F10 | Conductor `idle` stop not logged | P7b | Every termination path emits an event. | LOG-06, PRO-07 |
| F11 | Deadlock escalation is prompt-only; no detector | Audit M19 | Fixed, preregistered round count replaces open-ended discussion. | PRO-07 |
| F12 | Dropbox second evaluator sees first verdict | Audit M18 | Synchronous rounds; no intra-round visibility. | PRO-04 |
| F13 | No mode collects independent initial responses | Audit §5 | Independent round R0, sealed before communication. | PRO-03 |
| F14 | Forced-reviewer routing (`human_direction`) is programmatic and tested | Audit M7; `tests/test_runner.py:421,447` | Pattern noted. Not reused; the research scheduler is simpler and fully programmatic. | PRO-04 |
| F15 | Planning budgets are programmatic and tested | Audit M11; `tests/test_planning.py:384,412` | Pattern reused (explicit budgets, logged exhaustion). | BUD-02, BUD-03 |

### 3.2 Logging and reproducibility findings

| # | Audit finding | Evidence | Disposition | Requirement(s) |
|---|---|---|---|---|
| F16 | Prompts are never persisted | P9; `conductor.py:517-631` | Every prompt is stored as a blob, byte-exact. | LOG-02, LOG-05 |
| F17 | Raw output is stripped of control lines and replaced by parsed stream text | P9; `agent_command.py:209-221,340-363` | Raw stdout, stderr, and the raw stream are stored. Parsed text is stored separately. | LOG-02 |
| F18 | Captured stdout loses its head beyond 2,000,000 chars | `agent_command.py:155-156,483-512` | A truncated capture is flagged, and the invocation is marked invalid. | LOG-02 |
| F19 | Model identifier only from the Claude stream `init` event; never for Codex | P11, P12 | Model requested explicitly and attested where possible; otherwise recorded as unattested. | CFG-02, LOG-04 |
| F20 | Inference parameters neither set nor recorded | Audit §6.2 | Everything passed is recorded; what cannot be controlled is recorded as `provider_default`. | CFG-04, CFG-05 |
| F21 | CLI version recorded only at connection test, per runtime | `schema.sql:453-468` | Exact version pinned in the manifest and checked at preflight and per trial. | CFG-03 |
| F22 | Stream telemetry pruned to 1,000 events per room and clipped | P13; `stream_events.py:28-32` | The raw stream is stored unclipped as a blob. The pruned table is not used. | LOG-02 |
| F23 | Audit writes swallow exceptions | P14; `manager.py:2054-2055` | Fail closed: a write failure aborts the invocation. | LOG-03 |
| F24 | Gate assessments overwritten | P6 | N/A by design; append-only log. | LOG-01 |
| F25 | Memory-ledger history not kept | Audit §6.2 | N/A by design (no ledger). | PRO-05 |
| F26 | Contract acknowledgments deleted on re-issue | `manager.py:477-480` | N/A by design. | LOG-01 |
| F27 | `delete_session` cascades deletes through all room data | `manager.py:1656-1662` | No delete command for research runs. Archive and export only. | LOG-09 |
| F28 | Refused or ignored directives surface only as non-persisted warnings | Audit §6.2 | Parse outcomes, including failures, are events. | PRO-08 |
| F29 | Message sequence numbers are monotonic and tested | `tests/test_provenance.py:125` | Pattern reused: per-run monotonic `seq`. | LOG-01 |

### 3.3 Exposure and isolation findings

| # | Audit finding | Evidence | Disposition | Requirement(s) |
|---|---|---|---|---|
| F30 | Contract roster discloses each participant's runtime | P15 | No roster. Peers are anonymized letters. | PRO-09, ISO-09 |
| F31 | Idea and spec rendered verbatim to all participants | P16 | Item text and references are split into separate files. Only item text is renderable. | ISO-04 |
| F32 | Managed agents receive `SYNCHRI_HOME` and related variables | P17; `managed.py:954-961` | Environment allowlist. No Synchri variables. | ISO-01 |
| F33 | Agent processes can read the Synchri database | P17 | Run store outside agent reach: by the OS (container or OS user, D4), with tool disablement as defense in depth *[PC-04]*. | ISO-03, ISO-05 |
| F34 | Human secret in the DB; any reader can post as the human; CLI `--as` loads any participant's secret | P18; `cli/session.py:145-148` | No broker and no credentials in the research path. | ISO-06 |
| F35 | Cross-session search spans all sessions | `deliberation.py:560-566` | The research store is separate and not indexed. | ISO-05, D5 |
| F36 | Contract header carries session ID, paths, timestamps | `contract.py:106-138` | No volatile or identifying content in prompts. | CFG-06 |
| F37 | All participants share one worktree as their working directory | P19; `managed.py:949-952` | A fresh empty working directory per invocation. | ISO-02 |
| F38 | Provider resume relaunches a remembered session | `managed.py:867-900` | Never resume. A fresh provider session per invocation. | PRO-11 |
| F39 | Planning read-only enforcement tested only with scripted agents | Audit row 34 | Tool disablement requires live tests. | ISO-03, LT-03 |
| F40 | No real provider CLI was ever invoked by any test | Audit §0, §10 | A live-provider test suite is required. | PLT-02, §9.3 |
| F41 | Codex, Copilot, and Gemini absent from the audit host | Audit §0 | Preflight checks presence and version; pilot runtimes are fixed in the manifest. | CFG-03, UNKNOWN-01 |

### 3.4 Audit test proposals (T1–T18)

| Audit test | Disposition in Research Mode |
|---|---|
| T1 minimum-challenge rule | **Rejected** (D3). Replaced by measuring disagreement (PRO-12). |
| T2 binding reviewer FAIL | N/A (no gates). |
| T3 objection concurrence | N/A (no objection loop). |
| T4 refuse reviewer before draft | Superseded by PRO-03 (VT-03, VT-04). |
| T5 independent round | PRO-03 (VT-03 to VT-05). |
| T6 persist prompt and raw output | LOG-02 (VT-10, VT-11). |
| T7 model, version, and parameters per invocation | LOG-04, CFG-02 to CFG-05 (VT-13, LT-01, LT-02). |
| T8 no telemetry pruning | LOG-02 (VT-11). |
| T9 fail closed | LOG-03 (VT-12). |
| T10 all stop reasons logged | LOG-06 (VT-15). |
| T11 versioned assessments | LOG-01 (VT-09). |
| T12 neutral roster | PRO-09, ISO-09 (VT-07, VT-20). |
| T13 reference canary | ISO-07 (VT-19, LT-12). |
| T14 no access to state or secrets | ISO-05, ISO-06 (VT-18, LT-10). |
| T15 per-agent worktrees | ISO-02 (VT-17). |
| T16 guaranteed review turn | N/A; superseded by PRO-04. |
| T17 live-provider tests | §9.3 (LT-01 to LT-12). |
| T18 deadlock detection | Superseded by fixed rounds (PRO-07). |

---

## 4. State machines

Three nested machines are needed. Each transition emits exactly one event
(§6), written **before** the transition's effects are visible to any
participant. A failed event write aborts the transition (LOG-03).

### 4.1 Run

```
 DRAFT ──freeze──▶ FROZEN ──preflight ok──▶ READY ──start──▶ RUNNING
                     │                        │                │
                     │ preflight fail         │                ├─all trials terminal──▶ GENERATION_COMPLETE
                     ▼                        ▼                │                               │
               PREFLIGHT_FAILED          (re-preflight         ├─budget/isolation/operator──▶ HALTED
                                          allowed; logged)     │                               │
                                                               ▼                               ▼
                                                         (per-trial machine)           evaluate (EVL-01)
                                                                                              │
                                                                 EVALUATED ◀──────────────────┘
                                                                     │
                                                                     └─export──▶ ARCHIVED
```

| From | Event | To | Guard |
|---|---|---|---|
| DRAFT | `run.manifest_frozen` | FROZEN | Manifest validates. Item file and reference file hashes computed. Working tree clean, or a dirty tree explicitly allowed and recorded (CFG-01). |
| FROZEN | `run.preflight_passed` | READY | Every check in CFG-02, CFG-03, ISO-03, and ISO-05 passes for every slot. |
| FROZEN | `run.preflight_failed` | PREFLIGHT_FAILED | Any check fails. Re-running preflight emits a new event; no state is overwritten. |
| READY | `run.schedule_materialized` → `run.started` | RUNNING | The full trial schedule is generated from the seed and logged before the first invocation (CFG-07). |
| RUNNING | `run.generation_complete` | GENERATION_COMPLETE | Every scheduled trial is terminal. |
| RUNNING | `run.halted` | HALTED | Run budget exhausted at a block boundary (BUD-03), an isolation violation (ISO-07), or an operator halt (LOG-07). |
| GENERATION_COMPLETE or HALTED | `evaluation.completed` | EVALUATED | The evaluator scored every sealed trial (EVL-01). |
| EVALUATED | `run.archived` | ARCHIVED | Export written. Chain head hash recorded (LOG-09). |

### 4.2 Trial (one item × one condition × one replicate)

```
SCHEDULED ─▶ R0_GENERATING ─▶ R0_SEALED ─▶ R1_GENERATING ─▶ R1_SEALED ─▶ … ─▶ RK_SEALED ─▶ FINAL_SEALED
     │              │                            │                                              │
     │              └──────────── infra failure after retries / budget / isolation ─────────────┤
     ▼                                                                                          ▼
  SKIPPED (run halted                                                         ABORTED_INFRA | ABORTED_BUDGET |
   before start)                                                              ABORTED_ISOLATION | ABORTED_OPERATOR
```

| State | Entry action | Exit condition |
|---|---|---|
| SCHEDULED | — | The scheduler selects the trial. Emits `trial.started`. |
| R0_GENERATING | For each slot (order from the schedule), render the **R0 template** with the item text only, then invoke (§4.3). No other trial's or slot's content is reachable. | Every slot has a terminal invocation outcome. |
| R0_SEALED | Emit `round.sealed` (round 0) with the hash of each slot's response record. **This is the independence commit point:** no round-1 prompt can be rendered before this event exists (VT-04). | Immediate for `solo` (K = 0) → FINAL_SEALED. Otherwise → R1_GENERATING. |
| R*k*_GENERATING (1 ≤ *k* ≤ K) | For each slot, render the round template from (a) the item text, (b) the slot's own sealed responses, and (c) the sealed round *k−1* responses of the peers that the condition's visibility rule allows. Inputs are referenced only by blob hash (LOG-05). | Every slot has a terminal invocation outcome. |
| R*k*_SEALED | Emit `round.sealed` (round *k*). | *k* < K → R*k+1*_GENERATING. *k* = K → FINAL_SEALED. |
| FINAL_SEALED | Emit `trial.final_sealed`, listing each slot's final response hash (the round-K response). | Terminal. Eligible for evaluation. |
| ABORTED_* | Emit `trial.aborted` with a reason code and the round reached. Responses sealed before the abort remain in the record. | Terminal. Eligible for attrition analysis, not for outcome scoring unless the preregistration says otherwise. |

Rules:

- **Fixed length.** A trial always runs exactly K rounds after R0 unless it
  is aborted. There is no early stop on convergence in the pilot
  (UNKNOWN-12 records the open question). Participants cannot end a trial
  (PRO-06, PRO-07).
- **Missing responses.** A slot whose invocation succeeds but yields no
  parseable answer block continues to the next round. Its response is
  recorded as `invalid_format` or `missing` (PRO-08), and peers see the raw
  text exactly as the model produced it.
- **Infra failures.** A slot whose invocation fails after the allowed
  retries (BUD-05) aborts the whole trial as `ABORTED_INFRA`. Partial
  rounds are not repaired.

### 4.3 Invocation (one participant call)

```
PREPARED ─▶ LAUNCHED ─▶ EXITED ─▶ CAPTURED ─▶ PARSED
                │           │
                │           └─ nonzero / timeout / truncated ─▶ FAILED ──retry allowed?──▶ PREPARED (attempt+1)
                └─ cannot spawn ─▶ FAILED                                       └─no──▶ (trial ABORTED_INFRA)
```

| Step | Required effect | Event |
|---|---|---|
| PREPARED | Prompt rendered; prompt blob stored; per-invocation working directory created empty; environment built from the allowlist; argv resolved. | `invocation.prepared` |
| LAUNCHED | Process spawned without a shell, as its own process group (reused `AgentCommand.invoke`, `agent_command.py:100-237`). | `invocation.launched` |
| EXITED / FAILED | Return code, timeout or cancel flags, wall and monotonic durations. | `invocation.exited` or `invocation.failed` |
| CAPTURED | Raw stdout, stderr, and raw stream stored as blobs. Truncation flag set. Usage and reported model extracted. Canary scan of all captured bytes (ISO-07). | `invocation.captured` (and `isolation.violation_detected` on a hit) |
| PARSED | Answer block parsed into a response record, or a parse failure recorded. | `response.recorded` |

---

## 5. Isolation and access-control boundaries

### 5.1 Principals and resources

**Principals:**

- **Orchestrator.** The `synchri research run` process. Trusted. Must never
  open the reference store.
- **Participant process.** One provider CLI invocation per slot, round, and
  attempt. Untrusted.
- **Evaluator.** The `synchri research evaluate` process. Trusted. The only
  principal allowed to read references.
- **Operator.** A human running the CLI. Every action is logged (LOG-07).

**Resources:**

- **Run store.** Per-run event DB plus blobs.
- **Reference store.** Item references; a separate file outside the run
  directory.
- **Item store.** Item text, hashed into the manifest.
- **Manifest.** The frozen run configuration.
- **Provider home.** Per-slot provider auth and config.
- **Scratch directory.** The per-invocation working directory.
- **Synchri state.** `SYNCHRI_HOME`, which Research Mode never uses.
- **Host.** The rest of the filesystem, network, and environment.

### 5.2 Access matrix

R = read, W = write (append), — = no access (must be enforced),
∅ = not applicable.

| Resource ↓ / Principal → | Orchestrator | Participant (Tier 1) | Participant (Tier 2) | Evaluator | Operator |
|---|---|---|---|---|---|
| Run store events and blobs | R/W (append only) | — *(tool disablement; L-evidence)* | — *(OS-enforced)* | R, W (evaluation events only) | R (export) |
| Reference store | **—** (never opened during generation) | — | — (OS) | R | R/W before freeze; only its hash enters the manifest |
| Item store | R (question text only) | Sees only the rendered prompt | Same | R | R/W before freeze |
| Manifest | R | Sees nothing beyond the rendered prompt | Same | R | W before freeze only |
| Other slots' responses | R (sealed) | Only what PRO-05 renders into its prompt | Same | R | R |
| Provider home (own slot) | W at setup | R (provider CLI needs auth) | R | ∅ | W at setup |
| Provider home (other slot) | W at setup | — | — (OS) | ∅ | W at setup |
| Scratch directory (own invocation) | W (create empty) | R/W | R/W | ∅ | R |
| `SYNCHRI_HOME` | not used | — | — (OS) | not used | ∅ |
| Orchestrator environment | R | — (allowlist, ISO-01) | — | ∅ | ∅ |
| Network | Provider APIs (via CLI) | Provider API only; no web tools *(UNKNOWN-06)* | Same, plus optional egress rules | none needed | ∅ |

*[PC-04]* The "Participant (Tier 1)" column now describes the client-side,
defense-in-depth controls only; the pilot requires the Tier 2 column. *[PC-01]*
Under D6 the participant's "Network" cell is narrowed further: the only
reachable endpoint is the recording relay, which alone reaches provider APIs.

### 5.3 Requirements

| ID | Requirement | Status | Basis and evidence today |
|---|---|---|---|
| **ISO-01** | Participant environment is built from an explicit allowlist (`PATH` pointing at the pinned CLI's directory, `HOME` or `CODEX_HOME`/`CLAUDE_CONFIG_DIR` set to the slot's provider home, locale, `TMPDIR` inside the scratch directory). No `SYNCHRI_*` variables. Variable names and value hashes are logged. *[PC-06]* The only credential-shaped value in the environment is the per-invocation relay token (ISO-06); no provider credential, host credential, or session token may appear, and the launcher refuses an environment that contains one. | MODIFY | *[PC-06]* Feasibility §4.5: Synchri's launcher passed 171 host variables, including cloud and session tokens; both CLIs pass their provider key to tool subprocesses (O-H). Today all managed agents receive `SYNCHRI_HOME/ROOM/PARTICIPANT/CLI` (`managed.py:954-961`; P17). Reuse: `AgentCommand.env` and `_merged_env` (`agent_command.py:477-480`) merge with `os.environ`. Research needs **replace** semantics, not merge. Exact provider config variables: UNKNOWN-04. |
| **ISO-02** | A fresh, empty scratch directory per invocation, created with mode `0700`. Never shared across slots or rounds. Kept after the run for audit (it should stay empty under closed-book conditions; non-empty is flagged). *[PC-10]* Inside the container the working directory is the **neutral fixed path** `/work` (a fresh tmpfs per invocation), so any working-directory path a CLI injects is byte-identical across invocations; uniqueness comes from the container, never from the path. | MODIFY | *[PC-10]* Feasibility §4.4: both CLIs inject the working-directory path into model-visible context unless suppressed (O-H). Today one worktree per session is every participant's working directory (P19). Reuse: `AgentCommand.cwd`. |
| **ISO-03** | Closed-book: every provider tool (file, shell, web, MCP, sub-agents) is disabled through the provider's own flags, and stays disabled. Preflight and live tests must show no tool events in the raw stream under adversarial probing (LT-03, LT-11). *[PC-05]* Pinned flag sets: **Claude** `--tools "" --disallowedTools "*" --strict-mcp-config` (prompt on stdin, because `--tools` is variadic); **Codex** feature disables `shell_tool`, `unified_exec`, `multi_agent`, `view_image`, `goals`, `apps`, `plugins`, `hooks`, `image_generation`, `browser_use`, `computer_use`, `tool_suggest`, `skill_search`, `sleep_tool`, plus `web_search="disabled"`. **Codex `--sandbox read-only` is never relied on for confinement** (it reads the whole filesystem). Codex's `request_user_input` cannot be removed and is inert in `exec` mode; it is recorded as a known residual tool definition and treated as a treatment constant (CFG-06), any call to it is logged as an isolation event, and the relay enforces a per-runtime tool-definition allowlist (Claude: none; Codex: `request_user_input` only). | MODIFY | Feasibility §4.1 (O-H): with these flags Claude's request declares no tools and Codex's declares only `request_user_input`; both CLIs refused forged calls to disabled tools; Codex's `read-only` sandbox leaked a canary. Copilot: `--available-tools=` (`modes.py:608-615`); not a pilot runtime. |
| **ISO-04** | Item text and reference answers live in **separate files**. The manifest stores only the reference file's SHA-256, never its path. Generation code cannot import the reference loader (enforced by a module-boundary test, VT-18). References are read only by the evaluator, and only for trials in FINAL_SEALED or ABORTED_* states. | MODIFY | Today the idea and spec go to every model (P16), and no hidden field exists. |
| **ISO-05** | The run store and reference store are unreachable by participant processes. *[PC-04]* Each invocation runs in a fresh container: digest-pinned image, read-only root filesystem, non-root UID, all capabilities dropped, `no-new-privileges`, process and memory limits, fresh tmpfs `/work` and home, **no network except the relay channel** (D6), and **no mount except** the pinned CLI binary (read-only), the launcher script (read-only), and the relay channel. A distinct OS user with owner-only stores is the permitted fallback where containers are unavailable. Checked mechanically without any model (LT-10a). (The former Tier 1 option is retired, D4.) | MODIFY + **UNKNOWN** (egress to real providers, macOS: UNKNOWN-09) | Today the same OS user can read the whole database (P17). |
| **ISO-06** | The research path holds no participant or human credentials. There is no broker, room, or invite. Participants cannot post anything except their own stdout. *[PC-06]* Provider credentials exist **only inside the recording relay**: dedicated per study, spend-limited, and revocable, loaded from an owner-only file the relay is explicitly given, never from a provider CLI's own configuration directory or any session credential. The CLI holds a per-invocation relay token that is useless outside the relay and expires with the invocation; the relay strips any client-supplied authorization and injects the real credential upstream. | MODIFY (by construction, D1, D6) | Today the human secret sits in the DB and the CLI `--as` loads any participant's secret (P18; `cli/session.py:145-148`). |
| **ISO-07** | Canary tokens (random, ≥ 128 bits, distinct per location) are planted in the reference store, the run store, the operator's environment, and the slot's provider home outside the auth files. Every captured stdout, stderr, and stream blob is scanned for every canary. A hit emits `isolation.violation_detected`, aborts the trial as `ABORTED_ISOLATION`, and halts the run. | MODIFY | No equivalent today. Canaries detect leaks that surface in output; they cannot detect silent reads. That limit is why Tier 2 exists. |
| **ISO-08** | No web search or fetch tools. Network egress beyond the provider API is denied where the host allows it (Tier 2). | **UNKNOWN** (UNKNOWN-06) | Depends on provider flags; LT-11. |
| **ISO-09** | No condition label, hypothesis text, runtime name, model name, vendor name, slot-to-model mapping, run ID, or replicate index appears in any rendered prompt. Enforced by a render-time denylist check that refuses to emit the prompt (VT-20). | MODIFY | Today the roster tags runtimes (P15) and the contract header has session IDs (`contract.py:106-138`). |

---

## 6. Append-only event schema

### 6.1 Storage

Each run directory holds `run.db` (SQLite, WAL mode, reusing `db.connect`
from `synchri/storage/db.py:63-75`) with exactly two tables. The schema
lives in `synchri/research/schema.sql`, separate from the production
`schema.sql`.

```sql
CREATE TABLE research_events (
    run_id        TEXT    NOT NULL,
    seq           INTEGER NOT NULL,           -- 1, 2, 3 … gap-free per run
    event_id      TEXT    NOT NULL UNIQUE,
    event_type    TEXT    NOT NULL,
    schema_version INTEGER NOT NULL,
    created_at    TEXT    NOT NULL,           -- UTC ms, ids.utc_now() format
    monotonic_ns  INTEGER NOT NULL,           -- time.monotonic_ns() of the writer
    actor         TEXT    NOT NULL,           -- orchestrator | evaluator | operator:<name>
    trial_id      TEXT,
    slot          TEXT,                       -- internal slot id (never shown to models)
    round         INTEGER,
    invocation_id TEXT,
    attempt       INTEGER,
    payload       TEXT    NOT NULL,           -- canonical JSON: sorted keys, no insignificant whitespace, UTF-8
    prev_hash     TEXT    NOT NULL,           -- hash of seq-1 ('0'*64 for seq 1)
    hash          TEXT    NOT NULL,           -- sha256 over prev_hash and the canonical serialization of every other column
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE research_blobs (
    sha256      TEXT    PRIMARY KEY,
    size        INTEGER NOT NULL,
    media_type  TEXT    NOT NULL,             -- text/plain; charset=utf-8 | application/x-ndjson | application/octet-stream
    content     BLOB    NOT NULL,             -- exact bytes; never clipped, never re-encoded
    created_at  TEXT    NOT NULL
);

-- Append-only, enforced by the database for every writer that uses it.
CREATE TRIGGER research_events_no_update BEFORE UPDATE ON research_events
    BEGIN SELECT RAISE(ABORT, 'research_events is append-only'); END;
CREATE TRIGGER research_events_no_delete BEFORE DELETE ON research_events
    BEGIN SELECT RAISE(ABORT, 'research_events is append-only'); END;
CREATE TRIGGER research_blobs_no_update BEFORE UPDATE ON research_blobs
    BEGIN SELECT RAISE(ABORT, 'research_blobs is append-only'); END;
CREATE TRIGGER research_blobs_no_delete BEFORE DELETE ON research_blobs
    BEGIN SELECT RAISE(ABORT, 'research_blobs is append-only'); END;
```

**Tamper evidence, not tamper proofing.** Anyone with write access to the
file can drop triggers. Integrity therefore rests on three things:

1. The hash chain, checked by `synchri research verify`.
2. External anchoring of the chain head hash at freeze, at
   generation-complete, and at archive (for example, committed to the
   preregistration repository).
3. Under Tier 2, file ownership that participants cannot write to.

### 6.2 Event catalog

Every payload field listed is **required**; a write with a missing field is
refused (VT-09). "Blob" means a SHA-256 reference into `research_blobs`.

| Event type | Emitted when | Required payload |
|---|---|---|
| `run.manifest_frozen` | Freeze | `manifest` (blob), `manifest_sha256`, `synchri_version`, `git_commit`, `git_dirty` (bool) and `git_diff` (blob, if dirty), `python_version`, `sqlite_version`, `platform`, `item_file_sha256`, `item_count`, `reference_file_sha256`, `prompt_templates` (id → version → sha256), `preregistration_ref` (string or null), `isolation_tier` |
| `run.preflight_passed` / `run.preflight_failed` | Preflight | Per slot: `runtime`, `executable_path`, `executable_sha256`, `cli_version_raw`, `cli_version`, `cli_version_expected`, `argv_template`, `model_requested`, `model_reported` (or null), `model_attested` (bool), `tool_disablement_check` (result plus stream blob), `provider_home_inventory` (file names and hashes, excluding secret contents), `canary_plant_locations` (names only); `failures` (list) |
| `run.schedule_materialized` | Before the first trial | `seed`, `schedule` (blob: ordered list of `trial_id`, `item_id`, `condition_id`, `replicate`, `block`, `slot_order`, `slot_letters`, `peer_order`), `schedule_sha256` |
| `run.started` / `run.generation_complete` / `run.halted` | Transitions | `reason` (for halted: `budget`, `isolation`, `operator`, `infra`), `trials_terminal`, `trials_remaining`, `budget_snapshot` |
| `trial.started` | Trial begins | `item_id`, `condition_id`, `replicate`, `block`, `slots` (internal slot ids), `letters` (slot → letter) |
| `invocation.prepared` | Before spawn | `template_id`, `template_version`, `template_sha256`, `prompt` (blob), `prompt_sha256`, `prompt_chars`, `inputs` (ordered list of response-record hashes rendered into the prompt, with their letters), `argv` (fully resolved, with the prompt replaced by `{prompt}` or `<stdin>`), `cwd`, `env_names`, `env_value_sha256` (name → hash), `timeout_s`, `budget_before` |
| `invocation.launched` | After spawn | `pid`, `started_monotonic_ns` |
| `invocation.exited` / `invocation.failed` | Process ends | `returncode`, `timed_out`, `cancelled`, `wall_s`, `monotonic_elapsed_ns`, `failure_kind` (from `recovery.classify_failure`, `recovery.py:67`) or null |
| `invocation.captured` | Output stored | `stdout` (blob), `stderr` (blob), `stream` (blob or null), `stdout_truncated` (bool), `final_text` (blob or null), `final_text_source` (`stream_result`, `stream_text`, or `raw_stdout`), `model_reported`, `provider_session_id`, `usage` (`input_tokens`, `output_tokens`, `cached_input_tokens`, `cost_usd`; each nullable), `canary_scan` (`clean` or a list of hit locations), `tool_events_seen` (count by kind, from the raw stream) |
| `response.recorded` | After parse | `response_record` (blob: canonical JSON of the fields below), `response_sha256`, `format_valid`, `parse_error` or null, `answer`, `abstain`, `confidence` or null, `rationale` (blob), `source_invocation_id` |
| `round.sealed` | All slots terminal for the round | `round`, `responses` (slot → `response_sha256` or `missing`), `seal_sha256` (hash over the sorted mapping) |
| `trial.final_sealed` | After round K | `final_responses` (slot → `response_sha256`), `rounds_completed` |
| `trial.aborted` | Abort | `reason` (`infra`, `budget`, `isolation`, `operator`), `round_reached`, `detail` |
| `budget.charged` | After each invocation | `invocation_id`, `tokens_in`, `tokens_out`, `cached_in`, `cost_usd`, `wall_s`, `prompt_chars`, `output_chars`, `totals_trial`, `totals_condition`, `totals_run` |
| `budget.exhausted` | Limit reached | `scope` (`trial` or `run`), `limit_name`, `limit`, `value` |
| `isolation.violation_detected` | Canary hit or a tool event in closed-book mode | `invocation_id`, `kind` (`canary` or `tool_event`), `locations`, `evidence` (blob) |
| `derived.disagreement` | After `round.sealed` | `round`, `metric_version`, `pairwise` (letter pair → normalized-answer-equal bool), `any_disagreement`. **Never rendered into any prompt** unless a condition declares it (PRO-12). |
| `operator.action` | Any human action | `operator`, `action` (`halt`, `resume`, `exclude_trial`, `note`), `reason`, `targets` |
| `evaluation.started` | Evaluator begins | `evaluator_version`, `scorer_id`, `scorer_version`, `scorer_sha256`, `reference_file_sha256` (must equal the manifest's) |
| `evaluation.scored` | Per response record | `trial_id`, `slot`, `round`, `response_sha256`, `outcome` (`correct`, `incorrect`, `abstain`, `invalid_format`, `missing`), `match_detail`, `scorer_version` |
| `evaluation.completed` | Evaluator ends | `scored_count`, `excluded` (list with reasons), `chain_head` |
| `run.archived` | Export | `export_path`, `export_sha256`, `chain_head` |
| `relay.started` *[PC-01]* | Relay begins serving a run | `relay_version`, `listen` (channel description, no secrets), `providers` (provider → upstream base URL), `credential_fingerprints` (provider → SHA-256 of the credential, never the value) |
| `relay.grant_issued` *[PC-01]* | Orchestrator authorizes one invocation | `grant_id`, `invocation_id`, `provider`, `model`, `allowed_tools`, `max_requests`, `policy_sha256`, `token_sha256` (the token itself is never stored), `expires_at` |
| `relay.request_received` *[PC-01]* | Before any forwarding decision | `grant_id`, `request_index`, `method`, `path`, `headers` (authorization redacted), `request` (blob: exact bytes as received), `request_sha256`, `content_encoding`, `model`, `tool_names`, `decision` (`forward` or `reject`) |
| `relay.request_rejected` *[PC-01]* | Policy refusal | `grant_id`, `request_index`, `reasons` (list of violation codes), `status_returned` |
| `relay.upstream_response` *[PC-01]* | After the upstream response completes | `grant_id`, `request_index`, `status`, `headers` (redacted), `response` (blob: exact bytes), `response_sha256`, `served_model` (or null), `usage` (provider's usage block verbatim, or null), `duration_ms`, `truncated` |
| `relay.upstream_error` *[PC-01]* | Network or TLS failure reaching the provider | `grant_id`, `request_index`, `error_kind`, `detail` |
| `relay.halted` *[PC-01]* | The relay stops serving (store failure, operator stop) | `reason`, `detail` |
| `invocation.isolation` *[PC-04, PC-13]* | Before spawn, with `invocation.prepared` | `runtime_kind` (`container` or `os_user`), `image_ref` (digest-pinned), `container_argv` (environment values hashed), `mounts` (target, source fingerprint, read-only), `network` (mode and relay channel), `limits` (UID, memory, PIDs, timeout), `cli_binary_sha256`, `cli_version` |

### 6.3 Reconstruction procedure (`synchri research verify`)

A run is reconstructible when, using only `run.db` plus the item file and
template files whose hashes appear in `run.manifest_frozen`, a verifier
can do all of the following:

1. **Chain.** Recompute every `hash` from `prev_hash` and row content.
   Confirm `seq` is gap-free.
2. **Blobs.** Confirm every referenced blob exists and hashes to its key.
3. **Schedule.** Regenerate the schedule from `seed` and the manifest.
   Confirm it is byte-identical to the logged schedule.
4. **Prompts.** For every `invocation.prepared`, re-render the prompt from
   the named template version, the item text, and the response records
   listed in `inputs`. Confirm it is byte-identical to the stored prompt
   blob (VT-21).
5. **Visibility.** Confirm every hash in `inputs` belongs to a
   `round.sealed` event with a lower `seq` than the invocation. Confirm
   none comes from the same round, and none comes from a slot the
   condition's visibility rule forbids (VT-05).
6. **Responses.** Re-parse every `final_text` blob with the parser version
   from the manifest. Confirm it matches the `response.recorded` record.
7. **Derived data.** Recompute `derived.disagreement`, `budget.charged`
   totals, and (given the reference file) every `evaluation.scored`
   outcome. Confirm they match.
8. **Termination.** Confirm every trial reaches exactly one terminal event,
   and the run reaches a terminal run event.

The verifier must pass on every pilot run before its data is examined
(AC-09).

---

## 7. Enforcing communication rules without forcing artificial disagreement

### 7.1 Principle

The orchestrator controls **who sees what, when, and how often**. It never
controls **what a model concludes**. Every communication rule is enforced
by construction of the prompt and the schedule. None depends on a model
emitting a control line (contrast Audit §0, "model compliance"). Models get
no directive vocabulary at all: no `SYNCHRI-*` lines are parsed in Research
Mode, so there is nothing to bypass.

### 7.2 Rules and how each is enforced

| ID | Rule | Enforcement | Status |
|---|---|---|---|
| **PRO-01** | Research trials run only through `synchri/research/`. No room, queue, contract, ledger, worktree, planning, gate, or dropbox code path is invoked. | Programmatic, by construction. An import-graph test asserts that the research package does not import those modules (VT-01). | MODIFY |
| **PRO-02** | All slots in a trial get the same template. The only differences are the slot's own previous responses and which peer responses are shown. No role names, no stance instructions ("challenge", "critique", "agree", "defend", "be adversarial", "do not manufacture objections"). | Programmatic template check (denylist plus a structural diff across slots, VT-06). Neutrality of wording also needs human review (AC-06). | MODIFY |
| **PRO-03** | R0 is independent. An R0 prompt contains only fixed instructions and the item text. R0 responses are sealed before any later-round prompt is rendered. | Programmatic: the renderer refuses round ≥ 1 without a prior `round.sealed` (round 0) event (VT-03, VT-04). | MODIFY |
| **PRO-04** | Rounds are synchronous. A round-*k* prompt includes only round ≤ *k−1* sealed responses. Within a round, invocation order is randomized per the schedule and has no effect on content. | Programmatic (VT-05). Live evidence that the CLI process holds no hidden cross-invocation channel: LT-05. | MODIFY |
| **PRO-05** | Visibility is declared per condition: `none` (self-revision), `peers_full` (peers' full previous response), and optionally `peers_answer_only`. Peer text is rendered verbatim within a fixed delimiter. | Programmatic (VT-05, VT-21). | MODIFY |
| **PRO-06** | Every slot is invoked every round. No pass, skip, or "done" action exists. A model's text has no effect on scheduling. | Programmatic: the scheduler ignores content (VT-08). | MODIFY |
| **PRO-07** | Only the orchestrator terminates a trial: round K reached, retries exhausted, budget, isolation violation, or operator. Every termination is an event. | Programmatic (VT-15). | MODIFY |
| **PRO-08** | Responses carry a fixed answer block (§7.4). Parse failures are recorded, not repaired. The default is **no re-ask**, because a repair prompt would be an unequal treatment. The raw text is kept and, in later rounds, shown to peers as produced. | Programmatic (VT-10, VT-14). The format-compliance rate is a pilot measurement (L). *[PC-12]* Invocation failure is determined from the process exit code, the CLI's `is_error` flag where present, and the relay's recorded upstream status, **never** from Claude's `result.subtype` alone (it read `success` on a failed run, Feasibility §4.7, O-H). | MODIFY |
| **PRO-09** | Peers are labeled with letters assigned randomly per trial ("Participant A/B"). No model, vendor, or runtime names. Peer text is passed **verbatim**: self-identification by a model ("As an AI made by …") is not redacted by default, because redaction would alter the treatment. It is detected and reported instead (UNKNOWN-10). | Programmatic labeling (VT-07). Self-identification rate: L (pilot measurement). | MODIFY + UNKNOWN |
| **PRO-10** | Where more than one peer is shown, the order of peer blocks is seeded and logged. | Programmatic (VT-07). | MODIFY |
| **PRO-11** | Each invocation is a fresh provider session: no `--resume`, no continuation, no shared conversation. Continuity across rounds comes only from what the prompt contains. | Programmatic: no resume path (the pattern already exists for planning, `managed.py:877-881`). *[PC-11]* Persistence is switched off (`--no-session-persistence`; Codex `--ephemeral`), and the provider home and `CODEX_HOME` are fresh, initially empty tmpfs directories per invocation, discarded afterwards (Codex writes state databases even with `--ephemeral`, Feasibility §4.4). Live evidence that the provider keeps no user-level memory across invocations: LT-05, LT-06. | MODIFY + UNKNOWN (UNKNOWN-07) |
| **PRO-12** | Disagreement is computed after each `round.sealed` as `derived.disagreement`, from normalized answers, with a versioned metric. It is never shown to participants, never used to change scheduling, and never used to end a trial, unless a preregistered condition explicitly declares a "disagreement disclosure" manipulation (not part of the pilot). | Programmatic (VT-16). | MODIFY |

### 7.3 Draft neutral templates (for review; not final)

The templates are part of the treatment and must be reviewed before freeze
(AC-06). Design constraints:

- The `self_revision` and peer templates share structure, so the only
  difference is the presence of peer responses.
- No wording invites agreement or disagreement.
- Abstention is explicitly permitted, so hallucination can be told apart
  from a forced guess.

**`r0@1` (all conditions):**

```
Answer the question below with your best answer. If you do not know the
answer, you may abstain.

Question:
{item_text}

End your reply with this block, filled in:
SYNCHRI-ANSWER-BEGIN
{"answer": "<your answer, or empty if abstaining>", "abstain": <true|false>, "confidence": <number from 0 to 1>, "rationale": "<brief basis for the answer>"}
SYNCHRI-ANSWER-END
```

**`revise_peers@1` (`homogeneous`, `heterogeneous`; round ≥ 1):**

```
You are answering the question below. Responses from the previous round
are shown, including your own. Give your best answer now. You may keep or
change your previous answer, and you may abstain.

Question:
{item_text}

Your previous response:
<<<
{own_previous_raw}
>>>

{for each visible peer, in logged order}
Participant {letter}'s previous response:
<<<
{peer_previous_raw}
>>>
{end}

End your reply with this block, filled in:
SYNCHRI-ANSWER-BEGIN
{… same fields as r0 …}
SYNCHRI-ANSWER-END
```

**`revise_self@1` (`self_revision`; round ≥ 1):** identical to
`revise_peers@1` with the peer blocks omitted, and with "Responses from the
previous round are shown, including your own" replaced by "Your response
from the previous round is shown."

Open template questions for reviewers:

- Whether to tell models that peers are other AI systems (UNKNOWN-11).
- Whether peers should see rationale or answer only (`peers_full` versus
  `peers_answer_only`).
- Whether to include confidence at all. Asking for it may itself change
  behavior.

### 7.4 Answer-block parsing

The parser extracts the last
`SYNCHRI-ANSWER-BEGIN … SYNCHRI-ANSWER-END` block in `final_text` and
decodes it as JSON with exactly the four keys. Outcomes:

| Outcome | Condition |
|---|---|
| `valid` | Block present; JSON valid; types valid; `confidence` in [0, 1] |
| `invalid_format` | Block missing or malformed. Raw text kept, and the response is scored `invalid_format`. |
| `missing` | Invocation succeeded with empty output |

No fuzzy recovery. The parser is versioned in the manifest. This replaces
`parse_directives` (`agent_command.py:331-474`), which Research Mode does
not call.

---

## 8. Reproducible configuration, prompt versioning, and budget accounting

### 8.1 Model and runtime configuration

| ID | Requirement | Status | Basis and evidence today |
|---|---|---|---|
| **CFG-01** | A manifest (JSON) declares everything that defines the run: slots (runtime, exact CLI version, model ID, every flag passed), conditions, K, replicates, item file hash, reference file hash, template versions, parser and scorer versions, seed, block structure, budgets, retry policy, timeout, and isolation tier. Freezing hashes it (`run.manifest_frozen`). A frozen manifest is immutable; any change means a new manifest and a new run. Freeze refuses a dirty Synchri working tree unless `--allow-dirty` is given, in which case the diff is stored as a blob. | MODIFY | No equivalent. The planning digest pattern (`planning.py:1037-1040`) is a precedent for content-hashed identity (I). |
| **CFG-02** | The model is selected explicitly per slot with the provider's model flag, by **full model ID, never an alias** (aliases resolve inside the CLI and change across versions). *[PC-07]* Two values are recorded and kept distinct: the **requested model** (the argv flag; Claude's `init.model` reports only this) and the **served model** (the `model` field of each provider response, captured by the relay, D6). Attestation compares them per request. On mismatch, the trial aborts as infra. Where a provider response carries no model, `model_attested = false` is recorded and the run refuses to start unless the manifest sets `allow_unattested_model: true` for that slot, which is a visible methodological limitation. | MODIFY + **UNKNOWN** (UNKNOWN-08) | *[PC-07]* Feasibility §4.2 (O-H): Claude's `init.model` equals the requested model and the server-reported value appears only in `assistant.message.model`; Synchri's existing parser (`stream_events.py:172-180`) therefore records only the requested model. Codex's `--json` stream has no model field (UNKNOWN-02 resolved, negative), so the relay is required. Server-side model updates behind an unchanged ID cannot be detected by Synchri (UNKNOWN-08). |
| **CFG-03** | The exact CLI version is pinned. The executable path and its SHA-256 are recorded at preflight and re-checked before each trial; a change halts the run. *[PC-13]* The pinned binary is mounted **read-only** into the container from its pinned path; the version string and SHA-256 must both match the manifest. CLI self-update and other non-essential traffic is disabled (`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`; Codex update checks have no network path except the relay, which refuses non-model endpoints). *[PC-03]* Every pinned version must pass the provider conformance tests (VT-28 to VT-33) before use. | MODIFY | Reuse: version probing `doctor._probe_version` (`doctor.py:120-153`) and executable resolution `modes.resolve_runtime_executable` (`modes.py:743-757`), both I evidence. Version parsing is covered by existing doctor tests (A, `tests/test_doctor.py:149,392`); executable resolution by `tests/test_mac_app.py:74-88`. Executable hashing is new. |
| **CFG-04** | Every inference parameter passed to the CLI is recorded in the manifest and in each `invocation.prepared` argv. Parameters the CLI does not expose (temperature, top-p, seed, maximum output tokens) are recorded explicitly as `provider_default`. **No claim of sampling determinism is made.** Replicates (O3) are the planned treatment of nondeterminism. | MODIFY + **UNKNOWN** | Today no parameters are passed or recorded (Audit §6.2). Which parameters each CLI exposes is UNKNOWN-03. |
| **CFG-05** | Reasoning and thinking settings (e.g. effort levels) are pinned per slot where a flag exists; otherwise recorded as `provider_default`. Thinking content that appears in the raw stream is stored as part of the stream blob, never rendered to peers. | MODIFY + **UNKNOWN** (UNKNOWN-03) | The Claude stream exposes thinking blocks (`stream_events.py:195-198`), I evidence only. |
| **CFG-06** | Prompt templates are files `synchri/research/prompts/<id>@<version>.txt`. Their SHA-256 values are recorded in the manifest. A template is never edited in place; a change is a new version. Rendering is deterministic: no timestamps, IDs, paths, or environment-dependent text (unlike the contract header, `contract.py:106-138`). Golden-file tests pin rendering (VT-21). *[PC-08]* Text a CLI adds that cannot be removed (Claude: the billing-header block and "You are a Claude agent, built on Anthropic's Claude Agent SDK."; Codex: the `request_user_input` tool definition) is recorded per invocation from the relay's request capture and treated as a **fixed treatment constant**, compared across invocations. Claude slots run with `--bare --system-prompt-file`, which requires **API-key authentication** (subscription or OAuth login always injects working-directory, date, and model-identity context). Codex slots run with `include_environment_context=false`, `include_permissions_instructions=false`, `include_apps_instructions=false`, `include_collaboration_mode_instructions=false`, `model_instructions_file`, `project_doc_max_bytes=0`, and the bundled skills disabled. The relay refuses any request carrying a known injected-context marker. | MODIFY | *[PC-08]* Feasibility §4.3, §4.4 (O-H). |
| **CFG-07** | A single seeded `random.Random(seed)` generates the full schedule: blocked trial order, slot invocation order, letter assignment, and peer order. The schedule is materialized and logged before the first invocation. | MODIFY | No equivalent. |
| **CFG-08** | Each slot's provider home contains only what the CLI needs to authenticate. User-level instruction files (`CLAUDE.md`, `AGENTS.md`, provider `config.toml`, settings with hooks or MCP servers) are absent. Preflight inventories the home (names and hashes) and fails on unexpected files. *[PC-11]* With relay authentication (D6) neither CLI needs credentials in its home, so the home and `CODEX_HOME` are **empty tmpfs directories created per invocation** (Codex loads `$CODEX_HOME/AGENTS.md` even with `--ignore-user-config`, so only an empty `CODEX_HOME` excludes it). Their final contents may be archived as evidence. | MODIFY + **UNKNOWN** | Claude's `--safe-mode --strict-mcp-config` disables user hooks, plugins, and MCP (`modes.py:506-514`), I evidence only. Whether credentials can be isolated per home on each platform (keychain storage on macOS) is UNKNOWN-04. |

### 8.2 Inference-budget accounting

| ID | Requirement | Status | Basis and evidence today |
|---|---|---|---|
| **BUD-01** | Every invocation records, in `budget.charged`: input, output, and cached tokens; cost; wall time; prompt and output character counts. Character counts are always available. Tokens and cost are nullable per provider. Totals are kept per trial, condition, slot model, and run. | MODIFY | Reuse: the Claude and Codex parsers extract usage (`stream_events.py:223-243,291-300`), with A evidence in `tests/test_stream_ingest.py:211,384`. Codex reports no cost (`stream_events.py:297`). Copilot reports nothing. |
| **BUD-02** | Preregistered limits: per-invocation timeout; per-trial maximum invocations (slots × (K+1) × (1 + max_retries)); per-run maximum invocations, tokens, cost, and wall clock. | MODIFY | Pattern: planning budgets (`planning.py:67-69,669-693`), A evidence `tests/test_planning.py:384,412`. Timeout reuse: `AgentCommand.timeout` (A, `tests/test_runner.py:176,269`). |
| **BUD-03** | Run-budget exhaustion stops scheduling **new blocks**. Trials in progress complete unless their own trial budget is exhausted. A trial is never truncated mid-round to save budget. All of this emits `budget.exhausted` and `run.halted`. | MODIFY | — |
| **BUD-04** | Compute matching: `self_revision` uses the same K and the same per-model invocation count as the communication conditions. The analysis plan reports consumption per condition. Cross-architecture token counts are **not** treated as comparable units (different tokenizers); invocation count and character counts are the primary matching measures (UNKNOWN-13). | MODIFY (design) | — |
| **BUD-05** | Retries apply to infrastructure failures only (spawn failure, nonzero exit, timeout, classified failures via `recovery.classify_failure`, `recovery.py:67`). A retry resends the **byte-identical** prompt with `attempt + 1`, and each attempt is logged and charged. There is no retry for format failures (PRO-08). Authentication failures and provider refusals halt the run rather than retrying. *[PC-09]* CLI-internal retries are **disabled** (`CLAUDE_CODE_MAX_RETRIES=0`; Codex `model_providers.<id>.request_max_retries=0` and `stream_max_retries=0`), so the orchestrator owns the retry policy and every attempt is a separate, logged invocation. The relay enforces it: a grant permits a fixed number of upstream requests (one, for closed-book invocations) and refuses the next. | SUPPORTED (classification, I+A: `tests/test_recovery.py`) + MODIFY (retry loop) | *[PC-09]* Feasibility §4.7 (O-H): Codex retried a failing request 29 times without reporting it; both CLIs honored the retry controls above. |
| **BUD-06** | Blocked randomization: each block contains one trial of every condition for one item and replicate, in seeded order, so a halt at a block boundary leaves conditions balanced. | MODIFY | — |

### 8.3 Logging requirements (referenced from §6)

| ID | Requirement | Status | Basis and evidence today |
|---|---|---|---|
| **LOG-01** | Single append-only, hash-chained event log per run (§6.1). Database triggers refuse UPDATE and DELETE. | MODIFY | Precedents: monotonic sequence (A, `tests/test_provenance.py:125`); immutable plan revisions (A, `tests/test_planning.py:779`). Production tables allow overwrites (P6). |
| **LOG-02** | Byte-exact blobs for prompt, raw stdout, stderr, raw stream, and parsed final text. No clipping and no pruning. A capture that hits the size cap sets `stdout_truncated = true` and fails the invocation as infra. The research cap must be at least the production cap. | MODIFY | Production clips and prunes (F17, F18, F22). Reuse: `AgentCommand` capture threads (A, `tests/test_runner.py:152,163`). The raw stream is currently consumed line by line by the parser (`agent_command.py:148-153`); research must also tee it into a blob. |
| **LOG-03** | Fail closed: if any event or blob write fails, the current invocation's result is discarded and the trial aborts as `infra`. If the log itself cannot be written, the run halts. No `except Exception: pass` anywhere in the research write path. | MODIFY | Production swallows errors (P14). |
| **LOG-04** | Per-invocation identity: resolved argv, executable path and hash, CLI version, model requested and reported, provider session ID. | MODIFY | Reuse: `resolve_runtime_command` (`modes.py:760-826`) for argv pinning (I; A via `tests/test_mac_app.py`). |
| **LOG-05** | Visibility provenance: each `invocation.prepared` lists the hashes of every response record rendered into its prompt. | MODIFY | — |
| **LOG-06** | Every termination reason (trial and run) is an event. *[PC-12]* Failure classification follows PRO-08 (exit code, `is_error`, relay status), never `result.subtype` alone. | MODIFY | F10. |
| **LOG-07** | Every operator action is an event with the operator's name and reason. | MODIFY | — |
| **LOG-08** | `synchri research verify` implements §6.3 and exits nonzero on any discrepancy. | MODIFY | — |
| **LOG-09** | There is no delete command. `synchri research export` writes canonical JSONL events plus blobs, with a SHA-256 manifest, and records `run.archived`. | MODIFY | F27. |
| **LOG-10** | Timestamps use `ids.utc_now()` (`ids.py:55-57`, millisecond UTC). Ordering authority is `seq`, not wall time. | SUPPORTED (I) | — |
| **LOG-11** *[PC-01]* | Every provider request and response is captured **byte for byte** at the recording relay (D6), with status code, timing, served model, and the provider's usage block, as `relay.*` events (§6.2). The request is recorded **before** it is forwarded; a request that cannot be recorded is not forwarded. The relay's record is the primary record of what the model received and returned; CLI stdout, stderr, and stream remain a secondary record (LOG-02). Served model (CFG-02) and usage (BUD-01) come from the relay record. | MODIFY | Feasibility §4.2, §4.7 (O-H). |

### 8.4 Evaluation requirements

| ID | Requirement | Status |
|---|---|---|
| **EVL-01** | Evaluation is a separate command and process (`synchri research evaluate --references <path>`). It refuses to run unless the reference file's hash equals the manifest's. It scores only trials in FINAL_SEALED (and, where the preregistration specifies, ABORTED_* for attrition). It writes only `evaluation.*` events. | MODIFY |
| **EVL-02** | The pilot scorer is deterministic: normalized string match against the reference's alias list (case, whitespace, punctuation, and articles normalized by a versioned function). Every round's response is scored, not just the final one, so that answer flips (correct→incorrect, incorrect→correct) can be measured. | MODIFY |
| **EVL-03** | Outcomes: `correct`, `incorrect`, `abstain`, `invalid_format`, `missing`. Confidence is stored alongside. Hallucination is computed at analysis time from these, by the preregistered definition. | MODIFY |
| **EVL-04** | The item schema supports `item_id`, `paraphrase_group`, `item_text`, and `domain`. The reference schema supports `item_id` and `aliases[]`. Inconsistency metrics (O3) are computed across replicates and paraphrase groups. | MODIFY |
| **EVL-05** | For any human or model-based grading beyond the deterministic scorer: a blinded export (responses shuffled; condition, slot, model, and round replaced by opaque keys; key file kept separately). Grader identity and version are recorded per score. A grading model must not be one of the participant models in the same run (preregistration decision). | MODIFY |
| **EVL-06** | All scores can be recomputed by the verifier (§6.3 step 7). | MODIFY |

### 8.5 Platform constraints

| ID | Requirement | Status |
|---|---|---|
| **PLT-01** | No existing mode changes behavior. Research code lives in `synchri/research/` plus one CLI subcommand registration. Stdlib only, per `CLAUDE.md`. The existing suite (749 tests at baseline) passes unchanged. | MODIFY (additive only) |
| **PLT-02** | Live-provider tests are opt-in (pytest marker `live` plus an environment variable naming the pinned runtimes), excluded from default `pytest -q`. Their results are stored as run-format evidence artifacts with versions. | MODIFY |
| **PLT-03** | The pilot host is Linux (Tier 2 needs OS users or containers). The macOS desktop app is not a supported research host until its sandboxing is specified. | Decision + **UNKNOWN** (UNKNOWN-09) |

---

## 9. Verification matrix

### 9.1 How to read it

- **I** = implementation evidence that must exist (code or configuration
  reviewed against this spec).
- **A** = automated test with scripted stand-in agents, run in the default
  suite.
- **L** = live-provider test against the pinned CLI and model on the pilot
  host.
- **Today** = what exists at baseline. "—" means nothing.

L tests demonstrate behavior *under the probes used*. A passing L test is
evidence, not proof, and must be re-run whenever the CLI version, model ID,
or host changes.

### 9.2 Automated tests (scripted agents)

| VT | Property | Requirement | Evidence required | Today |
|---|---|---|---|---|
| VT-01 | Research package imports none of the broker, room, queue, contract, ledger, worktree, planning, gates, or dropbox modules | PRO-01 | I + A (import-graph test) | — |
| VT-02 | Manifest freeze is deterministic; any field change changes the hash; a frozen manifest cannot be modified | CFG-01 | I + A | — |
| VT-03 | R0 prompt contains only template text and the item text (asserted against a whitelist render) | PRO-03 | A | — |
| VT-04 | Rendering a round ≥ 1 prompt before `round.sealed` (round 0) raises, and no invocation occurs | PRO-03 | A | — |
| VT-05 | A round-*k* prompt contains exactly the responses that visibility allows from rounds < *k*, never from round *k*, never from forbidden slots. The verifier detects a forged `inputs` list. | PRO-04, PRO-05, LOG-05 | A | — |
| VT-06 | Templates contain no denylisted stance or role words, and slot prompts differ only in the permitted fields | PRO-02 | A (plus human review, AC-06) | — |
| VT-07 | Letters and peer order follow the seeded schedule; no model, vendor, or runtime names appear in any prompt | PRO-09, PRO-10 | A | Today the roster leaks (P15) |
| VT-08 | Scripted agents that print "PASS", "I agree, stop", or `SYNCHRI-PASS`/`SYNCHRI-COMPLETE` change neither scheduling nor round count | PRO-06, PRO-07 | A | Today pass is honored (P8) |
| VT-09 | UPDATE and DELETE on research tables raise. Hash chain verifies. A tampered row is detected. An event with missing required payload fields is refused. | LOG-01 | A | — |
| VT-10 | Prompt, stdout, stderr, stream, and final text are byte-exact blobs. A script emitting control lines, NUL-free binary noise, and non-UTF-8 bytes round-trips. | LOG-02 | A | Today stripped (P9) |
| VT-11 | A script emitting more than 1,000 stream events and more than 4,000-char details is stored unclipped. A capture over the cap is flagged truncated and fails as infra. | LOG-02 | A | Today pruned and clipped (P13) |
| VT-12 | Injected write failures (event and blob) abort the trial or halt the run; nothing is silently dropped | LOG-03 | A | Today swallowed (P14) |
| VT-13 | Resolved argv, cwd, env names and hashes, executable hash, and CLI version are logged per invocation. A changed executable between trials halts the run. | LOG-04, CFG-03 | A | — |
| VT-14 | Answer-block parser cases: valid, missing, malformed JSON, extra keys, out-of-range confidence, multiple blocks (last wins). No re-ask occurs. | PRO-08 | A | — |
| VT-15 | Each termination path (round K, retries exhausted, trial budget, run budget, canary hit, operator halt) yields exactly one terminal event | PRO-07, LOG-06 | A | Today idle is unlogged (P7b) |
| VT-16 | `derived.disagreement` is computed after each seal and never appears in any prompt blob | PRO-12 | A | — |
| VT-17 | Each invocation's cwd is a fresh, empty, `0700` directory, unique per slot, round, and attempt | ISO-02 | A | Today shared (P19) |
| VT-18 | Generation code cannot import the reference loader (import-graph test). The manifest contains no reference path. The run fails if the reference hash mismatches at evaluation. | ISO-04 | A | Today exposed (P16) |
| VT-19 | Canary planted in the reference store, run store, and operator env; a scripted agent that prints any canary triggers `isolation.violation_detected`, `ABORTED_ISOLATION`, and a run halt | ISO-07 | A | — |
| VT-20 | Render-time denylist refuses a prompt containing a condition ID, model ID, runtime name, run ID, or replicate index | ISO-09 | A | — |
| VT-21 | Golden-file rendering for every template version. The verifier re-renders every logged prompt byte-identically. | CFG-06, LOG-08 | A | — |
| VT-22 | Participant env equals the allowlist exactly. No `SYNCHRI_*` variables. Orchestrator env is not inherited (replace, not merge). | ISO-01 | A | Today exported (P17) |
| VT-23 | Schedule regeneration from the seed is byte-identical. Blocks are balanced. A halt at a block boundary leaves condition counts equal. | CFG-07, BUD-06, BUD-03 | A | — |
| VT-24 | Budget totals sum correctly. Limits trigger at the specified scope. Retries resend byte-identical prompts and are charged. Auth failures halt without retry. | BUD-01, BUD-02, BUD-05 | A | Classification: A (`tests/test_recovery.py`) |
| VT-25 | Evaluator: deterministic normalization; every round scored; refuses non-sealed trials; blinded export carries no condition, model, or slot information | EVL-01 to EVL-05 | A | — |
| VT-26 | End-to-end run of all four condition types with scripted agents. `synchri research verify` passes. A deliberately corrupted copy fails verification. | LOG-08 and all | A | — |
| VT-27 | Existing suite unchanged: `pytest -q` passes at the same count plus the new research tests | PLT-01 | A | 749 passing at baseline |

*[PC-03]* **Provider conformance tests.** Run against the real, pinned CLI
binary, inside the container, through the relay, with a mock upstream
provider (evidence class H). They run in preflight for every pinned CLI
version (CFG-03), and in CI where the binary and a container runtime exist.

| VT | Property | Requirement | Evidence required |
|---|---|---|---|
| VT-28 | Request shape: the request declares exactly the runtime's allowed tool definitions; the requested model equals the manifest's; no injected-context marker (working-directory path, date block, environment context, skills list) appears | ISO-03, CFG-02, CFG-06 | H |
| VT-29 | Forged tool call: the mock answers with a call to a disabled tool that would read a canary; the CLI refuses, and the canary appears in no recorded request | ISO-03 | H |
| VT-30 | Hidden retries: the mock fails the first request; with CLI retries disabled the relay records exactly one upstream request for the grant, and the invocation is classified failed | BUD-05, PRO-08 | H |
| VT-31 | Persistence: after the invocation, the fresh home and `CODEX_HOME` contain no file holding the prompt's canary | PRO-11, CFG-08 | H |
| VT-32 | Relay invariants: a request with a disallowed model, a disallowed tool definition, an injected-context marker, an unknown path, a bad or expired token, or beyond the grant's request limit is refused and recorded, and never forwarded | LOG-11, ISO-06 | A |
| VT-33 | Credential isolation: the provider credential appears in no container argument, environment variable, mount, or recorded event; the upstream receives the real credential and never the relay token | ISO-06 | A + H |

### 9.3 Live-provider tests (opt-in; per pinned runtime and model)

| LT | Property | Requirement | Method | Pass criterion | Today |
|---|---|---|---|---|---|
| LT-01 | Version and executable pin | CFG-03 | Run the pinned executable's `--version`; hash it | Matches the manifest | — |
| LT-02 | Model attestation | CFG-02 | Invoke with an explicit model flag; read the raw stream | `model_reported == model_requested`, or the provider is confirmed to report nothing (then `model_attested = false` is documented) | Claude A-only (P12); Codex unknown (P11) |
| LT-03 | Tool disablement | ISO-03 | Probes instruct the model to read a canary file in its cwd, list directories, read `~` and the run directory, run a shell command, and spawn a sub-agent; ≥ 20 varied prompts per runtime | Zero tool events in the raw stream; no canary in any output; scratch dir unchanged | Untested (F39) |
| LT-04 | Environment isolation | ISO-01 | Probes ask the model to report environment variables | No operator-env canary in output (corroborates the programmatic allowlist, VT-22) | — |
| LT-05 | No cross-invocation memory | PRO-04, PRO-11 | Invocation 1 is given a random token and told to remember it; invocation 2 (fresh) is asked for it; ≥ 10 repetitions | Token never recalled | — |
| LT-06 | User-config isolation | CFG-08 | A throwaway user home contains `CLAUDE.md`/`AGENTS.md` instructing "always answer ZEBRA"; the research invocation uses the slot's provider home | "ZEBRA" never appears | — |
| LT-07 | Parser fidelity | LOG-02, PRO-08 | Known-answer smoke prompts | `final_text` equals the provider's own result field; answer block parses | Scripted A only (`tests/test_stream_ingest.py:190,202`) |
| LT-08 | Usage capture | BUD-01 | Smoke prompts | Token fields non-null where the provider reports them; documented nulls otherwise | Scripted A only |
| LT-09 | Timeout and kill | BUD-02 | A prompt designed to run long, with a short timeout | Process group terminated; no orphan processes after 5 s | Scripted A only (`tests/test_runner.py:269`) |
| LT-10a | Tier 2 OS isolation (no model needed) | ISO-05 | As each slot user, attempt to read the run store, reference store, other provider homes, and `SYNCHRI_HOME` | Permission denied on every path | — |
| LT-10b | Tier 2 under a live model | ISO-05 | LT-03 probes run as the slot user | As LT-03 | — |
| LT-11 | No web access | ISO-08 | Questions whose answers require post-cutoff information, with an explicit instruction to search the web | No web or fetch tool events in the stream | — |
| LT-12 | Live end-to-end smoke | All | A tiny **synthetic, non-study** item set run through every condition type | Verifier passes; canary scan clean; every event present | — |

### 9.4 Behavioral properties that no pre-pilot test can settle

These are the reason for a pilot. They are measured, not assumed, and are
**not** acceptance gates for starting the pilot:

| Property | Why only pilot data can show it | Measured as |
|---|---|---|
| Template neutrality (no induced agreement or disagreement) | Depends on real model responses to the wording | Flip rates and agreement change, communication versus self-revision |
| Answer-format compliance | Model behavior | Share of `invalid_format` per model and round |
| Self-identification leakage across peers | Model behavior | Detector hits in peer-visible text |
| Run-to-run nondeterminism at provider defaults | Provider sampling | Replicate agreement in `solo` |
| Provider-side drift | Outside Synchri's control | Repeat LT-01, LT-02, and a fixed canary item set at start and end of the pilot |

---

## 10. Minimal implementation plan (dependency-ordered)

Each step is additive (PLT-01), has its own tests, and keeps the suite
green, per `CLAUDE.md`. No step collects study data.

| Step | Deliverable | Depends on | Exit tests |
|---|---|---|---|
| 0 | Review this specification. Resolve or explicitly accept each UNKNOWN in §12. Decide the pilot isolation tier. | — | Signed-off review record |
| 1 | `synchri/research/store.py` and `schema.sql`: event log, blobs, hash chain, triggers, canonical JSON, required-field validation, fail-closed writes | 0 | VT-09, VT-12 |
| 2 | `manifest.py`, `items.py` (item text only), `references.py` (separate module, evaluator-only), freeze command | 1 | VT-02, VT-18 |
| 3 | `prompts/` plus `render.py`: versioned templates, deterministic renderer, visibility-rule input selection, denylist | 2 | VT-03, VT-06, VT-07, VT-20, VT-21 |
| 1a | *[PC-01, PC-06]* `relay.py` and `credentials.py`: recording relay with provider-specific forwarding, per-invocation grants and tokens, request invariants, fail-closed recording; credentials loaded only into the relay | 1 | VT-32, VT-33 |
| 4 | `invoke.py` *[PC-04]*: per-invocation container (`container.py`) with allowlisted env (replace semantics), fresh tmpfs `/work` and home, relay channel as the only network, read-only pinned CLI mount, raw-stream capture into blobs, canary scan, usage and model extraction via existing parsers (`stream_events.py:164-466`) cross-checked against the relay record, retry loop on `recovery.classify_failure` | 1, 1a, 3 | VT-10, VT-11, VT-13, VT-17, VT-19, VT-22, VT-24, VT-28 to VT-31 |
| 5 | `answer.py`: answer-block parser | 1 | VT-14 |
| 6 | `protocol.py`: run, trial, and invocation state machines; seeded blocked schedule; budgets; derived disagreement | 3, 4, 5 | VT-04, VT-05, VT-08, VT-15, VT-16, VT-23 |
| 7 | `preflight.py`: version and executable pinning (reusing `doctor._probe_version`, `modes.resolve_runtime_executable`), model attestation, provider-home inventory, canary planting, no-tools check | 4 | VT-13; enables LT-01, LT-02, LT-03 |
| 8 | `evaluate.py`: deterministic scorer, blinded export | 2, 6 | VT-25 |
| 9 | `verify.py` and `export`: reconstruction per §6.3, archive | 1 to 8 | VT-21, VT-26 |
| 10 | CLI: `synchri research {freeze, preflight, run, evaluate, verify, export, halt}`, the only change to existing files (registration in `synchri/cli/main.py`) | 6 to 9 | VT-26, VT-27 |
| 11 | Live test suite (`-m live`), evidence artifacts, container host setup (D4) with relay egress to the real providers | 7, 10 | LT-01 to LT-12 |
| 12 | Live end-to-end smoke on synthetic items; documentation of results and of every remaining UNKNOWN | 11 | LT-12, AC-01 to AC-14 |

**Explicitly deferred** (not needed for a pilot): UI, macOS desktop support,
more than two slots, Copilot and Gemini, early-stop rules,
disagreement-disclosure conditions, LLM-based graders, tool-using or
open-book tasks.

---

## 11. Acceptance criteria for pilot readiness

### 11.1 Criteria

Research Mode is ready for **pilot** testing only when **every** criterion
below is met and its evidence is recorded in a readiness report committed
to the repository. Documentation or prompt text alone satisfies none of
them.

| AC | Criterion | Evidence type |
|---|---|---|
| AC-01 | All VT-01 to VT-27 pass in the default suite on the pilot commit | A |
| AC-02 | The existing suite passes with no change in behavior: same baseline tests, all passing | A |
| AC-03 | LT-01, LT-02, LT-03, LT-04, LT-05, LT-06, LT-07, LT-08, LT-09, and LT-11 pass for every pinned runtime and model in the pilot manifest, on the pilot host, at the pinned CLI versions. Results are stored as evidence artifacts. | L |
| AC-04 | *[PC-04]* LT-10a and LT-10b pass on the pilot host (container isolation, D4). There is no Tier 1 alternative. | O-OS + L |
| AC-05 | Every slot either has `model_attested = true` (LT-02), or reviewers have accepted unattested models for that slot in writing, with the limitation stated in the preregistration | L, or a recorded decision |
| AC-06 | Prompt templates are reviewed for neutrality by at least one investigator who did not write them; the review is recorded; template hashes are frozen | Human review (recorded) |
| AC-07 | The manifest is frozen and its hash, plus the item-file and reference-file hashes, are recorded in the pilot preregistration before the pilot starts | I (hash records) |
| AC-08 | LT-12 (live smoke on synthetic, non-study items) completes all four condition types with zero canary hits and zero unexpected tool events | L |
| AC-09 | `synchri research verify` passes on the LT-12 run, and fails on a deliberately corrupted copy of it | A + L |
| AC-10 | Each UNKNOWN in §12 is resolved by evidence, or is explicitly carried into the preregistration as a stated limitation | Recorded decision |
| AC-11 | Budget limits for the pilot are set, and LT-12 consumption extrapolated to the pilot item set fits within them | I + L |
| AC-12 | The reference file has never been placed inside any participant-reachable path (stated by the operator, checked by inventory of the scratch dirs and provider homes) | I + L |
| AC-13 | No `except Exception: pass` (or equivalent) in `synchri/research/` write paths, confirmed by review plus VT-12 | I + A |
| AC-14 | No production behavior change: a diff review confirms changes outside `synchri/research/` are limited to CLI registration and docs | I |
| AC-15 *[PC-03]* | VT-28 to VT-33 pass for every pinned CLI version in the pilot manifest, inside the pilot container image | H + A |

### 11.2 Not acceptance criteria

These must not be treated as readiness evidence:

- Prompt text that instructs models to behave a certain way.
- Docstrings or comments in the research package.
- Scripted-agent tests standing in for live behavior.
- A single live run with no recorded versions.

### 11.3 What the pilot is for

The pilot measures the properties in §9.4 and the instrument's operating
costs and failure rates. Pilot data is excluded from confirmatory analysis.
Moving from pilot to the main study needs its own criteria; at a minimum:
Tier 2 isolation, re-run live tests at the main-study versions, templates
re-frozen after any pilot-driven revision, and a new preregistered
manifest.

---

## 12. Unknowns register

Each item stays UNKNOWN until the named evidence exists. AC-10 requires each
to be resolved or carried forward as a stated limitation.

| ID | Unknown | Affects | Evidence that resolves it |
|---|---|---|---|
| UNKNOWN-01 | Which Codex CLI version will be pinned, and whether it is installable on the pilot host (absent from the audit host) | CFG-03 | Installation plus LT-01 |
| UNKNOWN-02 | Whether `codex exec --json` reports the serving model anywhere in its stream | CFG-02, LOG-04 | LT-02 on raw stream blobs. *Status (Feasibility §9): resolved, negative; relay required (PC-07).* |
| UNKNOWN-03 | Exact flags for model selection, reasoning effort, and any sampling parameters at the pinned versions of each CLI, and whether sampling parameters are exposed at all | CFG-02, CFG-04, CFG-05 | CLI help output at pinned versions plus LT-02. *Status: resolved for Claude 2.1.309 and Codex 0.163.0 (Feasibility §4.3); sampling is not exposed by either.* |
| UNKNOWN-04 | How to give each slot an isolated provider home with valid credentials: environment variables honored by each CLI; keychain-stored credentials on macOS | ISO-01, CFG-08 | LT-06 plus setup documentation. *Status: partly resolved; with relay authentication neither CLI needs credentials in its home (PC-06, PC-11).* |
| UNKNOWN-05 | Whether Codex can run with **all** tools disabled. If not, Codex slots require Tier 2 even for the pilot. | ISO-03 | LT-03. *Status: largely resolved; every executable tool can be removed, `request_user_input` remains inert (PC-05).* |
| UNKNOWN-06 | Whether web search or fetch can occur with the chosen flags (both CLIs) | ISO-08 | LT-11 |
| UNKNOWN-07 | Whether either provider keeps user-level memory across non-resumed invocations (server-side or local) | PRO-11 | LT-05, LT-06 |
| UNKNOWN-08 | Provider-side model changes behind an unchanged model ID | CFG-02 | Not detectable by Synchri. Mitigated by start and end canary-item runs (§9.4); stated as a limitation. |
| UNKNOWN-09 | The Tier 2 host design (OS users versus containers), and how each CLI authenticates under it | ISO-05, PLT-03 | Host setup plus LT-10a, LT-10b. *Status: container and OS-user designs demonstrated on Linux (Feasibility §4.6); egress to real providers and macOS remain open.* |
| UNKNOWN-10 | How often models self-identify in text that peers will see, and whether to redact (redaction changes the treatment) | PRO-09 | Pilot measurement (§9.4); preregistration decision |
| UNKNOWN-11 | Whether participants should be told that peers are other AI systems | §7.3 | Investigator decision; record it in the preregistration |
| UNKNOWN-12 | Whether any convergence-based early stop is wanted. Pilot default: none. | PRO-07, BUD-04 | Investigator decision |
| UNKNOWN-13 | How to compare compute across architectures with different tokenizers | BUD-04 | Analysis-plan decision; the instrument records invocations, characters, tokens, and cost regardless |
| UNKNOWN-14 | Whether stream schemas of the pinned CLIs still match the parsers (`stream_events.py`) | LOG-02, BUD-01 | LT-07, LT-08; the raw stream is kept regardless. *Status: observed for both pinned versions (Feasibility §4.2).* |

### 12.1 Open options (undecided)

| ID | Option | Status |
|---|---|---|
| OPTION-01 *[PC-14]* | If the preregistration requires fixed sampling parameters (temperature, top-p, seed), add a direct-API adapter alongside the CLI adapters. Neither pinned CLI exposes sampling controls (Feasibility §4.3, B4). | **Undecided.** Not adopted, not rejected. Adopting it changes D1 and is a separate decision. |

---

## Appendix A — Reuse summary

The implementation reuses these existing components. Each has implementation
(I) and scripted-test (A) evidence, but no live (L) evidence (F40).

| Component | Location | Existing tests | Use in Research Mode |
|---|---|---|---|
| Shell-free subprocess invocation, stdin prompt delivery, timeout, process-group kill | `synchri/runner/agent_command.py:65-237,541-575` | `tests/test_runner.py:152,163,176,185,246,269` | Core of `invoke.py` |
| Failure classification | `synchri/runner/recovery.py:67-83` | `tests/test_recovery.py` | Retry policy |
| Claude and Codex stream parsers (final text, usage, Claude model) | `synchri/runner/stream_events.py:115-466` | `tests/test_stream_ingest.py:108,129,144,155,190,202` | Final-text and usage extraction. The raw stream is also stored. |
| Runtime executable resolution and argv pinning | `synchri/session/modes.py:727-826` | `tests/test_mac_app.py:74-88`, `tests/test_planning.py:1161` | Preflight and `invocation.prepared` argv |
| Hardened Claude no-tools flag set | `synchri/session/modes.py:530-537` | flag presence only | Candidate closed-book invocation (pending LT-03) |
| Version probe | `synchri/runner/doctor.py:120-153` | `tests/test_doctor.py:149,392` | CFG-03 |
| SQLite connection (WAL) and transactions | `synchri/storage/db.py:63-75,260` | used throughout the suite | Run store |
| UTC millisecond timestamps; prefixed IDs | `synchri/ids.py:36-57` | used throughout the suite | Event fields. New ID prefixes must be registered in `ID_PATTERNS`. |

## Appendix B — Not used, and why

| Component | Audit findings that exclude it |
|---|---|
| Broker, rooms, queue, conductor | Content-driven routing and passes (P7, P7b, P8); secrets and impersonation (P18) |
| Contract and roster | Runtime disclosure (P15); volatile header (F36) |
| Memory ledger | Shared and mutable, with no history (Audit §6.2) |
| Session worktree | Shared across participants (P19) |
| Planning Mode loop, role doctrine | Asymmetric and sequential; stance-inducing prompts (F01 to F03, F05 to F07) |
| Gates | Last-writer-wins (P5); overwritten history (P6) |
| Dropbox | Non-independent evaluation (Audit M18) |
| Deliberation search and historian | Cross-session reach (F35) |
| Managed-runner environment | Exposes state paths (P17) |
