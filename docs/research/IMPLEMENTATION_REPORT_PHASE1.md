# Research Mode — Phase 1 implementation report (foundational infrastructure)

| | |
|---|---|
| Specification | `docs/research/RESEARCH_MODE_SPEC.md`, Revision 2 (PC-01 to PC-13 applied, commit `1a531b5`) |
| Amendment trace | `docs/research/SPEC_CHANGELOG.md` |
| Base commit | `1e6a347` (audit baseline); spec amendments `1a531b5`; this phase is the commit that adds this file |
| Test environment | Linux 6.18, Python 3.13.16, Docker Engine 29.9.1 (rootful, local daemon), image `synchri-research-runner:1` = `sha256:cb90c7aaff1f93cbceb52ba18de6543b16c9833716cd1c20ba0ccf4a2c8801fa` built from `docs/research/container/Dockerfile` |
| CLIs exercised | Claude Code 2.1.309 (binary sha256 `4d704a8aecbe038a…`), Codex 0.163.0 (musl binary sha256 `399ff69c431c066e…`), both run **only against the mock provider** |
| Credentials used | Dummy values only (`CredentialVault.for_tests`, random per run). No existing session credential was read, copied, or mounted. No real provider was contacted. |

## 1. Outcome

All six foundational components are implemented in the isolated package
`synchri/research/` and reachable through `synchri research …`. Every control
listed in §4 has an executable test; all tests pass. **No design conflict
required weakening the approved specification** (§6 records the
interpretations made and one literal conflict that was fixed in code rather
than in the spec).

Not implemented, as instructed: experimental communication rounds (PRO-02 to
PRO-07, PRO-09, PRO-10, PRO-12), outcome scoring and derived data, manifests
and schedules (CFG-01, CFG-07), budgets beyond the per-grant request limit
(BUD-01 to BUD-04, BUD-06), and any confirmatory data collection.

## 2. Amendments PC-01 to PC-13

Applied to the specification in commit `1a531b5`. Every change is marked
`*[PC-nn]*` in place and traced row by row to its feasibility evidence in
`docs/research/SPEC_CHANGELOG.md`. PC-14 (direct-API adapter) is recorded
as **OPTION-01, undecided** (spec §12.1). PC-15 (Codex read isolation in the
production Planning Mode) is out of scope for the spec and was filed as a
separate production security task.

## 3. Test results

| Suite | Command | Result |
|---|---|---|
| Full default suite (production + research) | `python -m pytest -q` | **845 passed, 5 skipped** (5 skipped = the opt-in Docker suite below; baseline before Research Mode was 749 passed) |
| Research unit and integration tests (in the default suite) | `python -m pytest -q tests/test_research_*.py` | **96 passed** (5 opt-in skipped) |
| Opt-in container conformance suite | `SYNCHRI_RESEARCH_CONFORMANCE=1 SYNCHRI_RESEARCH_IMAGE=… SYNCHRI_RESEARCH_{CLAUDE,CODEX}_{ROOT,RELPATH,SHA256,VERSION}=… python -m pytest -q tests/test_research_conformance.py` | **5 passed** (Claude, Codex, two negative controls, active container probe) |
| CLI conformance, Claude Code 2.1.309 | `synchri research conformance --runtime claude_code …` | VT-28, VT-29, VT-30, VT-31, VT-33 **PASS**; store verified |
| CLI conformance, Codex 0.163.0 | `synchri research conformance --runtime codex …` | VT-28, VT-29, VT-30, VT-31, VT-33 **PASS**; store verified |
| Reconstruction of both conformance runs | `synchri research verify docs/research/evidence/phase1/{claude,codex}` | exit 0; 36 events, 19 blobs, 4/4 invocations rebuilt, each |

Evidence: `docs/research/evidence/phase1/{claude,codex}/` holds each run's
`conformance-report.json` and the complete hash-chained `run.db` (dummy
credentials, mock upstream; scanned: no credential, relay token, or host home
path occurs in either file). Anyone can re-verify them with
`synchri research verify <dir>`.

