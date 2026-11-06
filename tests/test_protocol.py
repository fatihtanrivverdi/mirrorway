import socket
import threading

import pytest

from mirrorway import protocol as P
from mirrorway.config import generate_pair_code, normalize_pair_code


def _pair():
    return socket.socketpair()


def _run_server(sock, code, out):
    try:
        ch, hello = P.server_handshake(sock, code)
        ch.send_json(P.MSG_HELLO, {"name": "mac"})
        out["ch"], out["hello"] = ch, hello
    except Exception as exc:
        out["err"] = exc


def test_pair_code_format():
    c = generate_pair_code()
    assert len(c) == 14 and c[4] == "-" and c[9] == "-"
    assert normalize_pair_code(" abcd-efgh jkmn ") == "ABCDEFGHJKMN"


def test_handshake_and_messages():
    a, b = _pair()
    out = {}
    t = threading.Thread(target=_run_server, args=(b, "ABCD-EFGH-JKMN", out))
    t.start()
    ch, server_hello = P.client_handshake(a, "abcd efgh jkmn", P.ROLE_CONTROL, {"name": "pc"})
    t.join(5)
    assert server_hello == {"name": "mac"}
    assert out["hello"] == {"name": "pc"}
    sch = out["ch"]
    assert sch.role == P.ROLE_CONTROL
    sch.send(P.MSG_MOVE, P.pack_move(1.5, -2.25))
    sch.send(P.MSG_KEY, P.pack_key(True, P.KEYFLAG_EXTENDED, 0x4D, 0))
    sch.send(P.MSG_FRAME, P.pack_frame(1600, 1000, 7, b"\xff\xd8jpeg"))
    m, pl = ch.recv()
    assert m == P.MSG_MOVE and P.unpack_move(pl) == (1.5, -2.25)
    m, pl = ch.recv()
    assert m == P.MSG_KEY and P.unpack_key(pl) == (True, P.KEYFLAG_EXTENDED, 0x4D, 0)
    m, pl = ch.recv()
    assert m == P.MSG_FRAME and P.unpack_frame(pl) == (1600, 1000, 7, b"\xff\xd8jpeg")
    ch.send(P.MSG_CLIPBOARD, "Hello world ğüşıöç".encode())
    m, pl = sch.recv()
    assert pl.decode() == "Hello world ğüşıöç"
    ch.close()
    sch.close()


def test_wrong_code_rejected_on_both_sides():
    a, b = _pair()
    out = {}
    t = threading.Thread(target=_run_server, args=(b, "ABCD-EFGH-JKMN", out))
    t.start()
    with pytest.raises(P.AuthError):
        P.client_handshake(a, "ZZZZ-ZZZZ-ZZZZ", P.ROLE_VIDEO, {"name": "pc"})
    t.join(5)
    assert isinstance(out.get("err"), P.AuthError)


def test_tampered_frame_detected():
    a, b = _pair()
    out = {}
    t = threading.Thread(target=_run_server, args=(b, "ABCDEFGHJKMN", out))
    t.start()
    ch, _ = P.client_handshake(a, "ABCDEFGHJKMN", P.ROLE_CONTROL, {})
    t.join(5)
    sch = out["ch"]
    # Send the same frame twice (replay): the second one must be rejected
    ct = sch._send_aead.encrypt(sch._nonce(sch._send_ctr), bytes((P.MSG_PING,)), None)
    raw = P._LEN.pack(len(ct)) + ct
    b.sendall(raw)
    b.sendall(raw)
    assert ch.recv()[0] == P.MSG_PING
    with pytest.raises(P.AuthError):
        ch.recv()


def test_role_binding():
    # The role (C/V) is part of the key derivation: a different role gives a different key
    psk = P.derive_psk("ABCDEFGHJKMN")
    k1 = P._session_keys(psk, b"a" * 16, b"b" * 16, P.ROLE_CONTROL)
    k2 = P._session_keys(psk, b"a" * 16, b"b" * 16, P.ROLE_VIDEO)
    assert k1 != k2
