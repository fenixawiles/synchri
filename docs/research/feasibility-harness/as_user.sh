#!/bin/bash
# as_user.sh RUNTIME TAG MODE -- args   : run claude|codex as rsch-a under the mock API; logs in /srv/rfeas/logs/<tag>
RT=$1; TAG=$2; MODE=$3; shift 3; [ "$1" = "--" ] && shift
L=/srv/rfeas/logs/$RT-$TAG; if [ -e "$L" ]; then echo "refusing: $L exists"; exit 2; fi; mkdir -p "$L"
PORT=$((20000 + RANDOM % 20000))
python3 -I /srv/rfeas/bin/mock_server.py $PORT "$L/requests.jsonl" "$MODE" & SRV=$!; sleep 0.7
W=/srv/rfeas/slot-a/cwd-$RT-$TAG; setpriv --reuid=rsch-a --regid=rsch-a --init-groups mkdir -p "$W"
if [ "$RT" = claude ]; then
  CMD=(env -i PATH=/opt/node22/bin:/usr/bin:/bin HOME=/srv/rfeas/slot-a/home TMPDIR="$W" ANTHROPIC_BASE_URL=http://127.0.0.1:$PORT ANTHROPIC_API_KEY=sk-ant-mock-not-a-real-key CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 timeout 90 claude -p "$@")
else
  CMD=(env -i PATH=/opt/node22/bin:/usr/bin:/bin HOME=/srv/rfeas/slot-a/home CODEX_HOME=/srv/rfeas/slot-a/home/codexhome TMPDIR="$W" MOCK_API_KEY=mock-not-a-real-key timeout 90 node /srv/rfeas/bin/codex-install/node_modules/@openai/codex/bin/codex.js exec --json --skip-git-repo-check --ephemeral -c model_provider=\"mock\" -c model_providers.mock.name=\"mock\" -c model_providers.mock.base_url=\"http://127.0.0.1:$PORT/v1\" -c model_providers.mock.env_key=\"MOCK_API_KEY\" -c model_providers.mock.wire_api=\"responses\" "$@")
  setpriv --reuid=rsch-a --regid=rsch-a --init-groups mkdir -p /srv/rfeas/slot-a/home/codexhome
fi
echo "What is 2+2?" | (cd "$W" && setpriv --reuid=rsch-a --regid=rsch-a --init-groups "${CMD[@]}") > "$L/stdout.txt" 2> "$L/stderr.txt"; echo $? > "$L/rc"
kill $SRV 2>/dev/null; wait $SRV 2>/dev/null; echo "rc=$(cat $L/rc) requests=$(wc -l < $L/requests.jsonl 2>/dev/null)"
