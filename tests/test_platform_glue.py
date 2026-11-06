"""Runs the Mac/Windows glue code with fake APIs in a separate process."""
import os
import subprocess
import sys
import textwrap

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def run_script(code: str):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([ROOT, HERE]))
    r = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True,
                       env=env, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def test_mac_tap_conversion_and_callback():
    out = run_script("""
        import stubs
        Q, AK, R, pb = stubs.install_mac()
        from conftest import DictCfg
        from mirrorway.mac.tap import EventTap, MacPlatform
        from mirrorway.mac.router import Router
        from mirrorway import protocol as P
        from stubs import FakeCGEvent as E

        class Sink:
            def __init__(s): s.msgs = []
            def connected(s): return True
            def send(s, m, p=b''): s.msgs.append((m, p))
            def send_json(s, m, o): s.msgs.append((m, o))

        plat = MacPlatform()
        assert [r.w for r in plat.displays()] == [1440, 1920]
        sink = Sink()
        cfg = DictCfg(side='right', speed=1.0, scroll_speed=1.0, swap_cmd_ctrl=True, left_option='alt',
                      keyboard_layout='iso', edge_switch=True)
        router = Router(plat, sink, cfg)
        tap = EventTap(router)
        tap.tap = object()
        cb = tap._callback
        # inner edge (right of display 1): no switch
        ev = E({4: 3}, x=1439.0, y=400.0)
        assert cb(None, 5, ev, None) is ev and not router.remote
        # outer edge (right of display 2): switch to Windows
        ev = E({4: 3}, x=3359.0, y=500.0)
        assert cb(None, 5, ev, None) is ev and router.remote
        assert ('hide', 1) in Q.calls
        # a drag -> turned into mouse movement
        ev = E({4: 2, 5: 1}, x=720.0, y=450.0)
        assert cb(None, 6, ev, None) is ev and ev.type == 5
        # a click and a key are suppressed
        assert cb(None, 1, E(), None) is None
        assert cb(None, 10, E({9: 0, 10: 40}), None) is None
        ev_k = tap._convert(10, E({9: 12, 10: 40}, text="@", flags=0x80020))
        assert ev_k.kind == "key_down" and ev_k.text == "@" and ev_k.keycode == 12
        assert cb(None, 11, E({9: 0, 10: 40}), None) is None
        # middle button and wheel
        assert cb(None, 25, E({3: 2}), None) is None
        assert cb(None, 22, E({88: 0, 11: -1}), None) is None
        assert sink.msgs[-1] == (P.MSG_SCROLL, P.pack_scroll(-120, 0))
        # media key (volume up pressed): data1 = (0 << 16) | (0x0A << 8)
        assert cb(None, 14, E(subtype=8, data1=0x0A00), None) is None
        assert sink.msgs[-1] == (P.MSG_KEY, P.pack_key(True, P.KEYFLAG_VK | P.KEYFLAG_EXTENDED, 0, 0xAF))
        # tap timeout -> re-enable
        cb(None, 0xFFFFFFFE, E(), None)
        assert ('tapenable', True) in Q.calls
        print('OK')
    """)
    assert "OK" in out


