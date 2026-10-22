# Provider feasibility investigation: Claude Code and Codex for Research Mode

| | |
|---|---|
| **Governing specification** | `docs/research/RESEARCH_MODE_SPEC.md` (cited as "Spec §n", requirement IDs as in the spec) |
| **Synchri commit at investigation** | `c5dd5bb` (branch `claude/synchri-source-audit-jv05fy`) |
| **Date** | 2026-10-22 |
| **Status** | Investigation complete for the questions answerable on this host. **No live model call was made, no credential was used or copied, and no experimental data was collected.** Live-model behavior remains unverified (§10). |
| **Specification changes** | None applied. Proposed changes are listed for review in §8. |

---

## Summary

**Verdict.** Both CLIs can be configured into a closed-book, tool-free,
minimal-context invocation, and both can be confined consistently by OS users or
containers. Neither, used alone, satisfies the specification's logging and
attestation requirements. The minimum defensible architecture (§7) is:

- a per-invocation container;
- a **recording relay** between each CLI and its provider, which holds the real
  credentials;
- the flag sets in §7.3.

**Findings that change the plan:**

1. **Codex's `--sandbox read-only` is not a read boundary.** A forged shell
   call read a protected canary file outside the working directory, and its
   contents were sent to the API. Synchri's Planning Mode relies on this mode for
   Codex (`modes.py:566`). Disabling Codex's shell features is what actually
   blocks this: forged calls were then rejected as `unsupported call` (§4.1).
2. **Neither CLI's output identifies the served model reliably.**
   - *Claude:* the stream's `init.model` is the *requested* model. The
     server-reported model appears only in `assistant.message.model`, and Synchri
     currently records `init.model`.
   - *Codex:* its `--json` stream contains no model field at all (§4.2).
3. **Both CLIs add content and requests that their output does not show.**
   - *Content:* identity lines; working-directory, date, and OS context; skill
     lists; tool definitions.
   - *Requests:* Codex retried a failing request 29 times without a single
     retry event, and Claude reported a failed run with `subtype: "success"`
     (§4.7).
   - *Consequence:* complete input and usage records need capture at the network
     boundary, not from CLI stdout.
4. **Provider credentials are visible to tool subprocesses** in both CLIs.
   Synchri's current launcher also passes the full parent environment. On this
   host, that means AWS keys, GitHub and cloud tokens, and the session's
   messaging token (§4.5).
5. **Sampling cannot be controlled.** Neither CLI exposes temperature, top-p, or
   seed. Model, effort, output-token cap (Claude), and retries can be controlled
   (§4.3).
6. **Claude's minimal-context mode (`--bare`) requires API-key authentication.**
   Subscription or OAuth login always injects working-directory, date, and
   model-identity context (§4.4).
7. **OS-user and container isolation worked identically for both CLIs** with
   every CLI-level protection switched off (§4.6).

---

## 1. Scope and constraints

**Investigated:** Claude Code CLI and OpenAI Codex CLI, against questions 1–7 of
the request.

**Constraints honored:**

- Only harmless canary strings (random tokens) were used.
- Disposable directories, OS users, and containers were used and then removed
  (§12).
- No reference answers, credentials, or research secrets were placed anywhere.
- Production code and Synchri workflows were not modified.

**Not done, and why:**

- **Live model calls.** This host's Claude CLI authenticates through the
  *hosting session itself*: an OAuth token supplied by session-scoped variables
  and files, with no credential file. A live child invocation would require
  either copying that credential, which the request forbids, or inheriting the
  full session environment. That environment includes a messaging socket able
  to post into the hosting session, the very contamination Research Mode must
  exclude. There are no OpenAI credentials on this host.
- **Live tests are therefore specified** for a dedicated pilot host (§10).
- **macOS was not tested.**

---

## 2. Method and evidence labels

Every claim carries one of these labels:

| Label | Meaning |
|---|---|
| **D** | *Documented*: stated in the provider's published documentation or the CLI's own `--help` text. Sources are cited. |
| **O-H** | *Observed, harness level*: the real CLI binary run against a local **mock provider API** that records each request and returns scripted replies. This shows what the client sends, executes, persists, and retries. It shows nothing about model behavior or server-side behavior. |
| **O-OS** | *Observed, OS level*: kernel-enforced behavior (file permissions, namespaces, firewall), with the CLI running as an isolated user or inside a container. |
| **U** | *Unverified*: an assumption, or a behavior that needs a live provider, a server-side guarantee, or another platform. |

### Techniques

1. **Help text and documentation.** `claude --help`, `codex --help`,
   `codex exec --help`, `codex features list`; plus the Claude Code CLI
   reference and the Codex command reference (§11).
2. **Request capture.** Both CLIs accept a custom API base URL (`ANTHROPIC_BASE_URL`;
   a Codex `model_providers.<id>.base_url`). A mock server logged every request
   body with authorization headers redacted.
3. **Forged tool calls.** The mock answered the first model request with a tool
   call the configuration should forbid, such as reading a canary file outside
   the working directory. This tests whether the *client* enforces the
   restriction, which no prompt can affect.
4. **Fault injection.** The mock returned HTTP 500 or 429 to measure hidden
   retries.
5. **Carry-over tests.** Two invocations shared one home directory; the first
   carried a unique token, and the second request was searched for it.
6. **Isolation tests.** Disposable OS users with permission-protected canaries,
   per-user `iptables` egress rules, and a locked-down Docker container.

All scripts are in `docs/research/feasibility-harness/`.

---

## 3. Environment and versions

