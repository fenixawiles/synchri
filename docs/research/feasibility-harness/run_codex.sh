#!/bin/bash
# run_codex.sh TAG MODE -- extra codex exec args...
# Scrubbed env (env -i), fresh HOME and CODEX_HOME, empty cwd, local mock API. Never deletes
# anything: each TAG gets a new run directory and an existing one is refused.
# SHARED_HOME=<dir> reuses one HOME/CODEX_HOME across runs. PROMPT_FILE=<file> feeds stdin.
E=${FEAS_DIR:?set FEAS_DIR to a disposable working directory}
HERE=$(cd "$(dirname "$0")" && pwd)
C=$E/codex-install/node_modules/@openai/codex/bin/codex.js
TAG=$1; MODE=$2; shift 2; [ "$1" = "--" ] && shift
R=$E/runs/codex-$TAG
if [ -e "$R" ]; then echo "refusing: $R exists"; exit 2; fi
mkdir -p "$R/cwd"; if [ -n "$PRESEED" ]; then cp -a "$PRESEED"/. "$R/cwd/"; fi
if [ -n "$SHARED_HOME" ]; then HOMEDIR=$SHARED_HOME; else HOMEDIR=$R; fi
mkdir -p "$HOMEDIR/home" "$HOMEDIR/codexhome"
PORT=$((20000 + RANDOM % 20000))
python3 -I "$HERE/mock_server.py" $PORT "$R/requests.jsonl" "$MODE" & SRV=$!
sleep 0.7
( cd "$R/cwd" && env -i PATH=/opt/node22/bin:/usr/bin:/bin HOME="$HOMEDIR/home" CODEX_HOME="$HOMEDIR/codexhome" TMPDIR="$R/cwd" \
  MOCK_API_KEY=mock-not-a-real-key RESEARCH_CANARY_ENV=CANARY-ENV-$TAG \
  timeout 90 node "$C" exec --json --skip-git-repo-check \
    -c model_provider=\"mock\" -c model_providers.mock.name=\"mock\" \
    -c model_providers.mock.base_url=\"http://127.0.0.1:$PORT/v1\" -c model_providers.mock.env_key=\"MOCK_API_KEY\" \
    -c model_providers.mock.wire_api=\"responses\" "$@" < "${PROMPT_FILE:-/dev/null}" > "$R/stdout.txt" 2> "$R/stderr.txt"; echo $? > "$R/rc" )
kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
echo "rc=$(cat "$R/rc") requests=$(wc -l < "$R/requests.jsonl" 2>/dev/null)"
