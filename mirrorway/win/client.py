"""Windows-side connections: control (input + clipboard) and video (the Mac's screen)."""
from __future__ import annotations

import collections
import io
import json
import logging
import importlib.util
import math
import platform
import socket
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

from .. import __version__
from .. import audio as AU
from .. import filexfer as FX
from .. import richclip as RC
from ..extend import FEATURE as EXTEND_FEATURE
from ..gestures import FEATURE as GESTURES_FEATURE
from ..i18n import T
from ..protocol import (MSG_BUTTON, MSG_CLIPBOARD, MSG_CLIPDATA, MSG_ECHO, MSG_ECHO_REPLY, MSG_ENTER, MSG_EXIT, MSG_GESTURE,
                        MSG_FRAME, MSG_FRAME2, MSG_HELLO, MSG_KEY, MSG_MOVE, MSG_PING, MSG_RELEASE_ALL,
                        MSG_FRAME_DELTA, MSG_FRAME_H264, MSG_KEYFRAME_REQ, MSG_LOG, MSG_SCROLL, MSG_STATS, MSG_TEXT,
                        MSG_UPDATE,
                        MSG_UPDATE_RESULT, MSG_VIDEO_INFO, MSG_VIEW,
                        unpack_echo, unpack_frame2, unpack_update,
                        PROTOCOL_VERSION, ROLE_CONTROL, ROLE_VIDEO, AuthError, client_handshake,
                        tune_socket, unpack_button, unpack_frame, unpack_key, unpack_move,
                        unpack_scroll)

log = logging.getLogger("mirrorway.client")
_MSG_ENROLL = 8          # secure.MSG_ENROLL (not imported, to avoid a cycle)


class Resolver:
    """The Mac address to connect to: the one in the settings, else the discovered one."""

    def __init__(self, cfg, discovery):
        self.cfg = cfg
        self.discovery = discovery

    def resolve(self) -> Optional[Tuple[str, int, str]]:
        host = (self.cfg.get("host") or "").strip()
        port = int(self.cfg.get("port"))
        if host:
            if ":" in host:
                host, p = host.rsplit(":", 1)
                try:
                    port = int(p)
                except ValueError:
                    pass
            return host, port, host
        best = self.discovery.best() if self.discovery else None
        if best:
            ip, name, port = best
            return ip, port, name
        fallback = (self.cfg.get("fallback_host") or "").strip()
        if fallback:
            return fallback, port, fallback
        return None


class NotPaired(Exception):
    pass


class Credentials:
    """The Windows device identity and the Mac it is paired with (its public key). The secret key is kept with DPAPI."""

    def __init__(self, cfg, store=None):
        self.cfg = cfg
        self.store = store
        self._ident = None

    def identity(self, create: bool = False):
        if self._ident is None:
            from ..devkeys import load_identity
            self._ident = load_identity(self.cfg, self.store, create=create)
        return self._ident

    def mac_pub(self) -> Optional[bytes]:
        v = self.cfg.get("mac_pub") or ""
        if not v:
            return None
        from ..devkeys import b64d
        try:
            return b64d(v)
        except Exception:
            return None

    def mac_name(self) -> str:
        return str(self.cfg.get("mac_name") or "Mac")

    def paired(self) -> bool:
        if not self.mac_pub():
            return False
        try:
            return self.identity(create=False) is not None
        except Exception:
            return False

    def save_pairing(self, ident, mac_pub: bytes, mac_name: str) -> None:
        from ..devkeys import b64e, save_identity
        if (self.cfg.get("device_key") or "") == "" or self._ident is not ident:
            save_identity(self.cfg, ident, self.store)
        self._ident = ident
        self.cfg.set("mac_pub", b64e(mac_pub), save=False)
        self.cfg.set("mac_name", mac_name)

    def forget(self) -> None:
        self.cfg.set("mac_pub", "", save=False)
        self.cfg.set("mac_name", "")

    def legacy_code(self) -> str:
        code = (self.cfg.get("pair_code") or "").strip()
        return code if any(ch.isalpha() for ch in code) else ""


