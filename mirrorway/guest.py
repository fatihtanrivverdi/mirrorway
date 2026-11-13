"""Reverse control (3.6): Windows's own keyboard and mouse used on the Mac — the parts both sides share.

The Mac's keyboard and mouse reach Windows as before (router.remote). Now Windows's own devices can visit the Mac
too: pushed against the edge of Windows that faces the Mac (or Ctrl+Alt + the arrow towards the Mac), Windows
captures its keyboard and mouse (low-level hooks), hides and parks its pointer, and sends what they do to the Mac,
which posts it as its own input. Pushed back against the Mac's edge that faces Windows (or Ctrl+Alt + the other
arrow), it returns. The screen arrangement and its walls apply both ways.

One rule keeps the two directions apart: Windows's devices can only be on the Mac while the Mac's own are on the Mac
too (the Mac's pointer is not parked). So when the Mac's devices go to Windows, Windows's come home, and when
Windows's devices go to the Mac while the Mac's were on Windows, the Mac's come home first.

Messages on the control channel (both sides announce GUEST_FEATURE):
  c->s MSG_G_ENTER  JSON {"ratio": float | None}  Windows's devices go to the Mac (None: the hotkey; the Mac's
                                                  pointer stays where it is)
  c->s MSG_G_MOVE   <ff dx, dy (Windows pixels, after Windows's pointer acceleration)
  c->s MSG_G_BUTTON <BB button (0 left, 1 right, 2 middle, 3/4 side), pressed
  c->s MSG_G_SCROLL <ii vertical, horizontal (WHEEL_DELTA units, 120 = one notch; positive = up / right)
  c->s MSG_G_KEY    <BBHH pressed, flags (KEYFLAG_EXTENDED), scan code, vk  (as MSG_KEY)
  c->s MSG_G_LEAVE  Windows took its devices back (hotkey, setting, Windows locked)
  s->c MSG_G_STATE  JSON {"active": bool, "error"?}  the Mac's answer to MSG_G_ENTER
  s->c MSG_G_EXIT   JSON {"ratio": float | None}  the pointer crossed the Mac's edge back to Windows (ratio along
                                                  Windows's facing edge; None: back where it was)
  s->c MSG_G_CONFIG JSON {"edge": side of Windows that faces the Mac, "span": [lo, hi] | None}
"""
from __future__ import annotations

import struct
from typing import Dict, Optional, Set, Tuple

from . import keymap as K
from .protocol import KEYFLAG_EXTENDED

GUEST_FEATURE = "guest"
MSG_G_ENTER = 80
MSG_G_MOVE = 81
MSG_G_BUTTON = 82
MSG_G_SCROLL = 83
MSG_G_KEY = 84
MSG_G_LEAVE = 85
MSG_G_STATE = 86
MSG_G_EXIT = 87
MSG_G_CONFIG = 88

# kCGEventSourceUserData on the Mac's posted events, so its own event tap lets them through untouched
GUEST_MARK = 0x4D57475354                     # "MWGST"

_MOVE = struct.Struct("<ff")
_BUTTON = struct.Struct("<BB")
_SCROLL = struct.Struct("<ii")
_KEY = struct.Struct("<BBHH")


def pack_move(dx: float, dy: float) -> bytes:
    return _MOVE.pack(float(dx), float(dy))


def unpack_move(b: bytes) -> Tuple[float, float]:
    return _MOVE.unpack(b)


def pack_button(button: int, down: bool) -> bytes:
    return _BUTTON.pack(button, 1 if down else 0)


def unpack_button(b: bytes) -> Tuple[int, bool]:
    btn, down = _BUTTON.unpack(b)
    return btn, bool(down)


def pack_scroll(v: int, h: int) -> bytes:
    return _SCROLL.pack(int(v), int(h))


def unpack_scroll(b: bytes) -> Tuple[int, int]:
    return _SCROLL.unpack(b)


def pack_key(down: bool, flags: int, scan: int, vk: int) -> bytes:
    return _KEY.pack(1 if down else 0, flags & 0xFF, scan & 0xFFFF, vk & 0xFFFF)


