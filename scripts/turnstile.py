#!/usr/bin/env python3
"""Cloudflare Turnstile auto-solver, folded into YOLO Mode.

Merged from the standalone turnstile-autosolve repo. Same job -- click the
"Verify you are human" checkbox -- but reusing YOLO Mode's clicker, log,
event feed and menubar instead of carrying its own copy of each.

WHY PIXELS AND NOT THE DOM
--------------------------
Cloudflare draws the same widget for the inline `.cf-turnstile` form control
and the full-page "Just a moment..." interstitial, but the interstitial hides
it in a closed shadow DOM, so no script can read its position. The pixels are
identical either way, so template-matching a screen grab finds both.

The click has to be a real one. Turnstile checks `isTrusted`, and a CDP or
Playwright click reports false, so it never counts. macinput.mouse_click posts
CGEvents to the HID tap, which is what a physical mouse produces -- the same
reason SeleniumBase reaches for PyAutoGUI here.

TWO DETECTION LAYERS, BOTH GATED
--------------------------------
  1. CDP debug ports (the scraper Braves on :9222/:9224/:9225). Reading
     `/json/list` over plain HTTP is cheap and, unlike `connect_over_cdp`,
     does not hang on a busy scraper Brave. An interstitial sets the tab title
     to "Just a moment...", so a challenged tab is visible without attaching.
     We foreground that exact tab, solve, then hand focus back.
  2. Whatever Chrome/Brave window is frontmost -- catches inline widgets,
     which never change the tab title so layer 1 cannot see them.

The gate matters for more than speed: nothing is ever captured unless a port
reports a challenge or a browser is already frontmost. A watcher that grabbed
the screen every two seconds regardless would be a screen recorder.

Kill switches: `~/.yolo_mode_off` (all of YOLO Mode),
`~/.yolo_mode_no_autoapprove` (all clicking), `~/.turnstile_autosolve_off`
(this solver only -- kept from the standalone tool so anything that touched
that path still works).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eventlog  # noqa: E402
import macinput  # noqa: E402
from macinput import autorelease_pool  # noqa: E402
from eventlog import log  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
CONFIG = REPO / "config.json"
TEMPLATE = REPO / "scripts" / "assets" / "turnstile_widget.png"

OFF_FLAG = Path.home() / ".turnstile_autosolve_off"

# Checkbox center as an offset into the 306x56 template (the empty box sits at
# the far left), scaled with whatever match scale won.
TPL_W, TPL_H = 306, 56
CHECKBOX_DX, CHECKBOX_DY = 22, 30
# The template was captured at 1x. `screencapture -R` returns logical points,
# so 1.0 is the usual winner; the rest cover page zoom and scaled displays.
SCALES = (1.0, 1.25, 0.75, 1.5, 0.5, 2.0)
MATCH_THRESHOLD = 0.70
# The coarse pass runs at half resolution in greyscale, which is ~25x cheaper
# than matching six scales against a full-colour 2560x1440 grab. It only has to
# decide "worth a closer look", so its bar is lower than the real threshold.
COARSE_SCALE = 0.5
COARSE_THRESHOLD = 0.55
REFINE_PAD = 160  # full-res points searched around a coarse hit

DEFAULTS = {
    "enabled": True,
    "ports": [9222, 9223, 9224, 9225],
    "interval": 2.0,
    "cooldown": 8.0,
    "settle": 6.0,
    "max_attempts": 3,
    "port_app": "Brave Browser",
    "browsers": [
        "Brave Browser", "Brave Browser Beta", "Brave Browser Nightly",
        "Google Chrome", "Google Chrome Canary", "Chromium",
    ],
}

CHALLENGE_TITLE_MARKERS = ("just a moment", "one more step",
                           "verifying you are human", "checking your browser")
CHALLENGE_URL_MARKERS = ("/cdn-cgi/challenge",)


def load_config() -> dict:
    """config.json's `turnstile` block over DEFAULTS, then env overrides.

    The env names are the standalone tool's, so an existing shell that set
    TURNSTILE_WATCH_PORTS keeps working.
    """
    cfg = dict(DEFAULTS)
    try:
        block = json.loads(CONFIG.read_text()).get("turnstile") or {}
        cfg.update({k: v for k, v in block.items() if not k.startswith("_")})
    except Exception as e:
        log(f"turnstile: config error, using defaults: {e}")

    env = os.environ.get("TURNSTILE_WATCH_PORTS")
    if env:
        cfg["ports"] = [int(p) for p in env.split(",") if p.strip()]
    for key, name in (("interval", "TURNSTILE_WATCH_INTERVAL"),
                      ("cooldown", "TURNSTILE_WATCH_COOLDOWN"),
                      ("settle", "TURNSTILE_WATCH_SETTLE")):
        if os.environ.get(name):
            cfg[key] = float(os.environ[name])
    if os.environ.get("TURNSTILE_DEFAULT_PORT_APP"):
        cfg["port_app"] = os.environ["TURNSTILE_DEFAULT_PORT_APP"]
    return cfg


def port_app(cfg: dict, port: int) -> str:
    return os.environ.get(f"TURNSTILE_PORT_APP_{port}", cfg["port_app"])


# ── template matching ──────────────────────────────────────────────────────
_tpl_cache: list = []


def _template():
    import cv2  # noqa: PLC0415

    if not _tpl_cache:
        img = cv2.imread(str(TEMPLATE))
        if img is None:
            raise FileNotFoundError(f"turnstile template missing: {TEMPLATE}")
        _tpl_cache.append(img)
    return _tpl_cache[0]


def _search(image_gray, tpl_gray, scales, offset=(0.0, 0.0)):
    """Best (x, y, conf) over `scales`, in image pixels plus `offset`."""
    import cv2  # noqa: PLC0415

    best = None
    for s in scales:
        w, h = int(TPL_W * s), int(TPL_H * s)
        if w < 16 or h < 6 or w > image_gray.shape[1] or h > image_gray.shape[0]:
            continue
        tpl = cv2.resize(tpl_gray, (w, h), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(image_gray, tpl, cv2.TM_CCOEFF_NORMED)
        _mn, mx, _ml, mxl = cv2.minMaxLoc(res)
        if best is None or mx > best[2]:
            best = (offset[0] + mxl[0] + CHECKBOX_DX * s,
                    offset[1] + mxl[1] + CHECKBOX_DY * s, mx)
    return best


def _find_in(image) -> tuple[float, float, float] | None:
    """Best checkbox match in one image: (x, y, confidence) in image pixels.

    Two passes. The coarse one halves the image and drops colour, so six
    scales cost a few milliseconds instead of two and a half seconds. Anything
    promising is then re-matched at full resolution inside a small crop, and
    it is that second confidence the caller thresholds on -- so the speed-up
    costs no accuracy at the point where the decision is actually made.
    """
    import cv2  # noqa: PLC0415

    tpl_gray = cv2.cvtColor(_template(), cv2.COLOR_BGR2GRAY)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, None, fx=COARSE_SCALE, fy=COARSE_SCALE,
                       interpolation=cv2.INTER_AREA)
    coarse = _search(small, tpl_gray, [s * COARSE_SCALE for s in SCALES])
    if coarse is None or coarse[2] < COARSE_THRESHOLD:
        return coarse

    cx, cy = coarse[0] / COARSE_SCALE, coarse[1] / COARSE_SCALE
    x0 = max(0, int(cx - TPL_W - REFINE_PAD))
    y0 = max(0, int(cy - TPL_H - REFINE_PAD))
    x1 = min(gray.shape[1], int(cx + TPL_W + REFINE_PAD))
    y1 = min(gray.shape[0], int(cy + TPL_H + REFINE_PAD))
    crop = gray[y0:y1, x0:x1]
    fine = _search(crop, tpl_gray, SCALES, offset=(x0, y0))
    return fine or coarse


def find_checkbox() -> tuple[float, float, float] | None:
    """Scan every display. Returns (x, y, conf) in global logical points.

    The standalone tool only looked at the main display, so a challenge parked
    on the second monitor was never solved. Displays are captured one at a
    time because screencapture -R takes one rectangle, and the region's origin
    is what converts a match back into a click coordinate.
    """
    best = None
    for (dx, dy, dw, dh) in macinput.display_rects() or [(0, 0, 1440, 900)]:
        image = macinput.grab_region(dx, dy, dw, dh)
        if image is None:
            continue
        hit = _find_in(image)
        if hit is None:
            continue
        x, y, conf = hit
        scale = image.shape[1] / float(dw) if dw else 1.0
        cand = (dx + x / scale, dy + y / scale, conf)
        if best is None or cand[2] > best[2]:
            best = cand
    if best is None or best[2] < MATCH_THRESHOLD:
        return None
    return best


# ── solving ────────────────────────────────────────────────────────────────
def solve_visible(cfg: dict, reason: str) -> str:
    """Click whatever challenge is on screen. 'none' | 'solved' | 'failed'.

    Restores the user's frontmost app afterwards -- the solve steals focus to
    put the checkbox in front, and leaving it stolen is how a background tool
    starts interrupting the person using the Mac.
    """
    hit = find_checkbox()
    if hit is None:
        return "none"

    prior = macinput.frontmost_app()
    try:
        for attempt in range(1, int(cfg["max_attempts"]) + 1):
            hit = find_checkbox()
            if hit is None:
                return "solved"  # gone between grabs
            x, y, conf = hit
            log(f"turnstile {reason}: attempt {attempt} conf={conf:.3f} at ({x:.0f},{y:.0f})")
            macinput.mouse_click(x, y)

            deadline = time.time() + float(cfg["settle"])
            while time.time() < deadline:
                time.sleep(0.6)
                if find_checkbox() is None:
                    return "solved"
        return "failed" if find_checkbox() else "solved"
    except Exception as e:
        log(f"turnstile solve error ({reason}): {e}")
        return "failed"
    finally:
        macinput.activate_app(prior)


def _record(reason: str, result: str) -> None:
    eventlog.record(
        "auto-approved",
        f"Solved a Cloudflare check ({reason})"
        if result == "solved" else f"Cloudflare check not cleared ({reason})",
        source="turnstile",
        project="Turnstile",
        detail="Pause with the menubar toggle or ~/.turnstile_autosolve_off",
        pushed=False,
    )


# ── layer 1: CDP ports ─────────────────────────────────────────────────────
def challenged_targets(cfg: dict):
    """[(port, target_id, title)] for tabs currently showing an interstitial."""
    out = []
    for port in cfg["ports"]:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=1.0) as r:
                tabs = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            continue  # port not listening or busy -- not worth a log line
        for t in tabs:
            if t.get("type") != "page":
                continue
            title = (t.get("title") or "").lower()
            url = (t.get("url") or "").lower()
            if (any(m in title for m in CHALLENGE_TITLE_MARKERS)
                    or any(m in url for m in CHALLENGE_URL_MARKERS)):
                out.append((port, t.get("id"), t.get("title") or ""))
    return out


def _activate_tab(port: int, target_id: str) -> None:
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/activate/{target_id}", timeout=1.0).read()
    except Exception:
        pass


# ── watch loop ─────────────────────────────────────────────────────────────
def paused(global_off: Path, auto_off: Path) -> bool:
    return global_off.exists() or auto_off.exists() or OFF_FLAG.exists()


def watch_loop(global_off: Path, auto_off: Path, status_cb=None) -> None:
    cfg = load_config()
    if not cfg.get("enabled", True):
        log("turnstile: disabled in config")
        return

    if not macinput.screen_recording_ok():
        # Without the grant screencapture returns the desktop with every
        # window stripped out, which matches as "nothing to solve" forever.
        # Say so and raise the prompt, rather than run as a silent no-op.
        log("turnstile: Screen Recording NOT granted -- cannot see challenges")
        macinput.request_screen_recording()
        eventlog.record(
            "error",
            "Turnstile solver needs Screen Recording",
            source="turnstile",
            project="YOLO Mode",
            detail="System Settings > Privacy & Security > Screen Recording > YOLO Mode",
            pushed=False,
        )

    browsers = set(cfg["browsers"])
    cooldown_until: dict = {}
    solves = 0
    log(f"turnstile watch started (ports={cfg['ports']}, "
        f"{len(macinput.display_rects())} displays)")

    while True:
        # One pool per pass: the frontmost-app and display lookups below hand
        # back autoreleased objects, and on this thread nothing else drains them.
        with autorelease_pool():
            try:
                if paused(global_off, auto_off):
                    time.sleep(3.0)
                    continue
                now = time.time()

                # Layer 1 -- interstitials on the scraper CDP ports.
                for port, tid, title in challenged_targets(cfg):
                    if now < cooldown_until.get(port, 0):
                        continue
                    log(f"turnstile: port {port} challenged: {title!r}")
                    prior = macinput.frontmost_app()
                    _activate_tab(port, tid)
                    macinput.activate_app(port_app(cfg, port))
                    time.sleep(0.5)
                    result = solve_visible(cfg, f"cdp:{port}")
                    macinput.activate_app(prior)
                    if result != "none":
                        _record(f"port {port}", result)
                        solves += result == "solved"
                    cooldown_until[port] = time.time() + float(cfg["cooldown"])

                # Layer 2 -- inline widget in the frontmost browser.
                if time.time() >= cooldown_until.get("front", 0):
                    front = macinput.frontmost_app()
                    if front in browsers:
                        result = solve_visible(cfg, f"front:{front}")
                        if result != "none":
                            _record(front, result)
                            solves += result == "solved"
                            cooldown_until["front"] = time.time() + float(cfg["cooldown"])

                if status_cb:
                    status_cb(solves)
            except Exception as e:
                log(f"turnstile loop error: {e}")
        time.sleep(float(cfg["interval"]))


def solve_once() -> str:
    """One detect/solve pass over both layers. For the menubar's test action."""
    cfg = load_config()
    for port, tid, title in challenged_targets(cfg):
        prior = macinput.frontmost_app()
        _activate_tab(port, tid)
        macinput.activate_app(port_app(cfg, port))
        time.sleep(0.5)
        r = solve_visible(cfg, f"cdp:{port}")
        macinput.activate_app(prior)
        if r != "none":
            return r
    return solve_visible(cfg, "manual")


if __name__ == "__main__":
    if "--once" in sys.argv:
        print(json.dumps({"status": solve_once()}))
    else:
        watch_loop(Path.home() / ".yolo_mode_off",
                   Path.home() / ".yolo_mode_no_autoapprove")
