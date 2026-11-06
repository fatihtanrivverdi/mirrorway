"""Playing the Mac's sound on Windows: the audio channel (ROLE_AUDIO) + the SDL2 audio queue.

When the Mac sends MSG_AUDIO_STATE {"on": true} on the control channel, AudioClient opens the audio connection; it closes it on "off".
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
from ..protocol import ROLE_AUDIO, tune_socket

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


class MacAudioError(RuntimeError):
    """The Mac cannot provide sound (off, no permission…): retrying is pointless, wait for a new "on" from the Mac."""


class AudioClient:
    STATS_INTERVAL = 5.0

    def __init__(self, control, player_factory: Callable[[int, int], object] = SDLAudioPlayer):
        self.control = control                  # ControlClient: creds, file_target
        self.player_factory = player_factory
        self._lock = threading.Lock()
        self._stop: Optional[threading.Event] = None
        self._ch = None
        self.packets = 0
        self.policy: Optional[A.QueuePolicy] = None
        self.last_error = ""

    @property
    def running(self) -> bool:
        return self._stop is not None and not self._stop.is_set()

    def set_state(self, on: bool) -> None:
        with self._lock:
            if on and not self.running:
                self._stop = threading.Event()
                threading.Thread(target=self._run, args=(self._stop,), daemon=True, name="mirrorway-audio").start()
            elif not on and self.running:
                self._stop.set()
                ch = self._ch
                if ch is not None:
                    ch.close()

    def stop(self) -> None:
        self.set_state(False)

    def _connect(self):
        from .client import open_channel
        target = self.control.file_target
        if target is None or not self.control.creds.paired():
            raise ConnectionError("not connected to the Mac")
        sock = socket.create_connection(target, timeout=10)
        try:
            tune_socket(sock)
            ch, _hello, mode = open_channel(self.control.creds, sock, ROLE_AUDIO,
                                            {"name": socket.gethostname(), "version": __version__})
        except Exception:
            sock.close()
            raise
        if mode != "key":
            ch.close()
            raise ConnectionError("audio needs a device key")
        ch.sock.settimeout(15)
        return ch

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            player = None
            try:
                ch = self._ch = self._connect()
                log.info("Playing the Mac's sound")
                fmt = None
                last_stats = time.monotonic()
                qsum = qn = 0
                while not stop.is_set():
                    mtype, payload = ch.recv()
                    if mtype == A.MSG_AUDIO_INFO:
                        info = json.loads(payload.decode("utf-8"))
                        if info.get("error"):
                            raise MacAudioError("Mac: " + str(info["error"]))
                        new = (int(info.get("rate") or A.RATE), int(info.get("channels") or A.CHANNELS))
                        if new != fmt:
                            if player is not None:
                                player.close()
                            player = self.player_factory(*new)
                            self.policy = A.QueuePolicy(*new)
                            fmt = new
                            log.info("Audio device opened: %d Hz, %d channels (%s)", new[0], new[1],
                                     getattr(player, "driver", "?"))
                    elif mtype == A.MSG_AUDIO_IDLE:
                        if player is not None and self.policy is not None and player.queued() == 0:
                            self.policy.idle()          # the Mac is silent: not an underrun; collect again when sound returns
                            player.play(False)
                    elif mtype == A.MSG_AUDIO and player is not None:
                        _seq, pcm = A.unpack_audio(payload)
                        queued = player.queued()
                        act = self.policy.on_packet(queued, len(pcm))
                        if act == "trim":
                            player.clear()
                        elif act == "rebuffer":
                            player.play(False)
                        player.queue(pcm)
                        if act in ("start", "trim"):
                            player.play(True)
                        self.packets += 1
                        qsum += queued
                        qn += 1
                    now = time.monotonic()
                    if now - last_stats >= self.STATS_INTERVAL and self.policy is not None:
                        last_stats = now
                        try:
                            ch.send_json(A.MSG_AUDIO_STATS, {
                                "queue_ms": round(self.policy.ms(qsum / qn), 1) if qn else 0.0,
                                "underruns": self.policy.underruns, "trims": self.policy.trims})
                        except Exception:
                            pass
                        qsum = qn = 0
            except MacAudioError as exc:
                self.last_error = str(exc)
                log.info("Cannot play sound — %s", exc)
                stop.set()                              # retried when a new MSG_AUDIO_STATE arrives from the Mac
            except Exception as exc:
                if not stop.is_set():
                    self.last_error = str(exc)
                    log.info("Audio connection dropped: %s", exc)
            finally:
                ch, self._ch = self._ch, None
                if ch is not None:
                    ch.close()
                if player is not None:
                    try:
                        player.close()
                    except Exception:
                        pass
            stop.wait(3)