| Item | Value |
|---|---|
| Host | Ubuntu 24.04.5 LTS, Linux 6.18.44 x86_64, running as root (a hosted Claude Code cloud session) |
| Claude Code | `2.1.309 (Claude Code)`, native binary `/opt/claude-code/bin/claude`, sha256 `4d704a8aecbe038affa3000f498ae17c1b4f753add8facb8764120affc99b658` |
| Codex CLI | `codex-cli 0.163.0` (npm `@openai/codex@0.163.0`, installed into a disposable prefix), static-pie musl binary sha256 `399ff69c431c066e40de318fba45c2b07f45a76b19939d247ee4828fc70c4215` |
| Node.js / npm | v22.22.0 / 10.9.4 |
| Docker | 29.9.1 (daemon started for the test, then stopped); cgroup v1; seccomp |
| Container base image | `mirror.gcr.io/library/debian@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8` (bookworm-slim) plus `python3` 3.11.2. Docker Hub returned HTTP 429, so the Google mirror was used. |
| Network | All outbound HTTPS goes through a host agent proxy that permits `CONNECT` only (plain-HTTP apt failed with 405) |
| Synchri test suite | Not re-run; no code changed (baseline 749 passing per the audit) |

**Redaction.** No credential value was printed or stored. Environment
variables are reported by name only. Mock authorization headers were replaced
with `<redacted>` before logging. The only key-like strings in the evidence are
fixed dummies (`sk-ant-mock-not-a-real-key`, `mock-not-a-real-key`).

---

## 4. Findings by question

### 4.1 Q1 — Disabling file, shell, network, and other tools

#### Claude Code 2.1.309

| Capability | Evidence | Result |
|---|---|---|
| Remove all built-in tools | **D** `--tools ""` "disable all tools" (help, CLI reference). **O-H** request `tools: []`, `init.tools: []` in every configuration using it. | ✓ |
| MCP tools | **D** "`--tools` doesn't affect MCP tools; to deny those too, use `--disallowedTools "mcp__*"`" and `--strict-mcp-config`. **O-H** `mcp_servers: []` with a fresh home. | ✓ with `--strict-mcp-config --disallowedTools "*"` |
| Client refuses a forged call to a disabled tool | **O-H** Forged `Bash` (`cat <canary>`) and `Read` (`<canary>`) with tools disabled returned `No such tool available: Bash. Bash is disabled for this session, in subagents as well as here.` The canary never appeared in any later request. | ✓ (client-enforced) |
| Default `-p` without tool flags | **O-H** 12 tools sent (Agent, Bash, Edit, Read, Write, Skill, ToolSearch, Workflow, …). Forged `cat` outside the working directory was "blocked. For security …"; `Read` outside it was a "permission request" and denied; `env` "requires approval". | Not closed-book: relies on the permission engine |
| `--restricted` | **D** removes command-running tools and WebFetch, confines file tools to working directories, ignores user/project settings. **O-H** forged `Read` outside the working directory refused ("is outside …"). | ✓ defense in depth |
| Bypass refused as root | **O-H** `--dangerously-skip-permissions cannot be used with root/sudo privileges` | Safety property; agents must run non-root anyway |
| Web search / fetch | **O-H** absent from the request when tools are disabled. WebSearch and WebFetch appear in the default `init` tool list. Server-side behavior when no tool is declared: **U**. | ✓ at request level |
| CLI quirk | **O-H** `--tools` is variadic and consumes a trailing positional prompt (`Error: Input must be provided …`). The prompt must go on stdin or be followed by another option. This matches Synchri's own note at `modes.py:515-518`. | Use stdin |

#### Codex CLI 0.163.0

| Capability | Evidence | Result |
|---|---|---|
| Dedicated "no tools" flag | **D** none documented. The documentation points to `--sandbox read-only` and `--disable <feature>`. | ✗ none |
| Default tools | **O-H** `exec_command`, `write_stdin`, `request_user_input`, `view_image`, `multi_agent_v1`, `get_goal`, `create_goal`, `update_goal`, `web_search` (`{"type":"web_search","external_web_access":false}`, i.e. cached server-side search) | — |
| Remove tools by feature flags | **O-H** `--disable shell_tool --disable unified_exec --disable multi_agent --disable view_image --disable goals --disable apps --disable plugins --disable hooks --disable image_generation --disable browser_use --disable computer_use --disable tool_suggest --disable skill_search --disable sleep_tool -c web_search="disabled"` → tools `["request_user_input"]` only | Nearly ✓ |
| Remaining tool `request_user_input` | **O-H** not removable by any flag found. A forged call returned `request_user_input is unavailable in Default mode`; nothing executed. | Offered but inert (B2, §6) |
| Client refuses forged shell calls once disabled | **O-H** forged `exec_command` and `shell` → `unsupported call: exec_command` / `unsupported call: shell`; canary absent from traffic | ✓ (client-enforced) |
| **`--sandbox read-only` blocks reads** | **O-H** with the shell enabled, a forged `exec_command` `cat <canary>` (outside the working directory) **succeeded, and the canary content was sent to the API**. The model-visible permission profile states `<entry access="read"><special>:root</special></entry>`, i.e. read access to `/`. | **✗ Not a read boundary** |
| Sandbox network | **O-H** sandboxed command: `Temporary failure in name resolution`. Escalation request rejected: "approval policy is Never". | Network blocked in sandbox |
| Feature-name stability | **D/O-H** `codex features list` labels many flags "under development", "removed", or "deprecated" | **U** across versions: re-verify per pinned version |

### 4.2 Q2 — Identifying and recording the served model

