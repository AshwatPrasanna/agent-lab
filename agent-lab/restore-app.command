#!/bin/bash
# Puts back Agent Lab's code from before the last change made in the "Modify app" tab, then starts
# Agent Lab. Use it if a change stopped Agent Lab from starting. (If Agent Lab is running, the
# Undo button in the Modify app tab does the same thing.) Plain bash, so it works even if the app's
# Python code is broken.
cd "$(dirname "$0")" || exit 1
LAB="$PWD"
RUN="$(cat runtime/selfdev/LAST_APPLIED 2>/dev/null | tr -cd 'a-z0-9-')"
if [ -z "$RUN" ] || [ ! -d "runtime/selfdev/$RUN" ]; then
  echo "There's no app change to undo."; read -r -p "Press Return to close." _; exit 1
fi
map() {  # repository path -> path on this computer (same mapping as app/selfdev.py)
  case "$1" in
    agent-lab/*) printf '%s\n' "$LAB/${1#agent-lab/}" ;;
    "Agent Lab.app"|"Agent Lab.app/"*) printf '%s\n' "$(dirname "$LAB")/$1" ;;
    *) printf '%s\n' "$LAB/$1" ;;
  esac
}
echo "Undoing app change $RUN…"
PORT="${AGENT_LAB_PORT:-8765}"
curl -s -m 3 -o /dev/null -X POST -H "X-Agent-Lab: restore" -H "Content-Type: application/json" \
     -d '{"force": true}' "http://127.0.0.1:$PORT/api/admin/shutdown" && sleep 1.5
if [ -d "runtime/selfdev/$RUN/backup" ]; then
  (cd "runtime/selfdev/$RUN/backup" && find . -type f) | while IFS= read -r f; do
    f="${f#./}"; dst="$(map "$f")"; mkdir -p "$(dirname "$dst")"
    cp -p "runtime/selfdev/$RUN/backup/$f" "$dst" && echo "  restored $f"
  done
fi
if [ -f "runtime/selfdev/$RUN/added.txt" ]; then
  while IFS= read -r f; do [ -n "$f" ] && rm -f "$(map "$f")" && echo "  removed $f"; done < "runtime/selfdev/$RUN/added.txt"
fi
touch "runtime/selfdev/$RUN/RESTORED"
rm -f runtime/selfdev/LAST_APPLIED
echo "Done. Starting Agent Lab…"
exec ./start.command
