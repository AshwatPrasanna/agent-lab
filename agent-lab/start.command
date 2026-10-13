#!/bin/bash
# Starts Agent Lab in the background and opens it in your browser.
# This Terminal window closes itself once Agent Lab is running. Stop Agent Lab from Settings → Stop Agent Lab.
cd "$(dirname "$0")"
if [ ! -f runtime/.setup-done ]; then bash ./setup.command || exit 1; fi
PY="$(cat runtime/python_path 2>/dev/null || command -v python3)"
PORT="${AGENT_LAB_PORT:-8765}"
URL="http://127.0.0.1:$PORT"
TTY="$(tty)"
mkdir -p runtime
up() { curl -s -m 1 -o /dev/null "$URL/api/status"; }

if up; then
  echo "Agent Lab is already running. Opening it."
  open "$URL"
  curl -s -m 2 -o /dev/null -X POST -H "X-Agent-Lab: start" -H "Content-Type: application/json" \
       -d "{\"tty\": \"$TTY\"}" "$URL/api/admin/close-terminal"
  exit 0
fi

echo "Starting Agent Lab…"
"$PY" -u app/server.py --port "$PORT" --detach --close-tty "$TTY"
for _ in $(seq 1 80); do
  if up; then echo "Agent Lab is running. This window will close."; open "$URL"; exit 0; fi
  sleep 0.25
done
echo
echo "Agent Lab didn't start. The end of runtime/server.log:"
tail -20 runtime/server.log
