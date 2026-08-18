#!/usr/bin/env python3
"""Real mouse, real screen, real frontmost app -- the OS-level primitives.

Split out of yolo_watch so the Turnstile solver can use the same clicker
without importing the dialog watcher (which imports the solver back). Every
function here is in-process Quartz/AppKit, with one deliberate exception:
screen capture shells out to /usr/sbin/screencapture.

WHY screencapture AND NOT CGDisplayCreateImage
----------------------------------------------
CGDisplayCreateImage and CGWindowListCreateImage are deprecated as of macOS 14
in favour of ScreenCaptureKit, and their behaviour on newer systems is not
something to bet a background watcher on. `screencapture -x -R` is the path the
standalone turnstile-autosolve proved on this Mac, it takes a rectangle in the
global logical coordinate space, and it returns one PNG per call. It costs a
subprocess (~150ms), which is why nothing calls it unless a challenge is
already suspected.

COORDINATES
-----------
One space throughout: global logical points, top-left origin, displays laid out
side by side (CGDisplayBounds). CGEvent mouse events use it, screencapture -R
takes it, and AXPosition reports in it. So a match found in a captured region
converts back to a click coordinate by adding the region's origin -- no flip,
no per-display translation.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time

from AppKit import NSRunningApplication, NSWorkspace  # type: ignore
from Quartz import (  # type: ignore
    CGDisplayBounds,
    CGEventCreateKeyboardEvent,
    CGEventCreateMouseEvent,
    CGEventKeyboardSetUnicodeString,
    CGEventPost,
    CGGetActiveDisplayList,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseUp,
    kCGEventMouseMoved,
    kCGHIDEventTap,
    kCGMouseButtonLeft,
)

MAX_DISPLAYS = 8


# ── autorelease pool ───────────────────────────────────────────────────────
# Every Quartz/AppKit/accessibility call hands back an autoreleased Objective-C
# object. On the main thread AppKit's run loop drains those between events; on a
# plain Python thread nothing ever does, so a `while True` poll loop holds on to
# every window list, every AX node and every NSString it ever read. Left alone
# the watcher's heap passed 2.9 GB in a week. Each pass through a polling loop
# gets its own pool, and the pass's garbage goes when the pool does.
try:  # PyObjC 4+
    from objc import autorelease_pool  # type: ignore  # noqa: F401
except ImportError:  # pragma: no cover - older PyObjC
    import contextlib

    from Foundation import NSAutoreleasePool  # type: ignore

    @contextlib.contextmanager
    def autorelease_pool():  # type: ignore[no-redef]
        pool = NSAutoreleasePool.alloc().init()
        try:
            yield
        finally:
            del pool


# ── mouse ──────────────────────────────────────────────────────────────────
def mouse_move(x: float, y: float) -> None:
    CGEventPost(
        kCGHIDEventTap,
        CGEventCreateMouseEvent(None, kCGEventMouseMoved, (x, y), kCGMouseButtonLeft),
    )


def mouse_click(x: float, y: float) -> None:
    """Move the real cursor to (x, y) and left-click, like a person would.

    Some dialogs -- notably 1Password's Authorize prompt, which is a web view
    inside an AXWebArea -- expose an AXButton whose AXPress does nothing or
    lands unreliably. Driving the actual mouse is what a human does and what
    those web controls actually respond to. The move-before-click matters:
    web hit-testing keys off the current pointer, so a click with no prior
    move can miss.

    These are CGEvents posted to the HID tap, so the browser sees
    `isTrusted === true`. Turnstile rejects anything else, which is why a
    CDP/Playwright click cannot solve one and this can.
    """
    mouse_move(x, y)
    CGEventPost(
        kCGHIDEventTap,
        CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, (x, y), kCGMouseButtonLeft),
    )
    CGEventPost(
        kCGHIDEventTap,
        CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, (x, y), kCGMouseButtonLeft),
    )


# ── keyboard ───────────────────────────────────────────────────────────────
def type_text(text: str, delay: float = 0.045) -> None:
    """Type a string as real keystrokes into whatever has focus.

    Every character rides on a keyboard CGEvent posted to the HID tap with
    CGEventKeyboardSetUnicodeString, so the keycode is irrelevant and the
    current keyboard layout cannot mangle the result -- a literal '@' arrives
    as '@' on a Dvorak or German layout alike.

    The three alternatives were all rejected:
      * AXValue on the field -- macOS secure text fields refuse the write, and
        the ones that accept it skip the change notifications an app may be
        waiting on.
      * osascript "keystroke" -- needs an Automation grant for System Events,
        which hands every script on the machine the same power, and it fails
        silently against SecurityAgent.
      * clipboard + Cmd-V -- puts the password in the pasteboard, where every
        other app on the Mac can read it.

    The delay is per keystroke and deliberate: SecurityAgent drops characters
    typed faster than roughly 40ms apart, which shows up as a wrong password
    rather than as an error.

    The caller is responsible for the text. Nothing here is logged.
    """
    for ch in text:
        for down in (True, False):
            event = CGEventCreateKeyboardEvent(None, 0, down)
            CGEventKeyboardSetUnicodeString(event, len(ch), ch)
            CGEventPost(kCGHIDEventTap, event)
        time.sleep(delay)


# ── frontmost app ──────────────────────────────────────────────────────────
def frontmost_app() -> str | None:
    """Name of the frontmost app, or None.

    NSWorkspace answers in microseconds from inside this process. The obvious
    alternative -- `osascript ... name of first process whose frontmost is
    true` -- costs a subprocess per poll AND needs an Automation grant for
    System Events, which is a permission this app otherwise never wants.
    """
    try:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return app.localizedName() if app else None
    except Exception:
        return None


def activate_app(name: str | None) -> bool:
    """Bring a named app to the front. True if it was found and raised."""
    if not name:
        return False
    try:
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            if app.localizedName() == name:
                # 1 << 1 is NSApplicationActivateIgnoringOtherApps -- without
                # it the raise is a request the current front app can win.
                app.activateWithOptions_(1 << 1)
                return True
    except Exception:
        pass
    return False


def activate_pid(pid: int) -> bool:
    try:
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app:
            app.activateWithOptions_(1 << 1)
            return True
    except Exception:
        pass
    return False


# ── displays ───────────────────────────────────────────────────────────────
def display_rects() -> list[tuple[float, float, float, float]]:
    """(x, y, w, h) in global logical points for every active display.

    The standalone solver only ever looked at the main display, so a challenge
    on the second monitor sat there unsolved. Enumerating them is four lines.
    """
    try:
        err, ids, count = CGGetActiveDisplayList(MAX_DISPLAYS, None, None)
        if err != 0:
            return []
        out = []
        for did in list(ids)[:count]:
            b = CGDisplayBounds(did)
            out.append((b.origin.x, b.origin.y, b.size.width, b.size.height))
        return out
    except Exception:
        return []


# ── screen capture ─────────────────────────────────────────────────────────
def screen_recording_ok() -> bool:
    """True if this process may actually see other apps' windows.

    Without the grant screencapture still succeeds -- it just returns the
    desktop with every window missing, which template-matches as "no challenge
    here" forever. A silent permanent no-op is the worst failure mode
    available, so callers check this and say so out loud.
    """
    try:
        from Quartz import CGPreflightScreenCaptureAccess  # noqa: PLC0415

        return bool(CGPreflightScreenCaptureAccess())
    except Exception:
        return True  # too old to ask; assume yes rather than block


def request_screen_recording() -> None:
    """Raise the Screen Recording prompt once, so a row exists to switch on."""
    try:
        from Quartz import CGRequestScreenCaptureAccess  # noqa: PLC0415

        CGRequestScreenCaptureAccess()
    except Exception:
        pass


def _grab_quartz(x: float, y: float, w: float, h: float):
    """In-process capture. BGR ndarray or None if the API declined.

    Measured on this Mac: 0.07s for a full 2560x1440 display, against 1.5s to
    launch screencapture for the same rectangle. That gap is the difference
    between a solver that can re-check every 0.6s while a challenge settles
    and one that cannot.
    """
    import numpy as np  # noqa: PLC0415
    from Quartz import (  # noqa: PLC0415
        CGDataProviderCopyData,
        CGImageGetBytesPerRow,
        CGImageGetDataProvider,
        CGImageGetHeight,
        CGImageGetWidth,
        CGRectMake,
        CGWindowListCreateImage,
        kCGNullWindowID,
        kCGWindowImageDefault,
        kCGWindowListOptionOnScreenOnly,
    )

    img = CGWindowListCreateImage(
        CGRectMake(x, y, w, h), kCGWindowListOptionOnScreenOnly,
        kCGNullWindowID, kCGWindowImageDefault,
    )
    if img is None:
        return None
    width, height = CGImageGetWidth(img), CGImageGetHeight(img)
    stride = CGImageGetBytesPerRow(img)
    data = CGDataProviderCopyData(CGImageGetDataProvider(img))
    if data is None or width == 0 or height == 0:
        return None
    buf = np.frombuffer(data, dtype=np.uint8)
    if buf.size < height * stride:
        return None
    # Rows are padded to `stride`, and the pixel order in memory is BGRA --
    # dropping the alpha column leaves exactly the BGR that OpenCV expects.
    return buf[:height * stride].reshape(height, stride // 4, 4)[:, :width, :3]


def _grab_screencapture(x: float, y: float, w: float, h: float):
    """Fallback via /usr/sbin/screencapture. Slow, but never deprecated."""
    import cv2  # noqa: PLC0415

    fd, path = tempfile.mkstemp(suffix=".tiff", prefix="yolo-grab-")
    os.close(fd)
    try:
        r = subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-o", "-t", "tiff", "-R",
             f"{int(x)},{int(y)},{int(w)},{int(h)}", path],
            capture_output=True, timeout=15,
        )
        if r.returncode != 0:
            return None
        return cv2.imread(path)
    except Exception:
        return None
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


def grab_region(x: float, y: float, w: float, h: float):
    """BGR ndarray of a screen rectangle in global logical points, or None.

    CGWindowListCreateImage is deprecated in favour of ScreenCaptureKit, which
    has no usable synchronous Python binding. It still works here and it is
    twenty times faster, so it leads -- and screencapture stands behind it for
    the day a macOS release finally removes it.
    """
    try:
        out = _grab_quartz(x, y, w, h)
        if out is not None and out.size:
            return out
    except Exception:
        pass
    return _grab_screencapture(x, y, w, h)
