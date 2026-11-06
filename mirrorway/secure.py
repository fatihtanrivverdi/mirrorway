"""Mirrorway protocol v2: mutual authentication with device keys, and pairing with SPAKE2.

Connection (paired devices):
  client -> server: MAGIC2 | role (C/V) | client static public key (32) | client ephemeral public key (32)
  server -> client: MAGIC2 | status (1) | server static public key (32) | server ephemeral public key (32)
  Keys: HKDF(DH(eC, eS) || DH(eC, sS) || DH(sC, eS), salt = hash(all public keys)).
  - DH(eC, sS): only the real Mac can compute it; DH(sC, eS): only the paired Windows.
  - Ephemeral keys are new for every connection: even if a key is stolen later, recorded traffic stays sealed.
  Then the same AES-256-GCM frames as v1 (protocol.SecureChannel); the first message is HELLO.

Pairing (once, with the 6-digit code shown in the Mac menu):
  client -> server: MAGIC2 | "P" | 2-byte length | SPAKE2 A message
  server -> client: MAGIC2 | status | 2-byte length | SPAKE2 B message
  Over a channel encrypted with the SPAKE2 key, both sides exchange their names and static public keys.
  A wrong code means nothing decrypts; the code cannot be guessed from recorded traffic (no offline guessing).
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
from typing import Callable, Optional, Tuple

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .devkeys import Identity, b64d, b64e, fingerprint
from .i18n import T
from .protocol import (MSG_HELLO, AuthError, ProtocolError, SecureChannel, recv_exact)

MAGIC2 = b"MRWAY2"
ROLE_PAIR = b"P"

ST_OK = 0
ST_UNKNOWN_DEVICE = 1       # this device is not registered on the Mac
ST_PAIRING_CLOSED = 2       # pairing mode is not open on the Mac
ST_TOO_MANY = 3             # too many wrong attempts
ST_LEGACY_DISABLED = 4      # (info) connecting with the old code is disabled

MSG_PAIR = 7                # pairing channel: JSON {name, pub, platform}
MSG_ENROLL = 8              # device key registration over a channel set up with the old code (migration)

_SPAKE_ID_A = b"mirrorway-windows"
_SPAKE_ID_B = b"mirrorway-mac"
_U16 = struct.Struct(">H")


class UnknownDevice(AuthError):
    pass


class PairingClosed(AuthError):
    pass


class WrongServer(AuthError):
    pass


def normalize_code(code: str) -> str:
    return "".join(ch for ch in str(code) if ch.isdigit())


def _pub(priv: X25519PrivateKey) -> bytes:
    return priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _dh(priv: X25519PrivateKey, pub: bytes) -> bytes:
    return priv.exchange(X25519PublicKey.from_public_bytes(pub))


def _keys(ikm: bytes, transcript: bytes, info: bytes):
    okm = HKDF(algorithm=hashes.SHA256(), length=64, salt=hashlib.sha256(transcript).digest(),
               info=info).derive(ikm)
    return okm[:32], okm[32:]          # (client->server, server->client)


# ---------------------------------------------------------------------------
# Connections between paired devices
# ---------------------------------------------------------------------------
def client_connect(sock, ident: Identity, server_pub: Optional[bytes], role: bytes,
                   hello: dict) -> Tuple[SecureChannel, dict, bytes]:
    """Client: (channel, server HELLO, server static public key)."""
    eph = X25519PrivateKey.generate()
    e_c = _pub(eph)
    sock.sendall(MAGIC2 + role + ident.public + e_c)
    head = recv_exact(sock, len(MAGIC2) + 1)
    if head[:len(MAGIC2)] != MAGIC2:
        raise ProtocolError(T("This Mirrorway server does not support the new connection format"))
    status = head[-1]
    if status == ST_UNKNOWN_DEVICE:
        raise UnknownDevice(T("This PC is not among the Mac's paired devices — pair again"))
    if status != ST_OK:
        raise AuthError(T("The Mac refused the connection (status {status})").format(status=status))
    rest = recv_exact(sock, 64)
    s_s, e_s = rest[:32], rest[32:]
    if server_pub is not None and s_s != server_pub:
        raise WrongServer(T("The Mac's identity is not the expected one ({id}); pair again").format(id=fingerprint(s_s)))
    ikm = _dh(eph, e_s) + _dh(eph, s_s) + ident.dh(e_s)
    c2s, s2c = _keys(ikm, MAGIC2 + role + ident.public + e_c + s_s + e_s, b"mirrorway-v2|" + role)
    ch = SecureChannel(sock, send_key=c2s, recv_key=s2c, role=role)
    ch.send_json(MSG_HELLO, hello)
    mtype, payload = ch.recv()
    if mtype != MSG_HELLO:
        raise ProtocolError("Unexpected first message")
    return ch, json.loads(payload.decode("utf-8")), s_s


def server_accept(sock, ident: Identity, role: bytes, is_known: Callable[[bytes], bool]):
    """Server (after MAGIC2 and the role have been read): (channel, client HELLO, client public key)."""
    rest = recv_exact(sock, 64)
    s_c, e_c = rest[:32], rest[32:]
    if not is_known(s_c):
        try:
            sock.sendall(MAGIC2 + bytes((ST_UNKNOWN_DEVICE,)))
        except OSError:
            pass
        raise UnknownDevice(f"unpaired device ({fingerprint(s_c)})")
    eph = X25519PrivateKey.generate()
    e_s = _pub(eph)
    sock.sendall(MAGIC2 + bytes((ST_OK,)) + ident.public + e_s)
    ikm = _dh(eph, e_c) + ident.dh(e_c) + _dh(eph, s_c)
    c2s, s2c = _keys(ikm, MAGIC2 + role + s_c + e_c + ident.public + e_s, b"mirrorway-v2|" + role)
    ch = SecureChannel(sock, send_key=s2c, recv_key=c2s, role=role)
    try:
        mtype, payload = ch.recv()
    except AuthError:
        try:
            sock.sendall(b"\x00\x00\x00\x00")
        except OSError:
            pass
        raise
    if mtype != MSG_HELLO:
        raise ProtocolError("First message is not HELLO")
    return ch, json.loads(payload.decode("utf-8")), s_c


# ---------------------------------------------------------------------------
# Pairing with SPAKE2
# ---------------------------------------------------------------------------
def _spake(side: str, code: str):
    from spake2 import SPAKE2_A, SPAKE2_B
    pw = normalize_code(code).encode("ascii")
    if len(pw) < 6:
        raise ValueError(T("The pairing code must have 6 digits"))
    cls = SPAKE2_A if side == "A" else SPAKE2_B
    return cls(pw, idA=_SPAKE_ID_A, idB=_SPAKE_ID_B)


def client_pair(sock, code: str, ident: Identity, name: str) -> dict:
    """Windows: derive a shared key from the code and exchange identities. Returns {name, pub(bytes), id}."""
    sp = _spake("A", code)
    msg_a = sp.start()
    sock.sendall(MAGIC2 + ROLE_PAIR + _U16.pack(len(msg_a)) + msg_a)
    head = recv_exact(sock, len(MAGIC2) + 1)
    if head[:len(MAGIC2)] != MAGIC2:
        raise ProtocolError(T("This Mirrorway server does not support pairing"))
    status = head[-1]
    if status == ST_PAIRING_CLOSED:
        raise PairingClosed(T("Pairing is not open on the Mac: click Mirrorway › Pair a New Device…"))
    if status == ST_TOO_MANY:
        raise PairingClosed(T("Too many wrong attempts; get a new code on the Mac"))
    if status != ST_OK:
        raise AuthError(T("The Mac refused pairing (status {status})").format(status=status))
    (n,) = _U16.unpack(recv_exact(sock, 2))
    msg_b = recv_exact(sock, n)
    try:
        key = sp.finish(msg_b)
    except Exception as exc:
        raise ProtocolError(f"Corrupt pairing message: {exc}")
    c2s, s2c = _keys(key, b"mirrorway-v2-pair" + msg_a + msg_b, b"mirrorway-v2-pair")
    ch = SecureChannel(sock, send_key=c2s, recv_key=s2c, role=ROLE_PAIR)
    ch.send_json(MSG_PAIR, {"name": name, "pub": b64e(ident.public), "platform": "windows"})
    try:
        mtype, payload = ch.recv()
    except AuthError:
        raise AuthError(T("Wrong pairing code"))
    if mtype != MSG_PAIR:
        raise ProtocolError("Unexpected pairing reply")
    info = json.loads(payload.decode("utf-8"))
    pub = b64d(info["pub"])
    if len(pub) != 32:
        raise ProtocolError("Invalid Mac key")
    return {"name": str(info.get("name") or "Mac"), "pub": pub, "id": str(info.get("id") or "")}


def server_pair(sock, code: Optional[str], ident: Identity, name: str,
                status_override: Optional[int] = None,
                on_success: Optional[Callable[[dict], None]] = None) -> dict:
    """Mac (after MAGIC2 and "P" have been read). Returns {name, pub(bytes), platform}.
    A wrong code raises AuthError; the caller counts attempts. If on_success is given, the device is saved BEFORE
    the final confirmation goes to Windows, so it is recognised as soon as Windows connects with its key."""
    (n,) = _U16.unpack(recv_exact(sock, 2))
    if n > 1024:
        raise ProtocolError("Pairing message too large")
    msg_a = recv_exact(sock, n)
    if status_override is not None or not code:
        try:
            sock.sendall(MAGIC2 + bytes((status_override if status_override is not None else ST_PAIRING_CLOSED,)))
        except OSError:
            pass
        raise PairingClosed("pairing mode is closed")
    sp = _spake("B", code)
    msg_b = sp.start()
    sock.sendall(MAGIC2 + bytes((ST_OK,)) + _U16.pack(len(msg_b)) + msg_b)
    try:
        key = sp.finish(msg_a)
    except Exception as exc:
        raise ProtocolError(f"Corrupt pairing message: {exc}")
    c2s, s2c = _keys(key, b"mirrorway-v2-pair" + msg_a + msg_b, b"mirrorway-v2-pair")
    ch = SecureChannel(sock, send_key=s2c, recv_key=c2s, role=ROLE_PAIR)
    try:
        mtype, payload = ch.recv()
    except AuthError:
        try:
            sock.sendall(b"\x00\x00\x00\x00")
        except OSError:
            pass
        raise AuthError("wrong pairing code")
    if mtype != MSG_PAIR:
        raise ProtocolError("Unexpected pairing message")
    info = json.loads(payload.decode("utf-8"))
    pub = b64d(info["pub"])
    if len(pub) != 32:
        raise ProtocolError("Invalid device key")
    result = {"name": str(info.get("name") or "Windows")[:64], "pub": pub,
              "platform": str(info.get("platform") or "")}
    if on_success is not None:
        on_success(result)
    ch.send_json(MSG_PAIR, {"name": name, "pub": b64e(ident.public), "id": fingerprint(pub)})
    return result


def new_pairing_code() -> str:
    import secrets
    return f"{secrets.randbelow(10 ** 6):06d}"


def format_code(code: str) -> str:
    c = normalize_code(code)
    return f"{c[:3]} {c[3:]}" if len(c) == 6 else c


def random_bytes(n: int) -> bytes:
    return os.urandom(n)
