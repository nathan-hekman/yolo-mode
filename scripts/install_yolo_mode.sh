#!/bin/bash
# Install YOLO Mode: build the .app bundle, then run it as a per-user
# LaunchAgent (KeepAlive + RunAtLoad) so it comes back after login or a crash.
# Idempotent -- safe to re-run after editing any script.
#
# Why a bundle instead of running the .py directly (the old way): macOS takes
# an app's name and Dock icon from the bundle around its executable. Without
# one the Dock tile reads "Python" with the generic rocket icon. The bundle's
# executable is a symlink to the venv python, which is enough for
# LaunchServices to treat this as a real app.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PY="$REPO_DIR/.venv/bin/python"
APP_PY="$REPO_DIR/scripts/yolo_menubar.py"
LABEL="com.hekman.yolo-mode"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
OUTLOG="$HOME/Library/Logs/yolo-mode.out.log"
APP="$HOME/Applications/YOLO Mode.app"

[ -x "$VENV_PY" ] || { echo "ERROR: venv python not found at $VENV_PY (run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)"; exit 1; }
[ -f "$APP_PY" ] || { echo "ERROR: $APP_PY missing"; exit 1; }

pkill -f yolo_menubar 2>/dev/null || true

# ---- build the app bundle -------------------------------------------------
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# The bundle executable is a symlink to the real python; argv[0] living inside
# the bundle is what gives the app its identity. Copying pyvenv.cfg next to it
# is what keeps the venv's site-packages (Quartz, rumps, requests) on the path
# -- python locates a venv relative to the executable it was launched as, and
# the bundle path is not inside .venv.
# Copy, not symlink: macOS resolves a symlinked executable and hands the app's
# identity to whatever bundle the real binary lives in (that is how the Dock
# ended up labelled "Python"). Copy the framework's *Python.app* binary rather
# than bin/python3.12 -- the latter re-execs itself into Python.app when it
# needs GUI access, which throws the identity away again.
REAL_PY="$(python3 -c "import os,sys;print(os.path.realpath(sys.argv[1]))" "$VENV_PY")"
GUI_PY="$(dirname "$(dirname "$REAL_PY")")/Resources/Python.app/Contents/MacOS/Python"
[ -f "$GUI_PY" ] || GUI_PY="$REAL_PY"
cp "$GUI_PY" "$APP/Contents/MacOS/YOLOMode"
cp "$REPO_DIR/.venv/pyvenv.cfg" "$APP/Contents/MacOS/pyvenv.cfg"
# Python takes Contents/ as the venv root (pyvenv.cfg sits one level below it),
# so Contents/lib must hold the venv's libraries.
#
# Copy them (~50MB), don't symlink. A symlink pointing outside the bundle makes
# Gatekeeper reject the whole app -- "invalid destination for symbolic link in
# bundle" -- because anyone who can write the target swaps the app's code
# without breaking its signature. A rejected bundle is not a valid app, and
# Notification Center refuses to talk to one.
rm -rf "$APP/Contents/lib"
cp -R "$REPO_DIR/.venv/lib" "$APP/Contents/lib"
# Compile the bytecode caches now, before signing. Python validates a .pyc
# against its source's mtime, and copying gives every file a new one -- so the
# first run would rewrite hundreds of .pyc files inside the bundle and break
# its own seal ("a sealed resource is missing or invalid"). Precompiled, the
# caches already match and the running app never writes into itself.
find "$APP/Contents/lib" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
"$VENV_PY" -m compileall -q "$APP/Contents/lib" >/dev/null 2>&1 || true

cat > "$APP/Contents/Info.plist" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>YOLO Mode</string>
  <key>CFBundleDisplayName</key><string>YOLO Mode</string>
  <key>CFBundleIdentifier</key><string>$LABEL</string>
  <key>CFBundleExecutable</key><string>YOLOMode</string>
  <key>CFBundleIconFile</key><string>appicon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>2.0</string>
  <!-- Notification Center refuses an app with no CFBundleVersion outright,
       before it even reaches the notification daemon. -->
  <key>CFBundleVersion</key><string>2</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLISTEOF

"$VENV_PY" "$REPO_DIR/scripts/make_icon.py" "$APP/Contents/Resources/appicon.icns" \
  || echo "WARN: icon generation failed, using default"

# Sign with a stable identity. macOS keys Accessibility and Automation grants
# to the signature, so an unsigned bundle loses those permissions on every
# reinstall and auto-approval silently stops working.
#
# Prefer Developer ID: Notification Center refuses posts from an app signed
# only for development. Developer ID means the hardened runtime, which needs
# the entitlements alongside this script -- a copied Homebrew Python cannot
# launch under the runtime's default library validation. Fall back to Apple
# Development (no runtime, no notifications) and then ad-hoc.
ENTS="$REPO_DIR/scripts/yolo_mode.entitlements"
SIGN_ID="$(security find-identity -v -p codesigning 2>/dev/null | grep "Developer ID Application" | head -1 | awk '{print $2}')"
if [ -n "$SIGN_ID" ]; then
  codesign --force --deep --timestamp -o runtime --entitlements "$ENTS" \
    -s "$SIGN_ID" "$APP" || { echo "ERROR: Developer ID signing failed"; exit 1; }
else
  SIGN_ID="$(security find-identity -v -p codesigning 2>/dev/null | grep "Apple Development" | head -1 | awk '{print $2}')"
  [ -n "$SIGN_ID" ] || SIGN_ID="-"
  codesign --force --deep -s "$SIGN_ID" "$APP" 2>/dev/null \
    || codesign --force --deep -s - "$APP" 2>/dev/null \
    || echo "WARN: could not sign the bundle"
fi

xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
touch "$APP"
echo "Built $APP (signed with ${SIGN_ID})"

# ---- LaunchAgent ----------------------------------------------------------
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string>
    <string>-W</string>
    <string>-a</string>
    <string>$APP</string>
    <string>--args</string>
    <string>$APP_PY</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$OUTLOG</string>
  <key>StandardErrorPath</key><string>$OUTLOG</string>
</dict>
</plist>
PLISTEOF

CREDS="$HOME/.config/yolo-mode/pushover.json"
if [ ! -f "$CREDS" ]; then
  mkdir -p "$(dirname "$CREDS")"
  cat > "$CREDS" <<'CREDEOF'
{
  "user_key": "",
  "api_token": ""
}
CREDEOF
  chmod 600 "$CREDS"
  echo "NOTE: add your Pushover keys to $CREDS (phone alerts are off until you do)"
fi

# ---- retire the standalone Turnstile watcher ------------------------------
# Its solver now lives in scripts/turnstile.py and runs on a thread inside this
# app. Leaving the old LaunchAgent loaded would mean two processes racing to
# click the same checkbox, each with its own cooldown, neither aware of the
# other -- and two menubar icons for one job.
OLD_TS="com.hekman.turnstile-autosolve"
if launchctl print "gui/$(id -u)/$OLD_TS" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)/$OLD_TS" 2>/dev/null || true
  echo "Retired the standalone Turnstile watcher ($OLD_TS)."
fi
rm -f "$HOME/Library/LaunchAgents/$OLD_TS.plist"
pkill -f turnstile_menubar 2>/dev/null || true
rm -f "$HOME/.turnstile_watch.pid"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"
echo "Installed + started $LABEL. Log: $OUTLOG"

# ---- Screen Recording, for the Turnstile solver ---------------------------
# Without it the capture still succeeds and simply returns the desktop with
# every window missing, so the solver would sit there matching nothing and
# never say why. Check and point at the pane instead.
if ! "$VENV_PY" -c "from Quartz import CGPreflightScreenCaptureAccess as p; import sys; sys.exit(0 if p() else 1)" 2>/dev/null; then
  echo
  echo "Screen Recording is NOT granted yet -- the Cloudflare solver cannot see"
  echo "challenges until it is. Opening the pane; enable 'YOLO Mode':"
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture" 2>/dev/null || true
fi