| Question | Claude Code | Codex |
|---|---|---|
| Explicit model selection | **D** `--model <alias or full name>`. **O-H** request `model` equals the flag value. | **D** `-m, --model`. **O-H** request `model` equals the flag value. |
| Aliases | **O-H** resolved *inside the CLI*: `sonnet`→`claude-sonnet-5-5`, `opus`→`claude-opus-5-5`, `haiku`→`claude-haiku-5-5`, `fable`→`claude-fable-5-1`. The mapping is version-dependent. | No alias behavior observed. An unknown name produced an `error` item ("Model metadata … not found. Defaulting to fallback metadata"), so Codex uses a local model catalog (`codex debug models`). |
| What the CLI output calls "the model" | **O-H** `system/init.model` = the **requested** model. `assistant.message.model` = the value **the server returned** (the mock returned `mock-served-model-XYZ` and the CLI passed it through). `result.modelUsage` is keyed by the requested name. | **O-H** no event in `--json` output (`thread.started`, `turn.started`, `item.completed`, `turn.completed`, `turn.failed`, `error`) contains a model field. The server's response `model` was not surfaced. |
| Can the served model be recorded? | Yes, as a **provider-reported** field from the stream (`assistant.message.model`) or, more completely, at a relay. Synchri's current parser (`stream_events.py:172-180`) records the requested value. | **Only at a relay**, from the Responses stream's `response.model`. CLI output alone records only what was requested. |
| Snapshot exactness and server routing | **U** (needs a live provider; and no client can see routing behind an ID) | **U** |

### 4.3 Q3 — Controllable model, sampling, context, and resource settings

| Setting | Claude Code | Codex |
|---|---|---|
| Model | ✓ `--model` (pin the full ID, not an alias) **O-H** | ✓ `-m` **O-H** |
| Effort / reasoning | ✓ `--effort low…max` → `output_config.effort` **O-H**. The default depends on the model (`medium`; `high` for `fable`) **O-H**. | ✓ `-c model_reasoning_effort="low"` → `reasoning.effort` **O-H** |
| Thinking / reasoning visibility | Request always carries `thinking: {"type":"adaptive","display":"updates"}`. `MAX_THINKING_TOKENS=0` and `--settings '{"alwaysThinkingEnabled":false}'` changed nothing **O-H**. Raw thinking text is not exposed (summarized "updates"). | `-c model_reasoning_summary="none"` removes the summary request **O-H**. The request includes `reasoning.encrypted_content`, which is opaque. |
| Temperature / top-p / seed | ✗ No flag (**D** help and reference). Absent from every request (provider default) **O-H**. | ✗ `temperature`, `top_p`, `model_temperature` rejected by `--strict-config` as unknown fields **O-H**; absent from requests |
| Output-token cap | ✓ `CLAUDE_CODE_MAX_OUTPUT_TOKENS=2048` → `max_tokens: 2048` **O-H**. Default 128000 (64000 for `fable`). | ✗ `model_max_output_tokens` rejected **O-H**. No request-level cap found (`exec_command.max_output_tokens` is a tool parameter). |
| Context / compaction | **D** `--autocompact`. Not exercised. | `model_context_window`, `model_auto_compact_token_limit` accepted **O-H**; effect on behavior **U** |
| Turn cap | **D** `--max-turns` | None found (**U**) |
| Cost cap | **D** `--max-budget-usd` (a client-side *estimate*; "spend can pass the cap") | None found |
| CLI-internal retries | ✓ `CLAUDE_CODE_MAX_RETRIES=0` → exactly 1 request on a persistent 500 **O-H**. `ANTHROPIC_MAX_RETRIES=0` had no effect. | ✓ `-c model_providers.<id>.request_max_retries=0 -c model_providers.<id>.stream_max_retries=0` → exactly 1 request **O-H** |
| System or base prompt | `--system-prompt` replaces the default, **but the CLI still prepends** a billing-header block (`cc_version=2.1.309…`) and the line "You are a Claude agent, built on Anthropic's Claude Agent SDK." **O-H** | `-c model_instructions_file=<file>` replaced the 17k-character base prompt with the file's text **O-H**. `developer_instructions` appends **O-H**. `base_instructions` rejected. |
| Structured final output | **D** `--json-schema` (not exercised) | **D** `--output-schema` (not exercised) |
| Verbosity | — | `model_verbosity` accepted; no request change with an unknown model **U** |

### 4.4 Q4 — Inheritance across invocations

| Channel | Claude Code | Codex |
|---|---|---|
| Transcript persistence (default) | **O-H** full transcript in `$HOME/.claude/projects/<cwd-slug>/<session>.jsonl` | **O-H** `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl`, plus `thread_history_1.sqlite` and `state_5.sqlite` write-ahead logs |
| Persistence disabled | **D** `--no-session-persistence`. **O-H** no file contained the token. | **D** `--ephemeral`. **O-H** no file contained the token. But `state_5`, `logs_2`, `memories_1`, `goals_1`, and `queue_1` SQLite files, `installation_id`, and `shell_snapshots/` were still created, with no prompt content found in them. |
| Carry-over into a fresh, non-resumed invocation | **O-H** none: the token from invocation 1 was absent from invocation 2's request (shared home, different working directory). Same working directory not tested (**U**). | **O-H** none (shared home) |
| User-level instruction files | **O-H** default: `~/.claude/CLAUDE.md` content **sent**. `--bare` or `--safe-mode`: not sent. | **O-H** `$CODEX_HOME/AGENTS.md` **sent even with `--ignore-user-config` and `model_instructions_file`**. Only control: a `CODEX_HOME` with no such file. |
| Project instruction files (in the working directory) | **O-H** default: `CLAUDE.md` sent. `--bare` or `--safe-mode`: not sent. | **O-H** default: `AGENTS.md` sent. `-c project_doc_max_bytes=0`: not sent. |
| Injected environment context | **O-H** without `--bare`, a message containing: working-directory path, "Is a git repository", platform, OS version, the model's own name and ID, knowledge cutoff, and today's date. This persists with `--safe-mode`, `--restricted`, and `--system-prompt`. With `--bare` and the default prompt: working-directory path plus date. **`--bare --system-prompt`: none.** | **O-H** `<environment_context>` (working-directory path, shell, date, timezone, filesystem permission profile), a skills list with absolute `CODEX_HOME` paths, multi-agent and collaboration instructions. Suppressed by `include_environment_context=false`, `include_permissions_instructions=false`, `include_collaboration_mode_instructions=false`, `include_apps_instructions=false`, and disabling the five bundled skills via `skills.config=[{path=…,enabled=false},…]`. |
| Auto-memory | **O-H** the default system prompt describes a persistent memory directory under `$HOME`. Absent with `--bare` or `--safe-mode`. Needs write tools, so inert when tools are off. | `memories` feature exists (stable, default `false`) **D/O-H**. Server-side memory for ChatGPT-login accounts **U**. |
| Working-directory contents | **O-H** with tools off, nothing in the working directory is read. With tools on, it is reachable (and so is everything else the OS permits, §4.5). | Same |
| Identifiers sent to the provider | **O-H** `metadata.user_id` = {`device_id` (per `$HOME`), `session_id`}; header `X-Claude-Code-Session-Id` | **O-H** `client_metadata` (session, window, turn IDs), `prompt_cache_key` = session ID, `installation_id` file, `store: false` |
| Server-side state (prompt caching, account features) | `cache_control: ephemeral` blocks **O-H**; effect on outputs **U** | `prompt_cache_key` **O-H**; effect **U** |