def open_channel(creds: Credentials, sock, role: bytes, hello: dict):
    """Connect with the device key if paired, else (migration) with the old long code. (channel, server hello, method)."""
    if creds.paired():
        from ..secure import client_connect
        ch, server_hello, _ = client_connect(sock, creds.identity(), creds.mac_pub(), role, hello)
        return ch, server_hello, "key"
    code = creds.legacy_code()
    if code:
        ch, server_hello = client_handshake(sock, code, role, hello)
        return ch, server_hello, "legacy"
    raise NotPaired("Pairing required")


_FORWARD_LOGGERS = ("mirrorway.client", "mirrorway.remote", "mirrorway.updater", "mirrorway.winapi", "mirrorway.winapp",
                    "mirrorway.viewer", "mirrorway.netinfo", "mirrorway.devkeys")


_FORWARD_INFO_LOGGERS = ("mirrorway.clipboard", "mirrorway.extend", "mirrorway.files", "mirrorway.audio")     # their info lines are forwarded too (clipboard transfers)


class _RemoteLogHandler(logging.Handler):
    """Queues warnings/errors on Windows (and clipboard transfers) to forward to the Mac (they land in the Mac log)."""

    def __init__(self, sink: "collections.deque"):
        super().__init__(level=logging.INFO)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_FORWARD_INFO_LOGGERS):
            pass
        elif not (record.name.startswith(_FORWARD_LOGGERS) and record.levelno >= logging.WARNING):
            return
        try:
            msg = record.getMessage()
            if record.exc_info:
                import traceback
                msg += " | " + "".join(traceback.format_exception(*record.exc_info))[-600:]
            self.sink.append({"level": record.levelname, "msg": f"[{record.name}] {msg}"[:1000]})
        except Exception:
            pass


def system_summary(monitors=None) -> str:
    has_np = importlib.util.find_spec("numpy") is not None
    has_sj = importlib.util.find_spec("simplejpeg") is not None
    mons = ", ".join(f"{int(r.w)}x{int(r.h)}@{int(r.x)},{int(r.y)}" for r in (monitors or []))
    return (f"Mirrorway {__version__}, {platform.platform()}, Python {platform.python_version()}, "
            f"monitors [{mons}], numpy={'yes' if has_np else 'no'}, simplejpeg={'yes' if has_sj else 'no'}")


