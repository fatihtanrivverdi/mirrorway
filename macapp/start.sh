#!/bin/bash
# Mirrorway.app — run as a child process by Contents/MacOS/Mirrorway.
# App code:  ~/.mirrorway/app
# Python:    the app's own (Contents/Resources/python, in the .dmg download) or ~/.mirrorway/venv (the zip download,
#            or after an update that needs new packages)
# (The code is kept outside the bundle, so updates do not change the app's signature and therefore keep the
#  permissions you granted. The .dmg's app carries a copy of the code for the first start.)
# MIRRORWAY_CHECK=1: set everything up, then only test-import the app instead of starting it (CI).

MHOME="$HOME/.mirrorway"
APP="$MHOME/app"
VENV="$MHOME/venv"
LOG="$MHOME/install.log"
mkdir -p "$MHOME"
BUNDLE="${MIRRORWAY_BUNDLE:-}"
RES="$BUNDLE/Contents/Resources"
BPY="$RES/python/bin/python3"
BAPP="$RES/app"

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

# The .dmg's app: downloaded files carry a quarantine mark that macOS checks for every program the app starts;
# once the app itself is allowed (the first start), the mark is removed from the whole bundle.
if [ -n "$BUNDLE" ] && /usr/bin/xattr -p com.apple.quarantine "$BUNDLE" >/dev/null 2>&1; then
  /usr/bin/xattr -dr com.apple.quarantine "$BUNDLE" 2>/dev/null
fi

# The .dmg's app brings the code: installed on the first start, or when it is newer than the installed code
# (a newer .dmg); updates installed by the app itself are newer and stay.
version_of() { /usr/bin/sed -n 's/^__version__ = "\(.*\)"/\1/p' "$1" 2>/dev/null | head -n 1; }
newer() {  # newer A B: is version A newer than version B?
  [ -z "$2" ] && return 0
  [ "$1" = "$2" ] && return 1
  [ "$(printf '%s\n%s\n' "$1" "$2" | /usr/bin/sort -t. -k1,1n -k2,2n -k3,3n | tail -n 1)" = "$1" ]
}
if [ -f "$BAPP/mirrorway/mac/__main__.py" ]; then
  BVER=$(version_of "$BAPP/mirrorway/__init__.py")
  IVER=$(version_of "$APP/mirrorway/__init__.py")
  if [ ! -f "$APP/mirrorway/mac/__main__.py" ] || newer "$BVER" "$IVER"; then
    echo "== $(date) — installing the code of Mirrorway $BVER from the app (was: ${IVER:-none}) ==" >>"$LOG"
    rm -rf "$APP.new"
    /usr/bin/ditto --noqtn "$BAPP" "$APP.new" >>"$LOG" 2>&1 && rm -rf "$APP" && mv "$APP.new" "$APP"
  fi
fi

if [ ! -f "$APP/mirrorway/mac/__main__.py" ]; then
  alert "$(say "The app files were not found ($APP). Run the Mirrorway installer again." \
               "Uygulama dosyaları bulunamadı ($APP). Mirrorway kurulumunu tekrar yapın.")"
  exit 1
fi

find_python() {
  for c in "$BPY" /opt/homebrew/bin/python3 /usr/local/bin/python3 "$HOME/.homebrew/bin/python3" \
           /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/bin/python3; do
    if [ -x "$c" ] && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
      echo "$c"; return 0
    fi
  done
  return 1
}

REQ="$APP/requirements-mac.txt"
REQ_HASH=$(/usr/bin/shasum "$REQ" | cut -d' ' -f1)
PYEXE="$VENV/bin/python"
if [ -x "$BPY" ] && [ "$(cat "$RES/python/.req" 2>/dev/null)" = "$REQ_HASH" ]; then
  PYEXE="$BPY"                     # the app's own Python already has exactly these packages
elif [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -c '' >/dev/null 2>&1 \
     || [ "$(cat "$VENV/.req" 2>/dev/null)" != "$REQ_HASH" ]; then
  PY=$(find_python) || {
    alert "$(say "Python 3 was not found. Install it from https://www.python.org/downloads/ and open Mirrorway again." \
                 "Python 3 bulunamadı. https://www.python.org/downloads/ adresinden kurup Mirrorway'i yeniden açın.")"
    exit 1
  }
  notify "$(say "Setting up for the first time, this may take 1-2 minutes…" "İlk kurulum yapılıyor, 1-2 dakika sürebilir…")"
  echo "== $(date) — Python: $PY ==" >>"$LOG"
  if [ -x "$VENV/bin/python" ] && ! "$VENV/bin/python" -c '' >/dev/null 2>&1; then
    rm -rf "$VENV"                 # made with a Python that is gone (e.g. the app was moved)
  fi
  if [ ! -x "$VENV/bin/python" ]; then
    # on the app's own Python the environment adds only what is new to the packages it already has
    SYS=""; [ "$PY" = "$BPY" ] && SYS="--system-site-packages"
    "$PY" -m venv $SYS "$VENV" >>"$LOG" 2>&1 || {
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
if [ "${MIRRORWAY_CHECK:-}" = "1" ]; then
  exec "$PYEXE" -c 'import sys, mirrorway, mirrorway.mac.app, mirrorway.mac.panel, mirrorway.mac.settings
print(mirrorway.__version__, sys.executable)'
fi
exec "$PYEXE" -m mirrorway.mac
