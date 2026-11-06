#!/bin/bash
# Mirrorway.app — run as a child process by Contents/MacOS/Mirrorway.
# App code:  ~/.mirrorway/app      Python environment:  ~/.mirrorway/venv
# (The code is kept outside the bundle, so updates do not change the app's signature and therefore keep the
#  permissions you granted.)

MHOME="$HOME/.mirrorway"
APP="$MHOME/app"
VENV="$MHOME/venv"
LOG="$MHOME/install.log"
mkdir -p "$MHOME"

# Interface language for these few messages: MIRRORWAY_LANG, then the app setting, then the system language.
lang_of() {
  local l="${MIRRORWAY_LANG:-}"
  if [ -z "$l" ] && [ -f "$MHOME/mac.json" ]; then
    l=$(/usr/bin/sed -n 's/.*"language"[[:space:]]*:[[:space:]]*"\([a-z]*\)".*/\1/p' "$MHOME/mac.json" | head -n 1)
  fi
  if [ -z "$l" ] || [ "$l" = "auto" ]; then
    l=$(/usr/bin/defaults read -g AppleLanguages 2>/dev/null | /usr/bin/sed -n '2s/[^a-zA-Z]*\([a-zA-Z]*\).*/\1/p')
  fi
  case "$l" in tr*) echo tr ;; *) echo en ;; esac
}
LANG_UI=$(lang_of)
# say "English text" "Turkish text"
say() { if [ "$LANG_UI" = "tr" ]; then echo "$2"; else echo "$1"; fi; }

notify() { /usr/bin/osascript -e "display notification \"$1\" with title \"Mirrorway\"" >/dev/null 2>&1; }
alert()  { /usr/bin/osascript -e "display alert \"Mirrorway\" message \"$1\" as critical" >/dev/null 2>&1; }

if [ ! -f "$APP/mirrorway/mac/__main__.py" ]; then
  alert "$(say "The app files were not found ($APP). Run the Mirrorway installer again." \
               "Uygulama dosyaları bulunamadı ($APP). Mirrorway kurulumunu tekrar yapın.")"
  exit 1
fi

find_python() {
  for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$HOME/.homebrew/bin/python3" \
           /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/bin/python3; do
    if [ -x "$c" ] && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
      echo "$c"; return 0
    fi
  done
  return 1
}

REQ="$APP/requirements-mac.txt"
REQ_HASH=$(/usr/bin/shasum "$REQ" | cut -d' ' -f1)
if [ ! -x "$VENV/bin/python" ] || [ "$(cat "$VENV/.req" 2>/dev/null)" != "$REQ_HASH" ]; then
  PY=$(find_python) || {
    alert "$(say "Python 3 was not found. Install it from https://www.python.org/downloads/ and open Mirrorway again." \
                 "Python 3 bulunamadı. https://www.python.org/downloads/ adresinden kurup Mirrorway'i yeniden açın.")"
    exit 1
  }
  notify "$(say "Setting up for the first time, this may take 1-2 minutes…" "İlk kurulum yapılıyor, 1-2 dakika sürebilir…")"
  echo "== $(date) — Python: $PY ==" >>"$LOG"
  if [ ! -x "$VENV/bin/python" ]; then
    "$PY" -m venv "$VENV" >>"$LOG" 2>&1 || {
      alert "$(say "Could not create the Python environment. Details: $LOG" "Python ortamı oluşturulamadı. Ayrıntı: $LOG")"
      exit 1
    }
  fi
  "$VENV/bin/python" -m pip install -q --disable-pip-version-check --upgrade pip >>"$LOG" 2>&1
  "$VENV/bin/python" -m pip install --disable-pip-version-check -r "$REQ" >>"$LOG" 2>&1 || {
    alert "$(say "Could not install the required packages (check the internet connection). Details: $LOG" \
                 "Gerekli paketler kurulamadı (internet bağlantısını kontrol edin). Ayrıntı: $LOG")"
    exit 1
  }
  echo "$REQ_HASH" > "$VENV/.req"
  notify "$(say "Setup complete. Click the Mirrorway icon in the menu bar." "Kurulum tamam. Menü çubuğundaki Mirrorway simgesine tıklayın.")"
fi

export PYTHONPATH="$APP"
export PYTHONPYCACHEPREFIX="$MHOME/pycache"
cd "$APP" || exit 1
exec "$VENV/bin/python" -m mirrorway.mac
