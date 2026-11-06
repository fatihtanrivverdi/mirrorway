"""Mirrorway network protocol.

Connection flow (for every TCP connection):
  client -> server: MAGIC | role (b"C" control / b"V" video) | 16-byte client nonce
  server -> client: MAGIC | 16-byte server nonce
  Both sides: psk = scrypt(pairing code)
              keys = HKDF-SHA256(psk, salt = nonce_c + nonce_s, info = "mirrorway-v1|" + role)
  Then: frames of [4-byte length][AES-256-GCM encrypted (1-byte type + data)].
  The client's first encrypted message is HELLO; if the server cannot decrypt it (wrong code) it sends
  4 zero bytes and closes the connection.

Nonces are a separate key per direction plus an increasing counter; replayed or reordered
frames fail authentication.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import threading
from functools import lru_cache

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .config import normalize_pair_code

MAGIC = b"MRWAY1"
ROLE_CONTROL = b"C"
ROLE_VIDEO = b"V"
ROLE_AUDIO = b"A"          # audio (Mac -> Windows; mirrorway.audio)
ROLE_FILE = b"F"           # file copy and paste (device keys only; mirrorway.filexfer)
NONCE_LEN = 16
MAX_FRAME = 32 * 1024 * 1024
PROTOCOL_VERSION = 1

# ---- Message types -------------------------------------------------------
MSG_HELLO = 1          # JSON, both directions
MSG_PING = 2           # empty
MSG_ECHO = 3           # s->c <dd server time (s), last round trip (ms) — latency measurement
MSG_ECHO_REPLY = 4     # c->s the same payload as MSG_ECHO
MSG_STATS = 5          # c->s JSON (viewer statistics, on the video channel)
MSG_LOG = 6            # c->s JSON {level, msg} — Windows warnings/errors into the Mac log
MSG_ENTER = 10         # s->c JSON {"from": "left"|"right"|"top"|"bottom"|"center", "ratio": float,
                      #          "exit": edge, "pos": [x, y]?, "extend": [x, y, w, h]? (second-screen monitor)}
MSG_LEAVE = 11         # c->s JSON {"ratio": float, "vd": [fx, fy]? (return to this point of the second screen)}
MSG_EXIT = 12          # s->c  (control returned to the Mac)
MSG_VIEW = 13          # s->c JSON {"open": true} — open the screen window on Windows (when the second screen turns on)
MSG_MOVE = 20          # s->c <ff  dx, dy  (Windows pikseli)
MSG_BUTTON = 21        # s->c <BB  button, pressed(1)/released(0)
MSG_SCROLL = 22        # s->c <ii  vertical, horizontal (WHEEL_DELTA units, 120 = one notch)
MSG_KEY = 23           # s->c <BBHH pressed, flags, scan code, vk
MSG_RELEASE_ALL = 24   # s->c  release everything that is still pressed
MSG_TEXT = 25          # s->c UTF-8 text (characters like @ € typed with Option, entered as Unicode)
MSG_GESTURE = 26       # s->c JSON {"action": "desktop_left"|"desktop_right"|"task_view"|"show_desktop"|"zoom", "steps"?}
MSG_CLIPBOARD = 30     # both directions, UTF-8 text (old; when the other side does not support clip2)
MSG_CLIPDATA = 31      # both directions, rich clipboard chunk (mirrorway.richclip: <IIB id, sequence, flags + data)
MSG_FRAME = 40         # s->c (video channel) <HHI width, height, sequence + JPEG
MSG_VIDEO_INFO = 41    # s->c JSON
MSG_FRAME2 = 42        # s->c <HHIHH width, height, sequence, encode ms, flags + JPEG
MSG_FRAME_DELTA = 43   # s->c only the changed rectangles (format: mirrorway.delta.pack_delta)
MSG_KEYFRAME_REQ = 44  # c->s ask for a full frame (canvas lost / size changed / H.264 could not be decoded)
MSG_FRAME_H264 = 45    # s->c MSG_FRAME2 header (flag 1 = key frame) + H.264 Annex B
MSG_UPDATE = 60        # s->c >I json length + JSON {version, sha256, size} + zip
MSG_UPDATE_RESULT = 61 # c->s JSON {version, ok, error}

BUTTON_LEFT, BUTTON_RIGHT, BUTTON_MIDDLE, BUTTON_X1, BUTTON_X2 = range(5)

KEYFLAG_EXTENDED = 0x01
KEYFLAG_VK = 0x02      # virtual key code instead of a scan code (media keys etc.)

_MOVE = struct.Struct("<ff")
_BUTTON = struct.Struct("<BB")
_SCROLL = struct.Struct("<ii")
_KEY = struct.Struct("<BBHH")
_FRAME_HDR = struct.Struct("<HHI")
_FRAME2_HDR = struct.Struct("<HHIHH")
_ECHO = struct.Struct("<dd")
_U32 = struct.Struct(">I")
_LEN = struct.Struct(">I")

_PSK_SALT = b"mirrorway-v1-psk-salt"


class ProtocolError(Exception):
    pass


class AuthError(ProtocolError):
    """The pairing code does not match."""


# ---- Helpers ---------------------------------------------------------
@lru_cache(maxsize=8)
def derive_psk(pair_code: str) -> bytes:
    code = normalize_pair_code(pair_code)
    if len(code) < 6:
        raise ValueError("Pairing code too short")
    kdf = Scrypt(salt=_PSK_SALT, length=32, n=2 ** 14, r=8, p=1)
    return kdf.derive(code.encode("ascii"))


def _session_keys(psk: bytes, nonce_c: bytes, nonce_s: bytes, role: bytes):
    okm = HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=nonce_c + nonce_s,
        info=b"mirrorway-v1|" + role,
    ).derive(psk)
    return okm[:32], okm[32:]   # (client->server, server->client)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed")
        buf.extend(chunk)
    return bytes(buf)


def tune_socket(sock: socket.socket) -> None:
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass


# ---- Encrypted channel -------------------------------------------------------
class SecureChannel:
    def __init__(self, sock: socket.socket, send_key: bytes, recv_key: bytes, role: bytes):
        self.sock = sock
        self.role = role
        self._send_aead = AESGCM(send_key)
        self._recv_aead = AESGCM(recv_key)
        self._send_ctr = 0
        self._recv_ctr = 0
        self._send_lock = threading.Lock()
        self._recv_lock = threading.Lock()
        self.closed = False

    @staticmethod
    def _nonce(counter: int) -> bytes:
        return b"\x00\x00\x00\x00" + counter.to_bytes(8, "big")

    def send(self, mtype: int, payload: bytes = b"") -> None:
        with self._send_lock:
            if self.closed:
                raise ConnectionError("Channel closed")
            ct = self._send_aead.encrypt(self._nonce(self._send_ctr), bytes((mtype,)) + payload, None)
            self._send_ctr += 1
            self.sock.sendall(_LEN.pack(len(ct)) + ct)

    def send_json(self, mtype: int, obj) -> None:
        self.send(mtype, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def recv(self):
        with self._recv_lock:
            (length,) = _LEN.unpack(recv_exact(self.sock, 4))
            if length == 0:
                raise AuthError("The other side rejected authentication")
            if length > MAX_FRAME:
                raise ProtocolError("Frame too large")
            ct = recv_exact(self.sock, length)
            try:
                pt = self._recv_aead.decrypt(self._nonce(self._recv_ctr), ct, None)
            except InvalidTag:
                raise AuthError("Could not decrypt (wrong pairing code?)")
            self._recv_ctr += 1
            return pt[0], pt[1:]

    def close(self) -> None:
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def client_handshake(sock: socket.socket, pair_code: str, role: bytes, hello: dict) -> "tuple[SecureChannel, dict]":
    """Client-side handshake. Returns (channel, server_hello)."""
    psk = derive_psk(pair_code)
    nonce_c = os.urandom(NONCE_LEN)
    sock.sendall(MAGIC + role + nonce_c)
    head = recv_exact(sock, len(MAGIC) + NONCE_LEN)
    if head[: len(MAGIC)] != MAGIC:
        raise ProtocolError("This is not a Mirrorway server")
    nonce_s = head[len(MAGIC):]
    c2s, s2c = _session_keys(psk, nonce_c, nonce_s, role)
    ch = SecureChannel(sock, send_key=c2s, recv_key=s2c, role=role)
    ch.send_json(MSG_HELLO, hello)
    mtype, payload = ch.recv()
    if mtype != MSG_HELLO:
        raise ProtocolError("Unexpected first message")
    return ch, json.loads(payload.decode("utf-8"))


def server_handshake(sock: socket.socket, pair_code, magic: bytes = None) -> "tuple[SecureChannel, dict]":
    """Server-side handshake (v1, with the pairing code). Returns (channel, client_hello); the caller sends
    the HELLO reply. magic: the first 6 bytes, already read. If pair_code is empty (old method disabled),
    every client is rejected."""
    if magic is None:
        head = recv_exact(sock, len(MAGIC) + 1 + NONCE_LEN)
    else:
        head = magic + recv_exact(sock, 1 + NONCE_LEN)
    if head[: len(MAGIC)] != MAGIC:
        raise ProtocolError("Unknown client")
    role = head[len(MAGIC): len(MAGIC) + 1]
    if role not in (ROLE_CONTROL, ROLE_VIDEO):
        raise ProtocolError("Invalid role")
    nonce_c = head[len(MAGIC) + 1:]
    nonce_s = os.urandom(NONCE_LEN)
    sock.sendall(MAGIC + nonce_s)
    psk = derive_psk(pair_code) if pair_code else os.urandom(32)
    c2s, s2c = _session_keys(psk, nonce_c, nonce_s, role)
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
    return ch, json.loads(payload.decode("utf-8"))


# ---- Payload packing -------------------------------------------------------
def pack_move(dx: float, dy: float) -> bytes:
    return _MOVE.pack(dx, dy)


def unpack_move(b: bytes):
    return _MOVE.unpack(b)


def pack_button(button: int, down: bool) -> bytes:
    return _BUTTON.pack(button, 1 if down else 0)


def unpack_button(b: bytes):
    btn, down = _BUTTON.unpack(b)
    return btn, bool(down)


def pack_scroll(v: int, h: int) -> bytes:
    return _SCROLL.pack(int(v), int(h))


def unpack_scroll(b: bytes):
    return _SCROLL.unpack(b)


def pack_key(down: bool, flags: int, scan: int, vk: int) -> bytes:
    return _KEY.pack(1 if down else 0, flags, scan, vk)


def unpack_key(b: bytes):
    down, flags, scan, vk = _KEY.unpack(b)
    return bool(down), flags, scan, vk


def pack_frame(width: int, height: int, seq: int, jpeg: bytes) -> bytes:
    return _FRAME_HDR.pack(width, height, seq & 0xFFFFFFFF) + jpeg


def unpack_frame(b: bytes):
    w, h, seq = _FRAME_HDR.unpack_from(b)
    return w, h, seq, b[_FRAME_HDR.size:]


def pack_frame2(width: int, height: int, seq: int, enc_ms: float, jpeg: bytes, flags: int = 0) -> bytes:
    return _FRAME2_HDR.pack(width, height, seq & 0xFFFFFFFF, min(int(enc_ms), 65535), flags) + jpeg


def unpack_frame2(b: bytes):
    w, h, seq, enc_ms, flags = _FRAME2_HDR.unpack_from(b)
    return w, h, seq, enc_ms, flags, b[_FRAME2_HDR.size:]


def pack_echo(t: float, last_rtt_ms: float) -> bytes:
    return _ECHO.pack(t, last_rtt_ms)


def unpack_echo(b: bytes):
    return _ECHO.unpack(b)


def pack_update(meta: dict, data: bytes) -> bytes:
    j = json.dumps(meta).encode("utf-8")
    return _U32.pack(len(j)) + j + data


def unpack_update(b: bytes):
    (n,) = _U32.unpack_from(b)
    meta = json.loads(b[4:4 + n].decode("utf-8"))
    return meta, b[4 + n:]


def parse_version(v) -> tuple:
    out = []
    for part in str(v or "0").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])