class ControlClient(threading.Thread):
    def __init__(self, cfg, resolver: Resolver, remote, clipsync_factory: Optional[Callable] = None,
                 on_status: Optional[Callable[[str, str], None]] = None, monitors_fn=None,
                 on_update: Optional[Callable[[str], None]] = None, install_root: Optional[Path] = None,
                 apply_update_fn: Optional[Callable] = None, netinfo_fn: Optional[Callable] = None,
                 credentials: Optional[Credentials] = None, on_view: Optional[Callable[[dict], None]] = None,
                 files_dir=None, on_notify: Optional[Callable[[str, str], None]] = None,
                 audio_player_factory: Optional[Callable] = None):
        super().__init__(daemon=True, name="mirrorway-control")
        self.on_view = on_view                 # the Mac asked for its screen window to open (second screen)
        self.file_target: Optional[Tuple[str, int]] = None     # the connected Mac's address, for the file channel
        self.files = None
        if files_dir is not None:
            from .files import FileClient
            self.files = FileClient(self, files_dir, notify=on_notify)
        self.audio = None
        if audio_player_factory is not None:
            from .audio import AudioClient
            self.audio = AudioClient(self, audio_player_factory)
        self.cfg = cfg
        self.creds = credentials or Credentials(cfg)
        self.auth_mode = ""                 # "key" (device key) / "legacy" (old long code)
        self.resolver = resolver
        self.remote = remote
        self.on_status = on_status or (lambda state, text: None)
        self.monitors_fn = monitors_fn or (lambda: [])
        self.ch = None
        self.server_name = ""
        self.server_version = ""
        self.last_rtt_ms: Optional[float] = None
        self.on_update = on_update
        self.install_root = Path(install_root) if install_root else Path(__file__).resolve().parent.parent.parent
        self._apply_update = apply_update_fn
        if netinfo_fn is None:
            from .netinfo import collect as netinfo_fn
        self.netinfo_fn = netinfo_fn
        self._updating = False
        self._log_queue: "collections.deque" = collections.deque(maxlen=50)
        self._log_handler: Optional[logging.Handler] = None
        self.state = "idle"
        self._stop_evt = threading.Event()
        self._wake = threading.Event()
        self._server_features: list = []
        self._clip_rx = RC.Assembler()
        self._clip_sender = RC.ClipSender(
            send_raw=lambda part: self._send_raw(MSG_CLIPDATA, part),
            rich_fn=lambda: RC.FEATURE in self._server_features,
            send_text=self._send_clip_text, peer="Mac")
        self.clip = clipsync_factory(self._send_clip) if clipsync_factory else None

    # ---- from outside ----------------------------------------------------------
    def enable_log_forwarding(self) -> None:
        if self._log_handler is None:
            self._log_handler = _RemoteLogHandler(self._log_queue)
            logging.getLogger("mirrorway").addHandler(self._log_handler)

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake.set()
        if self.audio is not None:
            self.audio.stop()
        self.kick()

    def kick(self) -> None:
        """The settings changed: close the current connection and retry at once."""
        self._wake.set()
        ch = self.ch
        if ch is not None:
            ch.close()

    def server_features(self) -> list:
        return list(self._server_features) if self.ch is not None else []

    def send_json(self, mtype: int, obj) -> None:
        ch = self.ch
        if ch is not None:
            ch.send_json(mtype, obj)

    def _send_clip(self, formats) -> None:
        if isinstance(formats, str):
            formats = {RC.TEXT: formats.encode("utf-8")}
        self._clip_sender.send(formats)

    def _send_clip_text(self, text: str) -> None:
        ch = self.ch
        if ch is not None:
            ch.send(MSG_CLIPBOARD, text.encode("utf-8"))

    def _send_raw(self, mtype: int, payload: bytes) -> None:
        ch = self.ch
        if ch is None:
            raise ConnectionError("not connected to the Mac")
        ch.send(mtype, payload)

    # ---- internals -----------------------------------------------------------------
    def _set(self, state: str, text: str) -> None:
        self.state = state
        try:
            self.on_status(state, text)
        except Exception:
            pass

    def _wait(self, sec: float) -> None:
        self._wake.wait(sec)
        self._wake.clear()

    def run(self) -> None:
        if self.clip is not None:
            self.clip.start()
        while not self._stop_evt.is_set():
            if (self.cfg.get("pairing_code") or "").strip():
                self._do_pairing()
                continue
            if not self.creds.paired() and not self.creds.legacy_code():
                self._set("nocode", T("Pairing required: on the Mac click Mirrorway › Pair a New Device… and enter the code"))
                self._wait(2)
                continue
            target = self.resolver.resolve()
            if target is None:
                self._set("searching", T("Looking for the Mac…"))
                self._wait(1)
                continue
            host, port, label = target
            self._set("connecting", T("Connecting to {address}…").format(address=label))
            sock = None
            try:
                sock = socket.create_connection((host, port), timeout=5)
                tune_socket(sock)
                hello = {"name": socket.gethostname(), "version": __version__, "protocol": PROTOCOL_VERSION,
                         "monitors": [[r.x, r.y, r.w, r.h] for r in self.monitors_fn()],
                         "auto_update": True,
                         "features": ["echo", "text", "devkeys", RC.FEATURE, EXTEND_FEATURE, GESTURES_FEATURE,
                                      FX.FEATURE] + ([AU.FEATURE] if self.audio is not None else [])}
                ch, server_hello, mode = open_channel(self.creds, sock, ROLE_CONTROL, hello)
            except Exception as exc:
                from ..secure import UnknownDevice, WrongServer
                if sock:
                    sock.close()
                if isinstance(exc, UnknownDevice):
                    self._set("unpaired", T("This PC is not paired with the Mac — get a code on the Mac with "
                                            "Mirrorway › Pair a New Device… and enter it here"))
                    self._wait(15)
                elif isinstance(exc, WrongServer):
                    self._set("auth", str(exc))
                    self._wait(15)
                elif isinstance(exc, AuthError):
                    self._set("auth", T("The old pairing code is no longer valid — pair again on the Mac with "
                                        "Mirrorway › Pair a New Device…"))
                    self._wait(15)
                elif isinstance(exc, NotPaired):
                    continue
                else:
                    self._set("error", T("Could not connect ({host}): {error}").format(host=host, error=exc))
                    self._wait(3)
                continue
            self.server_name = str(server_hello.get("name") or label)
            self.server_version = str(server_hello.get("version") or "")
            self.auth_mode = mode
            self._server_features = list(server_hello.get("features") or [])
            self.file_target = (host, port)
            self._clip_rx = RC.Assembler()
            sock.settimeout(15)
            self.ch = ch
            self._set("connected", T("Connected to {name}").format(name=self.server_name))
            log.info("Connected to the Mac: %s (%s), %s", self.server_name, host,
                     "with the device key" if mode == "key" else "with the old pairing code")
            if mode == "key" and self.cfg.get("pair_code"):
                self.cfg.set("pair_code", "")          # the old long code is no longer needed
            if mode == "legacy" and "devkeys" in (server_hello.get("features") or []):
                threading.Thread(target=self._enroll, args=(ch,), daemon=True, name="mirrorway-enroll").start()
            pinger = threading.Thread(target=self._pinger, args=(ch,), daemon=True)
            pinger.start()
            threading.Thread(target=self._send_netinfo, args=(ch,), daemon=True, name="mirrorway-netinfo").start()
            try:
                self._recv_loop(ch)
            except Exception as exc:
                if not self._stop_evt.is_set():
                    log.info("Connection dropped: %s", exc)
            finally:
                self.ch = None
                ch.close()
                self.remote.on_disconnect()
                if self.audio is not None:
                    self.audio.stop()
            if not self._stop_evt.is_set():
                self._set("disconnected", T("Connection dropped, retrying…"))
                self._wait(2)

    # ---- pairing -----------------------------------------------------------------
    def _do_pairing(self) -> None:
        from ..secure import PairingClosed, client_pair
        code = (self.cfg.get("pairing_code") or "").strip()
        target = self.resolver.resolve()
        if target is None:
            self._set("searching", T("Looking for the Mac to pair with…"))
            self._wait(1)
            return
        host, port, label = target
        self._set("pairing", T("Pairing with {name}…").format(name=label))
        sock = None
        try:
            from ..devkeys import KeyUnavailable
            try:
                ident = self.creds.identity(create=True)
            except KeyUnavailable:            # the old key is unreadable: make a new one while pairing again
                self.cfg.set("device_key", "")
                self.creds._ident = None
                ident = self.creds.identity(create=True)
            sock = socket.create_connection((host, port), timeout=10)
            tune_socket(sock)
            info = client_pair(sock, code, ident, socket.gethostname())
        except (PairingClosed, AuthError) as exc:
            self.cfg.set("pairing_code", "")
            self._set("auth", T("Could not pair: {error}").format(error=exc))
            self._wait(10)
            return
        except Exception as exc:
            self._set("error", T("Could not open the pairing connection ({host}): {error}").format(host=host, error=exc))
            self._wait(3)
            return
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self.creds.save_pairing(ident, info["pub"], info["name"])
        self.cfg.set("pairing_code", "")
        self.cfg.set("pair_code", "")
        log.info("Paired with the Mac: %s (Mac fingerprint %s)", info["name"], info.get("id") or "?")
        self._set("paired", T("Paired with {name}, connecting…").format(name=info['name']))

    def _enroll(self, ch) -> None:
        """While connected with the old code, register the device key with the Mac (automatic migration)."""
        from ..devkeys import b64e
        from ..secure import MSG_ENROLL
        try:
            ident = self.creds.identity(create=True)
            self._enroll_ident = ident
            ch.send_json(MSG_ENROLL, {"name": socket.gethostname(), "pub": b64e(ident.public),
                                      "platform": "windows"})
        except Exception as exc:
            log.warning("Could not send the device key registration: %s", exc)

    def _on_enrolled(self, info: dict) -> None:
        from ..devkeys import b64d
        ident = getattr(self, "_enroll_ident", None) or self.creds.identity(create=True)
        try:
            self.creds.save_pairing(ident, b64d(info["mac_pub"]), str(info.get("mac_name") or self.server_name))
            log.info("Device key registered with the Mac (%s); reconnecting with the key", info.get("id"))
        except Exception as exc:
            log.warning("Could not save the pairing information: %s", exc)
            return
        # Finish the migration right away: reconnect with the key after a short drop (the Mac disables the old code)
        threading.Timer(1.0, self.kick).start()

    def _pinger(self, ch) -> None:
        try:
            ch.send_json(MSG_LOG, {"level": "INFO", "msg": "Windows info: " + system_summary(self.monitors_fn())})
        except Exception:
            pass
        tick = 0
        while not ch.closed and not self._stop_evt.is_set():
            time.sleep(1)
            tick += 1
            if ch.closed:                     # the connection was renewed meanwhile: let the new one send the queue
                return
            try:
                sent = 0
                while self._log_queue and sent < 10:
                    item = self._log_queue.popleft()
                    try:
                        ch.send_json(MSG_LOG, item)
                    except Exception:
                        self._log_queue.appendleft(item)     # keep it; it goes with the next connection
                        raise
                    sent += 1
                if tick % 3 == 0:
                    ch.send(MSG_PING)
            except Exception:
                return

    def _send_netinfo(self, ch) -> None:
        """Report network adapter information (MAC, medium, wake setting) to the Mac for Wake-on-LAN."""
        try:
            info = self.netinfo_fn()
        except Exception as exc:
            log.warning("Could not collect the network information: %s", exc)
            return
        if not info or ch.closed:
            return
        try:
            ch.send_json(MSG_HELLO, {"adapters": info.get("adapters") or [],
                                     "fast_startup": info.get("fast_startup")})
        except Exception:
            pass

    def _recv_loop(self, ch) -> None:
        r = self.remote
        while not self._stop_evt.is_set():
            mtype, payload = ch.recv()
            if mtype == MSG_MOVE:
                r.on_move(*unpack_move(payload))
            elif mtype == MSG_KEY:
                r.on_key(*unpack_key(payload))
            elif mtype == MSG_BUTTON:
                r.on_button(*unpack_button(payload))
            elif mtype == MSG_SCROLL:
                r.on_scroll(*unpack_scroll(payload))
            elif mtype == MSG_ENTER:
                r.on_enter(json.loads(payload.decode("utf-8")))
            elif mtype == MSG_EXIT:
                r.on_exit()
            elif mtype == AU.MSG_AUDIO_STATE:
                if self.audio is not None:
                    self.audio.set_state(bool(json.loads(payload.decode("utf-8")).get("on")))
            elif mtype == FX.MSG_FILES_OFFER:
                if self.files is not None:
                    self.files.on_offer(json.loads(payload.decode("utf-8")))
            elif mtype == MSG_GESTURE:
                r.on_gesture(json.loads(payload.decode("utf-8")))
            elif mtype == MSG_VIEW:
                if self.on_view is not None:
                    try:
                        self.on_view(json.loads(payload.decode("utf-8")))
                    except Exception:
                        log.exception("could not handle the screen window request")
            elif mtype == MSG_RELEASE_ALL:
                r.release_all()
            elif mtype == MSG_CLIPBOARD:
                if self.clip is not None:
                    self.clip.apply_remote(payload.decode("utf-8", "replace"))
            elif mtype == MSG_CLIPDATA:
                data = self._clip_rx.feed(payload)
                if data is not None and self.clip is not None:
                    try:
                        self.clip.apply_remote(RC.unpack_bundle(data))
                    except Exception as exc:
                        log.info("Could not read the clipboard from the Mac: %s", exc)
            elif mtype == MSG_TEXT:
                r.on_text(payload.decode("utf-8", "replace"))
            elif mtype == MSG_ECHO:
                _, last_rtt = unpack_echo(payload)
                if last_rtt > 0:
                    self.last_rtt_ms = last_rtt
                ch.send(MSG_ECHO_REPLY, payload)
            elif mtype == MSG_UPDATE:
                self._start_update(ch, payload)
            elif mtype == _MSG_ENROLL:
                self._on_enrolled(json.loads(payload.decode("utf-8")))
            elif mtype in (MSG_PING, MSG_HELLO):
                pass

    # ---- automatic update -------------------------------------------------
    def _start_update(self, ch, payload: bytes) -> None:
        if self._updating:
            return
        try:
            meta, data = unpack_update(payload)
        except Exception:
            return
        version = str(meta.get("version"))
        if self.cfg.get("update_failed") == version:
            log.info("The %s update failed before, skipping it", version)
            return
        self._updating = True
        threading.Thread(target=self._do_update, args=(ch, meta, data), daemon=True,
                         name="mirrorway-update").start()

    def _do_update(self, ch, meta: dict, data: bytes) -> None:
        version = str(meta.get("version"))
        self._set("updating", T("Updating to {version}…").format(version=version))
        try:
            apply = self._apply_update
            if apply is None:
                from .updater import apply_update as apply
            apply(meta, data, self.install_root)
        except Exception as exc:
            log.error("Update failed: %s", exc)
            self.cfg.set("update_failed", version)
            try:
                ch.send_json(MSG_UPDATE_RESULT, {"version": version, "ok": False, "error": str(exc)[:500]})
            except Exception:
                pass
            self._updating = False
            self._set("connected", T("Connected to {name} (update failed)").format(name=self.server_name))
            return
        try:
            ch.send_json(MSG_UPDATE_RESULT, {"version": version, "ok": True})
        except Exception:
            pass
        if self.on_update is not None:
            self.on_update(version)


class VideoClient(threading.Thread):
    """Receives the Mac's screen; decodes the frames, scales them to the target size and hands them to on_frame."""

    STATS_INTERVAL = 5.0

    def __init__(self, cfg, resolver: Resolver, on_frame: Callable, on_status: Callable[[str], None],
                 target_size: Callable[[], Tuple[int, int]], stats_fn: Optional[Callable[[], dict]] = None,
                 on_patches: Optional[Callable] = None, needs_full: Optional[Callable] = None,
                 on_video: Optional[Callable] = None, hw_decode: Optional[str] = None,
                 credentials: Optional[Credentials] = None, on_info: Optional[Callable[[dict], None]] = None):
        super().__init__(daemon=True, name="mirrorway-video")
        self.on_info = on_info            # when the stream information changes (e.g. the second screen opened/closed)
        self.cfg = cfg
        self.creds = credentials or Credentials(cfg)
        self.resolver = resolver
        self.on_frame = on_frame
        self.on_status = on_status
        self.target_size = target_size
        self.stats_fn = stats_fn
        self.on_patches = on_patches
        self.needs_full = needs_full or (lambda size: True)
        self.on_video = on_video          # GPU viewer: the decoded YUV frame is handed over directly
        self.hw_decode = hw_decode        # e.g. "d3d11va"
        self._delta = None
        self._h264 = None              # H264Decoder (per connection)
        self._h264_wait_key = False    # waiting for a key frame after a decode error
        self._key_req_t = 0.0
        self._prev_rgba = None         # the last H.264 frame shown (for partial drawing)
        self._h264_lock = threading.Lock()   # the decoder is used from one thread
        self.decode_errors = 0
        self.ch = None
        self.info = {}
        self._stop_evt = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()
        ch = self.ch
        if ch is not None:
            ch.close()

    # If the changed area is larger than this share of the window, draw a full frame instead of pieces
    PATCH_AREA_LIMIT = 0.4

    def _display_size(self, w: int, h: int):
        tw, th = self.target_size()
        if tw > 10 and th > 10:
            s = min(tw / w, th / h)
            return s, (max(1, int(w * s)), max(1, int(h * s)))
        return 1.0, (w, h)

    def _handle_h264(self, ch, payload: bytes) -> None:
        with self._h264_lock:
            self._handle_h264_locked(ch, payload)

    def _handle_h264_locked(self, ch, payload: bytes) -> None:
        import numpy as np
        from PIL import Image
        from ..h264 import FLAG_KEY
        t0 = time.monotonic()
        w, h, seq, enc_ms, flags, data = unpack_frame2(payload)
        key = bool(flags & FLAG_KEY)
        if self._h264_wait_key and not key:
            if t0 - self._key_req_t > 2.0:            # repeat if the request got lost
                self._key_req_t = t0
                ch.send(MSG_KEYFRAME_REQ)
            return
        try:
            frames = self._h264.decode(data)
        except Exception as exc:
            log.warning("Could not decode an H.264 frame (%s, %s); asking for a key frame", exc, self._h264.kind)
            self.decode_errors += 1
            if self._h264.kind != "software":
                self._h264.fail_hw(exc)
            else:
                self._h264.reset()
            self._h264_wait_key = True
            self._prev_rgba = None
            self._key_req_t = t0
            ch.send(MSG_KEYFRAME_REQ)
            return
        self._h264_wait_key = False
        if not frames:
            return
        frame = frames[-1]
        if self.on_video is not None:
            meta = {"enc_ms": float(enc_ms), "bytes": len(payload), "decoder": self._h264.kind,
                    "decode_ms": (time.monotonic() - t0) * 1000.0}
            self.on_video(frame, meta)
            return
        _scale, dsize = self._display_size(frame.width, frame.height)
        rgba = self._h264.to_rgba(frame, dsize)
        prev, self._prev_rgba = self._prev_rgba, rgba
        meta = {"enc_ms": float(enc_ms), "bytes": len(payload)}
        if prev is not None and self.on_patches is not None and not self.needs_full(dsize):
            rects = self._delta.changed_rects(prev, rgba) if self._delta is not None else None
            if rects == []:
                return                                  # no visible change
            if rects is not None:
                area = sum(rw * rh for _, _, rw, rh in rects)
                if area <= self.PATCH_AREA_LIMIT * dsize[0] * dsize[1]:
                    patches = [(x, y, Image.fromarray(np.ascontiguousarray(rgba[y:y + rh, x:x + rw])))
                               for x, y, rw, rh in rects]
                    meta["decode_ms"] = (time.monotonic() - t0) * 1000.0
                    self.on_patches(dsize, patches, meta)
                    return
        img = Image.fromarray(rgba)
        meta["decode_ms"] = (time.monotonic() - t0) * 1000.0
        self.on_frame(img, meta)

    def _handle_frame(self, ch, mtype: int, payload: bytes, canvas) -> None:
        from PIL import Image
        t0 = time.monotonic()
        if mtype == MSG_FRAME_H264:
            if self._h264 is not None:
                self._handle_h264(ch, payload)
            return
        if mtype == MSG_FRAME_DELTA:
            if canvas is None:
                return
            w, h, seq, enc_ms, _flags, parts = self._delta.unpack_delta(payload)
            if not canvas.apply(w, h, parts):
                ch.send(MSG_KEYFRAME_REQ)          # no/mismatched canvas: ask for a full frame
                return
            scale, dsize = self._display_size(w, h)
            if self.on_patches is not None and not self.needs_full(dsize):
                patches = self._make_patches(canvas, parts, scale, dsize)
                if patches is not None:
                    meta = {"enc_ms": float(enc_ms), "decode_ms": (time.monotonic() - t0) * 1000.0,
                            "bytes": len(payload)}
                    self.on_patches(dsize, patches, meta)
                    return
            img = Image.fromarray(canvas.arr)
        elif mtype == MSG_FRAME2 and canvas is not None:
            w, h, seq, enc_ms, _flags, jpeg = unpack_frame2(payload)
            img = Image.fromarray(canvas.set_full(jpeg))
        else:
            if mtype == MSG_FRAME2:
                w, h, seq, enc_ms, _flags, jpeg = unpack_frame2(payload)
            else:
                w, h, seq, jpeg = unpack_frame(payload)
                enc_ms = 0
            img = Image.open(io.BytesIO(jpeg))
            img.load()
        _scale, dsize = self._display_size(img.width, img.height)
        if dsize != img.size:
            img = img.resize(dsize, Image.BILINEAR)
        elif canvas is not None:
            img = img.copy()                       # copy, because the canvas changes with the next frame
        meta = {"enc_ms": float(enc_ms), "decode_ms": (time.monotonic() - t0) * 1000.0, "bytes": len(payload)}
        self.on_frame(img, meta)

    def _make_patches(self, canvas, parts, scale: float, dsize):
        """Turns the changed rectangles into pieces at window scale; None if too large (full frame)."""
        from PIL import Image
        src = Image.fromarray(canvas.arr)
        dw, dh = dsize
        patches, area = [], 0
        for (x, y, w, h), _ in parts:
            if scale == 1.0:
                patches.append((x, y, src.crop((x, y, x + w, y + h))))
                area += w * h
                continue
            dx0, dy0 = int(x * scale), int(y * scale)
            dx1 = min(dw, int(math.ceil((x + w) * scale)))
            dy1 = min(dh, int(math.ceil((y + h) * scale)))
            if dx1 <= dx0 or dy1 <= dy0:
                continue
            img = src.resize((dx1 - dx0, dy1 - dy0), Image.BILINEAR,
                             box=(dx0 / scale, dy0 / scale, dx1 / scale, dy1 / scale))
            patches.append((dx0, dy0, img))
            area += (dx1 - dx0) * (dy1 - dy0)
        if area > self.PATCH_AREA_LIMIT * dw * dh:
            return None
        return patches

    def run(self) -> None:
        while not self._stop_evt.is_set():
            target = self.resolver.resolve()
            if not (self.creds.paired() or self.creds.legacy_code()):
                self.on_status(T("Pairing required (on the Mac: Mirrorway › Pair a New Device…)"))
                self._stop_evt.wait(1)
                continue
            if target is None:
                self.on_status(T("Looking for the Mac…"))
                self._stop_evt.wait(1)
                continue
            host, port, label = target
            sock = None
            try:
                sock = socket.create_connection((host, port), timeout=5)
                tune_socket(sock)
                features = ["frame2", "stats"]
                canvas = None
                try:
                    from .. import delta as _delta
                    self._delta = _delta
                    canvas = _delta.Canvas()
                    features.append("delta")
                except Exception as exc:
                    log.info("Changed-region decoding unavailable (%s); full frames will be used", exc)
                self._h264, self._h264_wait_key, self._prev_rgba = None, False, None
                try:
                    from ..h264 import H264Decoder, decoder_available
                    if decoder_available():
                        self._h264 = H264Decoder(hw=self.hw_decode)
                        features.append("h264")
                except Exception as exc:
                    log.info("H.264 decoder unavailable (%s); JPEG will be used", exc)
                ch, _, _mode = open_channel(self.creds, sock, ROLE_VIDEO,
                                            {"name": socket.gethostname(), "version": __version__,
                                             "features": features})
                sock.settimeout(15)
                self.ch = ch
                self.on_status(T("Waiting for the screen of {name}…").format(name=label))
                last_stats = time.monotonic()
                while not self._stop_evt.is_set():
                    mtype, payload = ch.recv()
                    if mtype in (MSG_FRAME, MSG_FRAME2, MSG_FRAME_DELTA, MSG_FRAME_H264):
                        self._handle_frame(ch, mtype, payload, canvas)
                    elif mtype == MSG_VIDEO_INFO:
                        upd = json.loads(payload.decode("utf-8"))
                        if "backend" in upd or "error" in upd:
                            self.info = upd                  # new stream information
                        else:
                            self.info = {**self.info, **upd}   # e.g. the adaptive quality target changed
                        if self.on_info is not None:
                            try:
                                self.on_info(dict(self.info))
                            except Exception:
                                log.exception("could not handle the stream information")
                        if self.info.get("error"):
                            self.on_status("Mac: " + self.info["error"].splitlines()[0])
                            break
                    now = time.monotonic()
                    if self.stats_fn is not None and now - last_stats >= self.STATS_INTERVAL:
                        last_stats = now
                        try:
                            ch.send_json(MSG_STATS, self.stats_fn())
                        except Exception:
                            pass
            except AuthError as exc:
                self.on_status(T("Authentication failed: {error}").format(error=exc))
                self._stop_evt.wait(10)              # without a pairing, do not poll the Mac often
            except Exception as exc:
                if not self._stop_evt.is_set():
                    self.on_status(T("No video connection: {error}").format(error=exc))
                    if not isinstance(exc, (ConnectionError, OSError)):
                        log.warning("Error while receiving video: %r", exc, exc_info=True)
            finally:
                if self.ch is not None:
                    self.ch.close()
                    self.ch = None
                elif sock is not None:
                    sock.close()
                if self.info.get("extend") and self.on_info is not None:
                    self.info = {}                   # the connection dropped: the window leaves the second-screen layout
                    try:
                        self.on_info({})
                    except Exception:
                        pass
            self._stop_evt.wait(2)