### 4.5 Q5 — Do environment scrubbing and empty working directories protect research state?

1. **Synchri's current launcher passes everything.** `AgentCommand` merges
   `os.environ` (`agent_command.py:477-480`), and the managed runner adds
   `SYNCHRI_*` on top. **O-H** on this host a child received 171 variables,
   including `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `GH_TOKEN`,
   `GITHUB_TOKEN`, `CLOUDSDK_AUTH_ACCESS_TOKEN`, `CLAUDE_CODE_MESSAGING_TOKEN`,
   `CLAUDE_SESSION_INGRESS_TOKEN_FILE`, and all four `SYNCHRI_*` variables.
   Names only were inspected.
2. **`env -i` scrubbing works at the process level** for both CLIs: they run
   correctly with only `PATH`, `HOME`, `TMPDIR`, a base URL, and a key (**O-H**).
   The parent canary variable did not appear in Claude's prompt (**O-H**).
3. **Both CLIs expose their provider credential to tool subprocesses.**
   - *Claude:* in the container test, the Bash tool's environment contained
     `ANTHROPIC_API_KEY`, plus CLI-generated `CLAUDE_CODE_MESSAGING_SOCKET` /
     `_TOKEN` / `SESSION_ID` variables (**O-H**).
   - *Codex:* sandboxed commands saw the provider key variable (`MOCK_API_KEY`)
     and the parent canary variable. As login shells they also loaded host
     `/etc/profile` variables (**O-H**). `shell_environment_policy.inherit="none"`
     removed everything, including `PATH`, which leaves the shell unusable
     (**O-H**).
4. **Empty working directories do not stop reads of absolute paths.**
   - *Codex `read-only`:* a canary outside the working directory was read and
     sent (**O-H**, §4.1).
   - *Run as an isolated OS user with protections bypassed:* both CLIs read a
     world-readable control file outside the working directory (**O-OS**).
   - *Claude's default permission engine* blocked out-of-directory reads in `-p`
     mode. That is a CLI policy, not an OS boundary (**O-H**).
5. **Conclusion.** Scrubbing and empty working directories are *necessary*:
   they stop credential leaks and accidental context. They are *not
   sufficient*. Research state is protected only by (a) client-enforced tool
   removal (**O-H**, version-dependent) or (b) OS or container isolation
   (**O-OS**). The provider credential must also not live in the CLI's
   environment at all (§7.2).

### 4.6 Q6 — OS-user and container isolation for both providers

**OS users (O-OS).**

- *Setup:* two disposable system users (`rsch-a`, `rsch-b`). The protected
  store was `root:root 0700`, each slot's home was owner-only `0700`, and a
  world-readable control file sat outside every working directory.
- *Probe:* both CLIs ran as `rsch-a` with tools enabled, Claude with
  `--dangerously-skip-permissions` and Codex with `-s danger-full-access`. A
  forged shell call tried to read every path.

| Path | Claude | Codex |
|---|---|---|
| `/srv/rfeas/protected/run-store-canary.txt` (root 0700) | Permission denied | Permission denied |
| `/srv/rfeas/slot-b/home/slot-b-canary.txt` (other slot) | Permission denied | Permission denied |
| `/tmp/claude-0` (operator scratch) | Permission denied | Permission denied |
| `/srv/rfeas/public-control.txt` (world-readable) | **read** (positive control) | **read** (positive control) |

Note that the repository checkout `/home/user/synchri` was readable by the
unprivileged user (**O-OS**). OS isolation protects only what is
permissioned, so references and run stores must live in owner-only
directories.

**Per-user egress (O-OS).** `iptables -m owner --uid-owner` rules let `rsch-a`
reach exactly one loopback endpoint. Another local port and the host proxy were
refused, and root was unaffected. The rules were removed afterwards.

**Container (O-OS).**

- *Flags:* `docker run --network none --read-only --user 10001:10001
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 --memory 2g`,
  with tmpfs `/work` and `/home/slot`, and the CLI binaries mounted read-only.
- *Probe:* the mock ran inside the container, and both CLIs ran with tools
  enabled and protections bypassed.
- *Result:* host paths (`/srv/rfeas/protected`, `/home/user/synchri`,
  `/tmp/claude-0`) did not exist, `/root` was denied, and outbound connections
  failed with `Network is unreachable`. The same image and flags worked for
  both CLIs.

**Consistency.** The same kernel mechanisms confined both providers, with no
provider-specific configuration.

**Not established (U):**

- container egress to real provider APIs through an allowlisting proxy;
- Codex's own Linux sandbox nested inside a container (the container was the
  boundary; Codex ran with `danger-full-access` inside it);
- macOS (the desktop app's platform);
- rootless or user-namespaced Docker.

### 4.7 Q7 — Complete input, output, failure, and usage records

| Record | Claude Code | Codex | Implication |
|---|---|---|---|
| Exact model input | CLI output shows none of: the billing header, the "Claude agent" identity line, the injected environment message, tool definitions, or effort and thinking parameters (**O-H**, all only in the captured request) | Base prompt, permissions block, skills, and environment context appear only in the request (**O-H**). `codex debug prompt-input` renders input items offline (**O-H**) but not tool definitions. | **Exact input exists only at the network boundary.** A relay is required for LOG-02 to mean "what the model received". |
| Output | `stream-json` carries assistant messages, a `result` text, and summarized thinking (**O-H**) | `--json` carries `agent_message` items and opaque encrypted reasoning (**O-H**) | Keep raw stdout and the relay's raw response stream |
| Hidden retries | Two injected 500s → 3 requests, and the stream showed 2 retry events (**O-H**) | Two injected 500s → 3 requests, **no retry event**. A persistent 500 produced **30 requests over 25 s** against 7 `error` events. A 429 failed after 1 request ("exceeded retry limit") (**O-H**). | Request counts and cost cannot come from CLI output. Disable CLI retries (§4.3) and count at the relay. |
| Failure reporting | After exhausted retries: `result.subtype: "success"`, `is_error: true`, message "Repeated 529 Overloaded errors" although the mock sent 500 (**O-H**) | `turn.failed` plus `error` items; exit code 1 (**O-H**) | Use exit code, `is_error`, and relay status codes; never `subtype` alone |
| Usage | input, output, cache-read and cache-creation tokens, thinking tokens, server-tool counts, client-side `total_cost_usd` (an estimate, **D**) (**O-H**) | input, cached, cache-write, output, and reasoning tokens; no cost (**O-H**) | Provider-reported in both; the relay records the provider's own usage block verbatim |
| Served model | `assistant.message.model` (provider-reported) | Not exposed | Relay records the response `model` |

---

## 5. Capability matrix

✓ = available and observed. ◐ = partial or with caveat. ✗ = not available.
U = unverified. The tag after each mark is the strongest evidence.

| # | Capability | Spec req. | Claude Code 2.1.309 | Codex 0.163.0 |
|---|---|---|---|---|
| 1 | Remove all executable tools | ISO-03 | ✓ O-H (`--tools "" --disallowedTools "*" --strict-mcp-config`) | ✓ O-H (feature disables) |
| 2 | Zero tool definitions in request | ISO-03 | ✓ O-H | ◐ O-H (`request_user_input` remains, inert) |
| 3 | Client refuses forged tool calls | ISO-03 | ✓ O-H | ✓ O-H (shell disabled) |
| 4 | Vendor sandbox blocks out-of-directory reads | ISO-05 | ◐ O-H (permission engine in `-p`; `--restricted`) | **✗ O-H** (`read-only` reads `/`) |
| 5 | No web search or fetch in request | ISO-08 | ✓ O-H; server U | ✓ O-H (`web_search="disabled"`); server U |
| 6 | Explicit model selection | CFG-02 | ✓ O-H | ✓ O-H |
| 7 | Served model in CLI output | CFG-02 | ◐ O-H (`assistant.message.model`; `init.model` is the request) | ✗ O-H |
| 8 | Served model capturable at relay | CFG-02 | ✓ O-H (custom base URL honored) | ✓ O-H (custom provider honored) |
| 9 | Temperature, top-p, seed | CFG-04 | ✗ O-H | ✗ O-H |
| 10 | Effort / reasoning level | CFG-05 | ✓ O-H | ✓ O-H |
| 11 | Disable thinking / reasoning summaries | CFG-05 | ✗ O-H (always adaptive) | ✓ O-H (summary off; encrypted reasoning remains) |
| 12 | Output-token cap | BUD-02 | ✓ O-H (env var) | ✗ O-H |
| 13 | Disable CLI-internal retries | BUD-05 | ✓ O-H | ✓ O-H |
| 14 | Replace system or base prompt | CFG-06 | ◐ O-H (fixed prefix lines remain) | ✓ O-H (`model_instructions_file`) |
| 15 | Suppress injected cwd, date, and OS context | CFG-06, ISO-09 | ◐ O-H (only `--bare`, which needs an API key) | ✓ O-H (config keys) |
| 16 | Ignore user and project instruction files | CFG-08 | ✓ O-H (`--bare` or `--safe-mode`) | ◐ O-H (project: `project_doc_max_bytes=0`; user `AGENTS.md`: only via a clean `CODEX_HOME`) |
| 17 | No transcript persistence | PRO-11 | ✓ O-H | ◐ O-H (no transcript; state DBs still written) |
| 18 | No carry-over into a fresh invocation | PRO-11 | ✓ O-H (client); server U | ✓ O-H (client); server U |
| 19 | Runs with `env -i` | ISO-01 | ✓ O-H | ✓ O-H |
| 20 | Provider credential hidden from tools | ISO-06 | ✗ O-H (key visible to Bash) | ◐ O-H (visible by default; `inherit="none"` hides all) |
| 21 | OS-user isolation | ISO-05 | ✓ O-OS | ✓ O-OS |
| 22 | Container isolation | ISO-05 | ✓ O-OS | ✓ O-OS |
| 23 | Per-identity egress allowlist | ISO-08 | ✓ O-OS (iptables owner) | ✓ O-OS |
| 24 | Retries visible in output | LOG-02, BUD-01 | ✓ O-H | ✗ O-H |
| 25 | Failure status reported accurately | LOG-06 | ◐ O-H (`subtype` misleading) | ✓ O-H |
| 26 | Usage tokens reported | BUD-01 | ✓ O-H | ✓ O-H (no cost) |
| 27 | Non-root execution compatible | ISO-05 | ✓ O-OS (bypass needs non-root) | ✓ O-OS |
| 28 | Minimal context with subscription login | CFG-06 | ✗ O-H (`--bare` is API-key only, **D**) | U (ChatGPT login not tested) |
| 29 | Live-model behavior under these flags | all LT | U | U |

---

## 6. Blockers and viable alternatives

None of these blockers is resolved by weakening the specification. Each has an
alternative that keeps the requirement intact.

| # | Blocker | Affects | Viable alternative |
|---|---|---|---|
| **B1** | Claude's only minimal-context mode (`--bare`) accepts only `ANTHROPIC_API_KEY` or an `apiKeyHelper`, never OAuth or subscription. Without it, the working-directory path, date, OS, and model identity are injected. | CFG-06, ISO-09 | Use a **dedicated research API key** (Console), held by the relay (§7.2). If subscription auth must be used, the injected context becomes a documented treatment constant: the container gives a neutral, identical `/work` path, and the date is recorded per invocation. That is weaker and needs reviewer acceptance. |
| **B2** | Codex always offers `request_user_input`. | ISO-03 (zero tools) | It is inert in `exec` mode (**O-H**). Either (a) accept it, record its definition as part of the treatment, and log any call as a protocol event, or (b) have the relay remove that one definition before forwarding. Option (b) changes what the model receives and needs review. |
| **B3** | Codex reports no served model, and Claude's `init.model` is only the request. | CFG-02 | Recording relay: take `model` from each provider response. Treat the CLI's view as "requested". |
| **B4** | No sampling control in either CLI. | CFG-04 | Unchanged spec position: record `provider_default` and use replicates. If the study needs fixed sampling, the alternative is a direct API adapter (no CLI). That is a design change, listed as option PC-14 for review, not a recommendation to weaken anything. |
| **B5** | Codex `--sandbox read-only` reads the whole filesystem. | ISO-03, ISO-05 | Never rely on it. Disable shell features (client-level) **and** use container or OS isolation (kernel-level). Also flag Planning Mode's reliance on it (`modes.py:566`, `planning.py:12-32`) as a separate production issue. |
| **B6** | Provider credentials are readable by tool subprocesses (both CLIs). | ISO-06 | The relay holds the real key and the CLI gets a dummy one. Both CLIs accepted a dummy key with a custom base URL (**O-H**). Relay-side auth injection against real providers: **U** until built and live-tested. |
| **B7** | Hidden context and hidden retries make CLI-side logging incomplete. | LOG-02, LOG-05, BUD-01 | Recording relay plus disabled CLI retries (§4.3). |
| **B8** | No live-model evidence is obtainable on this host. | All LT tests | Dedicated pilot host with dedicated, spend-limited research keys for both providers (§10). |
| **B9** | Feature flags and config keys are version-unstable. | CFG-03 | Pin exact versions and binary hashes. Re-run the harness conformance checks (§8, PC-03) at every version change, as preflight. |
| **B10** | Codex writes state databases even with `--ephemeral`. | PRO-11 | A fresh, empty, per-invocation `CODEX_HOME` (tmpfs in the container), discarded afterwards. Archive it first if the study wants it as evidence. |

---

## 7. Recommended minimum defensible isolation architecture

### 7.1 Shape

```
 orchestrator (trusted; owns run store; never reads references during generation)
     │ creates one container per invocation, passes prompt on stdin
     ▼
 ┌──────────────── invocation container (untrusted) ────────────────┐
 │ image pinned by digest; rootfs read-only; uid 10001; no caps;     │
 │ no-new-privileges; pids/memory limits; wall-clock timeout         │
 │ tmpfs /work (empty, neutral fixed path) · tmpfs /home/slot (empty)│
 │ provider CLI binary mounted read-only from a hashed, pinned path  │
 │ env: PATH, HOME, TMPDIR, base URL → relay, DUMMY key, retries=0   │
 │ network: internal-only; the single reachable endpoint is the relay│
 └──────────────────────────────┬───────────────────────────────────┘
                                │ HTTP (internal network)
                                ▼
 recording relay (trusted; holds the real API keys)
   • appends exact request bytes, response stream bytes, status codes,
     timing, response `model`, and usage to the run store (content-addressed)
   • enforces invariants: model ∈ manifest, no tool definitions beyond the
     manifest's allowlist, max-token value, one request per invocation
   • forwards to api.anthropic.com / api.openai.com over TLS
