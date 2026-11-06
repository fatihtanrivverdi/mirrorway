#!/bin/bash
# Installs Mirrorway as a Mac app: Mirrorway.app -> ~/Applications, code -> ~/.mirrorway/app
# The permissions (Accessibility, Input Monitoring, Screen Recording) are granted to Mirrorway, not to Terminal.
# Double-click it (the first time: right-click › Open).
cd "$(dirname "$0")" || exit 1
set -e

APPS="$HOME/Applications"
MHOME="$HOME/.mirrorway"
mkdir -p "$APPS" "$MHOME"

# Mirrorway.app comes with the release download. In a copy of the source code it is built here
# (this needs the Xcode command line tools: xcode-select --install).
BUNDLE="Mirrorway.app"
if [ ! -d "$BUNDLE" ]; then
  echo "Mirrorway.app is not in this folder; building it from macapp/launcher.c…"
  if ! xcrun --find cc >/dev/null 2>&1; then
    echo "The Xcode command line tools are needed to build it: run  xcode-select --install"
    echo "Or download the ready-made Mac package from the Releases page:"
    echo "  https://github.com/fatihtanrivverdi/mirrorway/releases/latest"
    read -n1 -r -p "Press any key to close…"; exit 1
  fi
  BUILD=$(mktemp -d)
  /usr/bin/python3 macapp/build_app.py "$BUILD"
  BUNDLE="$BUILD/Mirrorway.app"
fi

# Moving over from Köprü (the app's former name): quit it; its settings and pairing are imported on first start.
if /usr/bin/pgrep -f "kopru\.mac" >/dev/null 2>&1; then
  echo "Quitting Köprü…"
  /usr/bin/pkill -f "kopru\.mac" || true
  sleep 1
fi
if [ -f "$HOME/Library/LaunchAgents/app.kopru.mac.plist" ]; then
  /bin/launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/app.kopru.mac.plist" >/dev/null 2>&1 || true
fi
if [ -d "$APPS/Köprü.app" ]; then
  rm -rf "$APPS/Köprü.app"
  echo "Removed the old Köprü.app (remove it from System Settings › Privacy & Security too)."
fi

rm -rf "$MHOME/app.new"
mkdir -p "$MHOME/app.new"
cp -R mirrorway requirements-mac.txt "$MHOME/app.new/"
[ -f CHANGELOG.md ] && cp CHANGELOG.md "$MHOME/app.new/"
find "$MHOME/app.new" -name "__pycache__" -prune -exec rm -rf {} +
rm -rf "$MHOME/app"
mv "$MHOME/app.new" "$MHOME/app"
rm -rf "$APPS/Mirrorway.app"
cp -R "$BUNDLE" "$APPS/"
xattr -dr com.apple.quarantine "$APPS/Mirrorway.app" 2>/dev/null || true
echo "Mirrorway is installed: ~/Applications/Mirrorway.app"
echo "If a Mirrorway started from Terminal is running, quit it first (Mirrorway menu › Quit Mirrorway)."
open "$APPS/Mirrorway.app"
