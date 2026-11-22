#!/bin/bash
# run_routine.sh <task-id> [<fire-epoch>]
#
# launchd fallback for a routine desktop_routine.py could not get into
# Claude Desktop. Fired by ~/Library/LaunchAgents/com.nathanhekman.routine.<id>.plist.
# Opens the same kind of session Desktop would: a detached Opus session that
# reads ~/.claude/scheduled-tasks/<id>/SKILL.md and does what it says.
#
# With <fire-epoch> it is a one-off: StartCalendarInterval has no year, so an
# early match is skipped, and after firing the job removes itself.
#
# Lives under ~/yolo-mode, not ~/Documents: a launchd job that reads files under
# ~/Documents can hang when its TCC access lapses. The ~/Documents launcher is
# run inside Terminal.app instead, which has that access.
set -u
tid="$1"
once="${2:-}"
label="com.nathanhekman.routine.$tid"
plist="$HOME/Library/LaunchAgents/$label.plist"
log="$HOME/Library/Logs/yolo-routines.log"
skill="$HOME/.claude/scheduled-tasks/$tid/SKILL.md"
say() { echo "$(date '+%Y-%m-%d %H:%M:%S %Z') $tid: $*" >>"$log"; }

remove_self() {
  rm -f "$plist"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null
}

if [[ -n "$once" ]] && (( $(date +%s) < once - 120 )); then
  say "calendar matched before fire time $once; waiting for the right year"
  exit 0
fi

# Desktop got it after all (a late registration): let Desktop run it.
if /usr/bin/python3 - "$tid" <<'EOF'
import glob, json, os, sys
p = glob.glob(os.path.expanduser("~/Library/Application Support/Claude/claude-code-sessions/*/*/scheduled-tasks.json"))
d = json.load(open(max(p, key=os.path.getmtime))) if p else {}
sys.exit(0 if any(t.get("id") == sys.argv[1] and t.get("enabled") for t in d.get("scheduledTasks", [])) else 1)
EOF
then
  say "Desktop has an enabled entry; skipping the launchd copy"
  [[ -n "$once" ]] && remove_self
  exit 0
fi

if [[ ! -f "$skill" ]]; then
  say "no $skill; nothing to run"
  exit 1
fi

# A THIN SHIM brief with exactly one ```bash block is only a guard plus a
# launcher call. Claude Desktop ran that block itself and opened no session
# for it; do the same here, so a shim no longer costs a Remote Control session
# of its own (every launchd shim did from 2026-11-11 to 2026-11-21, and a
# weekend skip opened a session only to read "weekend — skip"). Any other brief
# still gets a Claude session that reads it.
shim=""
if grep -q 'THIN SHIM' "$skill" && [[ "$(grep -c '^```bash' "$skill")" == 1 ]]; then
  shim="$HOME/yolo-mode/state/routines/$tid.shim.sh"
  mkdir -p "$(dirname "$shim")"
  awk '/^```bash/{f=1;next} f&&/^```/{exit} f' "$skill" >"$shim"
  [[ -s "$shim" ]] || shim=""
fi

prompt="Read $skill in full, then do what it says. You are the scheduled run of routine $tid, fired by launchd because Claude Desktop did not take the registration."

# How the session gets started, and why not an AppleScript Terminal tab:
# a Terminal tab opened by an Apple Event from a launchd job runs in the job's
# privacy (TCC) context, which has no access to ~/Documents. Its shell dies on
# "Operation not permitted" reading the launcher, osascript still returns 0, and
# the routine never runs. That silently killed every launchd-fired routine from
# 2026-10-30 to 2026-11-11 (wn-trust-review-1105, ask-usage-recheck-1106,
# requote-expired-3-1111, vault-loss-list-1111). `open -a Terminal x.command`
# goes through LaunchServices, so the shell runs in Terminal's own context,
# which does have access (tested from a real launchd job 2026-11-11).
#
# Success means the launcher itself printed "launched", which it does only
# after it sees the Claude process up. Anything else is retried, then pushed.
state="$HOME/yolo-mode/state/routines"
mkdir -p "$state"
runner="$state/$tid.command"
out="$state/$tid.out"

attempt() {
  rm -f "$out"
  if [[ -n "$shim" ]]; then
    cat >"$runner" <<EOF
#!/bin/bash
cd "\$HOME"
bash $(printf '%q' "$shim") >"$out.tmp" 2>&1
echo "rc=\$?" >>"$out.tmp"
mv "$out.tmp" "$out"
exit
EOF
  else
  cat >"$runner" <<EOF
#!/bin/bash
cd "\$HOME"
bash "\$HOME/Documents/scrape-collection/scripts/launch-skill-in-terminal.sh" --detached --model opus --effort medium --raw-prompt $(printf '%q' "$prompt") $tid $tid "\$HOME/Documents" >"$out.tmp" 2>&1
echo "rc=\$?" >>"$out.tmp"
mv "$out.tmp" "$out"
exit
EOF
  fi
  chmod +x "$runner"
  open -a Terminal "$runner" >>"$log" 2>&1 || { say "open -a Terminal failed"; return 1; }
  for _ in $(seq 1 150); do
    [[ -f "$out" ]] && break
    sleep 2
  done
  if [[ ! -f "$out" ]]; then say "no result from the Terminal tab after 5 min"; return 1; fi
  if grep -q '^launched' "$out"; then
    say "fired: $(grep -m1 '^launched' "$out")"
    return 0
  fi
  if [[ -n "$shim" ]]; then
    # The shim ran. rc=0 without "launched" is its own guard saying skip.
    # A failing shim already sent its own push; do not rerun it (a second
    # run could open a second session).
    if grep -q '^rc=0$' "$out"; then
      say "fired: shim ran, no session: $(grep -v '^rc=' "$out" | tr '\n' ' ' | cut -c1-200)"
      return 0
    fi
    say "shim failed: $(tr '\n' ' ' <"$out" | cut -c1-300)"
    return 2
  fi
  say "launch failed: $(tr '\n' ' ' <"$out" | cut -c1-300)"
  return 1
}

attempt; rc=$?
if (( rc == 1 )); then sleep 60; attempt; rc=$?; fi
if (( rc == 0 )); then
  [[ -n "$once" ]] && remove_self
  exit 0
fi

eval "$("$HOME/bin/pushover-creds")"
why="$(tail -2 "$log" | tr '\n' ' ' | cut -c1-400)"
what="did not start after 2 tries. Nothing ran."
(( rc == 2 )) && what="ran its shim, and the shim failed."
msg="$("$HOME/bin/push-link" yolo-mode "Routine $tid $what $why Log: ~/Library/Logs/yolo-routines.log")"
curl -s -m 20 --form-string "token=$PUSHOVER_APP_TOKEN" --form-string "user=$PUSHOVER_USER_KEY" \
  --form-string "priority=1" \
  --form-string "title=routine $tid did not start" --form-string "message=$msg" \
  https://api.pushover.net/1/messages.json >/dev/null
exit 1