```

This is the spec's **Tier 2** applied from the pilot onward. On this host, a
container per invocation needed only a stock Docker daemon and one small image
(§4.6). Tier 1 rests entirely on client-side enforcement, and finding 1 shows
how a vendor "restriction" can be weaker than its name.

### 7.2 Why each element is the minimum

| Element | Removes | Evidence |
|---|---|---|
| Container (or distinct OS user) | Reads of the run store, references, other slots, and host files, whatever the CLI does | O-OS §4.6 |
| `env -i` allowlist | Host credentials and Synchri variables | O-H §4.5 |
| Dummy key plus relay-held key | Provider credential exposure to tools | O-H §4.5; relay injection U |
| Recording relay | Hidden context, hidden retries, unknown served model | O-H §4.2, §4.7 |
| CLI tool disables | The model's ability to act at all (defense in depth inside the container) | O-H §4.1 |
| Fresh tmpfs homes, persistence off | Carry-over and instruction-file leakage | O-H §4.4 |
| Neutral fixed `/work` path | Run identifiers leaking via injected working-directory paths | O-H §4.4 |

### 7.3 Invocation flag sets (observed to produce the minimal request against the mock)

**Claude Code 2.1.309** (prompt on stdin):

```
env -i PATH=/usr/bin:/bin HOME=/home/slot TMPDIR=/work \
  ANTHROPIC_BASE_URL=http://<relay>:<port> ANTHROPIC_API_KEY=<dummy> \
  CLAUDE_CODE_MAX_RETRIES=0 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
  [CLAUDE_CODE_MAX_OUTPUT_TOKENS=<n>] \
