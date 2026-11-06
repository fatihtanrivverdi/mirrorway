"""Interface language.

English is the source language: every user-facing string in the code is written in English and wrapped in
T("..."). Translations live in i18n_tr.py (Turkish). The language follows the system (macOS preferred
languages, Windows UI language) unless the "language" setting or the MIRRORWAY_LANG environment variable
chooses one. Log messages are always English.

Strings with values use str.format placeholders, translated as a whole:
    T("Files from {peer} are ready").format(peer="Windows")
"""
from __future__ import annotations

import locale
import os
import sys
from typing import Optional

SUPPORTED = ("en", "tr")
NAMES = {"en": "English", "tr": "Türkçe"}
_lang: Optional[str] = None


def system_language() -> str:
    """Two-letter code of the system's interface language ("en" when it cannot be read)."""
    try:
        if sys.platform == "darwin":
            from Foundation import NSLocale
            langs = NSLocale.preferredLanguages()
            if langs:
                return str(langs[0])[:2].lower()
        elif sys.platform == "win32":
            import ctypes
            langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "tr" if (langid & 0x3FF) == 0x1F else "en"          # LANG_TURKISH
    except Exception:
        pass
    try:
        loc = locale.getlocale()[0] or os.environ.get("LANG", "")
    except Exception:
        loc = os.environ.get("LANG", "")
    return (loc or "en")[:2].lower()


def resolve(setting: Optional[str] = "auto") -> str:
    choice = (os.environ.get("MIRRORWAY_LANG") or setting or "auto").lower()
    if choice == "auto":
        choice = system_language()
    return choice if choice in SUPPORTED else "en"


def set_language(setting: Optional[str] = "auto") -> str:
    global _lang
    _lang = resolve(setting)
    return _lang


def language() -> str:
    global _lang
    if _lang is None:
        _lang = resolve()
    return _lang


def T(text: str) -> str:
    """The interface text in the current language."""
    if language() == "en":
        return text
    from .i18n_tr import TR
    return TR.get(text, text)
