#!/usr/bin/env python3
"""Pushover credentials, kept out of the repo.

Reads, in order:
  1. PUSHOVER_USER_KEY / PUSHOVER_API_TOKEN in the environment
  2. ~/.config/yolo-mode/pushover.json  {"user_key": "...", "api_token": "..."}

Returns ("", "") when neither is set, and callers skip the push rather than
failing -- the app still works locally, it just cannot reach a phone.

This file exists because the credentials used to be hardcoded in two scripts,
which is fine in a private repo and a leak the moment one is published.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_FILE = Path.home() / ".config/yolo-mode/pushover.json"


def pushover() -> tuple[str, str]:
    user = os.environ.get("PUSHOVER_USER_KEY", "")
    token = os.environ.get("PUSHOVER_API_TOKEN", "")
    if user and token:
        return user, token
    try:
        data = json.loads(CONFIG_FILE.read_text())
        return data.get("user_key", ""), data.get("api_token", "")
    except Exception:
        return "", ""


def configured() -> bool:
    return all(pushover())
