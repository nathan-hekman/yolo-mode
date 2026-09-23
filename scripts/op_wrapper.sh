#!/bin/bash
# `op` wrapper: Bots-vault reads go through the read-only service account, so
# they never raise 1Password's "Authorize" prompt. Everything else goes to the
# desktop app as usual (Touch ID).
#
# Why: automations and Claude sessions call `op` directly, and each call via the
# desktop app pops an Authorize dialog (~10 min per terminal session). The
# service account can only read the Bots vault, so routing just those reads to
# it removes the prompts without widening access. Writes still need the app.
#
# Installed as ~/.local/bin/op (ahead of /opt/homebrew/bin on PATH) by
# install_yolo_mode.sh. Kill switch: OP_WRAPPER_OFF=1.
REAL=/opt/homebrew/bin/op
TOKEN_FILE="$HOME/.config/yolo-mode/op-token"

bots=0
read_only=0
prev=""
for a in "$@"; do
  case "$a" in
    op://Bots/*|--vault=Bots) bots=1 ;;
  esac
  [ "$prev" = "--vault" ] && [ "$a" = "Bots" ] && bots=1
  prev="$a"
done
case "$1 $2" in
  "read "*|"inject "*|"item get"|"item list") read_only=1 ;;
esac

if [ -z "$OP_WRAPPER_OFF" ] && [ -z "$OP_SERVICE_ACCOUNT_TOKEN" ] \
   && [ "$bots" = 1 ] && [ "$read_only" = 1 ] && [ -s "$TOKEN_FILE" ]; then
  OP_SERVICE_ACCOUNT_TOKEN="$(cat "$TOKEN_FILE")" exec "$REAL" "$@"
fi
exec "$REAL" "$@"
