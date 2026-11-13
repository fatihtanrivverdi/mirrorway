"""CI (GitHub Windows runner): this PC's keyboard and mouse on the Mac (3.6), the Windows plumbing for real.

The runner has no real mouse or keyboard, so input is injected with SendInput and the hooks are told to take
injected input too (accept_injected). Checked:
  - the thread starts: message-only window, raw mouse input, the Ctrl+Alt+arrow hotkey;
  - a push past the edge towards the Mac (raw input) starts the capture: hooks installed, pointer parked and the
    system cursors swapped for a blank one;
  - while captured, a mouse move is kept from Windows (the pointer stays parked) and goes out as a movement;
    a key and a click go out and do not reach Windows;
  - Ctrl+Alt + the arrow away from the Mac ends it: hooks removed, pointer back where it was, cursors restored;
  - Ctrl+Alt + the arrow towards the Mac starts it again (the hotkey); the Mac sending it back at a ratio puts the
    pointer on the edge there.
If the runner delivers no input to the hooks at all, those parts are only reported.
Run: python tests/ci_guest_windows.py
"""
import ctypes
import os
import sys
import tempfile
import time

if not os.environ.get("CI") or sys.platform != "win32":
    sys.exit("This script is for the Windows CI only.")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
os.environ.setdefault("MIRRORWAY_HOME", tempfile.mkdtemp(prefix="mirrorway-ci-guest-"))

import logging  # noqa: E402

from conftest import DictCfg  # noqa: E402
from mirrorway import guest as GU  # noqa: E402
from mirrorway.win import winapi as W  # noqa: E402
from mirrorway.win.guest import WinGuest  # noqa: E402
from mirrorway.win.guest_hooks import GuestHooks  # noqa: E402

logging.basicConfig(level=logging.INFO, format="  %(name)s: %(message)s", stream=sys.stdout)
W.set_dpi_aware()
failures, warnings = [], []


def check(name, cond, info=""):
    print(("[OK]   " if cond else "[FAIL] ") + name + (f": {info}" if info else ""), flush=True)
    if not cond:
        failures.append(name)


def report(name, cond, info=""):
    print(("[OK]   " if cond else "[WARN] ") + name + (f": {info}" if info else ""), flush=True)
    if not cond:
        warnings.append(name)


def wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def key(vk, up=False, ext=False):
    flags = (W.KEYEVENTF_KEYUP if up else 0) | (W.KEYEVENTF_EXTENDEDKEY if ext else 0)
    return W.key_input(vk, W.map_vk_to_scan(vk), flags)


def cursor_shown():
    """True while the arrow is the normal system cursor (not Mirrorway's blank one)."""
    class CURSORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint32), ("flags", ctypes.c_uint32), ("hCursor", ctypes.c_void_p),
                    ("x", ctypes.c_long), ("y", ctypes.c_long)]
    ci = CURSORINFO(cbSize=ctypes.sizeof(CURSORINFO))
    ctypes.windll.user32.GetCursorInfo(ctypes.byref(ci))
    return ci.hCursor


sent = []
cfg = DictCfg(guest=True, guest_cursor_hidden=False)
hooks = GuestHooks(W.monitors, cfg=cfg, accept_injected=True)
guest = WinGuest(hooks, lambda m, p=b"": sent.append((m, p)))
hooks.guest = guest
guest.configure("left", None)                    # the Mac is to the left of this PC
hooks.start()
check("the thread starts (window, raw input)", not hooks.error and bool(hooks.hwnd), hooks.error)
check("Ctrl+Alt+Left is registered", wait(lambda: hooks._hotkey_vk == 0x25, 2), str(hooks._hotkey_vk))
mons = W.monitors()
prim = mons[0]
print(f"monitors: {mons}")

