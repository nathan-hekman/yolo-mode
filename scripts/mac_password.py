#!/usr/bin/env python3
"""The Mac login password, fetched from 1Password on demand.

Used by the SecurityAgent auto-fill rule (see yolo_watch.auth_fill). Nothing
here caches, logs, writes or returns the value anywhere except to its one
caller, which types it and drops it.

WHY op AND NOT THE KEYCHAIN
---------------------------
Storing the login password in the login keychain is circular: the keychain is
unlocked by that same password, so anything running as Nathan can read it back
with no gate at all. 1Password keeps it behind a vault that a stolen laptop
session cannot open, and a read-only service account scoped to one vault means
a compromised YOLO Mode can reach that item and nothing else.

TWO WAYS IN, IN ORDER
---------------------
  1. A service-account token in TOKEN_FILE (0600, outside any repo). Tap-free,
     scoped to the Bots vault alone, and `op` is physically prevented from
     reading Private -- the failure is enforced by 1Password's servers, not by
     this code being careful.
  2. The 1Password desktop-app integration. No token to create, but every read
     raises the "Access Requested" dialog. That is survivable here only because
     the watcher auto-clicks Authorize (config.json, the buttons=["Authorize"]
     rule) -- otherwise this call would hang for 60s and time out, which is
     exactly what it did before that rule was fixed.

Create the service account by hand, in a terminal, never from an agent session:

    op service-account create yolo-mode --vault Bots:read_items
    # the command prints a paragraph, not a bare token -- keep only the token:
    grep -oE 'ops_[A-Za-z0-9_.=-]+' <output> > ~/.config/yolo-mode/op-token
    chmod 600 ~/.config/yolo-mode/op-token

No --expires-in: 1Password caps it at 90 days, and a token that dies
mid-automation is a worse failure than a long-lived read-only one scoped to a
single vault. Verified 2026-08-19 -- with this token `op vault list` shows
Bots and nothing else, and reading Private fails outright.

The token is printed once and would otherwise land in a session transcript, so
redirect the command's output straight to the file and never echo it.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

OP = "/opt/homebrew/bin/op"
TOKEN_FILE = Path.home() / ".config/yolo-mode/op-token"
DEFAULT_REF = "op://Bots/Mac login/password"

# A read has to outlast a 1Password unlock plus the Authorize dialog plus the
# watcher's next poll. Well under SecurityAgent's own patience, which is what
# actually bounds this.
TIMEOUT = 45


class PasswordUnavailable(RuntimeError):
    """Raised instead of returning "" -- an empty password would be typed."""


def _op_reference() -> str:
    """Secret reference from config.json, or the default Bots item."""
    try:
        config = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text())
        return config.get("auth_fill", {}).get("op_item") or DEFAULT_REF
    except Exception:
        return DEFAULT_REF


def _environment() -> dict:
    env = dict(os.environ)
    # Both are removed first: inheriting a half-set token from whatever
    # launched the app is how a read silently authenticates as the wrong
    # identity and returns the wrong vault's item.
    env.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
    try:
        token = TOKEN_FILE.read_text().strip()
        if token:
            env["OP_SERVICE_ACCOUNT_TOKEN"] = token
    except FileNotFoundError:
        pass  # fall through to the desktop-app integration
    except Exception:
        pass
    return env


def login_password() -> str:
    """The Mac login password. Raises PasswordUnavailable rather than lying.

    `op read` writes the secret to stdout and nothing else, so it never lands
    in a file or a log. The subprocess output is not captured to any variable
    that outlives this call.
    """
    if not Path(OP).exists():
        raise PasswordUnavailable(f"1Password CLI not installed at {OP}")
    try:
        result = subprocess.run(
            [OP, "read", _op_reference(), "--no-newline"],
            capture_output=True, text=True, timeout=TIMEOUT, env=_environment(),
        )
    except subprocess.TimeoutExpired:
        raise PasswordUnavailable(
            "op read timed out -- 1Password never authorized the CLI"
        ) from None
    if result.returncode != 0:
        # stderr from op names the vault and the item, never the secret.
        raise PasswordUnavailable((result.stderr or "op read failed").strip()[:200])
    password = result.stdout
    if not password:
        raise PasswordUnavailable("1Password returned an empty password field")
    return password


def source() -> str:
    """Which route a read would take, for the status line. No secret involved."""
    if not Path(OP).exists():
        return "unavailable (no op CLI)"
    return "service account" if TOKEN_FILE.exists() else "1Password desktop app"


if __name__ == "__main__":
    # Self-test. Asserts a length, never prints the value -- a test that echoes
    # the secret puts it in the terminal scrollback and the shell history.
    try:
        print(f"source: {source()}")
        print(f"read ok: {len(login_password())} characters")
    except PasswordUnavailable as exc:
        raise SystemExit(f"password unavailable: {exc}")
