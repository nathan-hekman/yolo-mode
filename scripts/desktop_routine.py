#!/usr/bin/env python3
"""Create a LOCAL Claude Desktop routine by asking Desktop itself.

Desktop keeps its routine list in memory and saves over scheduled-tasks.json
at any time, so an entry written from outside disappears within minutes. The
only write that sticks is Desktop's own create_scheduled_task tool, which
exists only inside sessions Desktop starts. So this script starts one:

  1. waits until the keyboard/mouse have been idle (never types over Nathan)
  2. opens claude://code/new?q=<instruction> (prompt prefilled, default mode)
  3. presses "Trust workspace" if Desktop asks
  4. sets the model popup to Opus 5 and permissions to Bypass permissions
  5. sends, presses "Allow once" if an approval still appears
  6. checks scheduled-tasks.json for the id

Every control is found by its accessibility name, not by screen position, so
a moved window or resized sidebar does not break it. Electron hides its
accessibility tree until AXManualAccessibility is set on the app.

The routine runs in a Desktop scratch folder: the tool has no cwd field, and a
&folder= link param only adds a trust prompt without sticking. Shims use
absolute paths, so this costs nothing. create_scheduled_task writes {id}/SKILL.md itself, so an
existing shim's body is passed as the prompt (--from-skill) rather than kept.

Usage:
  desktop_routine.py <id> --fire-at 2026-09-25T09:00:00-05:00 --from-skill PATH
  desktop_routine.py <id> --cron "7 9 * * *" --prompt "..." --title "..."
Exit 0 = registered, 1 = not registered, 2 = gave up waiting for idle,
3 = switched off in the YOLO Mode menu (~/.yolo_mode_no_desktop_routine);
callers then fall back to scrape-collection/scripts/add_scheduled_task.py.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import time
import urllib.parse

from AppKit import NSWorkspace  # type: ignore
from ApplicationServices import (  # type: ignore
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementPerformAction,
    AXUIElementSetAttributeValue,
    AXUIElementSetMessagingTimeout,
)

HOME = os.path.expanduser("~")
MODEL = "Opus 5"
PERMISSION = "Bypass permissions"


def idle_seconds() -> float:
    out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "HIDIdleTime" in line:
            return int(line.split()[-1]) / 1e9
    return 0.0


def claude_pid() -> int | None:
    for a in NSWorkspace.sharedWorkspace().runningApplications():
        if a.localizedName() == "Claude":
            return a.processIdentifier()
    return None


def ax(e, attr):
    err, v = AXUIElementCopyAttributeValue(e, attr, None)
    return v if err == 0 else None


def app_element():
    app = AXUIElementCreateApplication(claude_pid())
    AXUIElementSetMessagingTimeout(app, 2.0)
    AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)
    return app


def find(app, pred, max_depth=45):
    """First element under any window where pred(role, title, desc) is true."""
    stack = [(w, 0) for w in (ax(app, "AXWindows") or [])]
    while stack:
        e, d = stack.pop()
        role, title, desc = ax(e, "AXRole"), ax(e, "AXTitle") or "", ax(e, "AXDescription") or ""
        if pred(role, title, desc):
            return e
        if d < max_depth:
            stack.extend((c, d + 1) for c in (ax(e, "AXChildren") or []))
    return None


def wait_for(app, pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        e = find(app, pred)
        if e is not None:
            return e
        time.sleep(0.5)
    return None


def press(e):
    AXUIElementPerformAction(e, "AXPress")


def choose(app, popup_pred, item_name):
    """Open a popup and press the menu item named item_name."""
    popup = find(app, popup_pred)
    if popup is None:
        return False
    press(popup)
    item = wait_for(app, lambda r, t, d: r in ("AXMenuItem", "AXButton", "AXMenuButton", "AXRadioButton")
                    and (t.startswith(item_name) or d.startswith(item_name)), 4)
    if item is None:
        subprocess.run(["osascript", "-e", 'tell application "System Events" to key code 53'])
        return False
    press(item)
    time.sleep(0.8)
    return True


def registry_has(task_id):
    paths = glob.glob(f"{HOME}/Library/Application Support/Claude/claude-code-sessions/*/*/scheduled-tasks.json")
    if not paths:
        return False
    data = json.load(open(max(paths, key=os.path.getmtime)))
    return any(t.get("id") == task_id for t in data.get("scheduledTasks", []))


def skill_body(path):
    text = open(os.path.expanduser(path)).read()
    if text.startswith("---"):
        text = text.split("---", 2)[2]
    return text.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("id")
    when = ap.add_mutually_exclusive_group(required=True)
    when.add_argument("--fire-at", help="ISO 8601 with offset, e.g. 2026-09-25T09:00:00-05:00")
    when.add_argument("--cron")
    body = ap.add_mutually_exclusive_group(required=True)
    body.add_argument("--prompt")
    body.add_argument("--from-skill")
    ap.add_argument("--title")
    ap.add_argument("--idle", type=int, default=60, help="seconds of user idle required first")
    ap.add_argument("--max-wait", type=int, default=3600, help="give up after this long without idle")
    a = ap.parse_args()

    if os.path.exists(f"{HOME}/.yolo_mode_no_desktop_routine"):
        print("off: Create routines through Desktop is unchecked in YOLO Mode")
        sys.exit(3)

    prompt = a.prompt or skill_body(a.from_skill)
    sched = f'fireAt "{a.fire_at}"' if a.fire_at else f'cronExpression "{a.cron}"'
    title = f', title "{a.title}"' if a.title else ""
    instruction = (
        f"Use your create_scheduled_task tool to create a LOCAL scheduled task (not a cloud "
        f"or remote routine). taskId \"{a.id}\"{title}, {sched}. Use the text between the "
        f"markers below, verbatim, as the task prompt. Do nothing else; reply with the tool "
        f"result.\n<<<PROMPT\n{prompt}\nPROMPT>>>"
    )

    end = time.time() + a.max_wait
    while idle_seconds() < a.idle:
        if time.time() > end:
            print("gave up: user never idle")
            sys.exit(2)
        time.sleep(5)

    if claude_pid() is None:
        subprocess.run(["open", "-a", "Claude"])
        time.sleep(8)
    app = app_element()
    url = "claude://code/new?" + urllib.parse.urlencode({"q": instruction})
    subprocess.run(["open", url])

    trust = wait_for(app, lambda r, t, d: r == "AXButton" and "Trust workspace" in (t + d), 6)
    if trust is not None:
        press(trust)
        time.sleep(1.5)

    if wait_for(app, lambda r, t, d: r == "AXTextArea" and d == "Prompt", 10) is None:
        print("no prompt box found")
        sys.exit(1)
    if find(app, lambda r, t, d: r == "AXPopUpButton" and d == f"Model: {MODEL}") is None:
        choose(app, lambda r, t, d: r == "AXPopUpButton" and d.startswith("Model: "), MODEL)
    if find(app, lambda r, t, d: r == "AXPopUpButton" and t == PERMISSION) is None:
        choose(app, lambda r, t, d: r == "AXPopUpButton" and t in
               ("Manual", "Ask permissions", "Accept edits", "Plan mode", "Auto"), PERMISSION)

    box = find(app, lambda r, t, d: r == "AXTextArea" and d == "Prompt")
    AXUIElementSetAttributeValue(box, "AXFocused", True)
    time.sleep(0.3)
    subprocess.run(["osascript", "-e", 'tell application "System Events" to key code 36'])

    end = time.time() + 180
    while time.time() < end:
        if registry_has(a.id):
            print(f"{a.id}: registered by Desktop")
            sys.exit(0)
        allow = find(app, lambda r, t, d: r == "AXButton" and (t + d).startswith("Allow once"))
        if allow is not None:
            press(allow)
        time.sleep(2)
    print(f"{a.id}: NOT registered")
    sys.exit(1)


if __name__ == "__main__":
    main()