claude -p --bare --no-session-persistence --output-format stream-json --verbose \
  --model <full-model-id> --effort <level> --system-prompt-file <frozen-file> \
  --strict-mcp-config --tools "" --disallowedTools "*"
```

Resulting request (**O-H**):

- `system` = [billing header, "You are a Claude agent, built on Anthropic's
  Claude Agent SDK.", the frozen text]
- `messages` = [the prompt]
- `tools` = []
- `thinking` = adaptive (not controllable)
- no temperature or top-p

**Codex CLI 0.163.0** (prompt on stdin; skill paths depend on the fixed `CODEX_HOME`):

```
env -i PATH=/usr/bin:/bin HOME=/home/slot CODEX_HOME=/home/slot/codexhome TMPDIR=/work \
  RELAY_KEY=<dummy> \
codex exec --json --ephemeral --skip-git-repo-check --strict-config \
  --ignore-user-config --ignore-rules -s read-only -m <model-id> \
  -c model_provider="research" -c model_providers.research.name="research" \
  -c model_providers.research.base_url="http://<relay>:<port>/v1" \
  -c model_providers.research.env_key="RELAY_KEY" -c model_providers.research.wire_api="responses" \
  -c model_providers.research.request_max_retries=0 -c model_providers.research.stream_max_retries=0 \
  -c model_instructions_file="<frozen-file>" -c project_doc_max_bytes=0 \
  -c include_environment_context=false -c include_permissions_instructions=false \
  -c include_apps_instructions=false -c include_collaboration_mode_instructions=false \
  -c 'skills.config=[{path="/home/slot/codexhome/skills/.system/<each of review-agent, skill-creator, imagegen, skill-installer, openai-docs>/SKILL.md",enabled=false}, …]' \
  -c web_search="disabled" -c shell_environment_policy.inherit="none" \
  -c model_reasoning_effort="<level>" -c model_reasoning_summary="none" \
  --disable shell_tool --disable unified_exec --disable multi_agent --disable view_image \
  --disable goals --disable apps --disable plugins --disable hooks --disable image_generation \
  --disable browser_use --disable computer_use --disable tool_suggest --disable skill_search \
  --disable sleep_tool
