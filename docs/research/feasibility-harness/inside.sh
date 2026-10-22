#!/bin/sh
# Runs inside the container as uid 10001. $1 = claude|codex, $2 = forged-call MODE
RT=$1; MODE=$2
python3 -I /mock/mock_server.py 18080 /log/requests.jsonl "$MODE" & sleep 0.7
cd /work; mkdir -p /home/slot/codexhome
if [ "$RT" = claude ]; then
  echo "What is 2+2?" | env -i PATH=/usr/bin:/bin HOME=/home/slot TMPDIR=/work ANTHROPIC_BASE_URL=http://127.0.0.1:18080 ANTHROPIC_API_KEY=sk-ant-mock-not-a-real-key CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    timeout 90 /opt/claude-code/bin/claude -p --output-format stream-json --verbose --no-session-persistence --dangerously-skip-permissions > /log/stdout.txt 2> /log/stderr.txt
else
  echo "What is 2+2?" | env -i PATH=/usr/bin:/bin HOME=/home/slot CODEX_HOME=/home/slot/codexhome TMPDIR=/work MOCK_API_KEY=mock-not-a-real-key \
    timeout 90 /codex/codex exec --json --skip-git-repo-check --ephemeral -s danger-full-access -m gpt-mock-requested -c model_provider=\"mock\" -c model_providers.mock.name=\"mock\" -c model_providers.mock.base_url=\"http://127.0.0.1:18080/v1\" -c model_providers.mock.env_key=\"MOCK_API_KEY\" -c model_providers.mock.wire_api=\"responses\" > /log/stdout.txt 2> /log/stderr.txt
fi
echo $? > /log/rc
