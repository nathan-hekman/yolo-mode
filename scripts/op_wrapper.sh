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
# The Bots vault counts when it is named in any case (Bots, bots) or by its
# UUID, in an op:// reference or a --vault flag, and `op vault get Bots` counts
# as a read (2026-10-24). A desktop-app authorization opens every vault in the
# account, so each read routed here is one fewer account-wide session.
# `op inject` with a template file or stdin names no vault on the command line,
# so it still goes to the desktop app.
#
# Installed as ~/.local/bin/op (ahead of /opt/homebrew/bin on PATH) by
# install_yolo_mode.sh. Kill switch: OP_WRAPPER_OFF=1.
#
# The AI clone's vault (nathan-ai-clone) gets the same treatment with its own
# read-only service account, token in ~/.config/nathan-ai-clone/op-token
# (2026-10-24). Its own token, not the Bots one, so neither can read the
# other's vault. A call that names both vaults goes to the desktop app.
REAL=/opt/homebrew/bin/op
TOKEN_FILE="$HOME/.config/yolo-mode/op-token"
BOTS_ID=3lbnjdhid2owggk4eh3bzspqrq
CLONE_TOKEN_FILE="$HOME/.config/nathan-ai-clone/op-token"
CLONE_ID=bdhph7gzuek7ptigsyjc2tv7fa

bots=0
clone=0
read_only=0
prev=""
shopt -s nocasematch
for a in "$@"; do
  case "$a" in
    op://Bots/*|op://$BOTS_ID/*|--vault=Bots|--vault=$BOTS_ID) bots=1 ;;
    op://nathan-ai-clone/*|op://$CLONE_ID/*|--vault=nathan-ai-clone|--vault=$CLONE_ID) clone=1 ;;
  esac
  if [ "$prev" = "--vault" ]; then
    case "$a" in Bots|$BOTS_ID) bots=1 ;; nathan-ai-clone|$CLONE_ID) clone=1 ;; esac
  fi
  prev="$a"
done
# The command words are the first positional arguments, wherever options sit:
# `op --format=json item get X --vault Bots` is still `item get`. Options that
# take a separate value (--account NAME, --vault Bots) skip that value too.
pos=()
want_val=0
for a in "$@"; do
  if [ "$want_val" = 1 ]; then want_val=0; continue; fi
  case "$a" in
    --account|--config|--encoding|--format|--session|--vault|--fields|--categories|--tags|--out-file|-o|--file-mode) want_val=1 ;;
    -*) ;;
    *) pos+=("$a") ;;
  esac
done
case "${pos[0]} ${pos[1]}" in
  "read "*|"inject "*|"item get"|"item list") read_only=1 ;;
  "vault get")
    case "${pos[2]}" in Bots|$BOTS_ID) bots=1; read_only=1 ;; nathan-ai-clone|$CLONE_ID) clone=1; read_only=1 ;; esac ;;
esac
shopt -u nocasematch

if [ -z "$OP_WRAPPER_OFF" ] && [ -z "$OP_SERVICE_ACCOUNT_TOKEN" ] && [ "$read_only" = 1 ]; then
  if [ "$bots" = 1 ] && [ "$clone" = 0 ] && [ -s "$TOKEN_FILE" ]; then
    OP_SERVICE_ACCOUNT_TOKEN="$(cat "$TOKEN_FILE")" exec "$REAL" "$@"
  fi
  if [ "$clone" = 1 ] && [ "$bots" = 0 ] && [ -s "$CLONE_TOKEN_FILE" ]; then
    OP_SERVICE_ACCOUNT_TOKEN="$(cat "$CLONE_TOKEN_FILE")" exec "$REAL" "$@"
  fi
fi
exec "$REAL" "$@"