```

Resulting request (**O-H**):

- `input` = [developer: the frozen text, user: the prompt]
- `tools` = [`request_user_input`]
- `reasoning.effort` set
- `store: false`
- no temperature or top-p

Both flag sets are **version-specific**. They must be re-verified by the
conformance checks (PC-03) for every pinned version.

---

## 8. Proposed changes to `RESEARCH_MODE_SPEC.md` (for review; not applied)

| ID | Proposed change | Affects | Rationale |
|---|---|---|---|
| **PC-01** | Add a **recording relay** requirement (new LOG-11): every provider request and response is captured byte-for-byte, with status code and timing, at a trusted relay between CLI and provider. Response `model` and usage come from the relay record. CLI stdout stays as a secondary record. | LOG-02, LOG-04, CFG-02, BUD-01 | §4.2, §4.7: the CLIs add unseen content, retry silently, and (Codex) never report the model |
| **PC-02** | Add evidence class **H** (harness): the real CLI binary against a recording mock API. Allow H evidence for client-enforced properties; keep L evidence required for model and server behavior. | Spec §0.4, §9 | This investigation shows H evidence is deterministic, automatable, credential-free, and catches real defects (finding 1) |
| **PC-03** | Add automated **provider conformance tests** (new VT-28…) run in preflight per pinned CLI version. Request-shape assertions: tool list, no injected working-directory path or date, model field, parameters. Forged-tool-call refusal. Retry count equals 1. No persistence files containing a canary. | CFG-03, ISO-03, PRO-11 | B9; the harness in `docs/research/feasibility-harness/` is a prototype |
| **PC-04** | Make **Tier 2 (container or OS user) the pilot minimum**, and drop Tier 1 as a pilot option. | D4, ISO-05, AC-04 | O-OS §4.6 shows it is cheap and consistent. Finding 1 shows a vendor restriction failing. Tier 1's residual risk was the open question in D4. |
| **PC-05** | ISO-03: remove reliance on Codex `--sandbox read-only` for confinement. Specify the Codex feature-disable set. Record `request_user_input` as a known residual tool with a disposition (B2). | ISO-03, UNKNOWN-05 | §4.1 |
| **PC-06** | ISO-06: the CLI must hold only a dummy credential; the relay holds real keys. Research keys are dedicated, per-study, spend-limited, and revocable. | ISO-06, ISO-01 | §4.5 point 3 |
| **PC-07** | CFG-02: define "requested model" (argv and `init.model`) separately from "served model" (relay response field). Attestation compares the two. Synchri's existing parser behavior is noted as recording only the requested model. | CFG-02 | §4.2 |
| **PC-08** | CFG-06: CLI-added text that cannot be removed (Claude's billing header and "Claude agent" identity line; Codex's `request_user_input` definition) is recorded per invocation from the relay and treated as a fixed treatment constant. Require `--bare` and API-key auth for Claude slots (B1). | CFG-06, ISO-09, UNKNOWN-04 | §4.3, §4.4 |
| **PC-09** | BUD-05: disable CLI-internal retries (`CLAUDE_CODE_MAX_RETRIES=0`; Codex `request_max_retries=0`, `stream_max_retries=0`), so the orchestrator owns the retry policy and every attempt is a logged invocation. | BUD-05, BUD-01 | §4.7 |
| **PC-10** | ISO-02: the per-invocation working directory is a neutral fixed path (e.g. `/work`) inside the container, so any injected path is byte-identical across invocations. Uniqueness comes from the container, not the path. | ISO-02, CFG-06 | §4.4 |
| **PC-11** | PRO-11 and CFG-08: per-invocation tmpfs home and `CODEX_HOME`, initially empty (Claude in `--bare` needs none; Codex needs none with relay auth). Optionally archive their final contents as evidence. | PRO-11, CFG-08, B10 | §4.4 |
| **PC-12** | PRO-08 and LOG-06: failure detection uses exit code, `is_error`, and relay status, never Claude's `result.subtype` alone. | PRO-08, LOG-06 | §4.7 |
| **PC-13** | CFG-03: pin the Claude native binary and Codex npm package by version **and** sha256, mounted read-only, with auto-update traffic disabled (`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`; Codex update checks were not observed and remain **U**). | CFG-03 | §3 |
| **PC-14** *(option, not a recommendation)* | If fixed sampling parameters are required by the preregistration, add a direct-API adapter alongside the CLI adapters. | D1, CFG-04 | B4: neither CLI exposes sampling |
| **PC-15** | Record a separate production issue: Planning Mode's Codex isolation relies on `--sandbox read-only`, which reads the whole filesystem (finding 1). | (outside the spec) | §4.1 |

---

## 9. Status of the specification's unknowns after this investigation

| Spec unknown | Status now | Evidence |
|---|---|---|
| UNKNOWN-01 Codex version and installability | Resolved for this host: 0.163.0 via npm into a disposable prefix. The pilot host must pin its own. | O-H |
| UNKNOWN-02 Codex reports the served model | **Resolved, negative**: not in `--json`. The relay is required. | O-H |
| UNKNOWN-03 Model, effort, and sampling flags | Resolved for these versions: §4.3. Sampling is not exposed by either CLI. | D, O-H |
| UNKNOWN-04 Per-slot provider home and credentials | Partly resolved: with relay auth, neither CLI needs credentials in its home. Subscription and ChatGPT-login paths are U. | O-H |
| UNKNOWN-05 Codex with all tools disabled | Largely resolved: every *executable* tool can be removed; `request_user_input` remains but is inert. | O-H |
| UNKNOWN-06 Web search | Request level resolved (absent). Server side U. | O-H |
| UNKNOWN-07 Memory across invocations | Client side resolved (§4.4). Server side U. | O-H |
| UNKNOWN-08 Provider-side drift | Unchanged (U). The relay's served-model record narrows it. | — |
| UNKNOWN-09 Tier 2 host design | Container and OS-user designs both demonstrated on Linux. Egress-proxy design and macOS are U. | O-OS |
| UNKNOWN-10 to UNKNOWN-13 | Unchanged (pilot measurements or investigator decisions). | — |
| UNKNOWN-14 Stream schema versus parsers | Observed for both versions. Codex has no model field; Claude has `assistant.message.model`. | O-H |

---

## 10. Live tests still required (dedicated pilot host)

**Prerequisites:**

- dedicated, spend-limited research API keys for Anthropic and OpenAI, held only
  by the relay;
- a Linux host with Docker;
- pinned CLI versions and binary hashes;
- the relay implemented (PC-01).

None of these exist yet.

| Spec LT | Additional focus from this investigation |
|---|---|
| LT-01 version and executable pin | Compare the binary hash with §3 or the pilot pin |
| LT-02 model attestation | Relay-recorded response `model` versus requested, for both providers |
| LT-03 tool disablement | Already shown at H level. L adds real-model attempts under adversarial prompts and confirms zero tool events. |
| LT-05, LT-06 memory and config isolation | Repeat §4.4 with real accounts. Confirm no server-side memory for API-key auth. |
| LT-07 parser fidelity | Real streams versus relay bytes |
| LT-08 usage capture | Relay usage versus CLI usage; check that totals agree |
| LT-09 timeout and kill | Inside the container |
| LT-10a/b Tier 2 | Container plus egress through the relay only |
| LT-11 no web | Server-side: confirm no search occurs when no tool is declared |
| **New: relay auth** | The CLI with a dummy key succeeds through relay-injected credentials, and the real key never appears in the container |

---

## 11. Sources and commands

**Documentation (D):**

- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference) (`--tools`, `--bare`, `--restricted`, `--safe-mode`, `--strict-mcp-config`, `--no-session-persistence`, `--model`, `--effort`, `--max-budget-usd`, system-prompt flags).
- [Codex developer commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli) (`exec`, `--sandbox`, `--ephemeral`, `--ignore-user-config`, `--ignore-rules`, `-c`, `--disable`, `--search`).
- [Codex skills](https://developers.openai.com/codex/skills) (`[[skills.config]]` with `enabled = false`).
- Local help text: `claude --help`, `codex --help`, `codex exec --help`, `codex sandbox --help`, `codex debug --help`, `codex features list`.

**Harness commands (O-H):** see `docs/research/feasibility-harness/README.md`.
Each test above corresponds to a `run_claude.sh` / `run_codex.sh` invocation
with the flags quoted in the relevant table, and a mock mode: `text`,
`toolcall:<name>:<json>`, or `status:<code>:<count>`.

**Isolation commands (O-OS):**

- OS users: `useradd --system --no-create-home --shell /usr/sbin/nologin rsch-a`;
  directories `chmod 700`; runs via `setpriv --reuid=rsch-a --regid=rsch-a
  --init-groups`.
- Egress: `iptables -A OUTPUT -m owner --uid-owner <uid> -o lo -p tcp --dport
  <port> -j ACCEPT; iptables -A OUTPUT -m owner --uid-owner <uid> -j REJECT`.
- Container: as in §4.6, using `feasibility-harness/Dockerfile` and `inside.sh`.

---

## 12. Cleanup and residual state

**Removed:**

- the OS users `rsch-a` and `rsch-b`;
- `/srv/rfeas` (test tree, Docker data root, copied binaries);
- all `iptables` rules added (OUTPUT chain empty);
- the Docker daemon (stopped; no containers remained).

**Retained, outside the repository:**

- evidence logs and disposable installs, in the session scratchpad only;
- these include captured request bodies containing the CLIs' own default prompts
  and are not committed.

**Committed:**

- this report;
- `docs/research/feasibility-harness/` (scripts and a README; dummy keys only).

**Not modified:** production code, the Research Mode specification, and the
audit.
