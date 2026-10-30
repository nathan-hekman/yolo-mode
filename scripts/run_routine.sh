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

prompt="Read $skill in full, then do what it says. You are the scheduled run of routine $tid, fired by launchd because Claude Desktop did not take the registration."
cmd="bash \$HOME/Documents/scrape-collection/scripts/launch-skill-in-terminal.sh --detached --model opus --effort medium --raw-prompt '$prompt' $tid $tid \$HOME/Documents; exit"
if osascript -e "tell application \"Terminal\" to do script \"$cmd\"" >>"$log" 2>&1; then
  say "fired"
  [[ -n "$once" ]] && remove_self
  exit 0
fi

say "Terminal launch failed"
eval "$("$HOME/bin/pushover-creds")"
msg="$("$HOME/bin/push-link" yolo-mode "Routine $tid did not start: the launchd fallback could not open Terminal. Log: ~/Library/Logs/yolo-routines.log")"
curl -s -m 20 --form-string "token=$PUSHOVER_APP_TOKEN" --form-string "user=$PUSHOVER_USER_KEY" \
  --form-string "title=routine $tid failed" --form-string "message=$msg" \
  https://api.pushover.net/1/messages.json >/dev/null
exit 1