# 1. a push past the left edge (raw input) starts the capture
W.user32.SetCursorPos(int(prim.x), int(prim.y + prim.h // 2))
home = W.get_cursor_pos()
arrow = cursor_shown()
for _ in range(4):
    W.send_inputs(W.mouse_input(W.MOUSEEVENTF_MOVE, -6, 0))
    time.sleep(0.05)
got_raw = wait(lambda: guest.capturing, 3)
report("raw input reaches the window (injected input on this runner)", hooks.raw_events > 0,
       f"{hooks.raw_events} raw events")
if got_raw:
    check("pushing past the edge towards the Mac starts the capture", sent and sent[-1][0] == GU.MSG_G_ENTER,
          sent[-1][1].decode() if sent else "")
    check("the hooks are installed", wait(lambda: len(hooks._hooks) == 2, 2), str(hooks._hooks))
    check("the pointer is parked in the middle", W.get_cursor_pos() == guest.park, f"{W.get_cursor_pos()} "
          f"vs {guest.park}")
    check("the pointer is hidden (blank system cursors)", hooks.hidden and cursor_shown() != arrow)
    guest.on_state({"active": True})

    # 2. captured: moves, keys and clicks go out and do not reach Windows
    n = len(sent)
    W.send_inputs(W.mouse_input(W.MOUSEEVENTF_MOVE, 40, 15))
    moved = wait(lambda: any(m == GU.MSG_G_MOVE for m, _ in sent[n:]), 2)
    report("the mouse hook sees input (injected input on this runner)", hooks.hook_events > 0,
           f"{hooks.hook_events} hook events")
    if moved:
        dx, dy = next(GU.unpack_move(p) for m, p in sent[n:] if m == GU.MSG_G_MOVE)
        check("a move goes out to the Mac", dx > 0 and dy > 0, f"dx {dx}, dy {dy}")
        check("and the pointer stays parked", W.get_cursor_pos() == guest.park, str(W.get_cursor_pos()))
        n = len(sent)
        W.send_inputs(key(0x41), key(0x41, up=True))           # A
        W.send_inputs(W.mouse_input(W.MOUSEEVENTF_LEFTDOWN), W.mouse_input(W.MOUSEEVENTF_LEFTUP))
        W.send_inputs(W.mouse_input(W.MOUSEEVENTF_WHEEL, data=120))
        wait(lambda: len(sent) >= n + 5, 2)
        kinds = [m for m, _ in sent[n:]]
        check("a key, a click and the wheel go out", kinds.count(GU.MSG_G_KEY) == 2
              and kinds.count(GU.MSG_G_BUTTON) == 2 and GU.MSG_G_SCROLL in kinds, str(kinds))
        keys = [GU.unpack_key(p) for m, p in sent[n:] if m == GU.MSG_G_KEY]
        check("with its scan code", keys and keys[0][2] == 0x1E and keys[0][3] == 0x41, str(keys))
        # 3. Ctrl+Alt+Right: home
        W.send_inputs(key(0xA2), key(0xA4), key(0x27, ext=True), key(0x27, up=True, ext=True),
                      key(0xA4, up=True), key(0xA2, up=True))
        check("Ctrl+Alt + the arrow away from the Mac ends it", wait(lambda: not guest.capturing, 2))
        check("with a message to the Mac", sent[-1][0] == GU.MSG_G_LEAVE, str(sent[-1][0]))
    else:
        guest.end(None, notify=True)
    check("the hooks are removed", wait(lambda: not hooks._hooks, 2), str(hooks._hooks))
    check("the pointer is back where it was", W.get_cursor_pos() == home, f"{W.get_cursor_pos()} vs {home}")
    check("the cursors are restored", not hooks.hidden and cfg.get("guest_cursor_hidden") is False)

# 4. the hotkey towards the Mac, and the Mac sending it back at a ratio
time.sleep(0.3)
W.send_inputs(key(0xA2), key(0xA4), key(0x25, ext=True), key(0x25, up=True, ext=True), key(0xA4, up=True),
              key(0xA2, up=True))
hot = wait(lambda: guest.capturing, 2)
report("Ctrl+Alt+Left starts it (hotkey, injected input on this runner)", hot)
if not hot:
    guest.hotkey()                                       # the same path without the keyboard
check("captured", wait(lambda: guest.capturing, 2) and sent[-1][0] == GU.MSG_G_ENTER)
guest.on_state({"active": True})
guest.on_exit({"ratio": 0.25})
check("sent back at 0.25: the pointer is on the left edge there", wait(lambda: not guest.capturing, 2)
      and W.get_cursor_pos()[0] <= prim.x + 4 and abs(W.get_cursor_pos()[1] - (prim.y + 0.25 * (prim.h - 1))) <= 2,
      str(W.get_cursor_pos()))
check("the cursors are restored again", not hooks.hidden)
hooks.stop()
print("RESULT: " + ("PASSED" if not failures else "FAILED: " + ", ".join(failures))
      + (f" (reported: {', '.join(warnings)})" if warnings else ""))
sys.exit(1 if failures else 0)