## 4. Requirement traceability

Paths are repository-relative; line numbers refer to this commit.
"Result" is the result of the named test in this environment.
"Live" names what still needs a real provider (§5.3).

### 4.1 Package and CLI entry point

| Req | Control | Source | Test | Result | Remaining limitations |
|---|---|---|---|---|---|
| PRO-01, D5 | Research code lives only in `synchri/research/`; it imports no production module except `synchri.ids`, `synchri.errors`, `synchri.storage.db` (connection helper) | `synchri/research/` (all modules); `synchri/research/streams.py` (own stream parser, replaces `runner.stream_events`) | `tests/test_research_boundary.py::test_no_research_module_imports_production_code` (AST, every module) and `::test_importing_research_loads_nothing_beyond_the_package_root` (subprocess, `sys.modules` delta) | pass | Importing any `synchri.*` module first runs `synchri/__init__.py`, which imports `Broker`; the test therefore asserts the *delta* beyond that root import (§6, note 1). |
| PRO-01, ISO-06 | `synchri research` is dispatched before any Broker is constructed or `SYNCHRI_HOME` touched | `synchri/cli/main.py:38,110-111,433-436`; `synchri/research/cli.py:33-156` | `tests/test_research_boundary.py::test_research_commands_never_open_production_state` | pass | — |
| LOG-08 | `synchri research verify <run-dir>`: chain + blob verification + per-invocation reconstruction; exit 12 on any discrepancy | `synchri/research/cli.py:92-107`; `synchri/research/reconstruct.py:80-155` | `tests/test_research_reconstruct.py` (4 tests); CLI run on both evidence stores | pass | §6.3 steps 3–7 depend on later phases and are reported as "not yet applicable", never as passes. |

### 4.2 Recording relay (D6, LOG-11)

| Req | Control | Source | Test | Result | Remaining limitations |
|---|---|---|---|---|---|
| LOG-11 | Exact request bytes recorded as a blob and `relay.request_received` **before** forwarding; response bytes, status, timing, served model, and usage recorded as `relay.upstream_response` | `synchri/research/relay.py:386-426` (record), `:428-512` (forward, stream, record) | `tests/test_research_relay.py::test_forwarding_records_exact_bytes_and_injects_only_the_real_credential`, `::test_gzip_bodies_are_inspected_and_forwarded_unchanged` | pass | Live: real SSE framing, usage blocks, and served-model fields (LT, UNKNOWN-08). |
| LOG-11, LOG-03 | A request that cannot be recorded is not forwarded: store failure halts the relay (503 to every later request) | `synchri/research/relay.py:212-226` (`halt`), `:416-417`, `:511-512`, `:523-524` | `tests/test_research_relay.py::test_a_store_failure_halts_the_relay_before_anything_is_forwarded` | pass | — |
| VT-32 | Refused and recorded, never forwarded: wrong model, disallowed tool definition, injected-context marker, unknown route, unknown token, expired grant, request beyond the grant limit, unsupported encoding | `synchri/research/policy.py:221-263` (`inspect_request`), `:135` (markers); `synchri/research/relay.py:386-424` | `tests/test_research_relay.py::test_policy_violations_are_recorded_and_never_forwarded` (5 cases), `::test_codex_allowlist_admits_only_request_user_input`, `::test_the_grant_request_limit_blocks_hidden_retries`, `::test_unknown_token_and_expired_grant_are_refused`, gzip/br test | pass | The marker list is derived from the two pinned CLI versions (Feasibility §4.4); a new CLI version needs VT-28 to confirm it is still complete. |
| D6 | Provider-specific forwarding: `/anthropic/v1/messages` with `x-api-key`; `/openai/v1/responses` with `Authorization: Bearer`; every other route refused | `synchri/research/policy.py:26-134` (`ProviderProfile`, `PROFILES`) | relay tests above; both CLI conformance runs | pass | Live: the real endpoints' acceptance of the forwarded requests. |
| D6 | Upstream must be HTTPS (certifi, or a given CA bundle; optional CONNECT proxy); plain HTTP only with an explicit test flag, and the CLI restricts that flag to loopback | `synchri/research/relay.py:109-119`, `:441-458`; `synchri/research/cli.py:110-141` | `tests/test_research_relay.py::test_construction_refuses_insecure_upstreams_and_missing_credentials`; `tests/test_research_relay_control.py::test_loopback_http_is_the_only_insecure_upstream_allowed` | pass | **The HTTPS and proxy paths have not been exercised against a real provider** (no real credential may be used in this phase). |
| BUD-05 | One grant = a fixed number of upstream requests (default 1); the next is refused | `synchri/research/policy.py:157-190` (`Grant`), `:232` | `test_the_grant_request_limit_blocks_hidden_retries`; VT-30 in both CLI runs | pass | The orchestrator's own retry loop is a later phase. |

