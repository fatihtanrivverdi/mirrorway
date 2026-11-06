"""Fake modules for the Mac and Windows APIs (only to test the logic of the glue code)."""
import ctypes
import sys
import types
from types import SimpleNamespace as NS


# ---------------------------------------------------------------------------
# macOS: Quartz, AppKit, ApplicationServices, rumps
# ---------------------------------------------------------------------------
class FakeCGEvent:
    def __init__(self, fields=None, **kw):
        self.f = dict(fields or {})
        self.f.update(kw)
        self.type = None


def install_mac():
    Q = types.ModuleType("Quartz")
    consts = dict(
        kCGEventLeftMouseDown=1, kCGEventLeftMouseUp=2, kCGEventRightMouseDown=3, kCGEventRightMouseUp=4,
        kCGEventMouseMoved=5, kCGEventLeftMouseDragged=6, kCGEventRightMouseDragged=7,
        kCGEventKeyDown=10, kCGEventKeyUp=11, kCGEventFlagsChanged=12, kCGEventScrollWheel=22,
        kCGEventOtherMouseDown=25, kCGEventOtherMouseUp=26, kCGEventOtherMouseDragged=27,
        kCGEventTapDisabledByTimeout=0xFFFFFFFE, kCGEventTapDisabledByUserInput=0xFFFFFFFF,
        kCGMouseEventButtonNumber=3, kCGMouseEventDeltaX=4, kCGMouseEventDeltaY=5,
        kCGKeyboardEventAutorepeat=8, kCGKeyboardEventKeycode=9, kCGKeyboardEventKeyboardType=10,
        kCGScrollWheelEventDeltaAxis1=11, kCGScrollWheelEventDeltaAxis2=12,
        kCGScrollWheelEventIsContinuous=88, kCGScrollWheelEventPointDeltaAxis1=96,
        kCGScrollWheelEventPointDeltaAxis2=97,
        kCGSessionEventTap=1, kCGHeadInsertEventTap=0, kCGEventTapOptionDefault=0,
        kCFRunLoopDefaultMode="default", kCGEventSourceStateCombinedSessionState=0,
    )
    for k, v in consts.items():
        setattr(Q, k, v)
    Q.calls = []
    Q.CGEventGetLocation = lambda e: NS(x=e.f.get("x", 0.0), y=e.f.get("y", 0.0))
    Q.CGEventGetDoubleValueField = lambda e, fld: float(e.f.get(fld, 0))
    Q.CGEventGetIntegerValueField = lambda e, fld: int(e.f.get(fld, 0))
    Q.CGEventGetFlags = lambda e: e.f.get("flags", 0)
    Q.CGEventKeyboardGetUnicodeString = lambda e, n, a, b: (len(e.f.get("text", "")), e.f.get("text", ""))
    Q.CGEventSetType = lambda e, t: setattr(e, "type", t)
    Q.CGEventTapEnable = lambda tap, v: Q.calls.append(("tapenable", v))
    Q.CGGetActiveDisplayList = lambda n, a, b: (0, [1, 2], 2)
    Q.CGMainDisplayID = lambda: 1
    bounds = {1: (0, 0, 1440, 900), 2: (1440, 0, 1920, 1080)}
    Q.CGDisplayBounds = lambda d: NS(origin=NS(x=bounds[d][0], y=bounds[d][1]),
                                     size=NS(width=bounds[d][2], height=bounds[d][3]))
    Q.CGDisplayPixelsWide = lambda d: bounds[d][2] * 2
    Q.CGDisplayPixelsHigh = lambda d: bounds[d][3] * 2
    Q.CGPointMake = lambda x, y: (x, y)
    Q.CGWarpMouseCursorPosition = lambda p: Q.calls.append(("warp", p))
    Q.CGAssociateMouseAndMouseCursorPosition = lambda v: None
    Q.CGDisplayHideCursor = lambda d: Q.calls.append(("hide", d))
    Q.CGDisplayShowCursor = lambda d: Q.calls.append(("show", d))
    Q.CGSetLocalEventsSuppressionInterval = lambda v: None
    Q.CGEventSourceCreate = lambda s: object()
    Q.CGEventSourceSetLocalEventsSuppressionInterval = lambda s, v: None
    Q.CGEventCreate = lambda s: FakeCGEvent(x=10, y=20)
    Q.CGPreflightScreenCaptureAccess = lambda: True
    Q.CGRequestScreenCaptureAccess = lambda: True
    Q.CGRequestListenEventAccess = lambda: True
    sys.modules["Quartz"] = Q

    AK = types.ModuleType("AppKit")

    class Pasteboard:
        """A fake NSPasteboard holding content by type (s: plain text, for older tests)."""
        def __init__(self):
            self.count, self.items = 0, {}

        @property
        def s(self):
            return self.items.get("public.utf8-plain-text")

        def changeCount(self):
            return self.count

        def types(self):
            return list(self.items)

        def stringForType_(self, t):
            v = self.items.get(t)
            return v if isinstance(v, str) else None

        def dataForType_(self, t):
            v = self.items.get(t)
            return v if isinstance(v, (bytes, bytearray)) else None

        def clearContents(self):
            self.count += 1
            self.items = {}

        def setString_forType_(self, s, t):
            self.items[t] = s

        def setData_forType_(self, d, t):
            self.items[t] = bytes(d)

        def writeObjects_(self, objs):
            self.items["public.file-url"] = list(objs)
            return True

        def readObjectsForClasses_options_(self, classes, opts):
            return list(self.items.get("public.file-url") or [])

    pb = Pasteboard()
    AK.NSPasteboard = NS(generalPasteboard=lambda: pb)
    AK.NSPasteboardTypeString = "public.utf8-plain-text"

    class Url:
        def __init__(self, p):
            self._p = p

        def path(self):
            return self._p
    AK.NSURL = NS(fileURLWithPath_=lambda p: Url(p))
    F = sys.modules.get("Foundation") or types.ModuleType("Foundation")
    F.NSData = NS(dataWithBytes_length_=lambda b, n: bytes(b[:n]))
    sys.modules["Foundation"] = F
    AK.NSApplication = NS(sharedApplication=lambda: NS(setActivationPolicy_=lambda p: None))

    class NSEvent:
        @staticmethod
        def eventWithCGEvent_(e):
            return NS(subtype=lambda: e.f.get("subtype", 0), data1=lambda: e.f.get("data1", 0))
    AK.NSEvent = NSEvent
    sys.modules["AppKit"] = AK

    AS = types.ModuleType("ApplicationServices")
    AS.kAXTrustedCheckOptionPrompt = "prompt"
    AS.AXIsProcessTrustedWithOptions = lambda d: True
    sys.modules["ApplicationServices"] = AS

    R = types.ModuleType("rumps")
    R.separator = object()
    R.warnings = []

    class Menu:
        def __init__(self):
            self._items = {}
            self._order = []

        def clear(self):
            self._items, self._order = {}, []

        def add(self, item):
            if item is None or item is R.separator:
                self._order.append(None)
                return
            key = item.title
            if key in self._items:
                R.warnings.append(f"duplicate title ignored: {key!r}")
                return
            self._items[key] = item
            self._order.append(item)

    class MenuItem(Menu):
        def __init__(self, title, callback=None, key=None):
            super().__init__()
            self.title = str(title)
            self.callback = callback
            self.state = 0

    class App:
        def __init__(self, name, title=None, icon=None, template=None, menu=None, quit_button="Quit"):
            self.name, self.title, self.icon, self.template = name, title, icon, template
            self._menu = Menu()

        @property
        def menu(self):
            return self._menu

        @menu.setter
        def menu(self, items):
            for it in items:
                self._menu.add(it)

        def run(self):
            pass

    class Timer:
        def __init__(self, cb, interval):
            self.cb = cb

        def start(self):
            pass

    class Response:
        def __init__(self, clicked, text):
            self.clicked, self.text = clicked, text

    class Window:
        responses = []          # tests append (clicked, text)

        def __init__(self, message="", title="", default_text="", ok=None, cancel=None, dimensions=(320, 160),
                     secure=False):
            self.message, self.title, self.secure = message, title, secure

        def run(self):
            c, t = Window.responses.pop(0) if Window.responses else (0, "")
            return Response(c, t)

    R.Menu, R.MenuItem, R.App, R.Timer, R.Window = Menu, MenuItem, App, Timer, Window
    R.notifications = []
    R.notification = lambda *a, **k: R.notifications.append(a)
    R.alerts = []
    R.alert = lambda *a, **k: (R.alerts.append(a), 1)[1]
    R.quit_application = lambda *a: None
    sys.modules["rumps"] = R
    return Q, AK, R, pb


