#!/usr/bin/env python3
"""Floating alert panel -- the persistent, clickable half of the alerts.

Why not Notification Center: posting there needs a notarized app. Tested
unsigned, ad-hoc signed and Apple-Development signed, with a full
NSApplication lifecycle; usernotificationsd answers "Notifications are not
allowed for this application" every time. Homebrew's terminal-notifier is
unsigned too, so it is refused the same way.

This panel does the job Notification Center was wanted for, and does parts of
it better: it floats above other windows, it stays until dismissed rather than
fading after five seconds, and each row has an Open Session button that fires
`claude://resume?session=<uuid>` straight into Claude Desktop.
"""
from __future__ import annotations

import subprocess

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSFloatingWindowLevel,
    NSFont,
    NSMakeRect,
    NSPanel,
    NSScreen,
    NSTextField,
    NSView,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskUtilityWindow,
)
from Foundation import NSObject

ROW_H = 78
MAX_ROWS = 5
WIDTH = 400


def open_session(session_id: str) -> None:
    """Deep-link into an existing Claude Code session in Claude Desktop.

    `claude://resume?session=<uuid>` is the only route that reopens an existing
    session; the claude-cli:// scheme only ever starts a fresh one.
    """
    if not session_id:
        return
    subprocess.run(["open", f"claude://resume?session={session_id}"], check=False)


class _RowTarget(NSObject):
    """Per-row button target: open the session, or drop the row."""

    def initWithPanel_session_key_(self, panel, session, key):
        self = objc.super(_RowTarget, self).init()
        self.panel = panel
        self.session = session
        self.key = key
        return self

    def onOpen_(self, sender):
        open_session(self.session)
        self.panel.dismiss(self.key)

    def onDismiss_(self, sender):
        self.panel.dismiss(self.key)


class AlertPanel:
    """A stack of pending approvals, newest on top."""

    def __init__(self):
        self.items: list[dict] = []
        self._targets: list[_RowTarget] = []
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, ROW_H),
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskUtilityWindow,
            NSBackingStoreBuffered,
            False,
        )
        self.panel.setTitle_("Waiting on you")
        self.panel.setLevel_(NSFloatingWindowLevel)
        self.panel.setReleasedWhenClosed_(False)
        self.panel.setHidesOnDeactivate_(False)
        # Show up on whichever Space is in front, without stealing focus.
        self.panel.setCollectionBehavior_(1 << 0 | 1 << 8)

    # -- content -----------------------------------------------------------

    def add(self, key: str, headline: str, detail: str, project: str, session: str) -> None:
        if any(i["key"] == key for i in self.items):
            return
        self.items.insert(0, {
            "key": key, "headline": headline, "detail": detail,
            "project": project, "session": session,
        })
        del self.items[MAX_ROWS:]
        self._rebuild()

    def dismiss(self, key: str) -> None:
        self.items = [i for i in self.items if i["key"] != key]
        self._rebuild()

    def dismiss_all(self) -> None:
        self.items = []
        self._rebuild()

    def _rebuild(self) -> None:
        if not self.items:
            self.panel.orderOut_(None)
            return

        height = ROW_H * len(self.items)
        content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, height))
        self._targets = []

        for idx, item in enumerate(self.items):
            # Rows are laid out from the bottom up, so index 0 lands on top.
            y = height - ROW_H * (idx + 1)

            title = NSTextField.alloc().initWithFrame_(NSMakeRect(12, y + 50, WIDTH - 24, 18))
            title.setStringValue_(item["headline"][:60])
            title.setFont_(NSFont.systemFontOfSize_weight_(13, 0.3))
            _plain(title)
            content.addSubview_(title)

            sub = NSTextField.alloc().initWithFrame_(NSMakeRect(12, y + 32, WIDTH - 24, 16))
            sub.setStringValue_(item["project"][:70])
            sub.setFont_(NSFont.systemFontOfSize_(11))
            sub.setTextColor_(NSColor.secondaryLabelColor())
            _plain(sub)
            content.addSubview_(sub)

            target = _RowTarget.alloc().initWithPanel_session_key_(
                self, item["session"], item["key"]
            )
            self._targets.append(target)

            if item["session"]:
                open_btn = _button(12, y + 4, 150, "Open Session", "onOpen:", target)
                content.addSubview_(open_btn)
                dismiss_x = 170
            else:
                dismiss_x = 12
            content.addSubview_(
                _button(dismiss_x, y + 4, 110, "Dismiss", "onDismiss:", target)
            )

        self.panel.setContentView_(content)
        self._place(height)
        self.panel.orderFrontRegardless()

    def _place(self, height: int) -> None:
        """Top-right of the main screen, where notifications would have been."""
        screen = NSScreen.mainScreen()
        if screen is None:
            return
        vis = screen.visibleFrame()
        x = vis.origin.x + vis.size.width - WIDTH - 16
        y = vis.origin.y + vis.size.height - height - 16
        self.panel.setFrame_display_(NSMakeRect(x, y, WIDTH, height + 22), True)


def _plain(field: NSTextField) -> None:
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(False)


def _button(x, y, w, title, action, target) -> NSButton:
    btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, 24))
    btn.setTitle_(title)
    btn.setBezelStyle_(NSBezelStyleRounded)
    btn.setFont_(NSFont.systemFontOfSize_(12))
    btn.setTarget_(target)
    btn.setAction_(action)
    return btn