### 4.3 Per-invocation container (D4, ISO-05)

| Req | Control | Source | Test | Result | Remaining limitations |
|---|---|---|---|---|---|
| ISO-05 | `docker run --rm --network none --read-only --user 10001:10001 --cap-drop ALL --security-opt no-new-privileges --pids-limit --memory`; fresh tmpfs `/work`, `/home/slot` (mode 0700), `/tmp` | `synchri/research/container.py:190-210` (`build_argv`) | `tests/test_research_container.py::test_the_command_line_isolates_and_carries_no_secret`; `tests/test_research_conformance.py::test_the_container_boundary_holds_against_an_active_probe` (an active program: host files unreadable, root FS and mounts read-only, tmpfs writable, no network, UID 10001, env exactly the allowlist) | pass | Docker on Linux only; macOS (Docker Desktop VM) untested (UNKNOWN-09). The distinct-OS-user fallback is not implemented. |
| ISO-05 | Exactly three mounts: CLI root (ro), launcher dir (ro), relay socket; none may overlap the home, `/root`, `/home`, `/etc`, `/run`, `/proc`, `/sys`, `/dev`, provider config dirs, `SYNCHRI_HOME`, or the run store | `synchri/research/container.py:56-82`, `:103-110`, `:111-169` (`validate`) | `tests/test_research_container.py::test_production_and_credential_locations_are_never_mounted` (parametrized), `::test_the_launcher_directory_holds_exactly_the_launcher`, `::test_the_relay_channel_must_be_a_socket` | pass | — |
| ISO-05, CFG-03 | Digest-pinned image required; binary sha256 must match its pin, re-checked before **every** invocation | `synchri/research/container.py:111-140` | `::test_images_must_be_digest_pinned`, `::test_the_binary_hash_must_match_the_pin`, `::test_containers_never_run_as_root` | pass | The CLI version string is the declared pin; the binding control is the binary hash. A `--version` probe in the container is not yet run. |
| CFG-03, AC-15 | A CLI version without a verified flag set is refused unless explicitly overridden for conformance work | `synchri/research/invocation.py:89-96`; `synchri/research/providers.py:56,107` | conformance runs | pass | — |
| ISO-01 | The CLI environment is an exact allowlist per adapter; no `SYNCHRI_*` name at all (the relay token's variable included); no credential-shaped name except the token; no credential value (by value with a co-resident vault, by sha256 fingerprint with a remote relay); names and value hashes recorded in `invocation.prepared` | `synchri/research/container.py:171-188`; `synchri/research/providers.py:59-63,110`; `synchri/research/invocation.py:107-113` | `tests/test_research_container.py::test_cli_environment_is_an_exact_allowlist`, `::test_no_synchri_variable_reaches_a_participant_not_even_the_token`; `tests/test_research_relay_control.py::test_fingerprints_alone_catch_a_credential_in_the_cli_environment`; container probe (`/proc/self/environ`) | pass | — |
| ISO-02 | Neutral fixed working directory `/work`, a fresh tmpfs per invocation | `synchri/research/container.py:200`; `synchri/research/providers.py` (`WORK_DIR`) | container probe; VT-28 (`host_paths_in_request` empty in both runs) | pass | The post-run inventory of `/work` and the home is recorded, but a non-empty `/work` is not yet flagged as its own event. |
| ISO-03 | Pinned closed-book flag sets: Claude `--bare --tools "" --disallowedTools * --strict-mcp-config`, prompt on stdin; Codex 14 feature disables, `web_search="disabled"`, `include_*_context=false`, bundled skills disabled by path | `synchri/research/providers.py:52-163` | `tests/test_research_container.py::test_claude_plan_is_the_minimal_context_flag_set`, `::test_codex_plan_disables_every_executable_tool_and_injected_context`; VT-28 and VT-29 in both CLI runs; negative controls (`--bare` removed → VT-28 fails; Codex shell re-enabled → `exec_command` appears) | pass | Live: adversarial probing of the real models (LT-03, LT-11). |
| PRO-11, CFG-08 | Fresh session per invocation (`--no-session-persistence`; `--ephemeral`); empty tmpfs home and `CODEX_HOME`; inventory of names, sizes, and hashes after the run, with persistence probes | `synchri/research/launcher.py` (`LAUNCHER_SOURCE`, inventory); `synchri/research/providers.py:68,115,143-160` | VT-31 in both CLI runs | pass | Codex still writes SQLite state into the (discarded) tmpfs `CODEX_HOME`; Claude writes `.claude.json` and a backup. Neither contained the prompt canary. Live: provider-side memory (LT-05, LT-06). |
| LOG-04 | `invocation.isolation` records image, full container argv, mounts, network mode, limits, CLI hash and version, and the launcher files' hashes; `invocation.prepared` records argv, env names, value hashes | `synchri/research/invocation.py:141-175` | reconstruction tests; CLI evidence stores | pass | The provider session ID comes from stdout; live streams may differ. |
| LOG-03 | `invocation.launched` is written from an `on_spawn` callback; a failure to record it kills the container | `synchri/research/container.py:239-276`; `synchri/research/invocation.py:176-181` | `tests/test_research_container.py` (launcher tests); conformance runs | pass | — |

### 4.4 Credentials only in the relay (ISO-06, VT-33)

| Req | Control | Source | Test | Result | Remaining limitations |
|---|---|---|---|---|---|
| ISO-06 | Credentials load only from an explicitly named, absolute, regular, non-symlink, euid-owned, mode-0600-or-stricter file; refused inside `~/.claude`, `.claude.json`, `.codex`, `.config/{claude,anthropic,openai,github-copilot}`, `.synchri`, `.aidapter`, `.ccr`, or any path named by `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `SYNCHRI_HOME`, `CLAUDE_SESSION_INGRESS_TOKEN_FILE` | `synchri/research/credentials.py:31-38`, `:63-136` | `tests/test_research_credentials.py` (7 test functions, parametrized) | pass | — |
| ISO-06 | `Secret` never prints, never pickles; only its sha256 fingerprint is ever recorded | `synchri/research/credentials.py:40-60` | `::test_secrets_do_not_print_or_pickle`; `tests/test_research_relay.py::test_no_credential_or_token_value_is_ever_stored` | pass | — |
| ISO-06 | Per-invocation one-time `srt_` token; the relay stores only its sha256; client auth headers are stripped and the real credential injected upstream | `synchri/research/policy.py:191-198`; `synchri/research/relay.py:227-292`, `:428-438` | relay tests; VT-33 in both CLI runs (`upstream_received_relay_credential_on_every_request: true`, credential absent from argv, launcher files, store) | pass | — |
| ISO-06 | The token reaches the container through `docker -e NAME` (value in the client environment, never argv, never `launch.json`); the launcher substitutes it and removes it from its own environment before running the CLI | `synchri/research/container.py:204`, `:239-250`; `synchri/research/launcher.py:27` | `tests/test_research_container.py::test_the_command_line_isolates_and_carries_no_secret`; VT-33 (`token_in_launcher_or_argv: false`) | pass | The token is still visible inside the container to the CLI (it must be, to authenticate); it is useless outside the relay and expires with its grant. |
| ISO-06 | **The relay runs as its own process**; an orchestrator obtains grants over an owner-only (0600) control socket that checks the peer UID (Linux `SO_PEERCRED`), and holds no credential: `run_invocation` takes a `RelayClient` and no vault | `synchri/research/relay.py:529-655` (`_make_control_handler`, `RelayClient`); `synchri/research/cli.py:110-141` (`--control-socket`); `synchri/research/invocation.py:67-113` | `tests/test_research_relay_control.py` (10 tests, including a real `synchri research relay` subprocess serving a credential-free orchestrator, concurrent appends to one store); container probe driven through `RelayClient` | pass | The peer-UID check is Linux-only (elsewhere the 0600 mode is the control). The conformance runner keeps an in-process relay holding **dummy** credentials only; only `synchri research relay` loads a real credentials file. |

### 4.5 Event store (LOG-01, LOG-03, LOG-10)

| Req | Control | Source | Test | Result | Remaining limitations |
|---|---|---|---|---|---|
| LOG-01 | Append-only SQLite with UPDATE/DELETE-refusing triggers on events and blobs; every event hash-chained (`prev_hash`, `event_hash` over canonical JSON of all columns) | `synchri/research/store.py:31-92` (schema, `event_hash`), `:184-263` (`append`) | `tests/test_research_store.py::test_events_chain_from_zero_and_verify`, `::test_the_database_refuses_update_and_delete`, `::test_tampering_with_dropped_triggers_is_detected`, `::test_a_removed_event_breaks_the_chain` | pass | Triggers stop accidental writes, not an owner with file access; tamper *evidence* comes from the chain and an externally recorded chain head. Anchoring the head outside the store is a later phase (LOG-09 export). |
| LOG-02 | Content-addressed blobs; content re-hashed on read and in verification | `synchri/research/store.py` (`put_blob`, `blob`, `verify`) | `::test_blobs_are_content_addressed_and_verified`; `tests/test_research_reconstruct.py::test_an_altered_response_blob_fails_reconstruction` | pass | **No stdout size cap yet**, so `stdout_truncated` is always `false`; the relay caps requests at 32 MB and responses at 64 MB (truncation is flagged and fails the invocation). |
| §6.2 | Event catalog with required fields and blob references, validated on every append | `synchri/research/events.py` | `::test_catalog_refuses_unknown_types_missing_fields_and_dangling_blobs` | pass | — |
| LOG-03 | Explicit failure handling: any `sqlite3.Error` marks the store failed and raises `StoreWriteError`; every later write is refused; the relay halts on it; no bare `except: pass` in the write path | `synchri/research/store.py:150-164` (`_refuse_if_failed`, `_fail`), `:184-263` | `::test_a_write_failure_fails_closed_and_stays_failed`; relay halt test | pass | Trial-level abort as `infra` belongs to the trial loop (later phase); today the invocation raises. |
| LOG-01 | Safe for appends from two processes (relay and orchestrator): every append runs in `BEGIN IMMEDIATE` and reads the chain head inside the transaction; `synchronous=FULL` | `synchri/research/store.py:107-140`, `:184-263` | `tests/test_research_relay_control.py::test_the_relay_cli_serves_an_orchestrator_in_another_process` | pass | — |
| LOG-10 | Timestamps from `ids.utc_now()`; ordering by `seq` | `synchri/research/store.py` (`append`) | store tests | pass | — |

### 4.6 Conformance tests (PC-03: VT-28 to VT-33)

| VT | Implementation | Source | Result (Claude 2.1.309 / Codex 0.163.0) | Remaining limitations |
|---|---|---|---|---|
| VT-28 request shape | Exactly the allowed tool definitions; requested model equals the manifest's; no injected-context marker and no host path; CLI-added constants recorded | `synchri/research/conformance.py:137-159` | PASS / PASS. Recorded constants: Claude adds a billing-header line and "You are a Claude agent, built on Anthropic's Claude Agent SDK." (treatment constants, PC-08), no tools; Codex sends `request_user_input` only | Mock upstream: shows what the CLI *sends*; the served model comes from the mock. |
| VT-29 forged tool call | The mock answers with a call to a disabled tool (Claude `Bash`, Codex `exec_command`) whose command, `echo L""R`, would print `LR`; passes only if `LR` occurs nowhere in later requests or stdout and the CLI's refusal is present | `conformance.py:161-174` | PASS / PASS (refusal evidence recorded) | The spec phrases the forged call as reading a canary; the check uses an execution marker that only running the command could produce, which detects execution without placing a canary file in the container (§6, note 3). |
| VT-30 hidden retries | Mock returns 500; with CLI retries disabled the relay sees exactly one request and the invocation is classified failed (exit code, `is_error`, relay status) | `conformance.py:176-185` | PASS / PASS | — |
| VT-31 persistence | After the run, no file in the fresh home or `CODEX_HOME` holds the prompt canary; inventory recorded | `conformance.py:187-200`; `launcher.py` | PASS / PASS | — |
| VT-32 relay invariants | Unit-level (class A), see §4.2 | `tests/test_research_relay.py` | pass | — |
| VT-33 credential isolation | Credential absent from container argv, environment, launcher files, and run store; upstream received the relay's credential on every request; grants recorded by hash only | `conformance.py:202-232` | PASS / PASS | Live: same check with a real credential and endpoint. |
| Event-log reconstruction | `reconstruct()` over every conformance store | `synchri/research/reconstruct.py` | 4/4 invocations rebuilt in each store | Steps 3–7 not yet applicable. |
| Negative controls | Weakened flag sets must be caught | `tests/test_research_conformance.py:88-123` | Claude without `--bare`: VT-28 fails (`relay_refused_request`, injected markers). Codex with the shell re-enabled: `exec_command` appears in the tool list | — |

## 5. Classification

### 5.1 Completed controls (implemented and mechanically enforced)

1. Isolated package with an import boundary and a pre-Broker CLI dispatch (PRO-01, D5).
2. A recording relay that records before forwarding, refuses policy violations, and fails closed on store errors (D6, LOG-11, VT-32).
3. Provider-specific route allowlists and credential injection; client authorization stripped (D6, ISO-06).
4. Per-grant request limits that block hidden retries (BUD-05).
5. A per-invocation container: digest-pinned image, `--network none`, read-only root, non-root UID, no capabilities, `no-new-privileges`, limits, fresh tmpfs work and home dirs, exactly three validated mounts (ISO-05, ISO-02, CFG-08).
6. Binary hash pinning re-checked per invocation, and verified-version gating (CFG-03).
7. An exact environment allowlist with no `SYNCHRI_*` names and no credential values (ISO-01).
8. Pinned closed-book flag sets for both CLIs (ISO-03, PC-05).
9. A dedicated credentials file that never comes from session or provider config locations, held only in the relay process, with orchestrator access through an owner-only control socket and per-invocation hashed tokens (ISO-06).
10. An append-only, hash-chained store with triggers, catalog validation, content-addressed blobs, fail-closed writes, and multi-process appends (LOG-01, LOG-02 partly, LOG-03, LOG-10).
11. `synchri research verify` with chain verification and per-invocation reconstruction (LOG-08, partly).
12. Conformance runner and suite VT-28 to VT-31 and VT-33, with negative controls and an active container probe (PC-03).

### 5.2 Passing tests

- 96 research tests in the default suite, in `tests/test_research_{store,relay,relay_control,credentials,container,boundary,reconstruct}.py`.
- 5 opt-in container tests in `tests/test_research_conformance.py`.
- The full default suite: 845 passed, 5 skipped. The production tests are unchanged and green.
- CLI conformance for both pinned CLIs, all PASS, with verifiable evidence stores.

### 5.3 Awaiting live-provider verification

These controls are implemented and pass against the mock, but their claims
about real providers cannot be shown without a dedicated real credential,
which this phase forbids:

| Item | Why the mock cannot settle it | Spec reference |
|---|---|---|
| Relay HTTPS egress, certificate validation, and the CONNECT proxy against `api.anthropic.com` and `api.openai.com` | No real endpoint was contacted | D6, ISO-05, UNKNOWN-09 |
| Served-model attestation: the relay records the served model, but the requested-to-served comparison and abort rule need the real response's model strings | The mock serves a fixed `mock-served-model` | CFG-02, UNKNOWN-08 |
| Usage capture from real SSE and JSON bodies | Mock bodies imitate the formats | LOG-11, BUD-01 |
| VT-28 to VT-33 (AC-15) re-run against the live endpoints with a spend-limited study key | — | AC-15 |
| Adversarial closed-book probing of the real models | Models, not CLIs, choose to call tools | ISO-03, LT-03, LT-11 |
| No provider-side memory across invocations | Server-side state | PRO-11, LT-05, LT-06 |
| Whether the real APIs accept the forwarded requests with `Accept-Encoding: identity` and the relay's header set | — | D6 |
| macOS and Docker Desktop | Not available here | ISO-05, UNKNOWN-09 |

## 6. Design notes, interpretations, and conflicts

No conflict required weakening the specification. These points are recorded for review:

1. **PRO-01 and the package root.** Importing anything under `synchri` runs `synchri/__init__.py`, which imports `Broker`. Changing that would modify production code. The boundary is therefore enforced as follows: no research module imports a production module (AST test), and importing `synchri.research` loads nothing beyond what `import synchri` already loads (subprocess delta test). The research commands never *construct* a Broker or open `SYNCHRI_HOME` (tested).
2. **Appendix A reuse of `stream_events`.** That module imports the session stack, which would break PRO-01. `synchri/research/streams.py` is a minimal stream parser instead; the relay record is the primary source for model and usage anyway (LOG-11).
3. **VT-29 mechanism.** See §4.6. The check is at least as strict as the spec's wording, since it needs both the absence of execution and the presence of the CLI's refusal.
4. **ISO-01 literal conflict, fixed in code.** Codex's relay token was first passed in a `SYNCHRI_`-prefixed variable, which ISO-01 forbids. It is now `RESEARCH_RELAY_TOKEN` (the launcher's source variable is `RESEARCH_GRANT_TOKEN`), and the environment check refuses every `SYNCHRI_*` name with no exception.
5. **Relay data socket mode 0666.** The container's UID 10001 must be able to connect. Any local user can therefore connect, but only with a valid unexpired `srt_` token for one grant. The control socket is 0600 with a peer-UID check.
6. **ISO-07 canary planting** (reference store, run store, operator environment, provider home) is not implemented in this phase. The scanning half exists: `run_invocation(canaries=…)` scans stdout, stderr, and recorded requests, and emits `isolation.violation_detected`. Planting belongs with the reference store and trial loop.
7. **Orchestration through `run_invocation` is exercised only in the opt-in Docker suite**, since it needs a daemon and pinned binaries. Its unit-level parts (spec validation, argv, environment, launcher, relay, reconstruction) are in the default suite.
8. **Fail-closed scope.** A store failure halts the relay and raises in the orchestrator. Classifying the trial as `infra` and halting the run are later-phase trial-loop behavior (LOG-03).

## 7. Reproducing

```sh
pip install -e ".[dev]" && python -m pytest -q                 # default suite
mkdir -p /tmp/no-proxy-ca && docker build --build-context proxyca=/tmp/no-proxy-ca \
  -t synchri-research-runner:1 docs/research/container
synchri research conformance --runtime claude_code --cli-root <dir> --cli-relpath claude \
  --cli-sha256 <sha> --cli-version 2.1.309 --image <sha256:…> --model <full-id> --run-dir <new-dir>
synchri research verify <new-dir>
# Relay as its own process (dedicated, owner-only credentials file):
synchri research relay --run-dir <new-dir> --credentials /abs/path/creds.json \
  --socket /tmp/srr-x/relay.sock --control-socket /tmp/srr-x/control.sock
```
