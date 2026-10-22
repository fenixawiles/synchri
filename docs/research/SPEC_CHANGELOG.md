# Research Mode specification — changelog

Changes to `docs/research/RESEARCH_MODE_SPEC.md`. Each amendment names the
proposal it implements (from `docs/research/PROVIDER_FEASIBILITY.md` §8), the
sections it changed, and the evidence behind it. Inside the specification,
each amended passage is marked *[PC-nn]*.

Evidence labels follow the feasibility report:

| Label | Meaning |
|---|---|
| **D** | Documented by the provider |
| **O-H** | Observed with the real CLI binary against a recording mock API |
| **O-OS** | Observed at the OS or container level |
| **U** | Unverified |

## Revision 2 — 2026-10-22

Approved amendments PC-01 to PC-13 applied. PC-14 recorded as an undecided
option. PC-15 left outside the specification as a separate production
security issue.

| PC | Change | Spec locations changed | Supporting evidence |
|---|---|---|---|
| **PC-01** | **Recording relay.** A trusted relay between every CLI and its provider captures request and response bytes, status, timing, served model, and usage. Requests are recorded *before* forwarding and refused if they cannot be recorded. The relay record is the primary record. | New decision **D6**; new requirement **LOG-11**; event catalog §6.2 (new `relay.*` events); access-matrix note §5.2; implementation plan step 1a | Feasibility §4.2 (O-H): Codex `--json` has no model field, and Claude `init.model` is only the request. §4.7 (O-H): hidden context and hidden retries (Codex: 30 requests, 7 error events, no retry event; Claude: `subtype: "success"` on a failed run). |
| **PC-02** | **Evidence class H** (harness: real CLI binary against a recording mock API). It may verify client-enforced properties only, never model or server behavior. | §0.4 evidence vocabulary | Feasibility §2: the method is deterministic and credential-free, and it found finding 1 (Codex read-only sandbox leak). |
| **PC-03** | **Provider conformance tests** VT-28 to VT-33 (request shape, forged tool calls, hidden retries, persistence, relay invariants, credential isolation), required per pinned CLI version. New acceptance criterion **AC-15**. | §9.2 (new table after VT-27); CFG-03; §11.1 AC-15 | Feasibility §6 B9 (O-H/D): feature flags and config keys are version-unstable. The harness in `docs/research/feasibility-harness/` is the prototype. |
| **PC-04** | **OS-level isolation from the pilot onward.** The former Tier 1 (client-side controls only) is retired; a container per invocation is required, with a distinct OS user as the fallback. | Decision **D4** rewritten; **ISO-05**; access-matrix note §5.2; mapping row F33 §3.3; plan step 11; **AC-04**; new `invocation.isolation` event §6.2 | Feasibility §4.1 finding 1 (O-H): Codex `--sandbox read-only` sent a protected canary to the API. §4.6 (O-OS): container and OS-user isolation confined both CLIs with every CLI protection off. |
| **PC-05** | **Codex confinement.** Never rely on `--sandbox read-only`. Pinned feature-disable set. `request_user_input` recorded as a known residual tool definition and treatment constant, with any call logged. Relay enforces a per-runtime tool-definition allowlist. | **ISO-03**; unknowns register UNKNOWN-05 status | Feasibility §4.1 (O-H): forged `exec_command`/`shell` → "unsupported call" once disabled; `request_user_input` → "unavailable in Default mode"; read-only sandbox read `/`. |
| **PC-06** | **Credentials only in the relay.** Dedicated per-study, spend-limited, revocable keys, loaded from an owner-only file given explicitly to the relay. The CLI holds only a per-invocation relay token. The relay strips client authorization and injects the real credential upstream. The launcher refuses an environment containing any credential-shaped value except the relay token. | **ISO-01**; **ISO-06**; decision D6 | Feasibility §4.5 (O-H): Synchri's launcher passed 171 host variables including cloud and session tokens; both CLIs expose their provider key to tool subprocesses; both accepted a dummy key with a custom base URL. Relay-side injection against real providers: **U**. |
| **PC-07** | **Requested versus served model.** The requested model is the argv full ID (no aliases; Claude `init.model` reports only this). The served model is the relay-captured response field. Attestation compares the two per request. | **CFG-02**; unknowns register UNKNOWN-02 status | Feasibility §4.2 (O-H): aliases resolve inside the CLI; `assistant.message.model` carries the server value; Synchri's existing parser records only the requested model. |
| **PC-08** | **CLI-added text as a treatment constant**, recorded from the relay. Claude slots require `--bare --system-prompt-file` and API-key authentication. Pinned Codex context-suppression keys. The relay refuses requests carrying known injected-context markers. | **CFG-06** | Feasibility §4.3, §4.4 (O-H): Claude always prepends a billing header and an identity line; without `--bare` it injects working-directory path, OS, date, and model identity; `--bare` is API-key only (**D**). Codex suppression keys observed effective. |
| **PC-09** | **CLI-internal retries disabled**, so the orchestrator owns retries. The relay enforces a per-grant request limit. | **BUD-05** | Feasibility §4.3, §4.7 (O-H): `CLAUDE_CODE_MAX_RETRIES=0` and Codex `request_max_retries=0`/`stream_max_retries=0` each produced exactly one request. |
| **PC-10** | **Neutral fixed working directory** `/work` inside the container, so injected paths are identical across invocations. | **ISO-02** | Feasibility §4.4 (O-H): both CLIs inject the working-directory path unless suppressed. |
| **PC-11** | **Fresh, empty tmpfs homes per invocation** (provider home and `CODEX_HOME`), persistence off, contents optionally archived. | **PRO-11**; **CFG-08**; unknowns register UNKNOWN-04 status | Feasibility §4.4 (O-H): default transcripts persist in both CLIs; Codex writes state databases even with `--ephemeral`; Codex loads `$CODEX_HOME/AGENTS.md` despite `--ignore-user-config`. |
| **PC-12** | **Failure detection** from exit code, `is_error`, and relay status, never Claude `result.subtype` alone. | **PRO-08**; **LOG-06** | Feasibility §4.7 (O-H): `subtype: "success"`, `is_error: true`, and a "529" message for an injected 500. |
| **PC-13** | **Version and hash pinning** with a read-only mount of the pinned binary; self-update and non-essential traffic disabled. | **CFG-03** | Feasibility §3 (binary hashes recorded); §4.6 (O-OS): read-only binary mounts worked for both CLIs. Codex update-check behavior: **U**. |

Also updated in revision 2, as direct consequences of the amendments above:

- **Header:** a revision status line and an "Evidence inputs" row.
- **Unknowns register:** status notes on UNKNOWN-02, -03, -04, -05, -09, and
  -14, each citing the feasibility section that resolves or narrows it. No
  unknown was closed beyond what the evidence shows.

### Not applied

| PC | Disposition |
|---|---|
| **PC-14** | **Undecided option.** Recorded in the specification as OPTION-01 (§12.1), "not adopted, not rejected". A direct-API adapter would change decision D1 and needs its own decision. |
| **PC-15** | **Separate production security issue, outside this specification.** Planning Mode's Codex isolation relies on `--sandbox read-only` (`synchri/session/modes.py`, `planning_command` for `codex`), which reads the whole filesystem (Feasibility §4.1). It is tracked as a separate task and is not addressed by Research Mode work. |

## Revision 1 — 2026-10-22

Initial specification derived from `docs/audits/source-audit-1e6a347.md`.
