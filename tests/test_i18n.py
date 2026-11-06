"""Interface language: every T("...") text has a Turkish translation with the same placeholders, and the
language follows the setting, the environment and the system."""
import ast
import pathlib
import string

from mirrorway import i18n
from mirrorway.i18n_tr import TR

ROOT = pathlib.Path(__file__).resolve().parents[1] / "mirrorway"


def _ui_texts():
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "T":
                arg = node.args[0]
                assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), \
                    f"T() needs a string literal: {path.name}:{node.lineno}"
                yield path.name, node.lineno, arg.value


def _fields(text):
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


def test_every_interface_text_has_a_turkish_translation():
    missing = [f"{name}:{line} {text!r}" for name, line, text in _ui_texts() if text not in TR]
    assert not missing, "missing from i18n_tr.TR:\n" + "\n".join(missing)


def test_translations_keep_the_placeholders():
    bad = [k for k, v in TR.items() if _fields(k) != _fields(v)]
    assert not bad, bad


def test_no_unused_translations():
    used = {text for _, _, text in _ui_texts()}
    assert not set(TR) - used, sorted(set(TR) - used)


def test_language_choice(monkeypatch):
    monkeypatch.delenv("MIRRORWAY_LANG", raising=False)
    monkeypatch.setattr(i18n, "system_language", lambda: "tr")
    try:
        assert i18n.set_language("auto") == "tr"
        assert i18n.T("Connecting…") == "Bağlanılıyor…"
        assert i18n.T("not a known text") == "not a known text"
        assert i18n.set_language("en") == "en" and i18n.T("Connecting…") == "Connecting…"
        assert i18n.set_language("de") == "en"                       # unsupported -> English
        monkeypatch.setattr(i18n, "system_language", lambda: "fr")
        assert i18n.set_language("auto") == "en"
        monkeypatch.setenv("MIRRORWAY_LANG", "tr")
        assert i18n.set_language("en") == "tr"                       # the environment wins
    finally:
        monkeypatch.delenv("MIRRORWAY_LANG", raising=False)
        i18n.set_language("en")
