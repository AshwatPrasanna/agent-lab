#!/bin/bash
# Agent Lab one-time setup. Double-click in Finder, or run: bash setup.command
set -euo pipefail
cd "$(dirname "$0")"
LAB="$PWD"
echo "== Agent Lab setup in $LAB"

PY="$(command -v python3 || true)"
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "!! Need python3 >= 3.9 on PATH to run the app itself."; exit 1
fi
mkdir -p runtime shared/bin shared/datasets shared/cache
echo "$PY" > runtime/python_path
echo "-- app runs on $PY ($("$PY" --version)); it uses only the standard library"

if [ ! -x shared/bin/uv ]; then
  echo "-- installing uv into the lab folder (your shell profile is not modified)"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$LAB/shared/bin" UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh
fi
export UV_PYTHON_INSTALL_DIR="$LAB/shared/python" UV_CACHE_DIR="$LAB/shared/uv-cache"
shared/bin/uv python install 3.12
echo "-- experiment Python 3.12 installed inside the lab folder, because agents can't read Python installs elsewhere in your home folder"

if command -v claude >/dev/null 2>&1; then
  echo "-- Claude Code: $(claude --version)"
  if claude auth status >/dev/null 2>&1; then echo "-- Claude Code is signed in"; else echo "!! Claude Code is not signed in: run 'claude' once in Terminal and log in with your Max account."; fi
else
  echo "!! Claude Code not found. Install it with:  curl -fsSL https://claude.ai/install.sh | bash   then run 'claude' once to sign in."
fi
touch runtime/.setup-done
echo
echo "Setup done. Start the app with start.command (double-click it)."
