#!/bin/bash
# Claude Code Notification hook entry point.
#
# Thin wrapper so ~/.claude/settings.json keeps pointing at a stable path while
# the real logic lives in claude_notify.py. Prefers the repo venv (it has
# requests); falls back to system python, which still logs the event locally
# even if the push can't go out.
DIR="$(cd "$(dirname "$0")" && pwd)"
PY="$DIR/../.venv/bin/python"
[ -x "$PY" ] || PY=/usr/bin/python3
exec "$PY" "$DIR/claude_notify.py"
