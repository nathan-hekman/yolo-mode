#!/usr/bin/env python3
"""Menubar + Dock app around the approval watcher.

The menubar shows the wordmark: YOLO when armed, YOLO with a pause glyph when
the kill switch (~/.yolo_mode_off) is set. Text rather than an icon because the
name is the status, and it reads at a glance in a crowded menubar.

Also a real Dock app with a window listing recent approval events, because a
menubar count alone can't tell you *what* was asked. Closing the window leaves
the watcher running; clicking the Dock icon brings it back.

rumps owns the main thread; the Quartz watcher runs on a daemon thread. This
is the entry point the LaunchAgent runs.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import yolo_watch as aw  # noqa: E402
import eventlog  # noqa: E402
from alert_panel import AlertPanel, open_session  # noqa: E402

import objc  # noqa: E402
import rumps  # noqa: E402
from AppKit import (  # noqa: E402
    NSApplication,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSFont,
    NSImage,
    NSMakeRect,
    NSScrollView,
    NSTextField,
    NSTextView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from AppKit import (  # noqa: E402
    NSAttributedString,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMutableParagraphStyle,
    NSParagraphStyleAttributeName,
)
from Foundation import NSMutableAttributedString, NSObject  # noqa: E402

SHOW_FLAG = Path.home() / ".yolo_mode_show"
NOTIFY_FLAG = Path.home() / ".yolo_mode_notify_test"
# Kept from the standalone turnstile-autosolve, so anything that touched this
# path to pause the solver still pauses it.
TURNSTILE_OFF_FLAG = Path.home() / ".turnstile_autosolve_off"
OUT_LOG = Path.home() / "Library/Logs/yolo-mode.out.log"

# The menubar wears the brand: the name is the status.
TITLE_ARMED = "YOLO"
TITLE_PAUSED = "YOLO ⏸"

KIND_MARK = {
    "approval": "●", "auto-approved": "✓", "idle": "○", "system": "○", "error": "▲",
}
KIND_COLOUR = {
    "approval": NSColor.systemRedColor,
    "auto-approved": NSColor.systemGreenColor,
    "error": NSColor.systemOrangeColor,
    "idle": NSColor.secondaryLabelColor,
    "system": NSColor.secondaryLabelColor,
}


def _todays_counts() -> tuple[int, int]:
    """(approvals pushed today, events recorded today)."""
    today = date.today().isoformat()
    pushed = total = 0
    for ev in eventlog.read_events(300):
        if not ev.get("ts", "").startswith(today):
            continue
        total += 1
        if ev.get("pushed"):
            pushed += 1
    return pushed, total


def _newest_ts() -> str:
    events = eventlog.read_events(1)
    return events[-1].get("ts", "") if events else ""


def _clip(s: str, width: int = 78) -> str:
    """One line per fact -- wrapped paragraphs turn the list into a wall."""
    s = " ".join(str(s).split())
    return s if len(s) <= width else s[: width - 1] + "…"


def _plain_field(field) -> None:
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(False)


def _attrs(size: float, weight: float, color, spacing: float = 0.0) -> dict:
    a = {
        NSFontAttributeName: NSFont.systemFontOfSize_weight_(size, weight),
        NSForegroundColorAttributeName: color,
    }
    if spacing:
        style = NSMutableParagraphStyle.alloc().init()
        style.setParagraphSpacing_(spacing)
        a[NSParagraphStyleAttributeName] = style
    return a


def _render_events(limit: int = 40):
    """Build the event list as styled text.

    Colour carries the meaning that used to need a legend: red for something
    waiting on you, green for something already handled, grey for noise.
    """
    events = eventlog.read_events(limit)
    out = NSMutableAttributedString.alloc().init()

    if not events:
        empty = (
            "Nothing waiting.\n\n"
            "This fills in when something actually needs you: a Claude Code tool "
            "asking permission, a macOS password prompt, a 1Password approval.\n\n"
            "Idle nudges are listed here but never sent to your phone."
        )
        out.appendAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(
                empty, _attrs(13, 0.0, NSColor.secondaryLabelColor())
            )
        )
        return out

    for ev in reversed(events):
        kind = ev.get("kind", "")
        ts = ev.get("ts", "")
        try:
            clock = datetime.fromisoformat(ts).strftime("%b %-d  %-I:%M %p")
        except Exception:
            clock = ts

        colour = KIND_COLOUR.get(kind, NSColor.secondaryLabelColor)()
        badge = "PHONE" if ev.get("pushed") else ""
        head = f"{KIND_MARK.get(kind, '·')}  {_clip(ev.get('what', ''), 64)}\n"
        out.appendAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(
                head, _attrs(13.5, 0.4, colour)
            )
        )

        meta = "     " + "  ·  ".join(
            p for p in [clock, ev.get("project") or "", badge] if p
        ) + "\n"
        out.appendAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(
                meta, _attrs(11, 0.0, NSColor.tertiaryLabelColor())
            )
        )

        detail = (ev.get("detail") or "").strip()
        if detail and detail != ev.get("what"):
            out.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    "     " + _clip(detail, 72) + "\n",
                    _attrs(11.5, 0.0, NSColor.secondaryLabelColor(), spacing=10),
                )
            )
        else:
            out.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(
                    "\n", _attrs(4, 0.0, NSColor.clearColor())
                )
            )
    return out


class EventWindow:
    """Plain AppKit window: status header, event list, three buttons."""

    def __init__(self, app: "YoloApp"):
        self.app = app
        rect = NSMakeRect(0, 0, 700, 520)
        mask = (
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable
        )
        self.win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, mask, NSBackingStoreBuffered, False
        )
        self.win.setTitle_("YOLO Mode")
        self.win.setReleasedWhenClosed_(False)
        self.win.center()

        content = self.win.contentView()

        self.title_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(20, 476, 400, 28)
        )
        self.title_label.setStringValue_("YOLO Mode")
        self.title_label.setFont_(NSFont.systemFontOfSize_weight_(20, 0.56))
        _plain_field(self.title_label)
        content.addSubview_(self.title_label)

        self.header = NSTextField.alloc().initWithFrame_(NSMakeRect(20, 452, 660, 20))
        _plain_field(self.header)
        self.header.setFont_(NSFont.systemFontOfSize_(12))
        self.header.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(self.header)

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(16, 56, 668, 386))
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(0)
        self.text = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, 668, 386))
        self.text.setEditable_(False)
        self.text.setTextContainerInset_((10, 12))
        self.text.setDrawsBackground_(False)
        scroll.setDocumentView_(self.text)
        content.addSubview_(scroll)

        self.pause_btn = self._button(16, "Pause", "onPause:")
        content.addSubview_(self.pause_btn)
        content.addSubview_(self._button(160, "Open Log", "onLog:"))
        content.addSubview_(self._button(304, "Clear History", "onClear:"))

        # Buttons target a tiny ObjC shim so AppKit can call back into Python.
        self.target = _WindowTarget.alloc().initWithOwner_(self)
        for sub in content.subviews():
            if isinstance(sub, NSButton):
                sub.setTarget_(self.target)

    def _button(self, x: float, title: str, action: str) -> NSButton:
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, 16, 140, 30))
        btn.setTitle_(title)
        btn.setBezelStyle_(1)
        btn.setAction_(action)
        return btn

    def refresh(self) -> None:
        pushed, total = _todays_counts()
        auto = "off" if aw.AUTO_OFF_FLAG.exists() else "on"
        state = "Paused" if self.app.paused else "Watching"
        self.header.setStringValue_(
            f"{state}  ·  auto-approve {auto}  ·  "
            f"{pushed} sent to phone today  ·  {total} events today"
        )
        self.pause_btn.setTitle_("Resume" if self.app.paused else "Pause")
        if self.win.isVisible():
            self.text.textStorage().setAttributedString_(_render_events())

    def show(self) -> None:
        self.refresh()
        self.win.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)


class _WindowTarget(NSObject):
    """ObjC target object; AppKit buttons can only message an ObjC receiver."""

    def initWithOwner_(self, owner):
        self = objc.super(_WindowTarget, self).init()
        self.owner = owner
        return self

    def onPause_(self, sender):
        self.owner.app.toggle_pause(None)
        self.owner.refresh()

    def onLog_(self, sender):
        subprocess.run(["open", str(eventlog.LOGFILE)], check=False)

    def onClear_(self, sender):
        try:
            eventlog.EVENTS.unlink(missing_ok=True)
        except Exception:
            pass
        self.owner.refresh()


class YoloApp(rumps.App):
    def __init__(self):
        super().__init__(name="YOLO Mode", title=TITLE_ARMED, quit_button=None)
        self.status_item = rumps.MenuItem("Status: starting", callback=None)
        self.count_item = rumps.MenuItem("Sent to phone today: 0", callback=None)
        self.last_item = rumps.MenuItem("Last: nothing yet", callback=None)
        self.pause_item = rumps.MenuItem("Pause", callback=self.toggle_pause)
        self.auto_item = rumps.MenuItem(
            "Auto-approve system dialogs", callback=self.toggle_auto
        )
        self.turnstile_item = rumps.MenuItem(
            "Auto-solve Cloudflare checks", callback=self.toggle_turnstile
        )
        self.menu = [
            self.status_item,
            self.count_item,
            self.last_item,
            None,
            rumps.MenuItem("Open Window", callback=self.open_window),
            rumps.MenuItem("Open Log", callback=self.open_log),
            rumps.MenuItem("Test Auto-Approve", callback=self.test_auto),
            rumps.MenuItem("Solve Cloudflare Now", callback=self.solve_turnstile),
            rumps.MenuItem("Enable Notifications", callback=self.enable_notifications),
            rumps.MenuItem("Fix Permissions…", callback=self.open_privacy),
            None,
            self.auto_item,
            self.turnstile_item,
            self.pause_item,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]
        self.paused = False
        self.turnstile_solves = 0
        self.window: EventWindow | None = None
        self.panel = AlertPanel()
        self._icon_kind = None
        # Only raise the panel for events that arrive after launch.
        self._last_seen_ts = _newest_ts()

    # -- lifecycle ---------------------------------------------------------

    def start_watcher(self):
        threading.Thread(
            target=aw.watch_loop,
            kwargs={"status_cb": self._on_status,
                    "turnstile_cb": self._on_turnstile},
            daemon=True,
        ).start()

    def _on_status(self, paused: bool):
        self.paused = paused

    def _on_turnstile(self, solves: int):
        self.turnstile_solves = solves

    def _ensure_window(self) -> EventWindow:
        if self.window is None:
            self.window = EventWindow(self)
        return self.window

    # -- menubar icon ------------------------------------------------------

    def _apply_icon(self):
        """Menubar wordmark, bold enough to find at a glance."""
        want = TITLE_PAUSED if self.paused else TITLE_ARMED
        if want == self._icon_kind:
            return
        self.title = want
        try:
            button = self._nsapp.nsstatusitem.button()
            button.setFont_(NSFont.systemFontOfSize_weight_(12, 0.56))
        except Exception:
            pass
        self._icon_kind = want

    # -- refresh -----------------------------------------------------------

    @rumps.timer(3)
    def refresh(self, _):
        self._apply_icon()
        # `touch ~/.yolo_mode_show` opens the window from anywhere --
        # a shell, a script, or a test.
        if SHOW_FLAG.exists():
            SHOW_FLAG.unlink(missing_ok=True)
            self.open_window(None)
        # Same trick for the notification check, so a reinstall can verify it
        # from a script instead of asking someone to find a menu item.
        if NOTIFY_FLAG.exists():
            NOTIFY_FLAG.unlink(missing_ok=True)
            self.enable_notifications(None)
        pushed, total = _todays_counts()
        self.status_item.title = "Status: paused" if self.paused else "Status: armed"
        self.pause_item.title = "Resume" if self.paused else "Pause"
        self.count_item.title = f"Sent to phone today: {pushed}  (of {total} events)"
        self.auto_item.state = 0 if aw.AUTO_OFF_FLAG.exists() else 1
        self.turnstile_item.state = 0 if TURNSTILE_OFF_FLAG.exists() else 1
        if self.turnstile_solves:
            self.turnstile_item.title = (
                f"Auto-solve Cloudflare checks ({self.turnstile_solves} today)"
            )
        events = eventlog.read_events(20)
        if events:
            last = events[-1]
            clock = last.get("ts", "")[11:16]
            self.last_item.title = f"Last {clock}: {last.get('what', '')[:48]}"
        self._raise_new_approvals(events)
        if self.window is not None:
            self.window.refresh()

    def _raise_new_approvals(self, events: list[dict]) -> None:
        """Put anything genuinely waiting into the floating panel."""
        for ev in events:
            ts = ev.get("ts", "")
            if ts <= self._last_seen_ts or ev.get("kind") != "approval":
                continue
            if "(duplicate suppressed)" in (ev.get("detail") or ""):
                continue
            self.panel.add(
                key=f"{ts}|{ev.get('what', '')}",
                headline=ev.get("what", ""),
                detail=ev.get("detail", ""),
                project=ev.get("project", ""),
                session=ev.get("session", ""),
            )
        if events:
            self._last_seen_ts = max(self._last_seen_ts, events[-1].get("ts", ""))

    # -- actions -----------------------------------------------------------

    def toggle_pause(self, _):
        if aw.OFF_FLAG.exists():
            aw.OFF_FLAG.unlink(missing_ok=True)
            self.paused = False
        else:
            aw.OFF_FLAG.touch()
            self.paused = True
        self._apply_icon()

    def toggle_auto(self, _):
        """Auto-approval of system permission dialogs; on unless flagged off."""
        if aw.AUTO_OFF_FLAG.exists():
            aw.AUTO_OFF_FLAG.unlink(missing_ok=True)
            eventlog.log("auto-approve enabled")
        else:
            aw.AUTO_OFF_FLAG.touch()
            eventlog.log("auto-approve disabled")

    def toggle_turnstile(self, _):
        """The Cloudflare solver alone; the global pause still overrides it."""
        import turnstile  # noqa: PLC0415

        if turnstile.OFF_FLAG.exists():
            turnstile.OFF_FLAG.unlink(missing_ok=True)
            eventlog.log("turnstile auto-solve enabled")
        else:
            turnstile.OFF_FLAG.touch()
            eventlog.log("turnstile auto-solve disabled")

    def solve_turnstile(self, _):
        """Solve whatever challenge is on screen right now, on demand.

        The watcher only looks when a CDP port reports one or a browser is
        frontmost. Clicking this menu item is how you say "look anyway" --
        useful when the challenge is sitting in a window that never came to
        the front.
        """
        def run():
            import turnstile  # noqa: PLC0415

            result = turnstile.solve_once()
            eventlog.log(f"turnstile manual solve: {result}")

        threading.Thread(target=run, daemon=True).start()

    def open_window(self, _):
        self._ensure_window().show()

    def open_log(self, _):
        subprocess.run(["open", str(eventlog.LOGFILE)], check=False)

    def test_auto(self, _):
        """End-to-end test of auto-approval, on a dialog of our own making.

        Raises a real dialog with a real Allow button and waits for the
        watcher to click it. Nothing is granted either way -- the only rule
        that matches it is keyed to this exact title. Never test on a live
        permission dialog: the click would grant whatever it was asking for.

        The failure this catches is silent by nature. Without Automation
        permission the click just times out and the watcher falls back to
        alerting, which looks the same as a dialog it chose not to touch.
        """
        threading.Thread(target=self._run_self_test, daemon=True).start()

    def _run_self_test(self):
        script = (
            'display dialog "Auto-approval self-test. The watcher should '
            'click Allow within about 10 seconds." '
            'with title "YOLO Mode self-test" '
            'buttons {"Don\'t Allow", "Allow"} default button "Allow" '
            'giving up after 25'
        )
        try:
            out = subprocess.run(
                ["/usr/bin/osascript", "-e", script],
                capture_output=True, text=True, check=False, timeout=40,
            )
            clicked = "Allow" in out.stdout and "gave up:true" not in out.stdout
        except Exception as e:
            out, clicked = None, False
            eventlog.log(f"self-test error: {e}")

        if clicked:
            eventlog.record(
                "system", "Auto-approve self-test passed: dialog was clicked",
                source="watcher", project="YOLO Mode",
                detail="A real dialog appeared and the watcher pressed Allow.",
            )
        else:
            eventlog.record(
                "error", "Auto-approve self-test failed: dialog was not clicked",
                source="watcher", project="YOLO Mode",
                detail="Grant Accessibility + Automation in System Settings.",
            )

    def enable_notifications(self, _):
        """Ask macOS for notification permission and log what it says.

        Kept as an explicit action because the answer is informative: macOS
        refuses this app outright unless it is notarized, and the refusal is
        otherwise invisible.
        """
        def run():
            try:
                from UserNotifications import (
                    UNAuthorizationOptionAlert,
                    UNAuthorizationOptionSound,
                    UNUserNotificationCenter,
                )

                centre = UNUserNotificationCenter.currentNotificationCenter()
                centre.requestAuthorizationWithOptions_completionHandler_(
                    UNAuthorizationOptionAlert | UNAuthorizationOptionSound,
                    lambda granted, err: eventlog.record(
                        "system" if granted else "error",
                        f"Notification permission {'granted' if granted else 'refused'}",
                        source="watcher", project="YOLO Mode",
                        detail=str(err) if err else "",
                    ),
                )
            except Exception as e:
                eventlog.log(f"notification request failed: {e}")

        threading.Thread(target=run, daemon=True).start()

    def open_privacy(self, _):
        """Auto-approval needs Accessibility; clicking needs Automation too."""
        # Ask macOS for the Accessibility prompt first: without it the Privacy
        # pane may not list this app at all, and Accessibility is the grant the
        # clicking actually needs.
        try:
            from ApplicationServices import (
                AXIsProcessTrustedWithOptions,
                kAXTrustedCheckOptionPrompt,
            )

            AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
        except Exception as e:
            eventlog.log(f"accessibility prompt failed: {e}")
        subprocess.run(
            ["open",
             "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
            check=False,
        )

    def quit_app(self, _):
        rumps.quit_application()


_APP: YoloApp | None = None


def _install_dock_reopen():
    """Make a Dock-icon click reopen the window.

    rumps' own NSApplication delegate has no reopen handler, so we graft one
    onto its class before the delegate is instantiated.
    """
    try:
        from rumps.rumps import NSApp as RumpsDelegate

        def applicationShouldHandleReopen_hasVisibleWindows_(self, app, flag):
            if _APP is not None:
                _APP.open_window(None)
            return True

        objc.classAddMethods(
            RumpsDelegate,
            [
                objc.selector(
                    applicationShouldHandleReopen_hasVisibleWindows_,
                    selector=b"applicationShouldHandleReopen:hasVisibleWindows:",
                    signature=b"Z@:@Z",
                )
            ],
        )
    except Exception as e:
        eventlog.log(f"dock reopen handler unavailable: {e}")


def _brand():
    """Make an un-bundled python script present itself as a real app.

    Without this the Dock tile and app menu both read "Python", because that
    is what the running executable's bundle says. Patching the main bundle's
    info dictionary is the standard fix for scripts that aren't inside a .app.
    """
    try:
        from Foundation import NSBundle

        bundle = NSBundle.mainBundle()
        for info in (bundle.localizedInfoDictionary(), bundle.infoDictionary()):
            if info is not None:
                info["CFBundleName"] = "YOLO Mode"
                info["CFBundleDisplayName"] = "YOLO Mode"
    except Exception as e:
        eventlog.log(f"branding failed: {e}")

    try:
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "bell.badge.fill", "YOLO Mode"
        )
        if img is not None:
            img.setSize_((128, 128))
            NSApplication.sharedApplication().setApplicationIconImage_(img)
    except Exception:
        pass


def _capture_output():
    """Send stdout and stderr to the log file ourselves.

    The LaunchAgent runs `open -a`, so launchd's StandardOutPath captures
    `open`'s output, not the app's -- LaunchServices starts the app as a
    separate process with its own descriptors. Tracebacks were vanishing.
    """
    try:
        OUT_LOG.parent.mkdir(parents=True, exist_ok=True)
        fh = open(OUT_LOG, "a", buffering=1)
        os.dup2(fh.fileno(), sys.stdout.fileno())
        os.dup2(fh.fileno(), sys.stderr.fileno())
        print(f"--- started {datetime.now():%Y-%m-%d %H:%M:%S} ---")
    except Exception as e:
        eventlog.log(f"could not capture output: {e}")


if __name__ == "__main__":
    _capture_output()
    # Regular activation policy puts the app in the Dock and Cmd-Tab; a plain
    # rumps app is menubar-only.
    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyRegular
    )
    _brand()
    _APP = YoloApp()
    _install_dock_reopen()
    _APP.start_watcher()
    _APP.run()
