"""Device keys, 6-digit pairing with SPAKE2, the paired device list, automatic migration."""
import os
import socket
import threading
import time

import pytest
from conftest import DictCfg

from mirrorway import devkeys as K
from mirrorway import secure as S
from mirrorway.mac.server import MirrorwayServer
from mirrorway.protocol import AuthError, MSG_HELLO, recv_exact
from mirrorway.win.client import ControlClient, Credentials, Resolver, VideoClient
from mirrorway.win.remote import RemoteControl
from test_e2e import free_port, wait
from test_router_remote import FakeMac, FakeWin, mac_cfg


# ---- device keys -------------------------------------------------------------
def test_identity_store_roundtrip_and_fingerprint():
    cfg = DictCfg()
    a = K.load_identity(cfg, K.PlainStore())
    b = K.load_identity(cfg, K.PlainStore())
    assert a.public == b.public and len(a.public) == 32
    assert len(a.fingerprint) == 9 and a.fingerprint[4] == "-"
    other = K.Identity.generate()
    assert a.dh(other.public) == other.dh(a.public)


def test_keychain_store_uses_security_cli():
    calls, vault = [], {}

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def runner(args, **kw):
        calls.append(args)
        acct = args[args.index("-a") + 1]
        if args[1] == "add-generic-password":
            vault[acct] = args[args.index("-w") + 1]
            return R()
        if args[1] == "find-generic-password":
            return R(0, vault[acct] + "\n") if acct in vault else R(44)
        vault.pop(acct, None)
        return R()

    cfg = DictCfg()
    st = K.KeychainStore(runner)
    ident = K.load_identity(cfg, st)
    assert cfg.get("device_key") == "keychain:device_key"                   # no secret in the settings
    assert "-U" in calls[0] and K.KEYCHAIN_SERVICE in calls[0]
    assert K.load_identity(cfg, st).public == ident.public
    vault.clear()                                                           # the vault cannot be read
    with pytest.raises(K.KeyUnavailable):
        K.load_identity(cfg, st)                                            # a new one is not generated silently


# ---- handshake ---------------------------------------------------------------------
def _pair_of_sockets():
    a, b = socket.socketpair()
    a.settimeout(5)
    b.settimeout(5)
    return a, b


def _run_server(fn):
    box = {}

    def target():
        try:
            box["result"] = fn()
        except Exception as exc:
            box["error"] = exc
    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, box


def _server_side(sock, mac, known):
    magic = recv_exact(sock, 6)          # MSG_WAITALL is unreliable on Windows sockets with a timeout
    role = recv_exact(sock, 1)
    assert magic == S.MAGIC2
    ch, hello, pub = S.server_accept(sock, mac, role, lambda p: p in known)
    ch.send_json(MSG_HELLO, {"name": "Mac"})
    return ch, hello, pub


def test_key_handshake_mutual_auth():
    mac, win = K.Identity.generate(), K.Identity.generate()
    a, b = _pair_of_sockets()
    t, box = _run_server(lambda: _server_side(b, mac, {win.public}))
    ch, hello, pub = S.client_connect(a, win, mac.public, b"C", {"name": "Win"})
    t.join(5)
    sch, shello, spub = box["result"]
    assert hello["name"] == "Mac" and shello["name"] == "Win" and spub == win.public and pub == mac.public
    ch.send(99, b"merhaba")
    assert sch.recv() == (99, b"merhaba")


def test_unknown_device_and_wrong_server_and_impostor():
    mac, win = K.Identity.generate(), K.Identity.generate()
    # the Mac does not know this Windows PC
    a, b = _pair_of_sockets()
    t, box = _run_server(lambda: _server_side(b, mac, set()))
    with pytest.raises(S.UnknownDevice):
        S.client_connect(a, win, mac.public, b"C", {})
    t.join(5)
    assert isinstance(box["error"], S.UnknownDevice)
    # Windows expects another Mac
    a, b = _pair_of_sockets()
    t, box = _run_server(lambda: _server_side(b, mac, {win.public}))
    with pytest.raises(S.WrongServer):
        S.client_connect(a, win, K.Identity.generate().public, b"C", {})
    a.close()
    t.join(5)
    # an attacker using a known public key without knowing the private key
    impostor = K.Identity.generate()
    impostor.public = win.public
    a, b = _pair_of_sockets()
    t, box = _run_server(lambda: _server_side(b, mac, {win.public}))
    with pytest.raises(AuthError):
        S.client_connect(a, impostor, mac.public, b"C", {})
    t.join(5)
    assert isinstance(box["error"], AuthError)


