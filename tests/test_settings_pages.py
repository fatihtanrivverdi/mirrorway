"""The Settings window's pages (mirrorway/prefs.py)."""
from mirrorway import prefs as P


class Cfg(dict):
    def get(self, k, d=None):
        return super().get(k, d)


def cfg(**kw):
    base = {"side": "right", "edge_switch": True, "speed": 1.25, "scroll_speed": 1.0, "keyboard_layout": "auto",
            "quality": "medium", "video_codec": "h264", "display_index": 0, "screen_share": True,
            "clipboard": True, "language": "auto"}
    base.update(kw)
    return Cfg(base)


def status(**kw):
    st = {"connected": False, "network": {"state": "trusted", "allowed": True, "text": "Home (aa:bb)"},
          "devices": [], "pairing": None, "ip": "192.168.1.20", "port": 24850, "all_ips": ["192.168.1.20"]}
    st.update(kw)
    return st


def pages(c=None, st=None, **ctx):
    return {p.id: p for p in P.mac_pages(c or cfg(), st or status(), ctx)}


def test_page_list_and_shapes():
    ps = P.mac_pages(cfg(), status())
    assert [p.id for p in ps] == ["general", "arrange", "keyboard", "screen", "audio", "clipboard", "network", "devices",
                                  "updates"]
    kinds = {"switch", "segmented", "popup", "button", "text", "keys", "arrange"}
    for p in ps:
        assert p.title and p.symbol and p.color.startswith("#")
        keys = [it.key for it in p.items()]
        assert len(keys) == len(set(keys)), p.id
        for it in p.items():
            assert it.kind in kinds, it
            if it.kind in ("segmented", "popup"):
                assert P.selected(it) is not None, (p.id, it.key, it.value)
                labels = [lab for _v, lab in it.options]
                assert len(labels) == len(set(labels)), it.key        # NSPopUpButton merges equal titles
            if it.kind == "button":
                assert it.button and (it.key.startswith("action:")), it.key


def test_general_page():
    g = pages(cfg(side="left", shortcuts={"screen": "x"}), login={"available": False})["general"]
    assert g.item("side").value == "left" and [v for v, _l in g.item("side").options] == \
        ["left", "right", "top", "bottom"]
    assert g.item("keys:to_win").value == "⌃⌥←" and g.item("keys:to_mac").value == "⌃⌥→"
    sc = g.item("shortcut:screen")
    assert sc.value == "x" and sc.options[0] == ("", "Off") and ("x", "⌃⌥X") in sc.options
    login = g.item("login")
    assert not login.enabled and "Mirrorway.app" in login.detail
    assert g.item("quick_panel").value is True and g.item("menubar_state").value is True
    assert not g.groups[2].note                                     # permissions fine: no warning
    g = pages(permission=False, login={"available": True, "enabled": True})["general"]
    assert "Accessibility" in g.groups[2].note and g.item("login").value and g.item("login").enabled


def test_keyboard_and_screen_pages():
    k = pages(st=status(connected=True, auto_scale=1.333, effective_speed=1.667))["keyboard"]
    assert k.item("speed").value == 1.25 and P.selected(k.item("speed")) == 3
    assert k.item("auto_speed").detail == "1.33× screen ratio (total 1.67×)"
    assert k.item("altgr").value is False
    assert pages(cfg(left_option="altgr"))["keyboard"].item("altgr").value is True
    s = pages(cfg(extend_monitor=[1920, 0, 2560, 1440]),
              displays=[(0, "Display 1 (1440×900) — main"), (1, "Display 2 (1920×1080)")],
              extend_choices=[("Off", None), ("Monitor 1", [0, 0, 1920, 1080]), ("Monitor 2", [1920, 0, 2560, 1440])],
              extend_text="On")["screen"]
    ext = s.item("extend_monitor")
    assert P.selected(ext) == 2 and ext.detail == "On" and ext.options[2][0] == (1920, 0, 2560, 1440)
    assert len(s.item("display_index").options) == 2
    assert P.selected(pages()["screen"].item("extend_monitor")) == 0     # off


