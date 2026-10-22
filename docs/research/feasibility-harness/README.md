# Provider feasibility harness

Evidence appendix for `docs/research/PROVIDER_FEASIBILITY.md`. These scripts are
**not** part of Synchri and are not collected by `pytest`. They run the real
provider CLI binaries against a local mock of the provider API, so they observe
client behavior (requests, tool enforcement, persistence, retries) without any
real model, credential, or network call.

| File | Purpose |
|---|---|
| `mock_server.py` | Mock Anthropic Messages and OpenAI Responses endpoint. Logs every request (auth headers redacted). Modes: `text`; `toolcall:<name>:<json>` (forges one tool call); `status:<code>:<count>` (fails the first *count* model requests). |
| `run_claude.sh`, `run_codex.sh` | Run one CLI invocation with `env -i`, a fresh home, an empty working directory, and the mock API. Never delete anything; each tag gets a new directory. `SHARED_HOME`, `PROMPT_FILE`, and `PRESEED` are optional. |
| `analyze_claude.py`, `analyze_codex.py` | Summarize a captured request: model, parameters, tools, injected context. |
| `as_user.sh` | OS-user isolation test (run as root; expects users `rsch-a`/`rsch-b` and the `/srv/rfeas` layout described in the report). |
| `inside.sh`, `Dockerfile` | Container isolation test (`--network none`, read-only rootfs, non-root UID, all capabilities dropped). |

Typical use (disposable directory, Codex installed into it with
`npm install --prefix "$FEAS_DIR/codex-install" @openai/codex@0.163.0`):

```bash
export FEAS_DIR=$(mktemp -d)
echo "What is 2+2?" > "$FEAS_DIR/prompt.txt"; export PROMPT_FILE="$FEAS_DIR/prompt.txt"
./run_claude.sh t1 text -- --output-format stream-json --verbose --no-session-persistence \
    --bare --system-prompt "Answer." --tools "" --disallowedTools "*"
python3 analyze_claude.py "$FEAS_DIR/runs/claude-t1"
```

The only key-like strings in these files are fixed dummy values
(`sk-ant-mock-not-a-real-key`, `mock-not-a-real-key`) that authenticate to the
mock and nothing else.
