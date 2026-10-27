#!/usr/bin/env python3
"""Pushover credentials, kept out of the repo.

Reads, in order:
  1. PUSHOVER_USER_KEY / PUSHOVER_API_TOKEN in the environment
  2. ~/.config/pushover/env, the stack-wide 0600 file that ~/bin/pushover-creds
     and scrape_common.pushover_creds keep current from 1Password (Bots vault)
  3. ~/.config/yolo-mode/pushover.json  {"user_key": "...", "api_token": "..."}

Returns ("", "") when neither is set, and callers skip the push rather than
failing -- the app still works locally, it just cannot reach a phone.

This file exists because the credentials used to be hardcoded in two scripts,
which is fine in a private repo and a leak the moment one is published.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

CONFIG_FILE = Path.home() / ".config/yolo-mode/pushover.json"
STACK_FILE = Path.home() / ".config/pushover/env"


def pushover() -> tuple[str, str]:
    user = os.environ.get("PUSHOVER_USER_KEY", "")
    token = os.environ.get("PUSHOVER_API_TOKEN", "")
    if user and token:
        return user, token
    try:
        vals = dict(line.strip().split("=", 1)
                    for line in STACK_FILE.read_text().splitlines() if "=" in line)
        if vals.get("PUSHOVER_USER_KEY") and vals.get("PUSHOVER_API_TOKEN"):
            return vals["PUSHOVER_USER_KEY"], vals["PUSHOVER_API_TOKEN"]
    except OSError:
        pass
    try:
        data = json.loads(CONFIG_FILE.read_text())
        return data.get("user_key", ""), data.get("api_token", "")
    except Exception:
        return "", ""


def configured() -> bool:
    return all(pushover())


_device = ""


def device() -> str:
    """This Mac's short name, e.g. "n8server". Every push title starts with
    it in brackets, because two Macs send through the same Pushover app and
    "Approve on Mac" did not say which Mac (asked for 2026-10-09)."""
    global _device
    if not _device:
        try:
            _device = subprocess.run(["/usr/sbin/scutil", "--get", "LocalHostName"],
                                     capture_output=True, text=True,
                                     timeout=3).stdout.strip()
        except Exception:
            pass
        _device = _device or os.uname().nodename.split(".")[0]
    return _device


def tag(title: str) -> str:
    return f"[{device()}] {title}"
