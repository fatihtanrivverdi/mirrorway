"""Windows global shortcuts (mirrorway/win/hotkeys.py) with a fake user32."""
import threading

from mirrorway.win import hotkeys as HK


class FakeUser32:
    def __init__(self, messages, taken=()):
        self.messages = list(messages)          # (message, wParam) in order; the loop ends after them
        self.taken = set(taken)
        self.registered, self.unregistered = {}, []
        self.posted = []

    def RegisterHotKey(self, hwnd, ident, mods, vk):
        if vk in self.taken:
            return 0
        self.registered[ident] = (mods, vk)
        return 1

    def UnregisterHotKey(self, hwnd, ident):
        self.unregistered.append(ident)
        return 1

    def GetMessageW(self, ref, hwnd, a, b):
        if not self.messages:
            return 0                            # WM_QUIT
        m, w = self.messages.pop(0)
        ref._obj.message, ref._obj.wParam = m, w
        return 1

    def PostThreadMessageW(self, tid, msg, w, lp):
        self.posted.append((tid, msg))
        return 1


def _run(hk):
    hk.start()
    hk._thread.join(5) if hk._thread else None


def test_hotkeys_register_dispatch_and_unregister():
    got = []
    u = FakeUser32([(HK.WM_HOTKEY, 1), (0x0200, 0), (HK.WM_HOTKEY, 99), (HK.WM_HOTKEY, 1)])
    hk = HK.Hotkeys({"screen": "s", "panel": ""}, got.append, user32=u)
    _run(hk)
    assert u.registered == {1: (HK.MOD_CONTROL | HK.MOD_ALT | HK.MOD_NOREPEAT, ord("S"))}
    assert got == ["screen", "screen"] and hk.registered == ["screen"] and hk.failed == []
    assert u.unregistered == [1]


def test_taken_shortcut_is_skipped():
    got = []
    u = FakeUser32([(HK.WM_HOTKEY, 1)], taken={ord("S")})
    hk = HK.Hotkeys({"screen": "s"}, got.append, user32=u)
    _run(hk)
    assert hk.failed == ["screen"] and got == [] and u.unregistered == []


def test_stop_posts_quit_and_errors_are_contained():
    release = threading.Event()

    class Blocking(FakeUser32):
        def GetMessageW(self, ref, hwnd, a, b):
            release.wait(5)
            return 0

    u = Blocking([])
    hk = HK.Hotkeys({"screen": "s"}, lambda a: None, user32=u)
    hk.start()
    assert hk.ready.wait(5)
    t = hk._thread
    hk.stop()
    release.set()
    t.join(5)
    assert u.posted and u.posted[0][1] == HK.WM_QUIT

    def boom(action):
        raise RuntimeError("x")
    hk = HK.Hotkeys({"screen": "s"}, boom, user32=FakeUser32([(HK.WM_HOTKEY, 1)]))
    _run(hk)                                    # the error is logged, the loop ends normally
    hk = HK.Hotkeys({}, boom)                   # nothing to register: no thread
    hk.start()
    assert hk._thread is None and hk.ready.is_set()