def test_mac_app_menu_and_clipboard():
    out = run_script("""
        import stubs
        Q, AK, R, pb = stubs.install_mac()
        from mirrorway.config import Config, MAC_DEFAULTS
        cfg = Config('mac-test', MAC_DEFAULTS)
        from mirrorway.mac import app as A
        a = A.MirrorwayMacApp(cfg)
        assert R.warnings == [], R.warnings
        titles = [i.title for i in a.menu._order if i is not None]
        assert 'Pair a New Device…' in titles, titles
        assert not cfg.get('pair_code')                       # a fresh install has no old long code
        a.server.load_identity()
        a._tick(None)
        assert a.mi_status.title.startswith('○'), a.mi_status.title
        assert a.m_devices.title == 'Paired Devices (0)'
        # pairing mode: the code is shown, with a countdown in the menu
        a.pair_new_device(None)
        assert R.alerts and 'enter this code' in R.alerts[-1][1]
        a._tick(None)
        assert a.mi_code.title.startswith('Pairing code: ') and ('(3:00)' in a.mi_code.title or '(2:5' in a.mi_code.title), a.mi_code.title
        # paired device list and removal
        import os
        dev = a.server.add_device('Office-PC', os.urandom(32), 'windows')
        a.server.last_paired = dev
        n_alerts = len(R.alerts)
        a._tick(None)
        assert len(R.alerts) == n_alerts + 1 and 'is paired' in R.alerts[-1][1]
        items = [i for i in a.m_devices._order if i is not None]
        assert a.m_devices.title == 'Paired Devices (1)' and items[0].title.startswith('Office-PC · ')
        items[0].callback(items[0])
        assert a.server.devices() == []
        a._tick(None)
        assert a.m_devices.title == 'Paired Devices (0)'
        assert R.warnings == [], R.warnings
        # settings menus
        a.side_items['left'].callback(a.side_items['left'])
        assert cfg.get('side') == 'left' and a.side_items['left'].state == 1 and a.side_items['right'].state == 0
        assert '⌃⌥←' in a.mi_switch.title
        a.mi_swap.callback(a.mi_swap)
        assert cfg.get('swap_cmd_ctrl') is False and a.server.router.translator.swap_cmd_ctrl is False
        a.quality_items['high'].callback(None)
        assert cfg.get('quality') == 'high'
        assert len(a.display_items) == 2
        # switch to Windows while not connected -> warning
        a.switch_to_windows(None)
        assert R.alerts
        # clipboard back end
        from mirrorway.mac.clipboard import MacClipboard
        c = MacClipboard(); n = c.change_count(); c.set_text('x'); assert c.get_text() == 'x' and c.change_count() == n + 1
        print('OK')
    """)
    assert "OK" in out


def test_windows_backend_and_winapi():
    out = run_script("""
        import stubs
        u = stubs.install_windows()
        from mirrorway.win import winapi as W
        from mirrorway.win.backend import WinInputBackend
        from mirrorway import protocol as P
        W.set_dpi_aware()
        b = WinInputBackend()
        assert b.get_pos() == (100, 200)
        mons = b.monitors()
        assert mons[0].contains(0, 0) and len(mons) == 2 and mons[1].x == -1280
        b.move_to(0, 0); b.move_to(1919, 1079); b.move_to(960, 540)
        moves = [s for s in u.sent if s[0] == 'mouse']
        f = W.MOUSEEVENTF_MOVE | W.MOUSEEVENTF_ABSOLUTE | W.MOUSEEVENTF_VIRTUALDESK
        assert all(m[4] == f for m in moves)
        # a normalised coordinate converted back gives the same pixel
        for (px, py), m in zip([(0, 0), (1919, 1079), (960, 540)], moves):
            assert m[1] * 1920 // 65536 == px and m[2] * 1080 // 65536 == py, (px, py, m)
        u.sent.clear()
        b.button(P.BUTTON_LEFT, True); b.button(P.BUTTON_X2, False)
        assert u.sent == [('mouse', 0, 0, 0, W.MOUSEEVENTF_LEFTDOWN), ('mouse', 0, 0, W.XBUTTON2, W.MOUSEEVENTF_XUP)]
        u.sent.clear()
        b.wheel(-120, 240)
        assert u.sent == [('mouse', 0, 0, (-120) & 0xFFFFFFFF, W.MOUSEEVENTF_WHEEL), ('mouse', 0, 0, 240, W.MOUSEEVENTF_HWHEEL)]
        u.sent.clear()
        b.key(True, P.KEYFLAG_EXTENDED, 0x4D, 0)
        b.key(False, 0, 0x1E, 0)
        b.key(True, P.KEYFLAG_VK | P.KEYFLAG_EXTENDED, 0, 0xAF)
        assert u.sent == [('key', 0, 0x4D, W.KEYEVENTF_SCANCODE | W.KEYEVENTF_EXTENDEDKEY),
                          ('key', 0, 0x1E, W.KEYEVENTF_SCANCODE | W.KEYEVENTF_KEYUP),
                          ('key', 0xAF, 0x30, W.KEYEVENTF_EXTENDEDKEY)], u.sent
        assert W.clipboard_sequence() == 7
        u.sent.clear()
        b.type_text("€😀")
        units = [e[2] for e in u.sent]
        assert units == [0x20AC, 0x20AC, 0xD83D, 0xD83D, 0xDE00, 0xDE00], units
        assert u.sent[0][3] == W.KEYEVENTF_UNICODE and u.sent[1][3] == W.KEYEVENTF_UNICODE | W.KEYEVENTF_KEYUP
        print('OK')
    """)
    assert "OK" in out


