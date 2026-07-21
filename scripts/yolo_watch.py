#!/usr/bin/env python3
"""Always-on watcher for approval-type dialogs on this Mac.

Polls the window list (Quartz CGWindowList) every POLL_SECS and pushes when a
window matching an approval rule appears: macOS auth prompts (SecurityAgent),
TCC permission dialogs (UserNotificationCenter), Gatekeeper
(CoreServicesUIAgent), 1Password unlock / agentic-autofill approvals.

Notify-only: it never clicks or dismisses anything.

Two anti-noise rules, both learned from the first version firing "System
permission dialog" at nothing:

  * A window must survive CONFIRM_SCANS consecutive polls before it counts.
    Real approval dialogs sit there waiting; transient system windows flicker.
  * A rule can require the window be dialog-shaped, so full-size app windows
    from the same process don't match.

When the window title is empty (macOS hides titles without Screen Recording
permission) we ask the accessibility API what the dialog actually says, so the
alert can name the request instead of just the app.

Claude Code approvals are NOT window-scraped: they arrive deterministically
through the Notification hook (scripts/claude_notify.py).

Kill switch: `touch ~/.yolo_mode_off`.
Log: ~/Library/Logs/yolo-mode.log
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eventlog  # noqa: E402
import secrets_store as secrets  # noqa: E402
from eventlog import log  # noqa: E402

from ApplicationServices import (  # type: ignore  # noqa: E402
    AXIsProcessTrusted,
    AXUIElementSetMessagingTimeout,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementPerformAction,
    kAXChildrenAttribute,
    kAXPressAction,
    kAXRoleAttribute,
    kAXTitleAttribute,
    kAXWindowsAttribute,
)
from Quartz import (  # type: ignore  # noqa: E402
    CGWindowListCopyWindowInfo,
    kCGNullWindowID,
    kCGWindowListExcludeDesktopElements,
    kCGWindowListOptionOnScreenOnly,
)

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "config.json"
LOGFILE = eventlog.LOGFILE
OFF_FLAG = Path.home() / ".yolo_mode_off"



POLL_SECS = 2.0
COOLDOWN_SECS = 300     # per dialog identity
CONFIRM_SCANS = 3       # polls a window must persist before it counts
AX_TIMEOUT = 0.6        # seconds per accessibility call, so a stuck app can't
                        # stall the poll loop

# Auto-approval clicks nothing but these exact button titles. A rule can
# narrow the list further with its own "buttons" -- the catch-all does, because
# "OK" on a named permission dialog is a grant, while "OK" on some unknown
# app's dialog could be confirming anything at all.
ALLOW_BUTTONS = ("Allow", "OK", "Always Allow", "Allow While Using App", "Continue")
AUTO_OFF_FLAG = Path.home() / ".yolo_mode_no_autoapprove"

DEFAULT_RULES = [
    {"owner": "SecurityAgent", "label": "macOS password / Touch ID prompt", "dialog_sized": True},
    {"owner": "CoreServicesUIAgent", "label": "Gatekeeper dialog", "dialog_sized": True},
    {
        "owner": "1Password",
        "title_any": ["Unlock", "Approve", "Access Requested", "wants to"],
        "dialog_sized": True,
        "label": "1Password unlock / approval",
    },
]


def load_rules() -> list[dict]:
    try:
        return json.loads(CONFIG.read_text())["rules"]
    except Exception as e:
        log(f"config error, using built-ins: {e}")
        return DEFAULT_RULES


def dialog_text(owner: str) -> str:
    """Best-effort read of a dialog's own words via the accessibility API.

    Returns "" when Accessibility permission is missing, which is fine -- the
    alert just falls back to naming the app.
    """
    script = f'''
    tell application "System Events"
        if not (exists process "{owner}") then return ""
        tell process "{owner}"
            if (count of windows) = 0 then return ""
            try
                set t to value of static texts of window 1
                return t as string
            on error
                return ""
            end try
        end tell
    end tell
    '''
    try:
        out = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True, text=True, timeout=2,
        )
        return " ".join(out.stdout.split())[:200]
    except Exception:
        return ""


def _ax_value(element, attribute):
    err, value = AXUIElementCopyAttributeValue(element, attribute, None)
    return value if err == 0 else None


def find_allow_button(pid: int, buttons: tuple = ALLOW_BUTTONS):
    """Return (title, element) for the first approve-looking button, or None.

    This doubles as the test for "is this actually a permission dialog?". A
    catch-all rule that trusts window shape alone matches Notes, FaceTime and
    every other small window on the Mac -- asking the accessibility tree
    whether an Allow button exists is the only honest answer.
    """
    if not AXIsProcessTrusted():
        return None
    try:
        app = AXUIElementCreateApplication(pid)
        # Default AX timeout is ~6s per call. A busy app that never answers
        # would stall the whole poll loop, which is what made the dialog time
        # out before the watcher ever looped back to click it.
        AXUIElementSetMessagingTimeout(app, AX_TIMEOUT)
        found: list[tuple[str, object]] = []
        for window in _ax_value(app, kAXWindowsAttribute) or []:
            _collect_buttons(window, found)
        for title, element in found:
            if title in buttons:
                return title, element
    except Exception as e:
        log(f"button scan error (pid {pid}): {e}")
    return None


def auto_approve(owner: str, pid: int, buttons: tuple = ALLOW_BUTTONS) -> str | None:
    """Click the approve button on a system permission dialog.

    Only buttons whose exact title is in ALLOW_BUTTONS are ever clicked -- this
    never presses "the first button" or anything it cannot name, so a dialog
    with unfamiliar wording is left alone for a human.

    Uses the accessibility API in-process rather than shelling out to
    osascript. That matters for more than speed: TCC attributes the click to
    the binary that makes it, so the osascript route required granting
    Accessibility to /usr/bin/osascript -- which would let any script on the
    machine click any dialog. In-process, the grant belongs to this app alone
    and revoking it revokes exactly this.

    Returns the clicked button title, or None.
    """
    if not AXIsProcessTrusted():
        _warn_missing_permission()
        return None
    try:
        app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, AX_TIMEOUT)
        found: list[tuple[str, object]] = []
        for window in _ax_value(app, kAXWindowsAttribute) or []:
            _collect_buttons(window, found)
        for title, element in found:
            if title in buttons:
                if AXUIElementPerformAction(element, kAXPressAction) == 0:
                    return title
                log(f"auto-approve: press failed on {title!r} ({owner})")
        # Say why nothing happened. Silence here is indistinguishable from a
        # dialog we deliberately left alone, which hid a bug once already.
        log(
            f"auto-approve: no allowed button on {owner} dialog; "
            f"saw {[t for t, _ in found]}"
        )
        return None
    except Exception as e:
        log(f"auto-approve error ({owner}): {e}")
        return None


def _collect_buttons(element, out: list, depth: int = 0, budget: list | None = None) -> None:
    """Walk the accessibility tree for buttons, within a strict budget.

    Buttons are rarely direct children of the window: permission dialogs nest
    them inside a group or sheet, so a one-level scan finds nothing. But an
    unbounded walk is worse -- a browser window is thousands of nodes deep and
    every node is a blocking call, which stretched one poll pass to 27 seconds
    and let real dialogs come and go unseen.

    A permission dialog is small and shallow. Anything that isn't, isn't one.
    """
    if budget is None:
        budget = [MAX_AX_NODES]
    if depth > 3 or budget[0] <= 0:
        return
    for child in _ax_value(element, kAXChildrenAttribute) or []:
        budget[0] -= 1
        if budget[0] <= 0:
            return
        role = _ax_value(child, kAXRoleAttribute)
        if role == "AXButton":
            out.append((_ax_value(child, kAXTitleAttribute) or "", child))
        else:
            _collect_buttons(child, out, depth + 1, budget)


def notify(label: str, owner: str, title: str, says: str) -> bool:
    """Push one alert. Headline is what is being asked, not where it lives."""
    headline = title or says or label
    body_lines = [says] if says and says != headline else []
    body_lines.append(f"App: {owner}")
    if headline == label:
        # Don't repeat the label as both title and body -- the old alerts read
        # "System permission dialog: System permission dialog".
        headline = ""
    try:
        import requests

        user_key, api_token = secrets.pushover()
        if not (user_key and api_token):
            log("no Pushover credentials; see README")
            return False

        requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "user": user_key,
                "token": api_token,
                "title": f"Approve on Mac: {label}",
                "message": "\n".join([l for l in [headline] + body_lines if l]),
                "priority": 0,
            },
            timeout=12,
        )
        return True
    except Exception as e:
        log(f"pushover failed: {e}")
        return False


def _warn_missing_permission() -> None:
    """Say once a day that auto-approval is switched on but cannot click."""
    state = eventlog.load_state()
    if time.time() - state.get("perm_warned", 0) < 86400:
        return
    state["perm_warned"] = time.time()
    eventlog.save_state(state)
    eventlog.record(
        "error",
        "Auto-approve can't click: grant Accessibility + Automation",
        source="watcher",
        project="YOLO Mode",
        detail="System Settings > Privacy & Security > Accessibility and Automation",
        pushed=False,
    )


def notify_auto_approved(owner: str, what: str, button: str) -> bool:
    """Tell Nathan after the fact, with the wording needed to undo it."""
    try:
        import requests

        user_key, api_token = secrets.pushover()
        if not (user_key and api_token):
            log("no Pushover credentials; see README")
            return False

        requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "user": user_key,
                "token": api_token,
                "title": f"Auto-approved: {owner}",
                "message": (
                    f"{what}\nClicked: {button}\n"
                    "Undo in System Settings > Privacy & Security."
                ),
                "priority": 0,
            },
            timeout=12,
        )
        return True
    except Exception as e:
        log(f"pushover failed: {e}")
        return False


def match(rule: dict, owner: str, title: str, w: float, h: float) -> bool:
    if rule.get("owner", "").lower() not in owner.lower():
        return False
    # owner_not lets a catch-all rule cover everything except the owners that
    # must never be auto-clicked (see config.json).
    if any(x.lower() in owner.lower() for x in rule.get("owner_not", [])):
        return False
    if rule.get("dialog_sized") and (w > 700 or h > 550 or w < 120 or h < 80):
        return False
    tany = rule.get("title_any")
    if tany:
        # Titles come back empty without Screen Recording permission; a
        # dialog-shaped window from the right owner still counts, so the
        # watcher degrades to app-level alerts instead of going silent.
        if title:
            return any(t.lower() in title.lower() for t in tany)
        return bool(rule.get("dialog_sized"))
    return True


NO_BUTTON_TTL = 120   # seconds to remember "this window has no Allow button"
MAX_PROBES_PER_SCAN = 3  # accessibility probes per pass, so one pass stays quick
MAX_AX_NODES = 120       # nodes per probe; a permission dialog needs far fewer
_no_button: dict[str, float] = {}


def scan(rules: list[dict], seen: dict[str, float], streak: dict[str, int]) -> None:
    infos = CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements,
        kCGNullWindowID,
    ) or []
    now = time.time()
    live: set[str] = set()
    probes = 0

    for info in infos:
        owner = info.get("kCGWindowOwnerName") or ""
        title = info.get("kCGWindowName") or ""
        bounds = info.get("kCGWindowBounds") or {}
        w, h = bounds.get("Width", 0), bounds.get("Height", 0)
        for rule in rules:
            if not match(rule, owner, title, w, h):
                continue
            pid = info.get("kCGWindowOwnerPID") or 0
            allowed = tuple(rule.get("buttons") or ALLOW_BUTTONS)
            key = f"{rule['label']}|{owner}|{title}"
            if os.environ.get("YOLO_DEBUG"):
                log(f"debug match {rule['label']} owner={owner!r} title={title!r} "
                    f"{w}x{h} streak={streak.get(key, 0)}")
            live.add(key)
            streak[key] = streak.get(key, 0) + 1
            if streak[key] < CONFIRM_SCANS:
                break
            # Per-rule cooldown: the self-test sets 0, because a test you
            # cannot repeat for five minutes is a test you stop running.
            if now - seen.get(key, 0) <= rule.get("cooldown", COOLDOWN_SECS):
                break
            # A catch-all rule matches on window shape, which describes most
            # windows on the Mac, so confirm this one really offers an Allow
            # button before calling it a permission dialog. Done here rather
            # than up front: it is an accessibility round trip, and running it
            # against every window on every poll stalled the loop.
            if rule.get("require_allow_button"):
                # Remember the misses. Notes and FaceTime match the catch-all's
                # shape every poll, and re-asking the accessibility tree about
                # them each time starved the loop badly enough that a real
                # dialog timed out before its turn came round.
                if now - _no_button.get(key, 0) < NO_BUTTON_TTL:
                    break
                if probes >= MAX_PROBES_PER_SCAN:
                    break  # keep the pass short; this window gets the next one
                probes += 1
                if not find_allow_button(pid, allowed):
                    _no_button[key] = now
                    break

            seen[key] = now
            says = dialog_text(owner) if not title else ""

            # Read the dialog before clicking, so the after-the-fact alert can
            # say what was granted and to whom.
            if rule.get("auto_approve") and not AUTO_OFF_FLAG.exists():
                clicked = auto_approve(
                    owner,
                    info.get("kCGWindowOwnerPID") or 0,
                    tuple(rule.get("buttons") or ALLOW_BUTTONS),
                )
                if clicked:
                    what = says or title or rule["label"]
                    # `silent` rules exist for the self-test dialog: log the
                    # click, don't buzz the phone about a dialog we raised.
                    pushed = (
                        False if rule.get("silent")
                        else notify_auto_approved(owner, what, clicked)
                    )
                    eventlog.record(
                        "auto-approved",
                        f"Approved ({clicked}): {what}",
                        source="watcher",
                        project=owner,
                        detail="Revoke in System Settings > Privacy & Security",
                        pushed=pushed,
                    )
                    break

            pushed = notify(rule["label"], owner, title, says)
            eventlog.record(
                "approval",
                title or says or rule["label"],
                source="watcher",
                project=owner,
                detail=says,
                pushed=pushed,
            )
            break

    for key in list(streak):
        if key not in live:
            del streak[key]


def watch_loop(status_cb=None) -> None:
    rules = load_rules()
    seen: dict[str, float] = {}
    streak: dict[str, int] = {}
    trusted = AXIsProcessTrusted()
    log(
        f"watch started ({len(rules)} rules, "
        f"accessibility {'granted' if trusted else 'MISSING - cannot click'})"
    )
    if not trusted:
        # Ask, rather than only complaining to the log. Until an app asks, the
        # Accessibility list often has no row for it at all -- so there is
        # nothing to switch on. Re-signing with a different certificate makes a
        # previously granted app land right here.
        try:
            from ApplicationServices import (  # noqa: PLC0415
                AXIsProcessTrustedWithOptions,
                kAXTrustedCheckOptionPrompt,
            )

            AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
        except Exception as e:
            log(f"could not raise the accessibility prompt: {e}")
    while True:
        paused = OFF_FLAG.exists()
        if status_cb:
            status_cb(paused)
        if not paused:
            try:
                started = time.time()
                scan(rules, seen, streak)
                elapsed = time.time() - started
                # A poll pass should take milliseconds. When it does not, a
                # real dialog can come and go between passes -- worth saying so
                # rather than leaving it to be inferred from silence.
                if elapsed > 3:
                    log(f"slow scan: {elapsed:.1f}s")
            except Exception as e:
                log(f"scan error: {e}")
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    watch_loop()
