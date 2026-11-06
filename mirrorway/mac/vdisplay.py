"""A virtual display on the Mac (for second-screen mode).

macOS's undocumented CGVirtualDisplay interface is used (DeskPad and BetterDisplay use it too). The virtual display
is opened in a SEPARATE helper process:
  - If that process crashes or exits, macOS removes the display on its own; Mirrorway itself is not affected.
  - The process that opens the display must not have queried the display list before (else it cannot read the new mode).

Helper: python -m mirrorway.mac.vdisplay --width 1920 --height 1080 [--x X --y Y]
  writes one line of JSON to stdout: {"ok": true, "id": <CGDirectDisplayID>, "bounds": [x, y, w, h]} or {"ok": false, ...}
  then waits until stdin closes; when it does (the main process closed it or died), it exits and the display goes away.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger("mirrorway.vdisplay")

VENDOR_ID = 0x4B50            # "KP"
NAME = "Mirrorway (Windows)"
START_TIMEOUT = 12.0


def product_id(w: int, h: int) -> int:
    """Every resolution gets its own "monitor" identity: macOS remembers placement and mode by identity."""
    return 0x0100 + ((int(w) * 31 + int(h)) % 0xFE00)


def size_mm(w: int, h: int, dpi: float = 96.0):
    return (w / dpi * 25.4, h / dpi * 25.4)


# ---- helper process (macOS) -----------------------------------------------------------------------
def _create(w: int, h: int, hidpi: bool):
    import objc
    import Quartz  # noqa: F401  (loads CoreGraphics; the private classes live in it)

    def cls(name):
        try:
            return objc.lookUpClass(name)
        except objc.nosuchclass_error:
            raise RuntimeError(f"{name} is missing on this macOS version (virtual displays not supported)")

    Descriptor, Display = cls("CGVirtualDisplayDescriptor"), cls("CGVirtualDisplay")
    Settings, Mode = cls("CGVirtualDisplaySettings"), cls("CGVirtualDisplayMode")

    d = Descriptor.alloc().init()
    queue = None
    try:
        import dispatch
        queue = dispatch.dispatch_queue_create(b"mirrorway.vdisplay", None)
    except Exception as exc:
        log.debug("no dispatch queue: %s", exc)
    if queue is not None:
        if d.respondsToSelector_("setDispatchQueue:"):
            d.setDispatchQueue_(queue)
        elif d.respondsToSelector_("setQueue:"):
            d.setQueue_(queue)
    d.setName_(NAME)
    d.setMaxPixelsWide_(int(w) * (2 if hidpi else 1))
    d.setMaxPixelsHigh_(int(h) * (2 if hidpi else 1))
    mw, mh = size_mm(w, h)
    d.setSizeInMillimeters_(Quartz.CGSizeMake(mw, mh))
    d.setVendorID_(VENDOR_ID)
    d.setProductID_(product_id(w, h))
    d.setSerialNum_(1)

    disp = Display.alloc().initWithDescriptor_(d)
    if disp is None:
        raise RuntimeError("macOS did not create the virtual display")
    s = Settings.alloc().init()
    s.setHiDPI_(1 if hidpi else 0)
    pw, ph = (int(w) * 2, int(h) * 2) if hidpi else (int(w), int(h))
    s.setModes_([Mode.alloc().initWithWidth_height_refreshRate_(pw, ph, 60.0)])
    if not disp.applySettings_(s):
        raise RuntimeError("macOS did not accept the virtual display settings")
    return disp, int(disp.displayID())


def _active_ids() -> List[int]:
    import Quartz
    err, ids, count = Quartz.CGGetActiveDisplayList(32, None, None)
    return [int(i) for i in (ids or [])[:count]] if err == 0 else []


def _bounds(did: int):
    import Quartz
    b = Quartz.CGDisplayBounds(did)
    return [int(b.origin.x), int(b.origin.y), int(b.size.width), int(b.size.height)]


def _place(did: int, x: int, y: int) -> Optional[str]:
    import Quartz
    res = Quartz.CGBeginDisplayConfiguration(None)
    err, cfg = res if isinstance(res, tuple) else (0, res)
    if err != 0 or cfg is None:
        return f"could not open the configuration ({err})"
    err = Quartz.CGConfigureDisplayOrigin(cfg, did, int(x), int(y))
    if err != 0:
        Quartz.CGCancelDisplayConfiguration(cfg)
        return f"could not set the position ({err})"
    err = Quartz.CGCompleteDisplayConfiguration(cfg, Quartz.kCGConfigureForSession)
    return None if err == 0 else f"could not apply the configuration ({err})"


def helper_main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--width", type=int, required=True)
    p.add_argument("--height", type=int, required=True)
    p.add_argument("--x", type=int)
    p.add_argument("--y", type=int)
    p.add_argument("--hidpi", type=int, default=0)
    a = p.parse_args(argv)

    def say(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    try:
        disp, did = _create(a.width, a.height, bool(a.hidpi))
    except Exception as exc:
        say({"ok": False, "error": str(exc)})
        return 2
    deadline = time.monotonic() + 4.0
    while did not in _active_ids() and time.monotonic() < deadline:
        time.sleep(0.05)
    note = None
    if did not in _active_ids():
        note = "the display did not appear in the list"
    elif a.x is not None and a.y is not None:
        note = _place(did, a.x, a.y)
        time.sleep(0.2)
    say({"ok": True, "id": did, "bounds": _bounds(did), "note": note, "pid": os.getpid()})
    try:
        sys.stdin.read()             # EOF when the main process closes it (or dies)
    except Exception:
        pass
    del disp
    return 0


# ---- main process side -----------------------------------------------------------------------------
class VirtualDisplayError(RuntimeError):
    pass


class VirtualDisplay:
    """Starts and manages the helper process. info: {"id", "bounds", "note"}."""

    def __init__(self, width: int, height: int, origin=None, hidpi: bool = False,
                 popen: Callable = subprocess.Popen, python: Optional[str] = None):
        self.width, self.height = int(width), int(height)
        self.origin = origin
        self.hidpi = hidpi
        self._popen = popen
        self.python = python or sys.executable
        self.proc = None
        self.info: dict = {}

    @property
    def display_id(self) -> int:
        return int(self.info.get("id") or 0)

    def start(self, timeout: float = START_TIMEOUT) -> dict:
        args = [self.python, "-m", "mirrorway.mac.vdisplay", "--width", str(self.width), "--height", str(self.height),
                "--hidpi", "1" if self.hidpi else "0"]
        if self.origin is not None:
            args += ["--x", str(int(self.origin[0])), "--y", str(int(self.origin[1]))]
        env = dict(os.environ)
        root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        self.proc = self._popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, cwd=root)
        box = {}

        def read_line():
            try:
                box["line"] = self.proc.stdout.readline()
            except Exception as exc:
                box["exc"] = exc

        t = threading.Thread(target=read_line, daemon=True, name="mirrorway-vdisplay-start")
        t.start()
        t.join(timeout)
        line = box.get("line")
        if not line:
            err = ""
            if self.proc.poll() is not None:
                try:
                    err = (self.proc.stderr.read() or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
                    err = err[0] if err else ""
                except Exception:
                    err = ""
            self.stop()
            raise VirtualDisplayError("the virtual display did not start" + (f": {err}" if err else " (timed out)"))
        try:
            info = json.loads(line.decode("utf-8") if isinstance(line, bytes) else line)
        except Exception:
            self.stop()
            raise VirtualDisplayError(f"unexpected reply from the helper: {line[:200]!r}")
        if not info.get("ok"):
            self.stop()
            raise VirtualDisplayError(str(info.get("error") or "the virtual display could not be created"))
        self.info = info
        return info

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout: float = 3.0) -> None:
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
        except Exception:
            pass
        try:
            p.wait(timeout)
        except Exception:
            try:
                p.terminate()
                p.wait(2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    sys.exit(helper_main())
