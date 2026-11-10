"""Mac-side network server: control and video sessions, clipboard, discovery beacon."""
from __future__ import annotations

import io
import json
import logging
import queue
import socket
import threading
import time
from typing import Callable, Optional

from .. import __version__
from .. import audio as AU
from .. import filexfer as FX
from .. import richclip as RC
from ..i18n import T
from ..clipsync import ClipboardSync
from ..extend import FEATURE as EXTEND_FEATURE
from ..discovery import Beacon, interfaces, lan_ips, vpn_active
from ..devkeys import KeyUnavailable, b64d, b64e, fingerprint, load_identity
from ..h264 import FLAG_KEY
from ..secure import (MAGIC2, MSG_ENROLL, ROLE_PAIR, ST_TOO_MANY, PairingClosed,
                      UnknownDevice, format_code, new_pairing_code, server_accept, server_pair)
from ..protocol import (ACTIONS_FEATURE, MSG_ACTION, MSG_CLIPBOARD, MSG_CLIPDATA, MSG_ECHO, MSG_ECHO_REPLY, MSG_FRAME, MSG_FRAME2, MSG_HELLO,
                        MSG_FRAME_DELTA, MSG_FRAME_H264, MSG_KEYFRAME_REQ, MSG_LEAVE, MSG_LOG, MSG_MOVE, MSG_PING, MSG_STATS,
                        MSG_UPDATE, MSG_UPDATE_RESULT,
                        MSG_VIDEO_INFO, MSG_VIEW, PROTOCOL_VERSION, ROLE_AUDIO, ROLE_CONTROL, ROLE_FILE, ROLE_VIDEO, AuthError, ProtocolError,
                        pack_echo, pack_frame, pack_frame2, pack_move, pack_update, parse_version,
                        recv_exact, server_handshake, tune_socket, unpack_echo, unpack_move)
from .router import Router

log = logging.getLogger("mirrorway.server")


def machine_name() -> str:
    name = socket.gethostname()
    return name[:-6] if name.endswith(".local") else name


# ---------------------------------------------------------------------------
class ControlSession:
    def __init__(self, server: "MirrorwayServer", ch, hello: dict, addr):
        self.server = server
        self.ch = ch
        self.hello = hello
        self.addr = addr
        self.name = str(hello.get("name") or addr[0])
        self.client_version = str(hello.get("version") or "?")
        self.closed = False
        self.rtt_ms: Optional[float] = None       # last measured round trip
        self.rtt_avg_ms: Optional[float] = None   # exponential average
        self.last_update_result: Optional[dict] = None
        self.wol_learned = False
        self.device: Optional[dict] = None        # its record, if it connected with a device key
        self._q: "queue.Queue" = queue.Queue()
        self._close_lock = threading.Lock()
        self._clip_rx = RC.Assembler()

    def start(self) -> None:
        threading.Thread(target=self._sender, daemon=True, name="mirrorway-ctl-send").start()
        threading.Thread(target=self._receiver, daemon=True, name="mirrorway-ctl-recv").start()

    def send(self, mtype: int, payload: bytes = b"") -> None:
        if not self.closed:
            self._q.put((mtype, payload))

    def send_json(self, mtype: int, obj) -> None:
        self.send(mtype, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def send_now(self, mtype: int, payload: bytes) -> None:
        """Send bypassing the queue (large clipboard chunks): thanks to the channel lock, mouse/keyboard messages
        slip in between the chunks; queued, they would wait until all chunks had been sent."""
        if self.closed:
            raise ConnectionError("Connection closed")
        self.ch.send(mtype, payload)

    ECHO_INTERVAL = 2.0

    def _sender(self) -> None:
        last_echo = 0.0
        try:
            while not self.closed:
                now = time.monotonic()
                if now - last_echo >= self.ECHO_INTERVAL:   # latency measurement + keep-alive
                    last_echo = now
                    # perf_counter time stamps: on Windows monotonic moves in ~15 ms steps, so local latency read 0
                    self.ch.send(MSG_ECHO, pack_echo(time.perf_counter(), self.rtt_ms or 0.0))
                try:
                    first = self._q.get(timeout=0.5)
                except queue.Empty:
                    continue
                items = [first]
                while True:
                    try:
                        items.append(self._q.get_nowait())
                    except queue.Empty:
                        break
                out = []
                for mtype, payload in items:
                    if mtype is None:
                        return
                    if mtype == MSG_MOVE and out and out[-1][0] == MSG_MOVE:
                        ax, ay = unpack_move(out[-1][1])
                        bx, by = unpack_move(payload)
                        out[-1] = (MSG_MOVE, pack_move(ax + bx, ay + by))
                    else:
                        out.append((mtype, payload))
                for mtype, payload in out:
                    self.ch.send(mtype, payload)
        except Exception as exc:
            if not self.closed:
                log.info("Control connection dropped while sending: %s", exc)
        finally:
            self.close()

    def _receiver(self) -> None:
        try:
            self.ch.sock.settimeout(20)
            while not self.closed:
                mtype, payload = self.ch.recv()
                if mtype == MSG_LEAVE:
                    info = json.loads(payload.decode("utf-8"))
                    vd = info.get("vd")
                    vd = (float(vd[0]), float(vd[1])) if isinstance(vd, list) and len(vd) == 2 else None
                    self.server.router.leave_from_client(float(info.get("ratio", 0.5)), vd=vd)
                elif mtype == MSG_CLIPBOARD:
                    self.server.clipboard_in(payload.decode("utf-8", "replace"))
                elif mtype == MSG_CLIPDATA:
                    data = self._clip_rx.feed(payload)
                    if data is not None:
                        try:
                            formats = RC.unpack_bundle(data)
                        except Exception as exc:
                            log.info("Could not read the clipboard from Windows: %s", exc)
                        else:
                            log.debug("Clipboard from Windows: %s", RC.summary(formats))
                            self.server.clipboard_in(formats)
                elif mtype == MSG_ECHO_REPLY:
                    t0, _ = unpack_echo(payload)
                    rtt = max((time.perf_counter() - t0) * 1000.0, 0.01)
                    self.rtt_ms = rtt
                    self.rtt_avg_ms = rtt if self.rtt_avg_ms is None else 0.8 * self.rtt_avg_ms + 0.2 * rtt
                elif mtype == MSG_UPDATE_RESULT:
                    res = json.loads(payload.decode("utf-8"))
                    self.last_update_result = res
                    if res.get("ok"):
                        log.info("Windows updated: %s (restarting)", res.get("version"))
                    else:
                        log.warning("Windows update failed (%s): %s", res.get("version"),
                                    res.get("error"))
                elif mtype == MSG_LOG:
                    rec = json.loads(payload.decode("utf-8"))
                    level = logging.getLevelName(str(rec.get("level", "INFO")))
                    log.log(level if isinstance(level, int) else logging.INFO, "Windows: %s",
                            str(rec.get("msg", ""))[:1000])
                elif mtype == MSG_ENROLL:
                    self.server.enroll(self, json.loads(payload.decode("utf-8")))
                elif mtype == MSG_ACTION:
                    self.server.action_from_windows(json.loads(payload.decode("utf-8")))
                elif mtype == MSG_HELLO:
                    upd = json.loads(payload.decode("utf-8"))
                    self.hello.update(upd)
                    if "adapters" in upd:
                        self.server.learn_wol(self, upd)
        except Exception as exc:
            if not self.closed:
                log.info("Control connection closed: %s", exc)
        finally:
            self.close()

    def close(self) -> None:
        with self._close_lock:
            if self.closed:
                return
            self.closed = True
        self._q.put((None, b""))
        self.ch.close()
        self.server._on_control_closed(self)


# ---------------------------------------------------------------------------
class VideoSession(threading.Thread):
    def __init__(self, server: "MirrorwayServer", ch, addr, hello: Optional[dict] = None):
        super().__init__(daemon=True, name="mirrorway-video")
        self.server = server
        self.ch = ch
        self.addr = addr
        self.hello = hello or {}
        features = self.hello.get("features") or []
        self.frame2 = "frame2" in features
        self.delta = None
        if "delta" in features:
            try:
                from .. import delta as _delta
                self.delta = _delta
            except Exception as exc:          # without numpy, fall back to full frames
                log.warning("Changed-region encoding unavailable: %s", exc)
        self.h264 = "h264" in features          # the client can decode H.264
        self.target_kbps = 0                    # current target of adaptive quality
        self.target_fps = 0
        self.adaptive = False
        self.net: dict = {}
        self.device: Optional[dict] = None
        self.codec = "jpeg"
        self.encoder_name = ""
        self.force_key = True
        self._last_arr = None
        self.area_pct = 100.0
        self.keyframes = 0
        self.closed = False
        self.backend_name = ""
        self.fps_actual = 0.0
        self.kbps = 0.0
        self.enc_ms = 0.0
        self.client_stats: dict = {}
        self._stat_lock = threading.Lock()
        self.minute = self._new_minute()       # per-minute summary for the log
        self.client_hist: list = []            # Windows measurements received within the minute
        self.ext: Optional[dict] = None         # second screen: {"id", "w", "h", "mrect"} (when on)

    def _stats_reader(self) -> None:
        """Viewer statistics sent by the client (1.3+ clients only)."""
        try:
            while not self.closed:
                mtype, payload = self.ch.recv()
                if mtype == MSG_STATS:
                    self.client_stats = json.loads(payload.decode("utf-8"))
                    with self._stat_lock:
                        if self.client_stats.get("fps", 0) > 0:
                            self.client_hist.append(dict(self.client_stats))
                elif mtype == MSG_KEYFRAME_REQ:
                    self.force_key = True
        except Exception:
            pass

    def close(self) -> None:
        self.closed = True
        self.ch.close()

    def _encode_full(self, frame, preset):
        """Full frame -> (width, height, JPEG)."""
        from .capture import fit
        if self.delta is not None:
            arr, cs = self._last_arr
            return arr.shape[1], arr.shape[0], self.delta.encode(arr, cs, preset["q"], preset["subsampling"])
        img = fit(frame, preset["max_w"])
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=preset["q"], subsampling=preset["subsampling"])
        return img.width, img.height, buf.getvalue()

    def run(self) -> None:
        from .capture import QUALITY_PRESETS
        cfg = self.server.cfg
        preset = dict(QUALITY_PRESETS.get(cfg.get("quality"), QUALITY_PRESETS["medium"]))
        want_h264 = self.h264 and str(cfg.get("video_codec") or "h264") == "h264"
        cap_fps = int(preset.get("h264_fps", preset["fps"])) if want_h264 else int(preset["fps"])
        # For the hardware encoder the screen is captured directly as NV12 (YUV); the software/JPEG paths want BGRA
        self.vt_allowed = not self.server.vt_blocked()   # the same decision for the whole session (capture + encoder)
        pixel_format = "nv12" if (want_h264 and self.server.capture_nv12 and self.vt_allowed) else "bgra"
        backend = None
        try:
            try:
                self.ext = self.server.extend.prepare()
            except Exception:
                log.exception("Could not prepare the second screen")
                self.ext = None
            cap = dict(preset, fps=cap_fps, pixel_format=pixel_format)
            if self.ext:
                # The virtual display is captured; not downscaled, so pixels map 1:1 to the Windows monitor
                cap.update(display_id=self.ext["id"], max_w=max(int(preset["max_w"]), int(self.ext["w"])))
                preset["max_w"] = cap["max_w"]
            try:
                backend = self.server.capture_factory(int(cfg.get("display_index") or 0), cap)
            except Exception as exc:
                log.error("Cannot capture the screen: %s", exc)
                self.server.last_error = str(exc).splitlines()[0]
                self.ch.send_json(MSG_VIDEO_INFO, {"error": str(exc)})
                return
            self.backend_name = backend.name
            self.ch.send_json(MSG_VIDEO_INFO, self._with_ext({"backend": backend.name, "fps": cap_fps,
                                                              "name": machine_name(), "delta": self.delta is not None,
                                                              "codec": "h264" if want_h264 else "jpeg"}))
            if "stats" in (self.hello.get("features") or []):
                threading.Thread(target=self._stats_reader, daemon=True, name="mirrorway-video-stats").start()
            if want_h264:
                if self._run_h264(backend, preset):
                    return
                # H.264 unavailable: fall back to JPEG on the same connection
                self.force_key = True
                self.ch.send_json(MSG_VIDEO_INFO, self._with_ext({"backend": backend.name, "fps": preset["fps"],
                                                                  "name": machine_name(),
                                                                  "delta": self.delta is not None,
                                                                  "codec": "jpeg"}))
            self._run_jpeg(backend, preset)
        except Exception as exc:
            if not self.closed:
                log.info("Video connection closed: %s", exc)
        finally:
            if backend is not None:
                try:
                    backend.stop()
                except Exception:
                    pass
            self.close()
            self.server._on_video_closed(self)
            if self.ext:
                self.server.extend.session_ended()

    def _with_ext(self, info: dict) -> dict:
        """Add the second screen to the video info: the Windows window opens full screen on that monitor."""
        if self.ext:
            info["extend"] = {"rect": self.ext["mrect"], "size": [self.ext["w"], self.ext["h"]]}
        return info

    # ---- measurement window ---------------------------------------------------------
    def _new_window(self, t: float) -> dict:
        return {"t": t, "n": 0, "bytes": 0, "enc": 0.0, "area": 0.0, "keys": 0}

    def _new_minute(self) -> dict:
        return {"t": time.monotonic(), "frames": 0, "bytes": 0, "enc": 0.0, "keys": 0,
                "peak_fps": 0.0, "peak_kbps": 0.0, "peak_enc": 0.0}

    def _account(self, win: dict, now: float, nbytes: int, enc_ms: float, area: float, key: bool) -> dict:
        win["n"] += 1
        win["bytes"] += nbytes
        win["enc"] += enc_ms
        win["area"] += area
        win["keys"] += int(key)
        if now - win["t"] >= 2.0:
            span = now - win["t"]
            self.fps_actual = win["n"] / span
            self.kbps = win["bytes"] / 1024.0 / span
            self.enc_ms = win["enc"] / win["n"]
            self.area_pct = 100.0 * win["area"] / win["n"]
            self.keyframes = win["keys"]
            with self._stat_lock:
                m = self.minute
                m["frames"] += win["n"]
                m["bytes"] += win["bytes"]
                m["enc"] += win["enc"]
                m["keys"] += win["keys"]
                m["peak_fps"] = max(m["peak_fps"], self.fps_actual)
                m["peak_kbps"] = max(m["peak_kbps"], self.kbps)
                m["peak_enc"] = max(m["peak_enc"], self.enc_ms)
            return self._new_window(now)
        return win

    def minute_summary(self, reset: bool = False) -> dict:
        """Since the last summary: average and peak values (of the 2 s windows)."""
        with self._stat_lock:
            m, hist = self.minute, list(self.client_hist)
            if reset:
                self.minute, self.client_hist = self._new_minute(), []
        span = max(time.monotonic() - m["t"], 1e-6)
        out = {"frames": m["frames"], "fps": m["frames"] / span, "peak_fps": m["peak_fps"],
               "kbps": m["bytes"] / 1024.0 / span, "peak_kbps": m["peak_kbps"],
               "enc_ms": m["enc"] / m["frames"] if m["frames"] else 0.0, "peak_enc": m["peak_enc"],
               "keys": m["keys"]}
        if hist:
            def avg(k):
                return sum(float(h.get(k, 0) or 0) for h in hist) / len(hist)
            out["client"] = {
                "fps": avg("fps"), "peak_fps": max(float(h.get("fps", 0) or 0) for h in hist),
                "decode_ms": avg("decode_ms"), "display_ms": avg("display_ms"),
                "latency_ms": avg("latency_ms"), "peak_latency": max(float(h.get("latency_ms", 0) or 0) for h in hist),
                "decoder": hist[-1].get("decoder", ""), "renderer": hist[-1].get("renderer", ""),
            }
        return out

    # ---- H.264 ------------------------------------------------------------------------
    # When motion stops, the last frame is re-encoded after these delays (s): the encoder sharpens areas
    # that stayed low quality during motion (no new frames arrive while the screen is still).
    REFRESH_AFTER = (0.3, 1.2)
    MAX_EMPTY_START = 20     # this many empty outputs among the first frames = the encoder is not working

    def _make_encoder(self, kind: str, w: int, h: int, fps: int, kbps: int):
        enc = self.server.h264_encoder_factory(kind, w, h, fps, kbps)
        self.encoder_name = getattr(enc, "name", kind)
        return enc

    MAX_INFLIGHT = 2         # at most this many frames in the encoder at once (limits latency)
    STALL_TIMEOUT = 2.0      # no output for this long = the encoder is stuck

    def _h264_sender(self, out_q: "queue.Queue", st: dict) -> None:
        """Sends encoded frames in order (the asynchronous encoder's output arrives on the VT thread)."""
        while True:
            item = out_q.get()
            if item is None:
                return
            data, key, err, t_grab, t_done, size, want_key = item
            if err or not data:
                self._release(st)
                if err:
                    st["error"] = err
                continue                                  # an error or a dropped frame
            st["outputs"] += 1
            if want_key and not key:
                self.force_key = True                     # no key frame arrived: ask again
            st["seq"] += 1
            enc_ms = (t_done - t_grab) * 1000.0
            payload = pack_frame2(size[0], size[1], st["seq"], enc_ms, data, flags=FLAG_KEY if key else 0)
            t_send = time.monotonic()
            try:
                self.ch.send(MSG_FRAME_H264, payload)
            except Exception as exc:
                if not self.closed:
                    log.info("Video connection closed: %s", exc)
                self._release(st)
                self.close()
                return
            now = time.monotonic()
            # The encoder slot is only released once the frame is sent: on a slow network frames are
            # skipped at capture instead of piling up in a queue and adding latency
            self._release(st)
            st["send_s"] += now - t_send
            st["sent_bytes"] += len(payload)
            st["sent_any"] = True
            st["last_send"] = now
            st["win"] = self._account(st["win"], now, len(payload), enc_ms, 1.0 if key else 0.0, key)

    @staticmethod
    def _release(st: dict) -> None:
        with st["cond"]:
            st["inflight"] = max(0, st["inflight"] - 1)
            st["cond"].notify_all()

    VIDEO_SNDBUF = 256 * 1024    # send buffer: on congestion at most ~0.1-0.3 s piles up

    def _adapt(self, ctl, st: dict, enc, now: float) -> None:
        """Once a second: feed the connection measurements to the controller and apply a changed target."""
        span = max(now - st["adapt_t"], 1e-3)
        sent, send_s = st["sent_bytes"] - st["adapt_bytes"], st["send_s"] - st["adapt_send_s"]
        st["adapt_t"], st["adapt_bytes"], st["adapt_send_s"] = now, st["sent_bytes"], st["send_s"]
        from ..adaptive import unsent_bytes
        unsent = unsent_bytes(self.ch.sock)
        rate_bps = max(sent / span, 50_000.0)
        queue_ms = None if unsent is None else unsent / rate_bps * 1000.0
        c = self.server.control
        rtt = c.rtt_ms if c is not None else None
        sent_kbps = sent * 8 / 1000.0 / span
        self.net = {"queue_ms": queue_ms, "blocked": send_s / span, "rtt_ms": rtt, "sent_kbps": sent_kbps}
        old_kbps, old_fps = ctl.kbps, ctl.fps
        if ctl.update(queue_ms, send_s / span, rtt, sent_kbps):
            if ctl.kbps != old_kbps and enc is not None and hasattr(enc, "set_bitrate"):
                enc.set_bitrate(ctl.kbps)
            if ctl.fps != old_fps and enc is not None and hasattr(enc, "set_fps"):
                enc.set_fps(ctl.fps)
            self.target_kbps, self.target_fps = ctl.kbps, ctl.fps
            log.info("Adaptive quality: %.1f → %.1f Mbit/s, %d → %d fps (%s)", old_kbps / 1000, ctl.kbps / 1000,
                     old_fps, ctl.fps, ctl.last_reason)
            try:          # for the stats box on Windows
                self.ch.send_json(MSG_VIDEO_INFO, {"target_kbps": ctl.kbps, "target_fps": ctl.fps,
                                                   "adaptive": True, "reason": ctl.last_reason})
            except Exception:
                pass

    def _run_h264(self, backend, preset) -> bool:
        """The H.264 stream. True when the connection ends; False if the encoder never works (falls back to JPEG)."""
        from .capture import to_array
        cfg = self.server.cfg
        fps = int(preset.get("h264_fps", 30))
        kbps = int(preset.get("kbps", 8000))
        interval = 1.0 / fps
        nv12 = getattr(backend, "pixel_format", "bgra") == "nv12"
        self_paced = bool(getattr(backend, "self_paced", False))
        vt_allowed = getattr(self, "vt_allowed", not self.server.vt_blocked())
        use_raw = bool(getattr(backend, "supports_raw", False)) and vt_allowed
        # If the hardware encoder is temporarily blocked, the connection is renewed and retried when the time is up
        vt_retry = not vt_allowed and self.server.capture_nv12 and not self.server.vt_failed
        vt_ok_noted = False
        retry_check_t = time.monotonic()
        now = time.monotonic()
        st = {"cond": threading.Condition(), "inflight": 0, "error": None, "outputs": 0, "seq": 0,
              "sent_any": False, "last_send": now, "win": self._new_window(now),
              "sent_bytes": 0, "send_s": 0.0, "adapt_t": now, "adapt_bytes": 0, "adapt_send_s": 0.0}
        ctl = None
        if cfg.get("adaptive_quality", True):
            from ..adaptive import RateController
            ctl = RateController(kbps, int(preset.get("min_kbps", kbps // 8)), int(preset.get("max_kbps", kbps)), fps)
        self.target_kbps, self.target_fps, self.adaptive = kbps, fps, ctl is not None
        try:
            self.ch.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self.VIDEO_SNDBUF)
        except OSError:
            pass
        last_submit = 0.0
        out_q: "queue.Queue" = queue.Queue()
        sender = threading.Thread(target=self._h264_sender, args=(out_q, st), daemon=True, name="mirrorway-h264-send")
        sender.start()
        enc = None
        submitted = 0
        none_streak = 0
        last_input = None
        last_real = 0.0
        refresh_i = len(self.REFRESH_AFTER)
        self.codec = "h264"

        def fail(reason, tb=False) -> str:
            """Encoder problem: 'continue' (switched to software), 'restart' or 'jpeg'."""
            nonlocal enc, use_raw, last_input, submitted, none_streak
            log.warning("H.264 encoder problem (%s): %s", self.encoder_name or "?", reason, exc_info=tb)
            if enc is not None:
                try:
                    enc.close()
                except Exception:
                    pass
            enc, last_input, submitted, none_streak = None, None, 0, 0
            with st["cond"]:
                st["inflight"], st["error"] = 0, None
            if use_raw:
                self.server.note_vt_failure()
                if nv12:
                    log.info("Hardware encoder abandoned; the video connection will restart with the software encoder")
                    return "restart"
                use_raw = False
                return "continue"
            return "continue" if st["sent_any"] else "jpeg"

        try:
            while not self.closed and cfg.get("screen_share") and str(cfg.get("video_codec") or "h264") == "h264":
                if st["error"]:
                    r = fail(st["error"])
                    if r != "continue":
                        return r == "restart"
                    continue
                now_c = time.monotonic()
                if now_c - retry_check_t >= 2.0:
                    retry_check_t = now_c
                    if vt_retry and not self.server.vt_blocked():
                        log.info("Retrying the hardware encoder (renewing the video connection)")
                        return True
                    if use_raw and not vt_ok_noted and st["outputs"] >= 300:
                        vt_ok_noted = True
                        self.server.note_vt_ok()
                # Limit the frames waiting in the encoder (a slow network/encoder means frames are skipped)
                deadline = time.monotonic() + self.STALL_TIMEOUT
                flushed, stalled = False, False
                while not self.closed:
                    with st["cond"]:
                        if st["inflight"] < self.MAX_INFLIGHT:
                            break
                        st["cond"].wait(0.03)
                        if st["inflight"] < self.MAX_INFLIGHT:
                            break
                    if time.monotonic() > deadline:
                        stalled = True
                        break
                    if not flushed and enc is not None and hasattr(enc, "flush"):
                        enc.flush()                    # drain an encoder that holds frames back
                        flushed = True
                if stalled and not self.closed:
                    r = fail("the encoder does not respond")
                    if r != "continue":
                        return r == "restart"
                    continue
                t0 = time.monotonic()
                timeout = 1.0
                if last_input is not None and refresh_i < len(self.REFRESH_AFTER):
                    timeout = max(0.0, last_real + self.REFRESH_AFTER[refresh_i] - t0)
                if st["inflight"] > 0:
                    timeout = min(timeout, 0.03)
                frame = backend.grab(timeout=timeout, raw=True) if use_raw else backend.grab(timeout=timeout)
                refresh = False
                if frame is None:
                    if st["inflight"] > 0 and enc is not None and hasattr(enc, "flush"):
                        enc.flush()                    # idle: no frame may get stuck
                    now = time.monotonic()
                    if (last_input is not None and refresh_i < len(self.REFRESH_AFTER)
                            and now >= last_real + self.REFRESH_AFTER[refresh_i]):
                        refresh_i += 1
                        refresh = True
                    else:
                        if now - st["last_send"] > 2.0:
                            self.ch.send(MSG_PING)
                            st["last_send"] = now
                        continue
                t_grab = time.monotonic()
                if refresh:
                    inp, size = last_input
                elif use_raw:
                    from .vtenc import pixel_buffer_size
                    inp, size = frame, pixel_buffer_size(frame)
                else:
                    arr, _cs = to_array(frame, preset["max_w"])
                    h, w = arr.shape[:2]
                    if w % 2 or h % 2:                    # 4:2:0 needs even sizes
                        arr = arr[:h - h % 2, :w - w % 2]
                    inp, size = arr, (arr.shape[1], arr.shape[0])
                if not refresh:
                    last_input, last_real, refresh_i = (inp, size), t_grab, 0
                if ctl is not None:
                    if t_grab - st["adapt_t"] >= 1.0:
                        self._adapt(ctl, st, enc, t_grab)
                    # If the frame rate was lowered, skip the frames in between (the last frame goes with the idle refresh)
                    if not refresh and ctl.fps < fps and t_grab - last_submit < 0.85 / ctl.fps:
                        continue
                if enc is None or (enc.width, enc.height) != size:
                    if enc is not None:
                        if hasattr(enc, "flush"):
                            enc.flush()
                        enc.close()
                    kind = "raw" if use_raw else "array"
                    try:
                        enc = self._make_encoder(kind, size[0], size[1], fps, ctl.kbps if ctl else kbps)
                    except Exception as exc:
                        enc = None
                        r = fail(f"could not open: {exc}")
                        if r == "jpeg":
                            return False
                        if r == "restart":
                            return True
                        continue
                    self.force_key = True
                    submitted = 0
                    is_async = bool(getattr(enc, "supports_async", False))
                    log.info("Video stream: %s (%s), H.264 %s, %dx%d, %d fps, %d kbit/s%s%s", backend.name,
                             getattr(backend, "pixel_format", "bgra"), self.encoder_name, size[0], size[1],
                             fps, ctl.kbps if ctl else kbps, ", asynchronous" if is_async else "",
                             f", adaptive ({ctl.min_kbps}-{ctl.max_kbps} kbit/s)" if ctl else "")
                    self.ch.send_json(MSG_VIDEO_INFO, self._with_ext({
                        "backend": backend.name, "fps": fps, "name": machine_name(), "codec": "h264",
                        "encoder": self.encoder_name, "width": size[0], "height": size[1],
                        "target_kbps": ctl.kbps if ctl else kbps, "target_fps": ctl.fps if ctl else fps,
                        "adaptive": ctl is not None}))
                want_key, self.force_key = self.force_key, False
                try:
                    if getattr(enc, "supports_async", False):
                        with st["cond"]:
                            st["inflight"] += 1

                        def on_done(data, key, err, t_grab=t_grab, size=size, want_key=want_key):
                            out_q.put((data, key, err, t_grab, time.monotonic(), size, want_key))

                        enc.submit(inp, want_key, on_done)
                        last_submit = t_grab
                        submitted += 1
                        if submitted >= self.MAX_EMPTY_START and st["outputs"] == 0 and not st["sent_any"]:
                            enc.flush()
                            time.sleep(0.05)
                            if st["outputs"] == 0:
                                r = fail(f"no output for {submitted} frames")
                                if r != "continue":
                                    return r == "restart"
                                continue
                    else:
                        res = enc.encode(inp, force_key=want_key)
                        if res is None:
                            self.force_key = self.force_key or want_key
                            none_streak += 1
                            if not st["sent_any"] and none_streak >= self.MAX_EMPTY_START:
                                r = fail(f"no output for {none_streak} frames")
                                if r != "continue":
                                    return r == "restart"
                            continue
                        none_streak = 0
                        last_submit = t_grab
                        with st["cond"]:
                            st["inflight"] += 1
                        out_q.put((res[0], res[1], None, t_grab, time.monotonic(), size, want_key))
                except Exception as exc:
                    self.force_key = True
                    r = fail(exc, tb=not st["sent_any"])
                    if r != "continue":
                        return r == "restart"
                    continue
                # If the capture method already limits the frame rate, no extra wait (a macOS sleep
                # takes 1-3 ms longer; it brought 60 fps down to 51)
                dt = time.monotonic() - t0
                if dt < interval and not refresh and not self_paced:
                    time.sleep(interval - dt)
            return True
        finally:
            if enc is not None:
                try:
                    if hasattr(enc, "flush"):
                        enc.flush()
                    enc.close()
                except Exception:
                    pass
            out_q.put(None)
            sender.join(2.0)

    # ---- JPEG (changed regions) -------------------------------------------------------
    def _run_jpeg(self, backend, preset) -> None:
        from .capture import to_array
        cfg = self.server.cfg
        self.codec = "jpeg"
        self.encoder_name = f"JPEG ({self.delta.backend_name()})" if self.delta else "JPEG"
        log.info("Video stream: %s, %s", backend.name,
                 f"changed-region encoding ({self.delta.backend_name()})" if self.delta else "full-frame JPEG")
        interval = 1.0 / preset["fps"]
        # Changed regions are small, so higher quality and 4:4:4 (sharp text) are cheap
        dq, dss = min(95, preset["q"] + 8), 0
        seq = 0
        prev = None
        last_key = 0.0
        last_send = time.monotonic()
        win = self._new_window(last_send)
        while not self.closed and cfg.get("screen_share"):
            t0 = time.monotonic()
            frame = backend.grab(timeout=1.0)
            if frame is None:
                if time.monotonic() - last_send > 2.0:
                    self.ch.send(MSG_PING)
                    last_send = time.monotonic()
                continue
            t_grab = time.monotonic()
            area = 1.0
            is_key = False
            if self.delta is not None:
                arr, cs = to_array(frame, preset["max_w"])
                self._last_arr = (arr, cs)
                rects = None if (self.force_key or prev is None) else self.delta.changed_rects(prev, arr)
                prev = arr
                if rects == []:
                    continue                       # no visible change
                if rects is not None and t_grab - last_key > self.delta.KEYFRAME_INTERVAL:
                    rects = None                   # periodic full frame
                if rects is None:
                    w, h, jpeg = self._encode_full(frame, preset)
                    payload_type, payload = MSG_FRAME2, None
                    last_key = t_grab
                    self.force_key = False
                    is_key = True
                else:
                    h, w = arr.shape[:2]
                    parts = [((x, y, rw, rh), self.delta.encode(arr[y:y + rh, x:x + rw], cs, dq, dss))
                             for x, y, rw, rh in rects]
                    area = sum(r[2] * r[3] for r in rects) / float(w * h)
                    payload_type, payload = MSG_FRAME_DELTA, parts
            else:
                w, h, jpeg = self._encode_full(frame, preset)
                payload_type, payload = (MSG_FRAME2 if self.frame2 else MSG_FRAME), None
            seq += 1
            enc_ms = (time.monotonic() - t_grab) * 1000.0
            if payload_type == MSG_FRAME_DELTA:
                data = self.delta.pack_delta(w, h, seq, enc_ms, payload)
            elif payload_type == MSG_FRAME2:
                data = pack_frame2(w, h, seq, enc_ms, jpeg)
            else:
                data = pack_frame(w, h, seq, jpeg)
            self.ch.send(payload_type, data)
            last_send = time.monotonic()
            win = self._account(win, last_send, len(data), enc_ms, area, is_key)
            dt = time.monotonic() - t0
            if dt < interval and not getattr(backend, "self_paced", False):
                time.sleep(interval - dt)


class AudioSession(threading.Thread):
    """The audio channel Windows opens: captures the sound playing on the Mac and sends it as 16-bit PCM."""

    QUEUE_MAX = 50             # ~1 s; if the network stalls, old packets are dropped

    def __init__(self, server: "MirrorwayServer", ch, addr):
        super().__init__(daemon=True, name="mirrorway-audio")
        self.server, self.ch, self.addr = server, ch, addr
        self.closed = False
        self.client_stats: dict = {}
        self._q: "queue.Queue" = queue.Queue(maxsize=self.QUEUE_MAX)
        self.dropped = 0

    def close(self) -> None:
        self.closed = True
        self.ch.close()

    def _on_pcm(self, pcm: bytes, rate: int, channels: int) -> None:
        try:
            self._q.put_nowait((pcm, rate, channels))
        except queue.Full:
            self.dropped += 1

    def _reader(self) -> None:
        try:
            while not self.closed:
                mtype, payload = self.ch.recv()
                if mtype == AU.MSG_AUDIO_STATS:
                    self.client_stats = json.loads(payload.decode("utf-8"))
        except Exception:
            pass
        finally:
            self.close()

    def run(self) -> None:
        srv = self.server
        cap = None
        try:
            if not srv.audio_enabled():
                self.ch.send_json(AU.MSG_AUDIO_INFO, {"error": T("audio is turned off on the Mac")})
                return
            try:
                cap = srv.audio_capture_factory()
                cap.start(self._on_pcm)
            except Exception as exc:
                srv.audio_error = str(exc).splitlines()[0]
                log.warning("Cannot capture the Mac's sound: %s", exc)
                self.ch.send_json(AU.MSG_AUDIO_INFO, {"error": T("cannot capture sound: {error}").format(error=srv.audio_error)})
                return
            srv.audio_error = ""
            srv._audio_started()
            log.info("The Mac's sound is playing on Windows (%s)", getattr(cap, "name", "?"))
            threading.Thread(target=self._reader, daemon=True, name="mirrorway-audio-stats").start()
            fmt, seq = None, 0
            m = {"t": time.monotonic(), "packets": 0, "bytes": 0, "lv_sum": 0.0, "lv_n": 0, "peak": -120.0}
            last_send = time.monotonic()
            while not self.closed:
                try:
                    pcm, rate, ch = self._q.get(timeout=0.5)
                except queue.Empty:
                    pcm = None
                    if time.monotonic() - last_send >= AU.IDLE_EVERY_S:
                        self.ch.send(AU.MSG_AUDIO_IDLE)         # silence: the connection is alive, Windows should wait
                        last_send = time.monotonic()
                        m["idle"] = m.get("idle", 0) + 1
                if pcm is not None:
                    last_send = time.monotonic()
                    if (rate, ch) != fmt:
                        fmt = (rate, ch)
                        self.ch.send_json(AU.MSG_AUDIO_INFO, {"rate": rate, "channels": ch, "format": AU.FORMAT})
                    self.ch.send(AU.MSG_AUDIO, AU.pack_audio(seq, pcm))
                    seq += 1
                    m["packets"] += 1
                    m["bytes"] += len(pcm)
                    if m["packets"] % 5 == 0:                 # level: every 5th packet (CPU)
                        lv = AU.level_dbfs(pcm)
                        m["lv_sum"] += lv
                        m["lv_n"] += 1
                        m["peak"] = max(m["peak"], lv)
                now = time.monotonic()
                if now - m["t"] >= 60.0:
                    span = now - m["t"]
                    cs = self.client_stats
                    if m["packets"] or self.dropped:
                        log.info("Audio (last 1 min): %d packets, %.0f kbit/s, level avg %s (peak %s)%s; "
                                 "Windows queue %s ms, underruns %s, trimmed %s",
                                 m["packets"], m["bytes"] * 8 / 1000.0 / span,
                                 AU.describe_level(m["lv_sum"] / m["lv_n"] if m["lv_n"] else None),
                                 AU.describe_level(m["peak"] if m["lv_n"] else None),
                                 f", dropped on the Mac {self.dropped}" if self.dropped else "",
                                 cs.get("queue_ms", "?"), cs.get("underruns", "?"), cs.get("trims", "?"))
                    else:
                        log.info("Audio (last 1 min): no audio samples from the Mac (silent)")
                    m = {"t": now, "packets": 0, "bytes": 0, "lv_sum": 0.0, "lv_n": 0, "peak": -120.0}
        except Exception as exc:
            if not self.closed:
                log.info("Audio connection closed: %s", exc)
        finally:
            if cap is not None:
                try:
                    cap.stop()
                except Exception:
                    pass
                srv._audio_stopped()
                log.info("Audio streaming stopped")
            self.close()
            srv._on_audio_closed(self)


# ---------------------------------------------------------------------------
def _default_capture_factory(display_index: int, preset: dict):
    from .capture import open_best
    return open_best(display_index, preset["max_w"], preset["fps"],
                     pixel_format=preset.get("pixel_format", "bgra"), display_id=preset.get("display_id"))


def _default_audio_capture():
    from .audio import SCKAudioCapture
    return SCKAudioCapture()


class _MacVolume:
    """Muting the Mac's own output (while its sound plays on Windows). Does nothing outside macOS."""

    def get(self):
        import sys
        if sys.platform != "darwin":
            return None
        from .audio import output_muted
        return output_muted()

    def set(self, muted: bool) -> bool:
        import sys
        if sys.platform != "darwin":
            return False
        from .audio import set_output_muted
        return set_output_muted(muted)


def _default_h264_factory(kind: str, width: int, height: int, fps: int, kbps: int):
    """kind="raw": ScreenCaptureKit pixel buffer -> VideoToolbox (hardware).
    kind="array": numpy/PIL frame -> x264 (software, PyAV)."""
    if kind == "raw":
        from .vtenc import VTEncoder
        return VTEncoder(width, height, fps, kbps)
    from ..h264 import AVEncoder
    return AVEncoder(width, height, fps, kbps)


class MirrorwayServer:
    def __init__(self, cfg, platform, clipboard_backend=None,
                 capture_factory: Optional[Callable] = None, beacon: bool = True,
                 h264_encoder_factory: Optional[Callable] = None, network_probe: Optional[Callable] = None,
                 vd_factory: Optional[Callable] = None, files_dir=None,
                 audio_capture_factory: Optional[Callable] = None, volume=None):
        self.cfg = cfg
        self.platform = platform
        self.router = Router(platform, self, cfg)
        from .extend_mgr import ExtendManager
        self.extend = ExtendManager(self, vd_factory=vd_factory)
        self.router.extend = self.extend
        from ..config import CONFIG_DIR
        from pathlib import Path
        self.files_dir = Path(files_dir) if files_dir else CONFIG_DIR   # incoming files: <files_dir>/incoming
        self.offers = FX.Offers()
        self.file_progress: Optional[FX.Progress] = None
        self.file_event: Optional[dict] = None   # a notification for the UI (the menu bar app picks it up)
        self._put_cancel: Optional[threading.Event] = None
        self.audio_capture_factory = audio_capture_factory or _default_audio_capture
        self.volume = volume or _MacVolume()
        self.audio: Optional[AudioSession] = None
        self.audio_error = ""
        self._muted_by_us = False
        self.capture_factory = capture_factory or _default_capture_factory
        self.h264_encoder_factory = h264_encoder_factory or _default_h264_factory
        from ..netlock import NetworkLock
        self.netlock = NetworkLock(cfg, probe=network_probe)
        self.identity = None                   # the Mac's persistent device key (loaded in start)
        self.identity_error = ""
        self._pairing = None                   # {"code", "until", "attempts"}
        self._dev_lock = threading.Lock()
        self.last_paired: Optional[dict] = None
        self.vt_failed = False                 # True: hardware encoder permanently off (3 failures)
        self._vt_fail_count = 0
        self._vt_block_until = 0.0             # after a temporary failure, the software encoder until this time
        self.capture_nv12 = True               # capture the screen as NV12 on the hardware H.264 path
        self.control: Optional[ControlSession] = None
        self.video: Optional[VideoSession] = None
        self.last_error: str = ""
        self.auth_failures = 0
        self._lock = threading.Lock()
        self._listeners = []
        self._sock: Optional[socket.socket] = None
        self._stop_evt = threading.Event()
        self._beacon = Beacon(machine_name(), int(cfg.get("port"))) if beacon else None
        self.clip: Optional[ClipboardSync] = None
        self._clip_sender = RC.ClipSender(
            send_raw=lambda part: self._send_now(MSG_CLIPDATA, part),
            rich_fn=lambda: self.supports(RC.FEATURE),
            send_text=lambda text: self.send(MSG_CLIPBOARD, text.encode("utf-8")), peer="Windows")
        if clipboard_backend is not None:
            self.clip = ClipboardSync(
                clipboard_backend,
                send_fn=self._clip_sender.send,
                enabled_fn=lambda: bool(self.cfg.get("clipboard")) and self.connected(),
                peer="Windows",
                files_fn=self.offer_files,
            )

    # ---- the "sink" interface for the Router ----------------------------------------
    def connected(self) -> bool:
        c = self.control
        return c is not None and not c.closed

    def send(self, mtype: int, payload: bytes = b"") -> None:
        c = self.control
        if c is not None:
            c.send(mtype, payload)

    def send_json(self, mtype: int, obj) -> None:
        c = self.control
        if c is not None:
            c.send_json(mtype, obj)

    def _send_now(self, mtype: int, payload: bytes) -> None:
        c = self.control
        if c is None:
            raise ConnectionError("Windows is not connected")
        c.send_now(mtype, payload)

    def supports(self, feature: str) -> bool:
        c = self.control
        return c is not None and feature in (c.hello.get("features") or [])

    def _update_auto_scale(self, sess: "ControlSession") -> None:
        """Automatic pointer speed: Windows primary monitor width (pixels) / Mac main display width (points)."""
        try:
            mons = sess.hello.get("monitors") or []
            mac_w = float(self.platform.main_display().w)
            if mons and mac_w > 0:
                win_w = float(mons[0][2])
                self.router.set_auto_scale(win_w / mac_w)
                log.info("Automatic pointer speed: %.2f× (Windows %d px / Mac %d pt)",
                         self.router.auto_scale, win_w, mac_w)
        except Exception:
            log.exception("Could not compute the automatic speed")

    # ---- life cycle -------------------------------------------------------
    def add_listener(self, fn) -> None:
        self._listeners.append(fn)

    def _notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                log.exception("listener error")

    # ---- devices and pairing ---------------------------------------------------------
    PAIRING_SECONDS = 180
    PAIRING_MAX_ATTEMPTS = 5

    def load_identity(self) -> None:
        try:
            self.identity = load_identity(self.cfg)
            self.identity_error = ""
            log.info("Mac device identity: %s", self.identity.fingerprint)
        except KeyUnavailable as exc:
            self.identity, self.identity_error = None, str(exc)
            log.error("%s — paired devices cannot connect; try restarting Mirrorway", exc)
        except Exception as exc:
            self.identity, self.identity_error = None, str(exc)
            log.exception("Could not load the device identity")

    def devices(self) -> list:
        return [dict(d) for d in (self.cfg.get("devices") or []) if isinstance(d, dict) and d.get("pub")]

    def _save_devices(self, devs: list) -> None:
        self.cfg.set("devices", devs)

    def device_by_pub(self, pub: bytes) -> Optional[dict]:
        p = b64e(pub)
        for d in self.devices():
            if d.get("pub") == p:
                return d
        return None

    def is_known(self, pub: bytes) -> bool:
        return self.device_by_pub(pub) is not None

    def add_device(self, name: str, pub: bytes, platform: str = "", pending: bool = False) -> dict:
        with self._dev_lock:
            devs = [d for d in self.devices() if d.get("pub") != b64e(pub)]
            dev = {"id": fingerprint(pub), "name": name, "pub": b64e(pub), "platform": platform,
                   "added": int(time.time()), "last_seen": 0, "pending": bool(pending)}
            devs.append(dev)
            self._save_devices(devs)
        log.info("Device paired: %s (%s)%s", name, dev["id"], " — migration, awaiting verification" if pending else "")
        self._notify()
        return dev

    def remove_device(self, dev_id: str) -> bool:
        with self._dev_lock:
            devs = self.devices()
            keep = [d for d in devs if d.get("id") != dev_id]
            if len(keep) == len(devs):
                return False
            self._save_devices(keep)
        log.info("Device pairing removed: %s", dev_id)
        for sess in (self.control, self.video):        # if connected, drop it at once
            dev = getattr(sess, "device", None) if sess is not None else None
            if dev is not None and dev.get("id") == dev_id:
                sess.close()
        self._notify()
        return True

    def _device_seen(self, device: dict) -> None:
        was_pending = False
        with self._dev_lock:
            devs = self.devices()
            for d in devs:
                if d.get("id") == device.get("id"):
                    d["last_seen"] = int(time.time())
                    was_pending = d.get("pending")
                    d["pending"] = False
            self._save_devices(devs)
        if was_pending:
            log.info("First connection with a device key succeeded: %s (%s)", device.get("name"), device.get("id"))
        # If every device can connect with its key, disable the old long code
        if self.legacy_code() and not any(d.get("pending") for d in self.devices()):
            self.cfg.set("legacy_enabled", False)
            self.cfg.set("pair_code", "")
            log.info("Old pairing code disabled; connections now use device keys")
            self._notify()

    def legacy_code(self) -> Optional[str]:
        """v1 (long code) only for migration: if there is a code and it has not been disabled."""
        code = (self.cfg.get("pair_code") or "").strip()
        if not code or self.cfg.get("legacy_enabled") is False:
            return None
        return code

    def enroll(self, sess, info: dict) -> None:
        """Register the device key of a Windows connected with the old code (automatic migration)."""
        if self.identity is None:
            return
        try:
            pub = b64d(str(info.get("pub", "")))
            if len(pub) != 32:
                raise ValueError("key length")
        except Exception as exc:
            log.warning("Invalid device registration: %s", exc)
            return
        dev = self.add_device(str(info.get("name") or sess.name)[:64], pub, str(info.get("platform") or ""),
                              pending=True)
        sess.send_json(MSG_ENROLL, {"mac_pub": b64e(self.identity.public), "mac_name": machine_name(),
                                    "id": dev["id"]})

    # ---- hardware encoder: after temporary problems use software for a while, then retry -----------
    VT_BLOCK_S = (60.0, 600.0)                 # wait after the 1st and 2nd failure; permanent on the 3rd

    def vt_blocked(self) -> bool:
        return self.vt_failed or time.monotonic() < self._vt_block_until

    def vt_temporarily_blocked(self) -> bool:
        return not self.vt_failed and time.monotonic() < self._vt_block_until

    def note_vt_failure(self) -> None:
        self._vt_fail_count += 1
        if self._vt_fail_count > len(self.VT_BLOCK_S):
            self.vt_failed = True
            log.warning("The hardware encoder failed %d times; the software encoder is used for this session",
                        self._vt_fail_count)
        else:
            wait = self.VT_BLOCK_S[self._vt_fail_count - 1]
            self._vt_block_until = time.monotonic() + wait
            log.info("Retrying the hardware encoder in %d s", int(wait))

    def note_vt_ok(self) -> None:
        if self._vt_fail_count:
            log.info("The hardware encoder works again")
        self._vt_fail_count, self._vt_block_until = 0, 0.0

    def start_pairing(self, seconds: Optional[int] = None) -> str:
        code = new_pairing_code()
        self._pairing = {"code": code, "until": time.monotonic() + (seconds or self.PAIRING_SECONDS), "attempts": 0}
        log.info("Pairing mode opened (%d s)", seconds or self.PAIRING_SECONDS)
        self._notify()
        return code

    def stop_pairing(self) -> None:
        self._pairing = None
        self._notify()

    def pairing_status(self) -> Optional[dict]:
        p = self._pairing
        if p is None:
            return None
        left = p["until"] - time.monotonic()
        if left <= 0:
            self._pairing = None
            return None
        return {"code": p["code"], "display": format_code(p["code"]), "remaining": int(left + 0.999)}

    def _handle_pairing(self, conn, addr) -> None:
        st = self.pairing_status()
        p = self._pairing
        override = None
        if st is None or self.identity is None:
            override = 2
        elif p["attempts"] >= self.PAIRING_MAX_ATTEMPTS:
            override = ST_TOO_MANY
        added = {}

        def register(info):                    # save before confirming to Windows (so it can connect at once)
            added["dev"] = self.add_device(info["name"], info["pub"], info.get("platform", ""))
            self._pairing = None               # the code works once

        try:
            info = server_pair(conn, p["code"] if p else None, self.identity, machine_name(),
                               status_override=override, on_success=register)
        except PairingClosed:
            log.info("%s tried to pair but pairing mode is closed", addr[0])
            self.last_error = T("{ip} tried to pair: pairing is not open on the Mac").format(ip=addr[0])
            conn.close()
            self._notify()
            return
        except AuthError:
            if p is not None:
                p["attempts"] += 1
            self.last_error = T("{ip} entered a wrong pairing code").format(ip=addr[0])
            log.warning("%s (attempt %d)", self.last_error, p["attempts"] if p else 0)
            conn.close()
            self._notify()
            return
        except Exception as exc:
            log.info("Pairing failed (%s): %s", addr[0], exc)
            conn.close()
            if "dev" in added:
                self.last_paired = added["dev"]
                self._notify()
            return
        conn.close()
        dev = added.get("dev") or self.add_device(info["name"], info["pub"], info.get("platform", ""))
        self._pairing = None
        self.last_paired = dev
        self.last_error = ""
        self._notify()

    # ---- home network restriction ---------------------------------------------------------------
    NET_CHECK_S = 10.0

    def _open_listener(self) -> None:
        port = int(self.cfg.get("port"))
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        s.listen(8)
        self._sock = s
        threading.Thread(target=self._accept_loop, args=(s,), daemon=True, name="mirrorway-accept").start()

    def _close_listener(self) -> None:
        s, self._sock = self._sock, None
        if s is None:
            return
        try:
            s.shutdown(socket.SHUT_RDWR)       # wake up a pending accept()
        except OSError:
            pass
        try:
            s.close()
        except OSError:
            pass

    def apply_network(self, changed: bool = True) -> None:
        """Open/close the listener when network permission changes."""
        if not changed:
            return
        if self.netlock.allowed:
            if self._sock is None:
                try:
                    self._open_listener()
                except OSError as exc:
                    log.error("Could not reopen the listener: %s", exc)
                    return
            log.info("Network: %s — accepting connections", self.netlock.describe())
        else:
            self._close_listener()
            self.disconnect_all()
            self.router.force_local()
            log.warning("Network: %s — listener closed, connections dropped", self.netlock.describe())
        self._notify()

    def trust_current_network(self) -> Optional[dict]:
        n = self.netlock.trust_current()
        self.apply_network(True)
        return n

    def _net_loop(self) -> None:
        while not self._stop_evt.wait(self.NET_CHECK_S):
            try:
                self.apply_network(self.netlock.refresh())
            except Exception:
                log.exception("Network check error")

    def start(self) -> None:
        if self.identity is None:
            self.load_identity()
        self._open_listener()                 # fails here if Mirrorway is already running
        self.restore_volume_after_crash()
        self.netlock.refresh()
        if not self.netlock.allowed:
            self._close_listener()
            log.warning("Network: %s — Mirrorway does not accept connections on this network", self.netlock.describe())
        else:
            log.info("Network: %s", self.netlock.describe())
        threading.Thread(target=self._net_loop, daemon=True, name="mirrorway-net").start()
        if self._beacon is not None:
            self._beacon.enabled_fn = lambda: self.netlock.allowed
            self._beacon.start()
        if self.clip is not None:
            self.clip.start()
        threading.Thread(target=self._stats_loop, daemon=True, name="mirrorway-stats").start()
        log.info("Mirrorway %s — server listening: %s:%d", __version__, lan_ips()[0], int(self.cfg.get("port")))
        log.info("Network interfaces: %s", ", ".join(f"{d['name']}={d['ip']} ({d['kind']})" for d in interfaces()))

    def stop(self) -> None:
        self._stop_evt.set()
        try:
            self.router.force_local()
        except Exception:
            pass
        for sess in (self.control, self.video):
            if sess is not None:
                try:
                    sess.close()
                except Exception:
                    pass
        self._close_listener()
        if self._beacon is not None:
            self._beacon.stop()
        if self.clip is not None:
            self.clip.stop()
        a = self.audio
        if a is not None:
            a.close()
            if a.is_alive():
                a.join(3.0)
        self._audio_stopped()
        try:
            self.extend.release("Mirrorway is quitting")
        except Exception:
            log.exception("Could not close the second screen")

    def _accept_loop(self, sock) -> None:
        while not self._stop_evt.is_set():
            try:
                conn, addr = sock.accept()
            except OSError:
                break
            if not self.netlock.allowed:
                conn.close()
                continue
            threading.Thread(target=self._handle_conn, args=(conn, addr), daemon=True,
                             name="mirrorway-handshake").start()

    def _handle_conn(self, conn: socket.socket, addr) -> None:
        tune_socket(conn)
        conn.settimeout(10)
        device = None
        try:
            magic = recv_exact(conn, len(MAGIC2))
            if magic == MAGIC2:
                role = recv_exact(conn, 1)
                if role == ROLE_PAIR:
                    self._handle_pairing(conn, addr)
                    return
                if role not in (ROLE_CONTROL, ROLE_VIDEO, ROLE_FILE, ROLE_AUDIO) or self.identity is None:
                    raise ProtocolError("invalid role or no Mac identity")
                ch, hello, pub = server_accept(conn, self.identity, role, self.is_known)
                device = self.device_by_pub(pub)
            else:
                # v1: with the long pairing code (only to migrate an old Windows client to the new method)
                ch, hello = server_handshake(conn, self.legacy_code(), magic=magic)
        except UnknownDevice as exc:
            self.auth_failures += 1
            self.last_error = T("{ip} tried to connect with an unpaired device").format(ip=addr[0])
            log.warning("%s: %s", self.last_error, exc)
            conn.close()
            self._notify()
            return
        except AuthError:
            self.auth_failures += 1
            self.last_error = (T("{ip} tried to connect with a wrong pairing code").format(ip=addr[0]) if self.legacy_code()
                               else T("{ip} tried to connect with the old pairing code (now disabled)").format(ip=addr[0]))
            log.warning(self.last_error)
            conn.close()
            self._notify()
            return
        except Exception as exc:
            log.info("Handshake failed (%s): %s", addr[0], exc)
            conn.close()
            return
        conn.settimeout(None)
        try:
            ch.send_json(MSG_HELLO, {
                "name": machine_name(), "version": __version__, "protocol": PROTOCOL_VERSION,
                "side": self.cfg.get("side"), "screen_share": bool(self.cfg.get("screen_share")),
                "features": ["devkeys", "pair", RC.FEATURE, EXTEND_FEATURE, FX.FEATURE, FX.FEATURE_PACK, AU.FEATURE,
                             ACTIONS_FEATURE],
                "id": self.identity.fingerprint if self.identity else "",
            })
        except Exception:
            ch.close()
            return
        if device is not None:
            self._device_seen(device)
        if ch.role == ROLE_FILE:
            threading.Thread(target=self._file_session, args=(ch,), daemon=True, name="mirrorway-files").start()
            return
        if ch.role == ROLE_AUDIO:
            asess = AudioSession(self, ch, addr)
            with self._lock:
                old, self.audio = self.audio, asess
            if old is not None:
                old.close()
                if old.is_alive():
                    old.join(3.0)               # let the old capture stop and the Mac's sound come back first
            asess.start()
            self._notify()
            return
        if ch.role == ROLE_CONTROL:
            sess = ControlSession(self, ch, hello, addr)
            sess.device = device
            with self._lock:
                old, self.control = self.control, sess
            if old is not None:
                old.close()
            self.last_error = ""
            sess.start()
            log.info("Windows connected: %s (%s), version %s, %s", sess.name, addr[0], sess.client_version,
                     f"with its device key ({device['id']})" if device else "with the old pairing code")
            self._update_auto_scale(sess)
            self.extend.remember_monitors()
            self.send_audio_state()
            self._offer_update(sess)
            threading.Thread(target=self._arp_fallback, args=(sess,), daemon=True, name="mirrorway-arp").start()
        elif ch.role == ROLE_VIDEO:
            if not self.cfg.get("screen_share"):
                try:
                    ch.send_json(MSG_VIDEO_INFO, {"error": T("screen sharing is turned off on the Mac")})
                finally:
                    ch.close()
                return
            vs = VideoSession(self, ch, addr, hello)
            vs.device = device
            with self._lock:
                old, self.video = self.video, vs
            if old is not None:
                old.close()
                # Do not open the new encoder before the old session's one has closed: with two VideoToolbox
                # sessions opening at once, one may fail (seen while Windows updated and reconnected)
                if old.is_alive() and old is not threading.current_thread():
                    old.join(3.0)
            vs.start()
            log.info("Screen viewer connected: %s", addr[0])
        self._notify()

    def _offer_update(self, sess: ControlSession) -> None:
        """If Windows runs an older version and supports automatic updates, send the new package."""
        if not sess.hello.get("auto_update"):
            if parse_version(sess.client_version) < parse_version(__version__):
                log.info("Windows runs an older version (%s) without automatic updates; "
                         "install the Windows package by hand once", sess.client_version)
            return
        if parse_version(sess.client_version) >= parse_version(__version__):
            return
        try:
            from ..winbundle import build_windows_bundle
            version, data, sha = build_windows_bundle()
        except Exception as exc:
            log.error("Could not prepare the Windows update package: %s", exc)
            return
        log.info("Sending the Windows update: %s -> %s (%d KB)", sess.client_version, version,
                 len(data) // 1024)
        sess.send(MSG_UPDATE, pack_update({"version": version, "sha256": sha, "size": len(data)}, data))

    # ---- Wake-on-LAN ---------------------------------------------------------
    ARP_DELAY = 25.0

    def learn_wol(self, sess: ControlSession, info: dict) -> None:
        """Pick the wake target from the adapters Windows reported, and save it."""
        from ..wol import is_wired, normalize_mac, pick_adapter
        adapters = info.get("adapters") or []
        a = pick_adapter(adapters, sess.addr[0])
        if a is None:
            log.info("Windows network adapter information arrived but without a MAC address")
            return
        target = {
            "mac": normalize_mac(a.get("mac", "")), "ip": sess.addr[0], "name": sess.name,
            "adapter": a.get("name", ""), "desc": a.get("desc", ""), "media": a.get("media", ""),
            "wired": is_wired(a), "wake": a.get("wake", ""), "adv": a.get("adv", ""),
            "armed": bool(a.get("armed")), "prefix": a.get("prefix"),
            "fast_startup": info.get("fast_startup"), "source": "windows", "t": int(time.time()),
        }
        sess.wol_learned = True
        self.cfg.set("wol_target", target)
        log.info("Wake-on-LAN target: %s %s (%s, %s), magic packet=%s, advanced=%s, can wake=%s, "
                 "fast startup=%s", target["name"], target["mac"], target["adapter"],
                 "wired" if target["wired"] else "wireless", target["wake"] or "?", target["adv"] or "?",
                 "yes" if target["armed"] else "no",
                 {True: "on", False: "off"}.get(target["fast_startup"], "?"))
        for w in wol_warnings(target):
            log.info("Wake-on-LAN note: %s", w)
        self._notify()

    def _arp_fallback(self, sess: ControlSession) -> None:
        """If Windows does not send adapter information, take the MAC from the ARP table."""
        if self._stop_evt.wait(self.ARP_DELAY) or sess.closed or getattr(sess, "wol_learned", False):
            return
        from ..wol import arp_lookup
        mac = arp_lookup(sess.addr[0])
        if not mac:
            return
        old = self.cfg.get("wol_target") or {}
        if old.get("mac") == mac and old.get("ip") == sess.addr[0]:
            return
        self.cfg.set("wol_target", {"mac": mac, "ip": sess.addr[0], "name": sess.name, "source": "arp",
                                    "t": int(time.time())})
        log.info("Wake-on-LAN target taken from the ARP table: %s %s", sess.addr[0], mac)
        self._notify()

    def wol_target(self) -> Optional[dict]:
        t = self.cfg.get("wol_target")
        return t if isinstance(t, dict) and t.get("mac") else None

    def wake_windows(self) -> int:
        """Sends the magic packet to the saved Windows PC; returns the number of packets sent (0 = no target)."""
        t = self.wol_target()
        if t is None:
            return 0
        from ..wol import send_wol
        return send_wol(t["mac"], last_ip=t.get("ip"))

    def _stats_loop(self) -> None:
        while not self._stop_evt.wait(60):
            if self.connected():
                log.info("Statistics (last 1 min): %s", self.stats_line(reset=True))

    def stats_line(self, reset: bool = False) -> str:
        st = self.status()
        v = self.video
        parts = []
        if st.get("rtt_ms") is not None:
            parts.append(f"latency {st['rtt_ms']:.1f} ms (round trip)")
        if st["video"] and v is not None:
            m = v.minute_summary(reset=reset)
            if not m["frames"]:                      # no 2 s window has closed yet: current values
                m.update(fps=st["video_fps"], peak_fps=st["video_fps"], kbps=st["video_kbps"],
                         peak_kbps=st["video_kbps"], enc_ms=st["video_enc_ms"], peak_enc=st["video_enc_ms"])
            rate = (f"avg {m['fps']:.0f} fps (peak {m['peak_fps']:.0f}), "
                    f"avg {m['kbps'] / 1024:.2f} MB/s (peak {m['peak_kbps'] / 1024:.2f})")
            if st.get("video_codec") == "h264":
                target = ""
                if st.get("video_target_kbps"):
                    target = (f", target {st['video_target_kbps'] / 1000:.1f} Mbit/s {st['video_target_fps']} fps"
                              + (" (adaptive)" if st.get("video_adaptive") else ""))
                parts.append(f"video H.264 [{st['video_encoder']}] {rate}, encoding {m['enc_ms']:.1f} ms "
                             f"(peak {m['peak_enc']:.1f}), key frames {m['keys']}{target}")
            else:
                parts.append(f"video {rate}, encoding {m['enc_ms']:.0f} ms, changed area {st['video_area_pct']:.0f}%, "
                             f"full frames {m['keys']}")
            cs = m.get("client")
            if cs is None and st.get("client_stats"):
                c = st["client_stats"]
                cs = {"fps": c.get("fps", 0), "peak_fps": c.get("fps", 0), "decode_ms": c.get("decode_ms", 0),
                      "display_ms": c.get("display_ms", 0), "latency_ms": c.get("latency_ms", 0),
                      "peak_latency": c.get("latency_ms", 0), "decoder": c.get("decoder", ""),
                      "renderer": c.get("renderer", "")}
            if cs:
                dec = f" ({cs['decoder']})" if cs.get("decoder") else ""
                ren = f" ({cs['renderer']})" if cs.get("renderer") else " (Tk)"
                parts.append(f"Windows: avg {cs['fps']:.0f} fps shown (peak {cs['peak_fps']:.0f}), "
                             f"decoding {cs['decode_ms']:.1f} ms{dec}, drawing {cs['display_ms']:.1f} ms{ren}, "
                             f"picture latency ≈ {cs['latency_ms']:.0f} ms (peak {cs['peak_latency']:.0f})")
        return "; ".join(parts) or "no data"

    def _on_control_closed(self, sess: ControlSession) -> None:
        with self._lock:
            if self.control is not sess:
                return
            self.control = None
        self.router.force_local()
        log.info("Windows disconnected")
        self._notify()

    def _on_video_closed(self, vs: VideoSession) -> None:
        with self._lock:
            if self.video is vs:
                self.video = None
        self._notify()

    # ---- other -------------------------------------------------------------
    def clipboard_in(self, content) -> None:
        """Clipboard from Windows: text (old) or a bundle of formats."""
        if self.clip is not None:
            self.clip.apply_remote(content)

    # ---- file copy and paste -------------------------------------------------------
    def files_enabled(self) -> bool:
        return bool(self.cfg.get("file_clipboard", True)) and bool(self.cfg.get("clipboard"))

    def files_max(self) -> int:
        return int(self.cfg.get("file_clipboard_max_mb") or 4096) * 1024 * 1024

    def offer_files(self, paths) -> bool:
        """Files were copied on the Mac (from the clipboard sync). True: the transfer took over (no clipboard text)."""
        if any(FX.inside(p, FX.staging_root(self.files_dir)) for p in paths):
            return True                           # files that came from Windows and were put on the clipboard: do not send back
        if not self.files_enabled() or not self.supports(FX.FEATURE):
            return False
        try:
            man = FX.build_manifest(paths)
        except FX.TransferError as exc:
            log.info("Could not offer the copied files to Windows (%s): %s", FX.names_of(paths), exc)
            self.file_event = {"title": T("Files were not sent to Windows"), "msg": f"{FX.names_of(paths)}: {exc}"}
            return True
        if man.total > self.files_max():
            log.info("The copied files are too large, not sent to Windows: %s (limit %s)", man.summary(),
                     FX.human_size(self.files_max()))
            self.file_event = {"title": T("Files were not sent"),
                               "msg": T("{summary} — limit {limit}").format(summary=man.summary(), limit=FX.human_size(self.files_max()))}
            return True
        self.offers.set(man)
        self.send_json(FX.MSG_FILES_OFFER, FX.offer_info(man))
        log.info("Files copied, offered to Windows: %s", man.summary())
        return True

    def _file_progress(self, direction: str, summary: str, total: int) -> FX.Progress:
        prog = FX.Progress(direction, summary, total, on_change=lambda _p: self._notify())
        self.file_progress = prog
        return prog

    def _file_session(self, ch) -> None:
        """The file channel Windows opens: "get" (download what was copied on the Mac) or "put" (send from Windows)."""
        prog = None
        try:
            FX.prepare_file_channel(ch)
            mtype, payload = ch.recv()
            req = json.loads(payload.decode("utf-8")) if mtype == FX.MSG_FILE_REQ else {}
            op = req.get("op")
            if op == "get":
                man = self.offers.get(str(req.get("id") or ""))
                if man is None:
                    ch.send_json(FX.MSG_FILE_RESULT, {"ok": False, "error": T("this copy is no longer current")})
                    return
                prog = self._file_progress("out", man.summary(), man.total)
                stats = FX.send_files(ch, man, prog, packed=bool(req.get("pack")))
                mtype, payload = ch.recv()
                res = json.loads(payload.decode("utf-8")) if mtype == FX.MSG_FILE_RESULT else {}
                if not res.get("ok"):
                    raise FX.TransferError(res.get("error") or T("Windows could not receive the files"))
                prog.finish("done")
                log.info("Files sent to Windows: %s, %.1f MB/s (%s)", man.summary(), prog.speed() / 1048576,
                         stats.text())
            elif op == "put":
                if not self.files_enabled():
                    ch.send_json(FX.MSG_FILE_RESULT, {"ok": False, "error": T("file copy and paste is turned off on the Mac")})
                    return
                if int(req.get("total") or 0) > self.files_max():
                    ch.send_json(FX.MSG_FILE_RESULT, {"ok": False, "error": T("larger than the limit on the Mac")})
                    return
                if self._put_cancel is not None:
                    self._put_cancel.set()           # a new copy: abandon the old half-finished send
                cancel = self._put_cancel = threading.Event()
                ch.send_json(FX.MSG_FILE_RESULT, {"ok": True})
                dest = FX.new_staging(self.files_dir)
                box = {}

                def factory(info):
                    box["p"] = self._file_progress("in", FX.describe(info["roots"], len(info["roots"]),
                                                                     info["total"]), info["total"])
                    return box["p"]
                roots, info = FX.recv_files(ch, dest, windows=False, max_total=self.files_max(),
                                            progress_factory=factory, cancel=cancel)
                prog = box.get("p")
                if self.clip is not None:
                    self.clip.apply_remote({RC.FILES: FX.paths_to_bytes(roots)})
                ch.send_json(FX.MSG_FILE_RESULT, {"ok": True, "count": len(roots)})
                if prog is not None:
                    prog.finish("done")
                summ = FX.describe(info["roots"], len(info["roots"]), info["total"])
                log.info("Files arrived from Windows: %s, %.1f MB/s — ready to paste in Finder (%s)", summ,
                         (prog.speed() if prog else 0) / 1048576, info["stats"].text())
                self.file_event = {"title": T("Files arrived from Windows"),
                                   "msg": T("{summary}. Paste them in Finder with ⌘V.").format(summary=summ)}
            else:
                ch.send_json(FX.MSG_FILE_RESULT, {"ok": False, "error": "unknown request"})
        except Exception as exc:
            if prog is not None:
                prog.finish("failed", str(exc))
            log.info("File transfer interrupted: %s", exc)
        finally:
            ch.close()
            self._notify()

    # ---- audio ---------------------------------------------------------------------------------
    def audio_enabled(self) -> bool:
        return bool(self.cfg.get("audio"))

    def send_audio_state(self) -> None:
        """Tell Windows to open or close the audio channel (on connect and when the menu changes)."""
        if not self.audio_enabled():
            a = self.audio
            if a is not None:
                a.close()
        if self.supports(AU.FEATURE):
            try:
                self.send_json(AU.MSG_AUDIO_STATE, {"on": self.audio_enabled()})
            except Exception:
                pass

    def _audio_started(self) -> None:
        if not self.cfg.get("audio_mute_mac", True) or self._muted_by_us:
            return
        if self.volume.get() is False and self.volume.set(True):
            self._muted_by_us = True
            self.cfg.set("audio_muted_by_app", True)
            log.info("The Mac's sound was muted (it plays on Windows)")

    def _audio_stopped(self) -> None:
        if self._muted_by_us:
            self._muted_by_us = False
            self.volume.set(False)
            self.cfg.set("audio_muted_by_app", False)
            log.info("The Mac's sound was unmuted")

    def audio_mute_changed(self) -> None:
        """The "mute the Mac" menu item changed: apply it at once if audio is streaming."""
        a = self.audio
        if self.cfg.get("audio_mute_mac", True):
            if a is not None and not a.closed:
                self._audio_started()
        else:
            self._audio_stopped()

    def _on_audio_closed(self, sess: "AudioSession") -> None:
        with self._lock:
            if self.audio is sess:
                self.audio = None
        self._notify()

    def restore_volume_after_crash(self) -> None:
        if self.cfg.get("audio_muted_by_app"):
            self.volume.set(False)
            self.cfg.set("audio_muted_by_app", False)
            log.info("The Mac's sound was unmuted (Mirrorway had muted it during its previous run)")

    def action_from_windows(self, req: dict) -> None:
        """The Windows panel changed a setting that lives on the Mac (only the audio transfer for now)."""
        if isinstance(req, dict) and isinstance(req.get("audio"), bool):
            if bool(self.cfg.get("audio")) != req["audio"]:
                log.info("Audio transfer turned %s from Windows", "on" if req["audio"] else "off")
                self.cfg.set("audio", req["audio"])
                threading.Thread(target=self.send_audio_state, daemon=True, name="mirrorway-audio-state").start()
            else:
                self.send_audio_state()          # tell Windows the current state anyway

    def request_viewer(self, show: bool = True) -> bool:
        """Open (or close) the screen window on Windows: for the second screen and the panel's Screen tile.
        Windows versions before 3.1 only open it."""
        if not self.supports(EXTEND_FEATURE):
            return False
        try:
            self.send_json(MSG_VIEW, {"open": bool(show)})
            return True
        except Exception:
            return False

    def _files_status(self) -> Optional[dict]:
        p = self.file_progress
        if p is None:
            return None
        return {"dir": p.direction, "summary": p.summary, "done": p.done, "total": p.total, "state": p.state,
                "error": p.error, "percent": p.percent(), "age": time.monotonic() - (p.finished or time.monotonic())}

    def restart_video(self) -> None:
        """When quality/display changes: the viewer reconnects by itself."""
        v = self.video
        if v is not None:
            v.close()

    def disconnect_all(self) -> None:
        for sess in (self.control, self.video):
            if sess is not None:
                sess.close()

    def status(self) -> dict:
        c, v = self.control, self.video
        return {
            "connected": self.connected(),
            "client_name": c.name if c else "",
            "client_ip": c.addr[0] if c else "",
            "remote": self.router.remote,
            "video": v is not None and not v.closed,
            "video_backend": v.backend_name if v else "",
            "video_fps": v.fps_actual if v else 0.0,
            "video_kbps": v.kbps if v else 0.0,
            "video_enc_ms": v.enc_ms if v else 0.0,
            "video_area_pct": v.area_pct if v else 0.0,
            "video_keyframes": v.keyframes if v else 0,
            "video_delta": bool(v and v.delta is not None),
            "video_codec": v.codec if v else "",
            "video_target_kbps": v.target_kbps if v else 0,
            "video_target_fps": v.target_fps if v else 0,
            "video_adaptive": bool(v and v.adaptive),
            "video_encoder": v.encoder_name if v else "",
            "client_stats": dict(v.client_stats) if v else {},
            "client_version": c.client_version if c else "",
            "rtt_ms": c.rtt_avg_ms if c else None,
            "auto_scale": self.router.auto_scale,
            "effective_speed": self.router.effective_speed(),
            "ip": lan_ips()[0],
            "all_ips": lan_ips(),
            "vpn": vpn_active(),
            "port": int(self.cfg.get("port")),
            "error": self.last_error,
            "wol": self.wol_target(),
            "network": {"state": self.netlock.state, "allowed": self.netlock.allowed,
                        "text": self.netlock.describe(), "enabled": self.netlock.enabled(),
                        "detail": list(self.netlock.detail), "weak": self.netlock.weak()},
            "devices": self.devices(),
            "pairing": self.pairing_status(),
            "legacy": bool(self.legacy_code()),
            "identity": self.identity.fingerprint if self.identity else "",
            "identity_error": self.identity_error,
            "client_device": (c.device or {}).get("id", "") if c else "",
            "extend": self.extend.status(),
            "files": self._files_status(),
            "audio": {"enabled": self.audio_enabled(), "active": self.audio is not None and not self.audio.closed,
                      "error": self.audio_error, "supported": self.supports(AU.FEATURE)},
            "extend_supported": self.extend.supported(),
        }


def wol_warnings(t: dict) -> list:
    """Short notes about Windows settings that may keep waking from working."""
    out = []
    if t.get("source") == "arp":
        return out
    if not t.get("wired"):
        out.append(T("Windows is connected over Wi-Fi; Wake-on-LAN does not work with most Wi-Fi adapters (an Ethernet cable is recommended)"))
    if str(t.get("wake", "")).lower() == "disabled":
        out.append(T("Device Manager > network adapter > Power Management: 'Only allow a magic packet to wake "
                     "the computer' is off"))
    if str(t.get("adv", "")).lower() in ("disabled", "devre dışı", "kapalı"):     # as reported by English/Turkish Windows
        out.append(T("Network adapter > Advanced tab: 'Wake on Magic Packet' is off"))
    if t.get("wake") and not t.get("armed"):
        out.append(T("The adapter's 'Allow this device to wake the computer' setting is off"))
    if t.get("fast_startup"):
        out.append(T("Fast startup is on; waking may not work after a shutdown (it works from sleep)"))
    return out