def test_network_devices_updates():
    n = pages(st=status(all_ips=["192.168.1.20", "10.0.0.5"]))["network"]
    assert n.item("net_state").tone == "ok" and n.item("net_state").detail == "Home (aa:bb)"
    assert n.item("action:copy_ip").detail == "192.168.1.20:24850  (+10.0.0.5)"
    assert pages(st=status(network={"state": "untrusted"}))["network"].item("net_state").tone == "warn"
    d = pages()["devices"]
    assert d.item("no_devices").kind == "text" and "6-digit" in d.item("action:pair").detail
    st = status(devices=[{"id": "AB12", "name": "Office-PC"}, {"id": "CD34", "name": "Laptop", "pending": True}],
                client_device="AB12", pairing={"display": "123 456", "remaining": 95})
    d = pages(st=st)["devices"]
    rm = d.item("action:remove_device:AB12")
    assert rm.title == "Office-PC" and rm.detail == "AB12 · connected" and rm.tone == "ok"
    assert d.item("action:remove_device:CD34").detail == "CD34 · verifying"
    assert d.item("action:pair").detail == "Code 123 456 · 1:35"
    u = pages(update={"text": "Mirrorway 3.1.1 is ready", "available": "3.1.2", "previous": "3.1.0"},
              version="3.1.1")["updates"]
    assert u.item("version").detail == "Mirrorway 3.1.1 is ready"
    assert u.item("action:install_update").enabled and "3.1.2" in u.item("action:install_update").title
    assert u.item("action:rollback").enabled and "3.1.0" in u.item("action:rollback").title
    u = pages(update={"busy": True}, version="3.1.1")["updates"]
    assert not u.item("action:check_updates").enabled and u.item("action:check_updates").button == "Checking…"
    assert not u.item("action:install_update").enabled and not u.item("action:rollback").enabled
    assert u.item("version").detail == "Mirrorway 3.1.1"


def test_signature_changes_only_with_shape():
    a = pages()["devices"].signature()
    b = pages(st=status(pairing={"display": "123 456", "remaining": 90}))["devices"].signature()
    assert a == b                                               # only texts changed: update in place
    c = pages(st=status(devices=[{"id": "AB12", "name": "PC"}]))["devices"].signature()
    assert c != a                                               # a new row: rebuild
    assert pages(cfg(clipboard=False))["clipboard"].signature() == pages()["clipboard"].signature()


# ---- Windows --------------------------------------------------------------------------------------------------
def win(c=None, **ctx):
    return {p.id: p for p in P.win_pages(c or Cfg(clipboard=True), ctx)}


def test_win_pages():
    ps = P.win_pages(Cfg(), {})
    assert [p.id for p in ps] == ["connection", "screen", "audio", "clipboard", "shortcuts", "general"]
    for p in ps:
        keys = [it.key for it in p.items()]
        assert len(keys) == len(set(keys)), p.id
        for it in p.items():
            if it.kind == "popup":
                assert P.selected(it) is not None, (p.id, it.key)
    c = win(state="connected", mac_name="MacBook-Pro", mac_ip="192.168.1.20", rtt_ms=3.4, auth="key", paired=True,
            fingerprint="7C4E 91A2", startup={"available": True, "enabled": True})["connection"]
    st = c.item("action:reconnect")
    assert st.title == "Connected to MacBook-Pro" and st.tone == "ok"
    assert st.detail == "192.168.1.20 · 3 ms · encrypted with this PC's device key"
    assert c.item("action:unpair").detail == "MacBook-Pro · fingerprint 7C4E 91A2"
    assert c.item("action:pair").title == "Pair with another Mac" and "6-digit" in c.item("action:pair").detail
    assert c.item("startup").value is True and c.item("host").kind == "entry"
    c = win(state="unpaired", status_text="Not paired", found="Looking for the Mac on the network…")["connection"]
    assert c.item("action:reconnect").title == "Not paired" and c.item("action:reconnect").tone == "warn"
    assert "action:unpair" not in [it.key for it in c.items()] and c.item("action:pair").title == "Pair with a Mac"
    a = win(state="connected", audio_supported=True, audio_on=True)["audio"].item("action:audio")
    assert a.enabled and a.button == "Turn off" and a.detail == "The Mac's sound plays on this PC"
    a = win(state="connected", audio_supported=False)["audio"].item("action:audio")
    assert not a.enabled and "Update Mirrorway" in a.detail
    s = win(Cfg(shortcuts={"screen": "k"}))["shortcuts"].item("shortcut:screen")
    assert s.value == "k" and ("k", "Ctrl+Alt+K") in s.options and s.options[0] == ("", "Off")
    g = win(version="3.1.3")["general"]
    assert g.item("version").detail == "Mirrorway 3.1.3" and g.item("quick_panel").value is True