# ---------------------------------------------------------------------------
# Windows: ctypes.WinDLL / WINFUNCTYPE / windll
# ---------------------------------------------------------------------------
class _Fn:
    def __init__(self, name, impl, log):
        self.name, self.impl, self.log = name, impl, log
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        self.log.append((self.name, args))
        return self.impl(*args)


class FakeUser32:
    def __init__(self):
        self.log = []
        self.cursor = [100, 200]
        self.sent = []
        metrics = {76: 0, 77: 0, 78: 1920, 79: 1080}

        def send_input(n, arr, size):
            for i in range(n):
                inp = arr[i]
                if inp.type == 0:
                    mi = inp.u.mi
                    self.sent.append(("mouse", mi.dx, mi.dy, mi.mouseData, mi.dwFlags))
                else:
                    ki = inp.u.ki
                    self.sent.append(("key", ki.wVk, ki.wScan, ki.dwFlags))
            return n

        def get_cursor_pos(ref):
            ref._obj.x, ref._obj.y = self.cursor
            return 1

        def enum_monitors(hdc, clip, cb, lp):
            from ctypes import wintypes
            for r in ((0, 0, 1920, 1080), (-1280, 100, 0, 1124)):
                rect = wintypes.RECT(*r)
                cb(None, None, ctypes.pointer(rect), 0)
            return 1

        impls = {
            "SendInput": send_input,
            "GetCursorPos": get_cursor_pos,
            "GetSystemMetrics": lambda i: metrics[i],
            "MapVirtualKeyW": lambda vk, t: 0x30,
            "GetClipboardSequenceNumber": lambda: 7,
            "EnumDisplayMonitors": enum_monitors,
            "SetProcessDpiAwarenessContext": lambda c: 1,
        }
        for name, impl in impls.items():
            setattr(self, name, _Fn(name, impl, self.log))


def install_windows():
    u = FakeUser32()
    ctypes.WinDLL = lambda name, use_last_error=False: u
    ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE
    ctypes.windll = NS(shcore=NS(SetProcessDpiAwareness=lambda v: 0))
    ctypes.get_last_error = lambda: 0
    return u
