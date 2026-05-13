#!/usr/bin/env bash
# Одноразовый refresh `dsk/cookies.json`: поднимает бypass-сервер на :8801,
# открывает Chrome, проходит AWS WAF / Cloudflare challenge для chat.deepseek.com
# и сохраняет свежие cookies. Сервер потом убивается.
#
# Запусти при симптомах:
#   • бот пишет "ответ AI не распознан" подряд
#   • DeepSeek отвечает пустой строкой
set -euo pipefail

cd "$(dirname "$0")"

VENV=./.venv/bin/python
if [[ ! -x "$VENV" ]]; then
    # No venv — use whatever python is on PATH (e.g. system Python in Docker).
    VENV="$(command -v python3 || command -v python || true)"
fi
if [[ -z "$VENV" || ! -x "$VENV" ]]; then
    echo "No Python interpreter found. Either create a venv or install python3."
    exit 1
fi

PORT=${BYPASS_PORT:-8801}

# Start bypass server in background
LOG=/tmp/kwork-bot-logs/bypass-server.log
mkdir -p /tmp/kwork-bot-logs
DISPLAY=${DISPLAY:-:0} SERVER_PORT=$PORT setsid "$VENV" dsk/server.py \
    >"$LOG" 2>&1 </dev/null &
BYPASS_PID=$!
echo "bypass server pid=$BYPASS_PID, log=$LOG"

cleanup() {
    kill -TERM "$BYPASS_PID" 2>/dev/null || true
}
trap cleanup EXIT

# Wait for port to start listening (up to ~20s). `ss` may not be present
# in minimal images — fall back to a curl probe against /docs.
for i in {1..40}; do
    if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":$PORT "; then
        break
    fi
    if curl -fsS --max-time 1 "http://127.0.0.1:$PORT/docs" >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

echo "requesting fresh cookies (opens Chrome, ~10-30s)..."
RESP=$(mktemp)
HTTP=$(curl -sS -o "$RESP" -w '%{http_code}' --max-time 120 \
    "http://127.0.0.1:$PORT/cookies?url=https://chat.deepseek.com&retries=5")
if [[ "$HTTP" != "200" ]]; then
    echo "FAIL: bypass server returned HTTP $HTTP"
    cat "$RESP" | head -c 400; echo
    exit 1
fi

"$VENV" - <<PY
import json, sys
d = json.load(open('$RESP'))
cookies = d.get('cookies', {})
out = {'cookies': cookies, 'user_agent': d.get('user_agent','')}
json.dump(out, open('dsk/cookies.json','w'), indent=2, ensure_ascii=False)
print('ok, wrote dsk/cookies.json, keys:', list(cookies.keys()))
PY
rm -f "$RESP"
echo "done."
