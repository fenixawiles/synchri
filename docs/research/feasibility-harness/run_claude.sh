#!/bin/bash
# run_claude.sh TAG MODE -- extra claude args...
# Scrubbed env (env -i), fresh HOME, empty cwd, local mock API. Never deletes anything:
# each TAG gets a new run directory and an existing one is refused.
# SHARED_HOME=<dir> reuses one HOME across runs (carry-over tests). PROMPT_FILE=<file> feeds stdin.
E=${FEAS_DIR:?set FEAS_DIR to a disposable working directory}
HERE=$(cd "$(dirname "$0")" && pwd)
TAG=$1; MODE=$2; shift 2; [ "$1" = "--" ] && shift
R=$E/runs/claude-$TAG
if [ -e "$R" ]; then echo "refusing: $R exists"; exit 2; fi
mkdir -p "$R/cwd"; if [ -n "$PRESEED" ]; then cp -a "$PRESEED"/. "$R/cwd/"; fi
if [ -n "$SHARED_HOME" ]; then mkdir -p "$SHARED_HOME"; HOMEDIR=$SHARED_HOME; else mkdir -p "$R/home"; HOMEDIR=$R/home; fi
PORT=$((20000 + RANDOM % 20000))
python3 -I "$HERE/mock_server.py" $PORT "$R/requests.jsonl" "$MODE" & SRV=$!
sleep 0.7
( cd "$R/cwd" && env -i PATH=/opt/node22/bin:/usr/bin:/bin HOME="$HOMEDIR" TMPDIR="$R/cwd" \
  ANTHROPIC_BASE_URL=http://127.0.0.1:$PORT ANTHROPIC_API_KEY=sk-ant-mock-not-a-real-key \
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 RESEARCH_CANARY_ENV=CANARY-ENV-$TAG \
  timeout 90 claude -p "$@" < "${PROMPT_FILE:-/dev/null}" > "$R/stdout.txt" 2> "$R/stderr.txt"; echo $? > "$R/rc" )
kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
echo "rc=$(cat "$R/rc") requests=$(wc -l < "$R/requests.jsonl" 2>/dev/null)"