def unpack_key(b: bytes) -> Tuple[bool, int, int, int]:
    down, flags, scan, vk = _KEY.unpack(b)
    return bool(down), flags, scan, vk


# ---- Windows keys -> Mac key codes ------------------------------------------------------------------------------
_FROM_SCAN: Dict[Tuple[int, bool], int] = {}
for _mac, (_scan, _ext) in K._BASE.items():
    if _mac in K.MODIFIER_KEYCODES:
        continue
    _FROM_SCAN.setdefault((_scan, _ext), _mac)

VK_NUMLOCK = K.VK_NUMLOCK
FAKE_CTRL_SCAN = 0x21D                         # the left Ctrl Windows sends along with AltGr


class WinToMacKeys:
    """Translates a Windows key (flags, scan, vk) into a Mac key code, the inverse of keymap.KeyTranslator with the
    same settings: with "Cmd -> Ctrl, Ctrl -> Win" (the default) Windows's Ctrl is the Mac's ⌘ and Win is ⌃; Alt
    is ⌥ and AltGr the right ⌥ (characters like @ € on a Turkish Q layout, as on Windows)."""

    def __init__(self, swap_cmd_ctrl: bool = True, iso: bool = True):
        self.swap_cmd_ctrl = swap_cmd_ctrl
        self.iso = iso

    def modifier(self, scan: int, ext: bool) -> Optional[int]:
        if (scan, ext) == K.WIN_LCTRL:
            return K.MAC_LCMD if self.swap_cmd_ctrl else K.MAC_LCTRL
        if (scan, ext) == K.WIN_RCTRL:
            return K.MAC_RCMD if self.swap_cmd_ctrl else K.MAC_RCTRL
        if (scan, ext) == K.WIN_LWIN:
            return K.MAC_LCTRL if self.swap_cmd_ctrl else K.MAC_LCMD
        if (scan, ext) == K.WIN_RWIN:
            return K.MAC_RCTRL if self.swap_cmd_ctrl else K.MAC_RCMD
        if (scan, ext) == K.WIN_LALT:
            return K.MAC_LOPT
        if (scan, ext) == K.WIN_RALT:
            return K.MAC_ROPT
        if (scan, ext) == (0x2A, False):
            return K.MAC_LSHIFT
        if (scan, ext) == (0x36, False):
            return K.MAC_RSHIFT
        if (scan, ext) == (0x3A, False):
            return K.MAC_CAPS
        return None

    def translate(self, flags: int, scan: int, vk: int) -> Optional[int]:
        """The Mac key code, or None (keys the Mac has no place for, the fake Ctrl of AltGr)."""
        if scan == FAKE_CTRL_SCAN or (scan & 0x200):
            return None
        ext = bool(flags & KEYFLAG_EXTENDED)
        scan &= 0xFF
        if vk == VK_NUMLOCK:
            return 71                                 # Keypad Clear, where Num Lock sits
        m = self.modifier(scan, ext)
        if m is not None:
            return m
        if (scan, ext) == (0x29, False):
            return 10 if self.iso else 50             # the key left of 1
        if (scan, ext) == (0x56, False):
            return 50                                 # ISO: the key between left Shift and Z
        return _FROM_SCAN.get((scan, ext))


class ModifierState:
    """Which Mac modifiers are held, as CGEventFlags (generic and left/right bits)."""

    def __init__(self):
        self.down: Set[int] = set()
        self.caps = False

    def update(self, keycode: int, down: bool) -> None:
        if keycode == K.MAC_CAPS:
            if down:
                self.caps = not self.caps
            return
        if down:
            self.down.add(keycode)
        else:
            self.down.discard(keycode)

    def flags(self) -> int:
        f = K.FLAG_CAPS if self.caps else 0
        for kc in self.down:
            f |= K._GENERIC_MASKS.get(kc, 0) | K._DEVICE_MASKS.get(kc, 0)
        return f

    def clear(self) -> None:
        self.down.clear()
