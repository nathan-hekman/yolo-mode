#!/usr/bin/env python3
"""Claude Code Notification hook -> descriptive Pushover alert.

Reads the hook JSON on stdin and answers the only question worth pushing to a
phone: *what* is Claude blocked on, in *which* project.

Two problems this fixes over the old shell version:

1. Noise. Claude's Notification hook fires both when a tool needs permission
   AND when a session has merely been idle for 60s. Only the first is an
   approval. We confirm it against the session transcript: a tool_use block
   with no matching tool_result is genuinely pending. Idle nudges are recorded
   locally and never pushed.

2. Vagueness. The old alert said "Approval needed" plus a filesystem path.
   Here we pull the pending tool call out of the transcript and describe it --
   the actual shell command, the file being edited, the URL being fetched --
   plus the user request that led to it.

Usage: cat hook.json | claude_notify.py
Never fails loudly: a hook that errors must not disturb the session.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eventlog  # noqa: E402
import secrets_store as secrets  # noqa: E402



DEDUPE_SECS = 240
IDLE_PAT = re.compile(r"waiting for your input|idle|has been waiting", re.I)
# Captures the tool name out of "Claude needs your permission to use Bash".
PERMISSION_PAT = re.compile(r"permission(?:\s+to\s+use\s+([\w\-. ]+))?|needs your approval", re.I)


# --------------------------------------------------------------------------
# transcript reading
# --------------------------------------------------------------------------

def _blocks(msg: dict) -> list[dict]:
    content = (msg or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def session_title(transcript_path: str) -> str:
    """The session's own name, as shown in Claude Code's session list.

    Claude writes a `custom-title` record into the transcript whenever the
    session is named or renamed; the last one wins. Falls back to the most
    recent prompt, which is what an unnamed session shows.
    """
    title = ""
    fallback = ""
    try:
        lines = Path(transcript_path).read_text(errors="ignore").splitlines()
    except Exception:
        return ""
    for ln in lines:
        if '"custom-title"' not in ln and '"last-prompt"' not in ln:
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if d.get("type") == "custom-title":
            title = d.get("customTitle") or title
        elif d.get("type") == "last-prompt":
            fallback = d.get("lastPrompt") or fallback
    return _short(title or fallback, 60)


def pending_tool(transcript_path: str) -> tuple[dict | None, str]:
    """Return (pending tool_use block, last user request text).

    A tool is pending when its id never shows up in a later tool_result. That
    is the difference between "Claude is blocked on you" and "Claude is idle".
    """
    try:
        lines = Path(transcript_path).read_text(errors="ignore").splitlines()[-400:]
    except Exception:
        return None, ""

    tool_uses: list[dict] = []
    done: set[str] = set()
    last_request = ""

    for ln in lines:
        try:
            d = json.loads(ln)
        except Exception:
            continue
        msg = d.get("message") or {}
        role = msg.get("role") or d.get("type")

        for b in _blocks(msg):
            btype = b.get("type")
            if btype == "tool_use":
                tool_uses.append(b)
            elif btype == "tool_result":
                done.add(b.get("tool_use_id") or "")

        if role == "user" and not d.get("isMeta"):
            text = msg.get("content")
            if isinstance(text, str):
                last_request = text
            else:
                parts = [b.get("text", "") for b in _blocks(msg) if b.get("type") == "text"]
                if parts:
                    last_request = " ".join(parts)

    last_request = re.sub(r"<[^>]+>.*?</[^>]+>", "", last_request, flags=re.S).strip()
    for tu in reversed(tool_uses):
        if tu.get("id") not in done:
            return tu, last_request
    return None, last_request


# --------------------------------------------------------------------------
# describing the pending tool in words a human can act on
# --------------------------------------------------------------------------

def _short(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _path_tail(p: str) -> str:
    parts = Path(str(p)).parts
    return "/".join(parts[-2:]) if len(parts) > 1 else str(p)


def describe(tool: dict) -> tuple[str, str]:
    """(headline, detail) for a pending tool call."""
    name = tool.get("name") or "tool"
    inp = tool.get("input") or {}

    if name == "Bash":
        cmd = _short(inp.get("command", ""), 220)
        return f"Run: {_short(cmd, 60)}", cmd
    if name in ("Edit", "Write", "NotebookEdit"):
        verb = {"Edit": "Edit", "Write": "Write", "NotebookEdit": "Edit notebook"}[name]
        f = _path_tail(inp.get("file_path", "?"))
        return f"{verb}: {f}", str(inp.get("file_path", ""))
    if name == "Read":
        return f"Read: {_path_tail(inp.get('file_path', '?'))}", str(inp.get("file_path", ""))
    if name in ("WebFetch", "WebSearch"):
        target = inp.get("url") or inp.get("query") or "?"
        return f"{name}: {_short(target, 60)}", str(target)
    if name in ("Agent", "Task"):
        return f"Subagent: {_short(inp.get('description', '?'), 60)}", json.dumps(inp)[:300]
    if name.startswith("mcp__"):
        pretty = name.split("__", 2)[-1].replace("_", " ")
        return f"{pretty}", _short(json.dumps(inp), 220)
    return f"{name}", _short(json.dumps(inp), 220)


# --------------------------------------------------------------------------
# push
# --------------------------------------------------------------------------

def push(title: str, message: str, session_id: str = "") -> bool:
    try:
        import requests

        user_key, api_token = secrets.pushover()
        if not (user_key and api_token):
            eventlog.log("no Pushover credentials; see README")
            return False  # imported late: the hook must survive a broken venv

        data = {
            "user": user_key,
            "token": api_token,
            "title": title,
            "message": message,
            "priority": 0,
        }
        if session_id:
            # Opens the session in Claude Desktop when tapped on the Mac.
            data["url"] = f"claude://resume?session={session_id}"
            data["url_title"] = "Open session on Mac"
        requests.post(
            "https://api.pushover.net/1/messages.json",
            data=data,
            timeout=12,
        )
        return True
    except Exception as e:
        eventlog.log(f"pushover failed: {e}")
        return False


def already_sent(key: str) -> bool:
    """Suppress a repeat of the same pending tool inside the dedupe window."""
    state = eventlog.load_state()
    sent = state.get("sent", {})
    now = time.time()
    sent = {k: v for k, v in sent.items() if now - v < 3600}
    if now - sent.get(key, 0) < DEDUPE_SECS:
        return True
    sent[key] = now
    state["sent"] = sent
    eventlog.save_state(state)
    return False


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    message = payload.get("message") or ""
    cwd = payload.get("cwd") or ""
    project = Path(cwd).name or "Claude"
    session_id = payload.get("session_id") or ""
    session = session_id[:8]
    transcript = payload.get("transcript_path") or ""

    tool, request = pending_tool(transcript) if transcript else (None, "")
    # The session's name is what Nathan recognises on his phone -- "Courtyard
    # card purchases quality" beats a folder path.
    title = session_title(transcript) if transcript else ""
    where = f"{title} ({project})" if title else project

    # The message decides push-or-not; the transcript only adds detail. Claude
    # appends an assistant turn to the transcript after the tool finishes, so
    # at prompt time the pending tool_use block is often not on disk yet --
    # gating on it would silence real approvals.
    asks_permission = bool(PERMISSION_PAT.search(message))
    if not asks_permission and (IDLE_PAT.search(message) or tool is None):
        kind = "idle" if IDLE_PAT.search(message) else "system"
        eventlog.record(
            kind,
            _short(message or "Claude notification", 90),
            source="claude",
            project=where,
            detail=request,
            pushed=False,
            session=session_id,
        )
        return 0

    if tool is not None:
        headline, detail = describe(tool)
        dedupe_key = f"{session}|{tool.get('id')}"
    else:
        # No transcript detail yet: name the tool out of the message itself.
        m = PERMISSION_PAT.search(message)
        named = (m.group(1) or "").strip() if m and m.lastindex else ""
        headline = f"Permission: {named}" if named else _short(message, 70)
        detail = ""
        dedupe_key = f"{session}|{headline}"

    if already_sent(dedupe_key):
        eventlog.record(
            "approval", headline, source="claude", project=where,
            detail="(duplicate suppressed) " + detail, pushed=False,
            session=session_id,
        )
        return 0

    body = detail if detail != headline else ""
    lines = [l for l in [body, f"Task: {_short(request, 110)}" if request else ""] if l]
    lines.append(f"Project: {project}")
    pushed = push(
        f"{title or project}: {headline}",
        "\n".join(lines),
        session_id=session_id,
    )

    eventlog.record(
        "approval", headline, source="claude", project=where,
        detail=detail, pushed=pushed, session=session_id,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # a hook must never break the session
        try:
            eventlog.record("error", f"notify hook failed: {e}", source="claude")
        except Exception:
            pass
        sys.exit(0)
