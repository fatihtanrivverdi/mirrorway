"""macOS virtual key code (kVK_*) -> Windows scan code (scan code set 1) mapping.

Because scan codes are used, the mapping follows the physical key position: if the same keyboard layout
(e.g. Turkish Q) is selected on the Mac and on Windows, the same characters come out.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from .protocol import KEYFLAG_EXTENDED, KEYFLAG_VK

E = True   # extended (E0-prefixed) key
N = False

# Mac keycode -> (scancode, extended)
_BASE: Dict[int, Tuple[int, bool]] = {
    # Letters
    0: (0x1E, N),   # A
    1: (0x1F, N),   # S
    2: (0x20, N),   # D
    3: (0x21, N),   # F
    4: (0x23, N),   # H
    5: (0x22, N),   # G
    6: (0x2C, N),   # Z
    7: (0x2D, N),   # X
    8: (0x2E, N),   # C
    9: (0x2F, N),   # V
    11: (0x30, N),  # B
    12: (0x10, N),  # Q
    13: (0x11, N),  # W
    14: (0x12, N),  # E
    15: (0x13, N),  # R
    16: (0x15, N),  # Y
    17: (0x14, N),  # T
    31: (0x18, N),  # O
    32: (0x16, N),  # U
    34: (0x17, N),  # I
    35: (0x19, N),  # P
    37: (0x26, N),  # L
    38: (0x24, N),  # J
    40: (0x25, N),  # K
    45: (0x31, N),  # N
    46: (0x32, N),  # M
    # Number row
    18: (0x02, N),  # 1
    19: (0x03, N),  # 2
    20: (0x04, N),  # 3
    21: (0x05, N),  # 4
    23: (0x06, N),  # 5
    22: (0x07, N),  # 6
    26: (0x08, N),  # 7
    28: (0x09, N),  # 8
    25: (0x0A, N),  # 9
    29: (0x0B, N),  # 0
    27: (0x0C, N),  # - (Minus)
    24: (0x0D, N),  # = (Equal)
    # Punctuation
    33: (0x1A, N),  # [
    30: (0x1B, N),  # ]
    41: (0x27, N),  # ;
    39: (0x28, N),  # '
    42: (0x2B, N),  # \
    43: (0x33, N),  # ,
    47: (0x34, N),  # .
    44: (0x35, N),  # /
    # Control keys
    36: (0x1C, N),  # Return
    48: (0x0F, N),  # Tab
    49: (0x39, N),  # Space
    51: (0x0E, N),  # Delete (Backspace)
    53: (0x01, N),  # Escape
    57: (0x3A, N),  # Caps Lock
    56: (0x2A, N),  # Sol Shift
    60: (0x36, N),  # Right Shift
    117: (0x53, E),  # Forward delete (Delete)
    114: (0x52, E),  # Help -> Insert
    115: (0x47, E),  # Home
    119: (0x4F, E),  # End
    116: (0x49, E),  # Page Up
    121: (0x51, E),  # Page Down
    123: (0x4B, E),  # Sol ok
    124: (0x4D, E),  # Right arrow
    125: (0x50, E),  # Down arrow
    126: (0x48, E),  # Up arrow
    110: (0x5D, E),  # Context menu (Apps)
    # Function keys
    122: (0x3B, N),  # F1
    120: (0x3C, N),  # F2
    99: (0x3D, N),   # F3
    118: (0x3E, N),  # F4
    96: (0x3F, N),   # F5
    97: (0x40, N),   # F6
    98: (0x41, N),   # F7
    100: (0x42, N),  # F8
    101: (0x43, N),  # F9
    109: (0x44, N),  # F10
    103: (0x57, N),  # F11
    111: (0x58, N),  # F12
    105: (0x64, N),  # F13
    107: (0x65, N),  # F14
    113: (0x66, N),  # F15
    106: (0x67, N),  # F16
    64: (0x68, N),   # F17
    79: (0x69, N),   # F18
    80: (0x6A, N),   # F19
    90: (0x6B, N),   # F20
    # Numeric keypad
    82: (0x52, N),  # 0
    83: (0x4F, N),  # 1
    84: (0x50, N),  # 2
    85: (0x51, N),  # 3
    86: (0x4B, N),  # 4
    87: (0x4C, N),  # 5
    88: (0x4D, N),  # 6
    89: (0x47, N),  # 7
    91: (0x48, N),  # 8
    92: (0x49, N),  # 9
    65: (0x53, N),  # .
    67: (0x37, N),  # *
    69: (0x4E, N),  # +
    78: (0x4A, N),  # -
    75: (0x35, E),  # /
    76: (0x1C, E),  # Enter
    81: (0x59, N),  # =
    # JIS
    93: (0x7D, N),  # Yen
    94: (0x73, N),  # Underscore (Ro)
    95: (0x7E, N),  # Keypad comma
    104: (0x70, N),  # Kana
}

# Windows virtual key codes (for keys that cannot be sent as scan codes)
VK_NUMLOCK = 0x90
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3

# Mac keycode -> (vk, extended)
_VK_KEYS: Dict[int, Tuple[int, bool]] = {
    71: (VK_NUMLOCK, E),   # Keypad Clear -> Num Lock position
}

# Modifier keys
MAC_LCMD, MAC_RCMD = 55, 54
MAC_LSHIFT, MAC_RSHIFT = 56, 60
MAC_LOPT, MAC_ROPT = 58, 61
MAC_LCTRL, MAC_RCTRL = 59, 62
MAC_FN = 63
MAC_CAPS = 57
MODIFIER_KEYCODES = {MAC_LCMD, MAC_RCMD, MAC_LSHIFT, MAC_RSHIFT, MAC_LOPT, MAC_ROPT,
                     MAC_LCTRL, MAC_RCTRL, MAC_FN, MAC_CAPS}

# Mac event flags (CGEventFlags)
FLAG_CAPS = 0x00010000
FLAG_SHIFT = 0x00020000
FLAG_CONTROL = 0x00040000
FLAG_OPTION = 0x00080000
FLAG_COMMAND = 0x00100000
# Device-specific (left/right) bits
_DEVICE_MASKS = {
    MAC_LCTRL: 0x0001, MAC_LSHIFT: 0x0002, MAC_RSHIFT: 0x0004, MAC_LCMD: 0x0008,
    MAC_RCMD: 0x0010, MAC_LOPT: 0x0020, MAC_ROPT: 0x0040, MAC_RCTRL: 0x2000,
}
_GENERIC_MASKS = {
    MAC_LCTRL: FLAG_CONTROL, MAC_RCTRL: FLAG_CONTROL,
    MAC_LSHIFT: FLAG_SHIFT, MAC_RSHIFT: FLAG_SHIFT,
    MAC_LCMD: FLAG_COMMAND, MAC_RCMD: FLAG_COMMAND,
    MAC_LOPT: FLAG_OPTION, MAC_ROPT: FLAG_OPTION,
}
_DEVICE_BITS_ALL = 0x207F

# Windows modifier scan codes
WIN_LCTRL = (0x1D, N)
WIN_RCTRL = (0x1D, E)
WIN_LALT = (0x38, N)
WIN_RALT = (0x38, E)      # AltGr
WIN_LWIN = (0x5B, E)
WIN_RWIN = (0x5C, E)

# NX_KEYTYPE_* (Mac media keys) -> Windows VK
MEDIA_KEYS = {
    0: VK_VOLUME_UP,          # NX_KEYTYPE_SOUND_UP
    1: VK_VOLUME_DOWN,        # NX_KEYTYPE_SOUND_DOWN
    7: VK_VOLUME_MUTE,        # NX_KEYTYPE_MUTE
    16: VK_MEDIA_PLAY_PAUSE,  # NX_KEYTYPE_PLAY
    17: VK_MEDIA_NEXT_TRACK,  # NX_KEYTYPE_NEXT
    18: VK_MEDIA_PREV_TRACK,  # NX_KEYTYPE_PREVIOUS
    19: VK_MEDIA_NEXT_TRACK,  # NX_KEYTYPE_FAST
    20: VK_MEDIA_PREV_TRACK,  # NX_KEYTYPE_REWIND
}


def modifier_is_down(keycode: int, flags: int) -> bool:
    """Whether the modifier involved in a flagsChanged event is pressed."""
    if keycode == MAC_CAPS:
        return bool(flags & FLAG_CAPS)
    dev = _DEVICE_MASKS.get(keycode)
    if dev is None:
        return False
    if flags & _DEVICE_BITS_ALL:
        return bool(flags & dev)
    return bool(flags & _GENERIC_MASKS.get(keycode, 0))


class KeyTranslator:
    """Translates a Mac key into a Windows (flags, scan, vk) triple according to the settings."""

    def __init__(self, swap_cmd_ctrl: bool = True, left_option: str = "alt", iso: bool = True):
        self.swap_cmd_ctrl = swap_cmd_ctrl
        self.left_option = left_option
        self.iso = iso

    def _modifier(self, keycode: int) -> Optional[Tuple[int, bool]]:
        if keycode in (MAC_LSHIFT, MAC_RSHIFT, MAC_CAPS):
            return _BASE[keycode]
        if keycode == MAC_LCMD:
            return WIN_LCTRL if self.swap_cmd_ctrl else WIN_LWIN
        if keycode == MAC_RCMD:
            return WIN_RCTRL if self.swap_cmd_ctrl else WIN_RWIN
        if keycode == MAC_LCTRL:
            return WIN_LWIN if self.swap_cmd_ctrl else WIN_LCTRL
        if keycode == MAC_RCTRL:
            return WIN_RWIN if self.swap_cmd_ctrl else WIN_RCTRL
        if keycode == MAC_LOPT:
            return WIN_RALT if self.left_option == "altgr" else WIN_LALT
        if keycode == MAC_ROPT:
            return WIN_RALT
        return None

    def translate(self, keycode: int, iso: Optional[bool] = None) -> Optional[Tuple[int, int, int]]:
        """Returns (flags, scan, vk); None for keys that cannot be mapped."""
        iso = self.iso if iso is None else iso
        if keycode == MAC_FN:
            return None
        if keycode in _VK_KEYS:
            vk, ext = _VK_KEYS[keycode]
            return (KEYFLAG_VK | (KEYFLAG_EXTENDED if ext else 0), 0, vk)
        if keycode in MODIFIER_KEYCODES:
            m = self._modifier(keycode)
            if m is None:
                return None
            scan, ext = m
            return ((KEYFLAG_EXTENDED if ext else 0), scan, 0)
        # ISO/ANSI difference: on Apple ISO keyboards the key left of "1" arrives as kVK_ISO_Section(10),
        # and the key between left Shift and Z as kVK_ANSI_Grave(50).
        if keycode == 10:
            return (0, 0x29, 0)
        if keycode == 50:
            return (0, 0x56 if iso else 0x29, 0)
        entry = _BASE.get(keycode)
        if entry is None:
            return None
        scan, ext = entry
        return ((KEYFLAG_EXTENDED if ext else 0), scan, 0)

    @staticmethod
    def media(nx_key: int) -> Optional[Tuple[int, int, int]]:
        vk = MEDIA_KEYS.get(nx_key)
        if vk is None:
            return None
        return (KEYFLAG_VK | KEYFLAG_EXTENDED, 0, vk)


def win_key_is_windows_logo(flags: int, scan: int) -> bool:
    return bool(flags & KEYFLAG_EXTENDED) and scan in (0x5B, 0x5C) and not (flags & KEYFLAG_VK)