def test_mac_app_mode_login_item(tmp_path):
    env_home = str(tmp_path)
    out = run_script(f"""
        import os, plistlib
        os.environ['HOME'] = {env_home!r}
        os.environ['MIRRORWAY_APP'] = '1'
        os.environ['MIRRORWAY_BUNDLE'] = '/Users/x/Applications/Mirrorway.app'
        import stubs
        Q, AK, R, pb = stubs.install_mac()
        from mirrorway.config import Config, MAC_DEFAULTS
        cfg = Config('mac-app-test', MAC_DEFAULTS)
        from mirrorway.mac import app as A, loginitem
        a = A.MirrorwayMacApp(cfg)
        assert R.warnings == [], R.warnings
        p = loginitem.plist_path()
        assert p.exists() and a.mi_login.state == 1
        d = plistlib.load(open(p, 'rb'))
        assert d['ProgramArguments'] == ['/usr/bin/open', '-g', '/Users/x/Applications/Mirrorway.app'] and d['RunAtLoad']
        a.mi_login.callback(a.mi_login)
        assert not p.exists() and a.mi_login.state == 0
        # if the user turned it off, it is not added again on the next start
        a2 = A.MirrorwayMacApp(cfg)
        assert not p.exists()
        assert "'Mirrorway'" in a2._permission_text()
        print('OK')
    """)
    assert "OK" in out


@pytest.mark.skipif(sys.platform == "win32",
                    reason="the Mac app's port conflict relies on BSD socket behaviour (different on Windows)")
def test_mac_app_restarts_running_instance(tmp_path):
    out = run_script(f"""
        import os, errno, socket
        os.environ['HOME'] = {str(tmp_path)!r}
        import stubs
        Q, AK, R, pb = stubs.install_mac()
        from mirrorway.config import Config, MAC_DEFAULTS
        cfg = Config('mac-restart-test', MAC_DEFAULTS)
        s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close()
        cfg.set('port', port)
        from mirrorway.mac import app as A
        busy = socket.socket(); busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.bind(('0.0.0.0', port)); busy.listen(1)
        killed = []
        A.other_instances = lambda p: [4242]
        A.stop_processes = lambda pids: (killed.extend(pids), busy.close())
        a = A.MirrorwayMacApp(cfg)
        a.server._beacon = None
        a._start_tap = lambda: None
        a.start()
        assert killed == [4242] and 'already running' in R.alerts[-1][1]
        assert a.server._sock is not None
        a.server.stop()
        # if "Cancel" is chosen, it exits
        busy2 = socket.socket(); busy2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy2.bind(('0.0.0.0', port)); busy2.listen(1)
        R.alert = lambda *a, **k: 0
        b = A.MirrorwayMacApp(cfg); b.server._beacon = None; b._start_tap = lambda: None
        try:
            b.start(); raise AssertionError('should have exited')
        except SystemExit as e:
            assert e.code == 0
        print('OK')
    """)
    assert "OK" in out
