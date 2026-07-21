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
# so point Contents/lib at the real venv's libraries.
ln -sfn "$REPO_DIR/.venv/lib" "$APP/Contents/lib"

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
# No hardened runtime (-o runtime): it makes Gatekeeper refuse to launch this
# bundle ("YOLOMode cannot be opened because of a problem"), because the
# executable is a copied Homebrew Python that the runtime then rejects.
SIGN_ID="$(security find-identity -v -p codesigning 2>/dev/null | grep "Apple Development" | head -1 | awk '{print $2}')"
[ -n "$SIGN_ID" ] || SIGN_ID="-"
codesign --force --deep -s "$SIGN_ID" "$APP" 2>/dev/null \
  || codesign --force --deep -s - "$APP" 2>/dev/null \
  || echo "WARN: could not sign the bundle"

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
    <string>$APP/Contents/MacOS/YOLOMode</string>
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

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"
echo "Installed + started $LABEL. Log: $OUTLOG"
