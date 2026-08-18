#!/usr/bin/env python3
"""Always-on watcher for approval-type dialogs on this Mac.

Polls the window list (Quartz CGWindowList) every POLL_SECS and pushes when a
window matching an approval rule appears: macOS auth prompts (SecurityAgent),
TCC permission dialogs (UserNotificationCenter), Gatekeeper
(CoreServicesUIAgent), 1Password unlock / agentic-autofill approvals.

Rules opt into clicking: a rule with auto_approve presses the dialog's
Allow/OK-type button (or drives the real mouse to it when click_method is
"mouse", for web-view dialogs like 1Password's Authorize prompt whose AXButton
ignores AXPress). Rules without auto_approve stay notify-only.

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
import mac_password  # noqa: E402
import macinput  # noqa: E402
import secrets_store as secrets  # noqa: E402
from eventlog import log  # noqa: E402
from macinput import autorelease_pool  # noqa: E402
from macinput import mouse_click  # noqa: E402  (re-exported: callers use aw.mouse_click)

from ApplicationServices import (  # type: ignore  # noqa: E402
    AXIsProcessTrusted,
    AXUIElementSetMessagingTimeout,
    AXUIElementCopyAttributeValue,
    AXUIElementCreateApplication,
    AXUIElementPerformAction,
    AXValueGetValue,
    kAXChildrenAttribute,
    kAXFocusedAttribute,
    kAXPositionAttribute,
    kAXPressAction,
    kAXRoleAttribute,
    kAXSizeAttribute,
    kAXSubroleAttribute,
    kAXTitleAttribute,
    kAXValueAttribute,
    kAXValueCGPointType,
    kAXValueCGSizeType,
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


def _element_center(element):
    """Global screen center (x, y) of an AX element, or None.

    AXPosition/AXSize come back as opaque AXValue structs; AXValueGetValue
    unpacks them into a CGPoint/CGSize. The coordinates share the top-left
    origin that CGEvent mouse events use, so the returned pair can drive a
    synthetic click directly with no flip.
    """
    pos = _ax_value(element, kAXPositionAttribute)
    size = _ax_value(element, kAXSizeAttribute)
    if pos is None or size is None:
        return None
    okp, p = AXValueGetValue(pos, kAXValueCGPointType, None)
    oks, s = AXValueGetValue(size, kAXValueCGSizeType, None)
    if not (okp and oks):
        return None
    return (p.x + s.width / 2.0, p.y + s.height / 2.0)


def find_allow_button(pid: int, buttons: tuple = ALLOW_BUTTONS, max_depth: int = 3,
                      ax_timeout: float = AX_TIMEOUT, seen_out: list | None = None):
    """Return (title, element) for the first approve-looking button, or None.

    This doubles as the test for "is this actually a permission dialog?". A
    catch-all rule that trusts window shape alone matches Notes, FaceTime and
    every other small window on the Mac -- asking the accessibility tree
    whether an Allow button exists is the only honest answer.

    seen_out collects every button title found, so a caller that rejects the
    window can say what it actually saw. Without that, a new kind of system
    prompt whose buttons sit deeper than max_depth looks exactly like an
    ordinary window, and the only evidence is silence.
    """
    if not AXIsProcessTrusted():
        return None
    try:
        app = AXUIElementCreateApplication(pid)
        # Default AX timeout is ~6s per call. A busy app that never answers
        # would stall the whole poll loop, which is what made the dialog time
        # out before the watcher ever looped back to click it. A web-view
        # dialog (1Password) needs a longer per-call budget than a native one,
        # or its tree reads back truncated at random -- rules raise ax_timeout.
        AXUIElementSetMessagingTimeout(app, ax_timeout)
        found: list[tuple[str, object]] = []
        for window in _ax_value(app, kAXWindowsAttribute) or []:
            _collect_buttons(window, found, max_depth=max_depth)
        if seen_out is not None:
            seen_out.extend(t for t, _ in found)
        for title, element in found:
            if title in buttons:
                return title, element
    except Exception as e:
        log(f"button scan error (pid {pid}): {e}")
    return None


def auto_approve(owner: str, pid: int, buttons: tuple = ALLOW_BUTTONS, max_depth: int = 3,
                 ax_timeout: float = AX_TIMEOUT, click_method: str = "press") -> str | None:
    """Click the approve button on a system permission dialog.

    Only buttons whose exact title is in ALLOW_BUTTONS are ever clicked -- this
    never presses "the first button" or anything it cannot name, so a dialog
    with unfamiliar wording is left alone for a human.

    click_method picks how the button is actuated:
      * "press" (default) fires AXPress in-process. TCC attributes the click to
        this binary, so the Accessibility grant belongs to this app alone and
        revoking it revokes exactly this -- unlike the osascript route, which
        would let any script on the machine click any dialog.
      * "mouse" reads the button's screen center and drives the real cursor
        there (see mouse_click). Needed for web-view dialogs whose AXButton
        ignores AXPress. Costs a visible cursor jump, so it is opt-in per rule.

    Returns the clicked button title, or None.
    """
    if not AXIsProcessTrusted():
        _warn_missing_permission()
        return None
    try:
        app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, ax_timeout)
        found: list[tuple[str, object]] = []
        for window in _ax_value(app, kAXWindowsAttribute) or []:
            _collect_buttons(window, found, max_depth=max_depth)
        for title, element in found:
            if title in buttons:
                if click_method == "mouse":
                    center = _element_center(element)
                    if center is None:
                        log(f"auto-approve: no coords for {title!r} ({owner})")
                        continue
                    mouse_click(*center)
                    return title
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


AUTH_OFF_FLAG = Path.home() / ".yolo_mode_no_password"
AUTH_MAX_ATTEMPTS = 2   # per dialog identity, per run
_auth_attempts: dict[str, int] = {}
_auth_busy: set[str] = set()


def _find_secure_field(pid: int, ax_timeout: float = AX_TIMEOUT):
    """Return (window, password field) for a macOS auth prompt, or None.

    The password box is identified by AXSubrole == AXSecureTextField, never by
    position. A SecurityAgent prompt has two text fields -- the username, which
    arrives prefilled, and the password -- and "the second one" is a rule that
    holds until the first prompt that omits the username row and silently types
    the password into a field that is about to be shown on screen.
    """
    try:
        app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, ax_timeout)
        for window in _ax_value(app, kAXWindowsAttribute) or []:
            for child in _ax_value(window, kAXChildrenAttribute) or []:
                if (_ax_value(child, kAXRoleAttribute) == "AXTextField"
                        and _ax_value(child, kAXSubroleAttribute) == "AXSecureTextField"):
                    return window, child
    except Exception as e:
        log(f"auth-fill: field scan error (pid {pid}): {e}")
    return None


def _clear_field(presses: int) -> None:
    """Backspace over whatever was typed, so a partial password is not left
    sitting in a field for a human to hit Return on."""
    from Quartz import CGEventCreateKeyboardEvent  # noqa: PLC0415
    from Quartz import CGEventPost, kCGHIDEventTap  # noqa: PLC0415

    for _ in range(presses):
        for down in (True, False):
            CGEventPost(kCGHIDEventTap, CGEventCreateKeyboardEvent(None, 51, down))
        time.sleep(0.03)


def _window_static_text(window) -> str:
    """What the dialog actually says, read straight off the window.

    SecurityAgent reports its window title as "Untitled" and osascript's
    `value of static texts` needs an Automation grant this app does not want,
    so every one of these alerts used to read "SecurityAgent: Untitled". The
    words are right there in the AX tree: "Claude", "An update is ready to
    install...", "Enter your password to allow this."
    """
    parts = []
    try:
        for child in _ax_value(window, kAXChildrenAttribute) or []:
            if _ax_value(child, kAXRoleAttribute) == "AXStaticText":
                text = (_ax_value(child, kAXValueAttribute) or "").strip()
                if text:
                    parts.append(text)
    except Exception:
        pass
    return " ".join(parts)[:300]


def auth_fill(owner: str, pid: int, key: str,
              ax_timeout: float = AX_TIMEOUT) -> tuple[str, str] | None:
    """Type the Mac login password into a macOS auth prompt and submit it.

    This is the one rule in the app that hands out privilege rather than
    granting a capability, so it is deliberately the narrowest path here:

      * The window must actually contain an AXSecureTextField. A dialog with no
        password box is not an auth prompt, whatever it is called.
      * Nothing is typed until the prompt's own process is confirmed frontmost
        and its field confirmed focused, and nothing is submitted until the
        field is confirmed to hold exactly as many characters as were sent.
        Synthetic keystrokes go to the frontmost app, so without those checks a
        dialog that quietly loses focus turns this into a password leak.
      * The submit is the window's own AXDefaultButton -- the one Return would
        press, the one already highlighted. There is no button-title allowlist
        because the wording is different every time ("Add Helper", "Modify
        Settings", "Install Helper"), and pressing whatever is default is
        exactly what a human hitting Return does.
      * Two attempts per dialog per run. A wrong password leaves the same
        dialog on screen, and an uncapped loop would retype it until the
        account locked out.
      * Three switches turn it off: ~/.yolo_mode_no_password (this alone),
        ~/.yolo_mode_no_autoapprove (all clicking), ~/.yolo_mode_off (all of it).

    Nathan chose to have every SecurityAgent prompt filled rather than an
    allowlist of apps, on the understanding that the Pushover alert -- sent
    after the fact, naming the app and what it asked for -- is the review step.
    That trade is only sound because the alert always fires: an auth prompt
    that was filled and NOT reported is the failure this is written to avoid,
    which is why the notify call sits outside the success branch.

    Returns (pressed button title, what the dialog said), or None.
    """
    if not AXIsProcessTrusted():
        _warn_missing_permission()
        return None
    if _auth_attempts.get(key, 0) >= AUTH_MAX_ATTEMPTS:
        return None
    found = _find_secure_field(pid, ax_timeout)
    if found is None:
        log(f"auth-fill: no password field on {owner} dialog")
        return None
    window, field = found
    says = _window_static_text(window)

    default_button = _ax_value(window, "AXDefaultButton")
    if default_button is None:
        log(f"auth-fill: {owner} dialog has no default button; left for a human")
        return None
    button_title = _ax_value(default_button, kAXTitleAttribute) or "(default)"

    try:
        password = mac_password.login_password()
    except mac_password.PasswordUnavailable as e:
        log(f"auth-fill: {e}")
        eventlog.record(
            "error", f"Could not fill password prompt: {e}",
            source="watcher", project=owner,
            detail="Check 1Password: scripts/mac_password.py", pushed=False,
        )
        return None

    center = _element_center(field)
    if center is None:
        log(f"auth-fill: no coords for the password field ({owner})")
        return None

    _auth_attempts[key] = _auth_attempts.get(key, 0) + 1
    try:
        # Click the field with the real mouse. Two quieter routes were tried
        # first and both failed the same way: NSRunningApplication.activate
        # will not raise SecurityAgent (it is a background agent, and the call
        # returns True while the previous app stays frontmost), and
        # AXFocused=True sets focus inside a process that is not receiving key
        # events. Synthetic keystrokes follow the FRONTMOST app, not AX focus,
        # so both left the password being typed into whatever happened to be in
        # front -- observed 2026-08-04, when it went to 1Password's window.
        # A mouse click on the field does both jobs at once and is what a
        # person would do.
        macinput.mouse_click(*center)
        time.sleep(0.5)

        # The gate. Never type a password unless the process that will receive
        # the keystrokes is the one that asked for it. This check is the whole
        # reason the bug above is not a password leak waiting to recur.
        front = macinput.frontmost_app() or ""
        if owner.lower() not in front.lower():
            log(f"auth-fill: aborted, {front!r} is frontmost, not {owner!r}")
            return None
        if not _ax_value(field, kAXFocusedAttribute):
            log(f"auth-fill: aborted, password field never took focus ({owner})")
            return None

        macinput.type_text(password)
        time.sleep(0.3)

        # A secure field reports its contents as one bullet per character
        # ("contains secure text"), which is enough to prove the keystrokes
        # landed here and all of them arrived. Submitting a half-typed password
        # burns an attempt and, three times over, locks the account.
        typed = len(_ax_value(field, kAXValueAttribute) or "")
        if typed != len(password):
            log(f"auth-fill: aborted, field holds {typed} of "
                f"{len(password)} characters ({owner})")
            _clear_field(len(password) + 4)
            return None

        if AXUIElementPerformAction(default_button, kAXPressAction) != 0:
            log(f"auth-fill: press failed on {button_title!r} ({owner})")
            return None
        return button_title, says
    except Exception as e:
        log(f"auth-fill error ({owner}): {e}")
        return None
    finally:
        del password


def notify_auth_filled(owner: str, what: str, button: str) -> bool:
    """Say, loudly and immediately, that a password was just typed for you."""
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
                "title": f"Password entered: {owner}",
                "message": (
                    f"{what}\nSubmitted: {button}\n"
                    "Not you? touch ~/.yolo_mode_no_password"
                ),
                # High priority, unlike the other alerts. Everything else here
                # can wait for the next time the phone is picked up; an admin
                # password given to something Nathan did not start cannot.
                "priority": 1,
            },
            timeout=12,
        )
        return True
    except Exception as e:
        log(f"pushover failed: {e}")
        return False


def start_auth_fill(owner: str, pid: int, key: str, label: str,
                    ax_timeout: float = AX_TIMEOUT) -> None:
    """Run auth_fill on its own thread and report the result.

    Off the poll thread on purpose: a 1Password read can take tens of seconds
    when the vault is locked, and blocking the loop for that long means every
    other dialog on the Mac goes unseen until it returns -- including, on a bad
    day, the Authorize dialog that this very read is waiting for. That is a
    deadlock, not a slow poll.
    """
    import threading  # noqa: PLC0415

    if key in _auth_busy:
        return
    _auth_busy.add(key)

    def run() -> None:
        try:
            result = auth_fill(owner, pid, key, ax_timeout)
            if not result:
                # Declined, out of attempts, or 1Password unreachable. The
                # prompt is still sitting there, so fall back to the ordinary
                # alert rather than going quiet -- silence here reads exactly
                # like "handled".
                pushed = notify(label, owner, "", "")
                eventlog.record(
                    "approval", label, source="watcher", project=owner,
                    detail="Password prompt not filled -- see the log",
                    pushed=pushed,
                )
                return
            button, says = result
            what = says or label
            pushed = notify_auth_filled(owner, what, button)
            eventlog.record(
                "auto-approved",
                f"Password entered ({button}): {what}",
                source="watcher",
                project=owner,
                detail="Turn this off with: touch ~/.yolo_mode_no_password",
                pushed=pushed,
            )
        except Exception as e:
            log(f"auth-fill thread error ({owner}): {e}")
        finally:
            _auth_busy.discard(key)

    threading.Thread(target=run, daemon=True).start()


def _collect_buttons(element, out: list, depth: int = 0, budget: list | None = None,
                     max_depth: int = 3) -> None:
    """Walk the accessibility tree for buttons, within a strict budget.

    Buttons are rarely direct children of the window: permission dialogs nest
    them inside a group or sheet, so a one-level scan finds nothing. But an
    unbounded walk is worse -- a browser window is thousands of nodes deep and
    every node is a blocking call, which stretched one poll pass to 27 seconds
    and let real dialogs come and go unseen.

    A native permission dialog is small and shallow, so max_depth defaults to 3.
    A web-view dialog (1Password renders its Authorize prompt inside an
    AXWebArea) buries its buttons ~8 levels down, so those rules raise max_depth
    themselves. The MAX_AX_NODES budget still caps total work either way, so a
    deeper limit stays safe on a small dialog.
    """
    if budget is None:
        budget = [MAX_AX_NODES]
    if depth > max_depth or budget[0] <= 0:
        return
    for child in _ax_value(element, kAXChildrenAttribute) or []:
        budget[0] -= 1
        if budget[0] <= 0:
            return
        role = _ax_value(child, kAXRoleAttribute)
        if role == "AXButton":
            out.append((_ax_value(child, kAXTitleAttribute) or "", child))
        else:
            _collect_buttons(child, out, depth + 1, budget, max_depth)


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
SEEN_TTL = 3600       # seconds to remember a fired rule before forgetting the key
MAX_SKIP_LOGGED = 500  # keys remembered for once-per-run skip logging
MAX_PROBES_PER_SCAN = 3  # accessibility probes per pass, so one pass stays quick
MAX_AX_NODES = 120       # nodes per probe; a permission dialog needs far fewer
_no_button: dict[str, float] = {}
_skip_logged: set[str] = set()


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
                saw: list[str] = []
                if not find_allow_button(pid, allowed, rule.get("max_depth", 3),
                                         rule.get("ax_timeout", AX_TIMEOUT), saw):
                    _no_button[key] = now
                    # Name the rejected window and its buttons. A new system
                    # prompt whose Allow sits deeper than max_depth is
                    # indistinguishable from an ordinary window otherwise --
                    # this line is what turns "it never fired" into a fact
                    # about which owner and which button titles were there.
                    # Named buttons only, and only a few. An ordinary app
                    # window answers with a couple of hundred untitled
                    # controls, and a log line that long buries the one case
                    # this is here to catch.
                    named = sorted({t for t in saw if t.strip()})[:8]
                    # Once per window per run. The point is to discover a new
                    # kind of dialog, and the same Finder or updater window
                    # sits there all day -- re-reporting it every NO_BUTTON_TTL
                    # would add a thousand lines a day to a log meant to be
                    # read.
                    if named and key not in _skip_logged:
                        _skip_logged.add(key)
                        log(f"skipped {owner!r} dialog {title!r} ({w:.0f}x{h:.0f}): "
                            f"no allowed button, saw {named}")
                    break

            seen[key] = now

            # Password prompts before anything else: this rule types a secret
            # rather than clicking a grant, so it has its own switch and its
            # own thread, and it never falls through to the click path.
            if rule.get("auth_fill"):
                if AUTO_OFF_FLAG.exists() or AUTH_OFF_FLAG.exists():
                    log(f"auth-fill: switched off, leaving {owner} prompt alone")
                else:
                    start_auth_fill(owner, pid, key, rule["label"],
                                    rule.get("ax_timeout", AX_TIMEOUT))
                    break

            says = dialog_text(owner) if not title else ""

            # Read the dialog before clicking, so the after-the-fact alert can
            # say what was granted and to whom.
            if rule.get("auto_approve") and not AUTO_OFF_FLAG.exists():
                clicked = auto_approve(
                    owner,
                    info.get("kCGWindowOwnerPID") or 0,
                    tuple(rule.get("buttons") or ALLOW_BUTTONS),
                    rule.get("max_depth", 3),
                    rule.get("ax_timeout", AX_TIMEOUT),
                    rule.get("click_method", "press"),
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

    # streak is keyed on window title, and a title can carry a filename or a
    # command line, so the key space is effectively unbounded over a long run.
    # streak clears itself above; these three do not, so age them out here.
    for key, when in list(seen.items()):
        if now - when > SEEN_TTL:
            del seen[key]
    for key, when in list(_no_button.items()):
        if now - when > NO_BUTTON_TTL * 4:
            del _no_button[key]
    if len(_skip_logged) > MAX_SKIP_LOGGED:
        _skip_logged.clear()


def start_turnstile(status_cb=None) -> None:
    """Run the Turnstile solver on its own thread.

    Separate thread, not another rule: the dialog watcher reads the
    accessibility tree, while the solver matches pixels and has to sit through
    a multi-second settle after each click. Sharing one loop would mean a
    Cloudflare solve blocking every permission dialog for six seconds.
    """
    import threading  # noqa: PLC0415

    try:
        import turnstile  # noqa: PLC0415
    except Exception as e:
        log(f"turnstile solver unavailable: {e}")
        return
    threading.Thread(
        target=turnstile.watch_loop,
        args=(OFF_FLAG, AUTO_OFF_FLAG),
        kwargs={"status_cb": status_cb},
        daemon=True,
    ).start()


def watch_loop(status_cb=None, turnstile_cb=None) -> None:
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
    start_turnstile(turnstile_cb)
    while True:
        # One pool per pass. Every window list and accessibility node this pass
        # reads is autoreleased, and on a non-main thread only this drains them.
        with autorelease_pool():
            paused = OFF_FLAG.exists()
            if status_cb:
                status_cb(paused)
            if not paused:
                try:
                    started = time.time()
                    scan(rules, seen, streak)
                    elapsed = time.time() - started
                    # A poll pass should take milliseconds. When it does not, a
                    # real dialog can come and go between passes -- worth saying
                    # so rather than leaving it to be inferred from silence.
                    if elapsed > 3:
                        log(f"slow scan: {elapsed:.1f}s")
                except Exception as e:
                    log(f"scan error: {e}")
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    watch_loop()
