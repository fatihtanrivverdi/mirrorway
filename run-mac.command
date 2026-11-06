#!/bin/bash
# Mirrorway — runs the Mac side from this folder (for development; installing with install-mac.command is
# recommended). Double-click it (the first time: right-click › Open).
cd "$(dirname "$0")" || exit 1

PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3 was not found. Install it from https://www.python.org/downloads/ and try again."
  read -n1 -r -p "Press any key to close…"; exit 1
fi

if [ ! -x .venv-mac/bin/python ]; then
  echo "Setting up for the first time (this may take 1-2 minutes)…"
  "$PY" -m venv .venv-mac || { echo "Could not create the virtual environment"; read -n1 -r; exit 1; }
fi

REQ_HASH=$(shasum requirements-mac.txt | cut -d' ' -f1)
if [ ! -f .venv-mac/.req ] || [ "$(cat .venv-mac/.req)" != "$REQ_HASH" ]; then
  .venv-mac/bin/python -m pip install -q --upgrade pip
  .venv-mac/bin/python -m pip install -r requirements-mac.txt || { echo "Could not install the packages"; read -n1 -r; exit 1; }
  echo "$REQ_HASH" > .venv-mac/.req
fi

echo ""
echo "Mirrorway is running. Click the Mirrorway icon in the menu bar."
echo "Keep this Terminal window open (you can minimise it). To stop: Mirrorway menu › Quit Mirrorway, or Ctrl+C"
echo ""
exec .venv-mac/bin/python -m mirrorway.mac "$@"
