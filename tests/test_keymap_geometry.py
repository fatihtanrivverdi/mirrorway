from mirrorway.geometry import Rect, edge_exit, entry_point
from mirrorway.keymap import (FLAG_SHIFT, KeyTranslator, modifier_is_down, _BASE)
from mirrorway.protocol import KEYFLAG_EXTENDED, KEYFLAG_VK


def test_letters_and_arrows():
    t = KeyTranslator()
    assert t.translate(0) == (0, 0x1E, 0)            # A
    assert t.translate(12) == (0, 0x10, 0)           # Q
    assert t.translate(124) == (KEYFLAG_EXTENDED, 0x4D, 0)   # right arrow
    assert t.translate(117) == (KEYFLAG_EXTENDED, 0x53, 0)   # Delete
    assert t.translate(76) == (KEYFLAG_EXTENDED, 0x1C, 0)    # keypad Enter
    assert t.translate(63) is None                   # Fn
    assert t.translate(999) is None


def test_no_duplicate_scancodes_among_main_keys():
    seen = {}
    for kc, (scan, ext) in _BASE.items():
        assert (scan, ext) not in seen or kc in (114, 82), (kc, seen.get((scan, ext)))
        seen[(scan, ext)] = kc


def test_iso_vs_ansi():
    t = KeyTranslator()
    assert t.translate(10, iso=True) == (0, 0x29, 0)
    assert t.translate(50, iso=True) == (0, 0x56, 0)
    assert t.translate(50, iso=False) == (0, 0x29, 0)


def test_modifiers_mapping():
    swap = KeyTranslator(swap_cmd_ctrl=True)
    assert swap.translate(55) == (0, 0x1D, 0)                    # Cmd -> LCtrl
    assert swap.translate(59) == (KEYFLAG_EXTENDED, 0x5B, 0)     # Ctrl -> LWin
    noswap = KeyTranslator(swap_cmd_ctrl=False)
    assert noswap.translate(55) == (KEYFLAG_EXTENDED, 0x5B, 0)
    assert noswap.translate(59) == (0, 0x1D, 0)
    assert swap.translate(58) == (0, 0x38, 0)                    # Option -> Alt
    assert swap.translate(61) == (KEYFLAG_EXTENDED, 0x38, 0)     # right Option -> AltGr
    assert KeyTranslator(left_option="altgr").translate(58) == (KEYFLAG_EXTENDED, 0x38, 0)
    assert KeyTranslator.media(0) == (KEYFLAG_VK | KEYFLAG_EXTENDED, 0, 0xAF)


def test_modifier_is_down():
    assert modifier_is_down(56, FLAG_SHIFT | 0x0002)
    assert not modifier_is_down(56, FLAG_SHIFT | 0x0004)   # only the right Shift is down
    assert modifier_is_down(60, FLAG_SHIFT | 0x0004)
    assert modifier_is_down(56, FLAG_SHIFT)                # without device bits, the general flag
    assert not modifier_is_down(56, 0x100)


MAC = [Rect(0, 0, 1440, 900)]


def test_edge_exit_right():
    assert edge_exit(MAC, 1439, 450, 3, 0, "right") == 0.5
    assert edge_exit(MAC, 1200, 450, 3, 0, "right") is None
    assert edge_exit(MAC, 1439, 450, -3, 0, "right") is None
    assert edge_exit(MAC, 1439, 2, 3, 0, "right") is None      # corner guard
    assert edge_exit(MAC, 0, 300, -2, 0, "left") is not None
    assert edge_exit(MAC, 700, 899, 0, 2, "bottom") is not None
    assert edge_exit(MAC, 700, 0, 0, -2, "top") is not None


def test_edge_exit_ignores_inner_edges():
    two = [Rect(0, 0, 1440, 900), Rect(1440, 0, 1920, 1080)]
    assert edge_exit(two, 1439, 450, 3, 0, "right") is None    # a second display is in between
    assert edge_exit(two, 3359, 500, 3, 0, "right") is not None
    # the second display is taller: in its lower part there is no right edge of the first one
    assert edge_exit(two, 1439, 899, 3, 0, "right", corner_guard=0) is None


def test_entry_point():
    win = [Rect(0, 0, 2560, 1440)]
    x, y = entry_point(win, "left", 0.5)
    assert x == 3 and abs(y - 719.5) < 1
    x, y = entry_point(win, "right", 0.0)
    assert x == 2556 and y == 0
    x, y = entry_point(win, "center", 0.5)
    assert (x, y) == (1280, 720)
    multi = [Rect(0, 0, 1920, 1080), Rect(-1920, 200, 1920, 1080)]
    x, y = entry_point(multi, "left", 0.5)
    assert x == -1917