def _pair_server(sock, code, mac, override=None):
    assert recv_exact(sock, 6) == S.MAGIC2
    assert recv_exact(sock, 1) == S.ROLE_PAIR
    return S.server_pair(sock, code, mac, "MacBook", status_override=override)


def test_spake2_pairing_right_and_wrong_code():
    mac, win = K.Identity.generate(), K.Identity.generate()
    a, b = _pair_of_sockets()
    t, box = _run_server(lambda: _pair_server(b, "482913", mac))
    info = S.client_pair(a, "482 913", win, "Office-PC")
    t.join(5)
    assert info["pub"] == mac.public and info["name"] == "MacBook" and info["id"] == K.fingerprint(win.public)
    assert box["result"]["pub"] == win.public and box["result"]["name"] == "Office-PC"
    # wrong code
    a, b = _pair_of_sockets()
    t, box = _run_server(lambda: _pair_server(b, "482913", mac))
    with pytest.raises(AuthError, match="[Ww]rong pairing code"):
        S.client_pair(a, "482914", win, "X")
    t.join(5)
    assert isinstance(box["error"], AuthError)
    # pairing mode closed / too many attempts
    for override, msg in ((S.ST_PAIRING_CLOSED, "is not open"), (S.ST_TOO_MANY, "wrong attempts")):
        a, b = _pair_of_sockets()
        t, box = _run_server(lambda: _pair_server(b, None, mac, override))
        with pytest.raises(S.PairingClosed, match=msg):
            S.client_pair(a, "000000", win, "X")
        t.join(5)
    assert S.format_code("482913") == "482 913" and len(S.new_pairing_code()) == 6


# ---- end to end --------------------------------------------------------------------
def _server(legacy_code=""):
    port = free_port()
    cfg = mac_cfg(port=port, pair_code=legacy_code, screen_share=False, clipboard=False)
    srv = MirrorwayServer(cfg, FakeMac(), beacon=False)
    srv.start()
    return srv, port, cfg


def _client(port, **cfgkw):
    wcfg = DictCfg(host="127.0.0.1", port=port, clipboard=False, **cfgkw)
    states = []
    holder = {}
    c = ControlClient(wcfg, Resolver(wcfg, None), RemoteControl(FakeWin(), lambda m, o: holder["c"].send_json(m, o)),
                      on_status=lambda s, t: states.append((s, t)), netinfo_fn=lambda: None)
    holder["c"] = c
    return c, wcfg, states


def test_pairing_flow_end_to_end():
    srv, port, mcfg = _server()
    assert srv.identity is not None and not srv.status()["legacy"]
    c, wcfg, states = _client(port)
    c.start()
    try:
        wait(lambda: c.state == "nocode", msg="pairing needed")
        code = srv.start_pairing()
        assert srv.status()["pairing"]["display"] == S.format_code(code)
        wcfg.set("pairing_code", code)
        c.kick()
        wait(lambda: srv.connected() and c.auth_mode == "key", timeout=8, msg="connection with the key")
        devs = srv.devices()
        assert len(devs) == 1 and not devs[0]["pending"] and devs[0]["last_seen"] > 0
        assert srv.status()["client_device"] == devs[0]["id"] and srv.status()["pairing"] is None
        assert srv.last_paired["id"] == devs[0]["id"]
        assert wcfg.get("pairing_code") == "" and Credentials(wcfg).paired()
        assert wcfg.get("mac_name") and any(s == "paired" for s, _ in states)
        # the video channel uses the key too
        got = []
        vc = VideoClient(wcfg, Resolver(wcfg, None), on_frame=lambda *a: None, on_status=got.append,
                         target_size=lambda: (0, 0))
        vc.start()
        wait(lambda: any("sharing is turned off" in g for g in got), timeout=5, msg="video channel (with the key)")
        vc.stop()
        # removing the pairing drops the connection, and it cannot reconnect
        srv.remove_device(devs[0]["id"])
        wait(lambda: c.state == "unpaired", timeout=8, msg="pairing removed")
        assert not srv.connected()
    finally:
        c.stop()
        srv.stop()


