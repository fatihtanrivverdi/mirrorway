"""Windows input injection (SendInput) and clipboard."""
from __future__ import annotations

from typing import List, Optional, Tuple

from ..geometry import Rect
from ..protocol import (BUTTON_LEFT, BUTTON_MIDDLE, BUTTON_RIGHT, BUTTON_X1, BUTTON_X2,
                        KEYFLAG_EXTENDED, KEYFLAG_VK)
from . import winapi as W

_BUTTONS = {
    BUTTON_LEFT: (W.MOUSEEVENTF_LEFTDOWN, W.MOUSEEVENTF_LEFTUP, 0),
    BUTTON_RIGHT: (W.MOUSEEVENTF_RIGHTDOWN, W.MOUSEEVENTF_RIGHTUP, 0),
    BUTTON_MIDDLE: (W.MOUSEEVENTF_MIDDLEDOWN, W.MOUSEEVENTF_MIDDLEUP, 0),
    BUTTON_X1: (W.MOUSEEVENTF_XDOWN, W.MOUSEEVENTF_XUP, W.XBUTTON1),
    BUTTON_X2: (W.MOUSEEVENTF_XDOWN, W.MOUSEEVENTF_XUP, W.XBUTTON2),
}


class WinInputBackend:
    def get_pos(self) -> Tuple[int, int]:
        return W.get_cursor_pos()

    def monitors(self) -> List[Rect]:
        return W.monitors()

    def move_to(self, x: float, y: float) -> None:
        W.move_absolute(int(round(x)), int(round(y)))

    def button(self, btn: int, down: bool) -> None:
        spec = _BUTTONS.get(btn)
        if spec is None:
            return
        d, u, data = spec
        W.send_inputs(W.mouse_input(d if down else u, data=data))

    def wheel(self, v: int, h: int) -> None:
        inputs = []
        if v:
            inputs.append(W.mouse_input(W.MOUSEEVENTF_WHEEL, data=int(v)))
        if h:
            inputs.append(W.mouse_input(W.MOUSEEVENTF_HWHEEL, data=int(h)))
        if inputs:
            W.send_inputs(*inputs)

    def key(self, down: bool, flags: int, scan: int, vk: int) -> None:
        f = 0 if down else W.KEYEVENTF_KEYUP
        if flags & KEYFLAG_EXTENDED:
            f |= W.KEYEVENTF_EXTENDEDKEY
        if flags & KEYFLAG_VK:
            W.send_inputs(W.key_input(vk, W.map_vk_to_scan(vk), f))
        else:
            W.send_inputs(W.key_input(0, scan, f | W.KEYEVENTF_SCANCODE))

    def type_text(self, text: str) -> None:
        inputs = type_text_inputs(text)
        if inputs:
            W.send_inputs(*inputs)


def utf16_units(text: str):
    data = text.encode("utf-16-le")
    return [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]


def type_text_inputs(text: str):
    """Turn text into Unicode key events (press + release for every UTF-16 unit)."""
    inputs = []
    for unit in utf16_units(text):
        inputs.append(W.key_input(0, unit, W.KEYEVENTF_UNICODE))
        inputs.append(W.key_input(0, unit, W.KEYEVENTF_UNICODE | W.KEYEVENTF_KEYUP))
    return inputs


class WinClipboard:
    def __init__(self):
        import pyperclip
        self._pc = pyperclip

    def change_count(self) -> int:
        return W.clipboard_sequence()

    def get_text(self) -> Optional[str]:
        text = self._pc.paste()
        return text if text else None

    def set_text(self, text: str) -> None:
        self._pc.copy(text)
