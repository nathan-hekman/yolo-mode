#!/usr/bin/env python3
"""Shared append-only event log for yolo-mode.

Everything that wants to record an approval event writes here: the Quartz
window watcher, the Claude Code notification hook, and the menubar/window UI
read it back. One JSON object per line so the UI can render structured rows
(kind, what, project, whether a push actually went out) instead of grepping
free text.

Two files on purpose:
  events.jsonl  -- machine-readable, what the UI shows
  .log          -- human-readable tail, what "Open log" opens
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

LOG_DIR = Path.home() / "Library/Logs"
EVENTS = LOG_DIR / "yolo-mode-events.jsonl"
LOGFILE = LOG_DIR / "yolo-mode.log"
STATE_DIR = Path.home() / "Library/Application Support/YOLOMode"
STATE_FILE = STATE_DIR / "state.json"

MAX_EVENTS = 500


def log(msg: str) -> None:
    """Append a human-readable line to the plain-text log (and stdout)."""
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOGFILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def record(
    kind: str,
    what: str,
    *,
    source: str = "",
    project: str = "",
    detail: str = "",
    pushed: bool = False,
    session: str = "",
) -> None:
    """Append one structured event and mirror it into the plain-text log.

    kind:    approval | idle | system | error
    what:    the one-line headline the user reads first
    source:  claude | watcher
    project: short project/app name for grouping
    pushed:  True if a Pushover notification actually went out
    """
    ev = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "what": what,
        "source": source,
        "project": project,
        "detail": detail,
        "pushed": bool(pushed),
        # Full CLI session uuid: what claude://resume?session=<uuid> needs.
        "session": session,
    }
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(EVENTS, "a") as f:
            f.write(json.dumps(ev) + "\n")
        _trim()
    except Exception:
        pass
    flag = "push" if pushed else "quiet"
    log(f"[{kind}/{flag}] {project + ': ' if project else ''}{what}")


def _trim() -> None:
    """Keep the events file bounded; it is a rolling window, not an archive."""
    try:
        if EVENTS.stat().st_size < 400_000:
            return
        lines = EVENTS.read_text().splitlines()[-MAX_EVENTS:]
        EVENTS.write_text("\n".join(lines) + "\n")
    except Exception:
        pass


def read_events(limit: int = 50) -> list[dict]:
    out: list[dict] = []
    try:
        with open(EVENTS) as f:
            for ln in f.read().splitlines()[-limit:]:
                try:
                    out.append(json.loads(ln))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass
