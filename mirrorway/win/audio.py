"""Sound between Windows and the Mac, Windows' side.

AudioClient: plays the Mac's sound here (ROLE_AUDIO + the SDL2 audio queue). When the Mac sends MSG_AUDIO_STATE
{"on": true} on the control channel it opens the audio connection; it closes it on "off".
WinAudioSender (3.4): sends Windows' sound to the Mac (ROLE_WINAUDIO + WASAPI loopback) while the Mac's choice is
"all sound on the Mac" (MSG_AUDIO_STATE {"capture": true}); Windows' speakers are muted meanwhile.
"""
from __future__ import annotations

import ctypes
import json
import logging
import socket
import threading
import time
from typing import Callable, Optional

from .. import __version__
from .. import audio as A
from ..protocol import ROLE_AUDIO, ROLE_WINAUDIO, tune_socket

log = logging.getLogger("mirrorway.audio")


class SDLAudioPlayer:
    """SDL2 audio device (default output), queue based. 16-bit interleaved PCM only."""

    def __init__(self, rate: int = A.RATE, channels: int = A.CHANNELS):
        import sdl2
        self.sdl = sdl2
        if sdl2.SDL_InitSubSystem(sdl2.SDL_INIT_AUDIO) != 0:
            raise RuntimeError("SDL audio: " + sdl2.SDL_GetError().decode("utf-8", "replace"))
        want, have = sdl2.SDL_AudioSpec(rate, sdl2.AUDIO_S16LSB, channels, 480), sdl2.SDL_AudioSpec(0, 0, 0, 0)
        self.dev = sdl2.SDL_OpenAudioDevice(None, 0, ctypes.byref(want), ctypes.byref(have), 0)
        if not self.dev:
            err = sdl2.SDL_GetError().decode("utf-8", "replace")
            sdl2.SDL_QuitSubSystem(sdl2.SDL_INIT_AUDIO)
            raise RuntimeError("Could not open the SDL audio device: " + err)
        self.rate, self.channels = rate, channels
        name = sdl2.SDL_GetCurrentAudioDriver()
        self.driver = name.decode("ascii", "replace") if name else "?"

    def queued(self) -> int:
        return int(self.sdl.SDL_GetQueuedAudioSize(self.dev))

    def queue(self, pcm: bytes) -> None:
        self.sdl.SDL_QueueAudio(self.dev, pcm, len(pcm))

    def clear(self) -> None:
        self.sdl.SDL_ClearQueuedAudio(self.dev)

    def play(self, on: bool = True) -> None:
        self.sdl.SDL_PauseAudioDevice(self.dev, 0 if on else 1)

    def close(self) -> None:
        if self.dev:
            self.sdl.SDL_CloseAudioDevice(self.dev)
            self.dev = 0
            self.sdl.SDL_QuitSubSystem(self.sdl.SDL_INIT_AUDIO)


MacAudioError = A.SourceError       # the Mac cannot provide sound: retrying is pointless until it says "on" again


def open_audio_channel(control, role: bytes):
    """An encrypted channel to the Mac with the device key (audio needs one)."""
    from .client import open_channel
    target = control.file_target
    if target is None or not control.creds.paired():
        raise ConnectionError("not connected to the Mac")
    sock = socket.create_connection(target, timeout=10)
    try:
        tune_socket(sock)
        ch, _hello, mode = open_channel(control.creds, sock, role,
                                        {"name": socket.gethostname(), "version": __version__})
    except Exception:
        sock.close()
        raise
    if mode != "key":
        ch.close()
        raise ConnectionError("audio needs a device key")
    ch.sock.settimeout(15)
    return ch


class _Switch:
    """A worker thread turned on and off by the Mac's MSG_AUDIO_STATE."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stop: Optional[threading.Event] = None
        self._ch = None
        self.last_error = ""

    @property
    def running(self) -> bool:
        return self._stop is not None and not self._stop.is_set()

    def set_state(self, on: bool) -> None:
        with self._lock:
            if on and not self.running:
                self._stop = threading.Event()
                threading.Thread(target=self._run, args=(self._stop,), daemon=True, name=self.THREAD).start()
            elif not on and self.running:
                self._stop.set()
                ch = self._ch
                if ch is not None:
                    ch.close()

    def stop(self) -> None:
        self.set_state(False)


class AudioClient(_Switch):
    """Plays the Mac's sound."""
    STATS_INTERVAL = 5.0
    THREAD = "mirrorway-audio"

    def __init__(self, control, player_factory: Callable[[int, int], object] = SDLAudioPlayer):
        super().__init__()
        self.control = control                  # ControlClient: creds, file_target
        self.player_factory = player_factory
        self.rx: Optional[A.Receiver] = None

    @property
    def packets(self) -> int:
        return self.rx.packets if self.rx is not None else 0

    @property
    def policy(self) -> Optional[A.QueuePolicy]:
        return self.rx.policy if self.rx is not None else None

    def _connect(self):
        return open_audio_channel(self.control, ROLE_AUDIO)

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            rx = None
            try:
                ch = self._ch = self._connect()
                log.info("Playing the Mac's sound")
                rx = self.rx = A.Receiver(self.player_factory, on_open=lambda r, c, p: log.info(
                    "Audio device opened: %d Hz, %d channels (%s)", r, c, getattr(p, "driver", "?")))
                last_stats = time.monotonic()
                while not stop.is_set():
                    mtype, payload = ch.recv()
                    rx.handle(mtype, payload)
                    now = time.monotonic()
                    if now - last_stats >= self.STATS_INTERVAL and rx.policy is not None:
                        last_stats = now
                        try:
                            ch.send_json(A.MSG_AUDIO_STATS, rx.stats())
                        except Exception:
                            pass
            except A.SourceError as exc:
                self.last_error = "Mac: " + str(exc)
                log.info("Cannot play sound — %s", self.last_error)
                stop.set()                              # retried when a new MSG_AUDIO_STATE arrives from the Mac
            except Exception as exc:
                if not stop.is_set():
                    self.last_error = str(exc)
                    log.info("Audio connection dropped: %s", exc)
            finally:
                ch, self._ch = self._ch, None
                if ch is not None:
                    ch.close()
                if rx is not None:
                    rx.close()
            stop.wait(3)


def default_capture():
    from .loopback import WasapiLoopback
    return WasapiLoopback("auto")


def default_speakers():
    from .loopback import SpeakerMute
    return SpeakerMute()


class WinAudioSender(_Switch):
    """Sends Windows' sound to the Mac (3.4) and mutes Windows' speakers meanwhile (restored afterwards; also after
    a crash, see restore_speakers)."""
    THREAD = "mirrorway-winaudio"
    QUEUE_MAX = 100                 # ~1 s of 10 ms packets; if the network stalls, old ones are dropped
    SILENCE_TAIL_S = 0.3            # silent packets are still sent this long after sound (short gaps stay smooth)
    LOG_EVERY_S = 60.0

    def __init__(self, control, capture_factory: Callable[[], object] = default_capture,
                 speakers=None, cfg=None):
        super().__init__()
        self.control = control
        self.capture_factory = capture_factory
        self.speakers = speakers
        self.cfg = cfg                  # Windows' settings: "audio_muted_by_app" survives a crash
        self.packets = 0
        self.dropped = 0
        self.mac_stats: dict = {}
        self.source = ""                # the capture's name while sending
        self._muted_by_us = False

    def _connect(self):
        return open_audio_channel(self.control, ROLE_WINAUDIO)

    def _speakers(self):
        if self.speakers is None:
            self.speakers = default_speakers()
        return self.speakers

    def _mute(self) -> None:
        if self._muted_by_us:
            return
        sp = self._speakers()
        if sp.get() is False and sp.set(True):
            self._muted_by_us = True
            if self.cfg is not None:
                self.cfg.set("audio_muted_by_app", True)
            log.info("Windows' speakers were muted (its sound plays on the Mac)")

    def _unmute(self) -> None:
        if not self._muted_by_us:
            return
        self._muted_by_us = False
        self._speakers().set(False)
        if self.cfg is not None:
            self.cfg.set("audio_muted_by_app", False)
        log.info("Windows' speakers were unmuted")

    def restore_speakers(self) -> None:
        """At start: if Mirrorway muted the speakers and then stopped without unmuting, unmute them."""
        if self.cfg is not None and self.cfg.get("audio_muted_by_app"):
            self._speakers().set(False)
            self.cfg.set("audio_muted_by_app", False)
            log.info("Windows' speakers were unmuted (Mirrorway had muted them before it stopped)")

    def _reader(self, ch, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                mtype, payload = ch.recv()
                if mtype == A.MSG_AUDIO_STATS:
                    self.mac_stats = json.loads(payload.decode("utf-8"))
                elif mtype == A.MSG_AUDIO_INFO:
                    info = json.loads(payload.decode("utf-8"))
                    if info.get("error"):
                        self.last_error = "Mac: " + str(info["error"])
                        log.info("The Mac does not take Windows' sound — %s", info["error"])
                        stop.set()
                        break
        except Exception:
            pass
        finally:
            ch.close()

    def _run(self, stop: threading.Event) -> None:
        import queue as _queue
        while not stop.is_set():
            cap = None
            q: "_queue.Queue" = _queue.Queue(maxsize=self.QUEUE_MAX)

            def on_pcm(pcm: bytes, silent: bool) -> None:
                try:
                    q.put_nowait((pcm, silent))
                except _queue.Full:
                    self.dropped += 1
            reader = None
            try:
                ch = self._ch = self._connect()
                self.last_error = ""
                reader = threading.Thread(target=self._reader, args=(ch, stop), daemon=True,
                                          name="mirrorway-winaudio-stats")
                reader.start()                          # first, so a refusal from the Mac is read
                cap = self.capture_factory()
                cap.start(on_pcm)
                self.source = getattr(cap, "name", "?")
                ch.send_json(A.MSG_AUDIO_INFO, {"rate": cap.rate, "channels": cap.channels, "format": A.FORMAT,
                                                "source": self.source})
                if getattr(cap, "mutes_ok", False):
                    self._mute()
                else:
                    log.info("Windows' speakers stay on (this Windows can only capture what the speakers play)")
                log.info("Windows' sound is playing on the Mac (%s)", self.source)
                self._send_loop(ch, cap, q, stop)
            except Exception as exc:
                if reader is not None and reader is not threading.current_thread():
                    reader.join(1.0)                    # the Mac may have said why it closed the channel
                if not stop.is_set():
                    self.last_error = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                    log.info("Windows' sound to the Mac stopped: %s", self.last_error)
            finally:
                if cap is not None:
                    try:
                        cap.stop()
                    except Exception:
                        pass
                self.source = ""
                ch, self._ch = self._ch, None
                if ch is not None:
                    ch.close()
                if not stop.is_set() and self._muted_by_us:
                    pass                                    # retrying: keep the speakers muted meanwhile
                else:
                    self._unmute()
            stop.wait(3)
        self._unmute()

    def _send_loop(self, ch, cap, q, stop: threading.Event) -> None:
        import queue as _queue
        seq = 0
        last_sent = last_sound = 0.0
        minute = {"t": time.monotonic(), "packets": 0, "bytes": 0, "silent": 0}
        meter = A.LevelMeter()
        prev_dropped = self.dropped
        while not stop.is_set():
            try:
                pcm, silent = q.get(timeout=0.5)
            except _queue.Empty:
                pcm = None
                err = getattr(cap, "error", None)
                if err is not None:                     # the capture stopped (e.g. the speakers were unplugged)
                    raise err
            now = time.monotonic()
            if pcm is not None:
                quiet = silent or not pcm.strip(b"\0")
                if not quiet:
                    last_sound = now
                if not quiet or now - last_sound < self.SILENCE_TAIL_S:
                    ch.send(A.MSG_AUDIO, A.pack_audio(seq, pcm))
                    seq += 1
                    last_sent = now
                    self.packets += 1
                    minute["packets"] += 1
                    minute["bytes"] += len(pcm)
                    meter.add(pcm)
                else:
                    minute["silent"] += 1
            if now - last_sent >= A.IDLE_EVERY_S:
                ch.send(A.MSG_AUDIO_IDLE)               # silence: the connection is alive, the Mac should wait
                last_sent = now
            if now - minute["t"] >= self.LOG_EVERY_S:
                span = now - minute["t"]
                ms = self.mac_stats
                dropped = self.dropped - prev_dropped
                if minute["packets"] or dropped:
                    log.info("Sound to the Mac (last 1 min): %d packets, %.0f kbit/s, %s%s; Mac queue %s ms, "
                             "underruns %s", minute["packets"], minute["bytes"] * 8 / 1000.0 / span,
                             meter.describe(), f", dropped {dropped}" if dropped else "", ms.get("queue_ms", "?"),
                             ms.get("underruns", "?"))
                else:
                    log.info("Sound to the Mac (last 1 min): silent")
                prev_dropped = self.dropped
                minute = {"t": now, "packets": 0, "bytes": 0, "silent": 0}
                meter.reset()