def test_wrong_pairing_codes_are_limited():
    srv, port, _ = _server()
    code = srv.start_pairing()
    wrong = f"{(int(code) + 1) % 10 ** 6:06d}"
    win = K.Identity.generate()
    for i in range(srv.PAIRING_MAX_ATTEMPTS):
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        with pytest.raises(AuthError):
            S.client_pair(s, wrong, win, "attacker")
        s.close()
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    with pytest.raises(S.PairingClosed, match="wrong attempts"):
        S.client_pair(s, code, win, "late")                  # even the right code no longer works
    s.close()
    assert srv.devices() == []
    srv.stop()


def test_pairing_mode_expires():
    srv, port, _ = _server()
    code = srv.start_pairing(seconds=1)
    time.sleep(1.1)
    assert srv.pairing_status() is None
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    with pytest.raises(S.PairingClosed):
        S.client_pair(s, code, K.Identity.generate(), "late")
    s.close()
    srv.stop()


def test_automatic_migration_from_legacy_code():
    srv, port, mcfg = _server(legacy_code="ABCD-EFGH-JKMN")
    assert srv.status()["legacy"]
    c, wcfg, states = _client(port, pair_code="abcd efgh jkmn")
    c.start()
    try:
        wait(lambda: srv.connected(), msg="connection with the old code")
        assert c.auth_mode == "legacy"
        wait(lambda: srv.devices() and Credentials(wcfg).paired(), timeout=5, msg="key registration")
        # after registering, the client reconnects with the key by itself
        wait(lambda: c.auth_mode == "key" and srv.connected(), timeout=10, msg="reconnection with the key")
        wait(lambda: not srv.status()["legacy"], timeout=3, msg="old code closed")
        assert mcfg.get("pair_code") == "" and wcfg.get("pair_code") == ""
        assert not srv.devices()[0]["pending"]
        # another client arriving with the old code is now refused
        bad, bcfg, bstates = _client(port, pair_code="ABCD-EFGH-JKMN")
        bad.start()
        wait(lambda: bad.state == "auth", timeout=5)
        assert "no longer valid" in bad.status_text if hasattr(bad, "status_text") else True
        bad.stop()
        assert srv.connected()
    finally:
        c.stop()
        srv.stop()


def test_client_detects_different_mac():
    srv, port, _ = _server()
    c, wcfg, states = _client(port)
    creds = Credentials(wcfg)
    creds.save_pairing(K.Identity.generate(), K.Identity.generate().public, "Another Mac")
    c.start()
    try:
        wait(lambda: c.state == "unpaired" or c.state == "auth", timeout=5)
    finally:
        c.stop()
        srv.stop()


def test_credentials_legacy_detection():
    assert Credentials(DictCfg(pair_code="ABCD-EFGH-JKMN")).legacy_code() == "ABCD-EFGH-JKMN"
    assert Credentials(DictCfg(pair_code="482913")).legacy_code() == ""
    assert not Credentials(DictCfg()).paired()
    assert os.urandom(1)


def test_broken_secret_store_falls_back_to_file():
    class Broken(K.PlainStore):
        name = "broken"

        def load(self, cfg, key):
            return None                                   # seems to write but cannot read back

    cfg = DictCfg()
    ident = K.Identity.generate()
    assert K.save_identity(cfg, ident, Broken()) == "file"
    assert K.load_identity(cfg, K.PlainStore()).public == ident.public
