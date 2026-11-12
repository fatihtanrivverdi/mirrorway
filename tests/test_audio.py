"""Audio transfer: sound playing on the Mac plays on Windows (ScreenCaptureKit -> ROLE_AUDIO -> SDL2 queue)."""
import json
import math
import queue
import struct
import threading
import time

import numpy as np
from conftest import DictCfg

from mirrorway import audio as A

BPM = A.bytes_per_ms()


def _sine(ms=10, freq=440.0, amp=0.5, start=0):
    n = int(A.RATE * ms / 1000)
    t = (np.arange(n) + start) / A.RATE
    s = (np.sin(2 * math.pi * freq * t) * amp * 32767).astype("<i2")
    return np.repeat(s[:, None], 2, axis=1).tobytes()


# ---- conversion and level ---------------------------------------------------------------------------
def test_planar_float_becomes_interleaved_s16():
    left = np.array([0.0, 0.5, -0.5, 1.5], dtype=np.float32)          # 1.5 is clipped
    right = np.array([0.25, -1.0, 0.0, -2.0], dtype=np.float32)
    pcm = A.to_s16(np.concatenate([left, right]).tobytes(), 4, 2, is_float=True, planar=True)
    out = np.frombuffer(pcm, dtype="<i2").reshape(4, 2)
    assert out[:, 0].tolist() == [0, 16383, -16383, 32767]
    assert out[:, 1].tolist() == [8191, -32767, 0, -32767]


def test_other_layouts_and_short_data():
    mono16 = np.array([100, -200, 300], dtype="<i2").tobytes()
    out = np.frombuffer(A.to_s16(mono16, 3, 1, is_float=False, planar=False, bits=16), dtype="<i2")
    assert out.tolist() == [100, 100, -200, -200, 300, 300]               # mono to stereo
    inter32 = np.array([1 << 16, -(2 << 16), 3 << 16, 4 << 16], dtype="<i4").tobytes()
    out = np.frombuffer(A.to_s16(inter32, 2, 2, is_float=False, planar=False, bits=32), dtype="<i2")
    assert out.tolist() == [1, -2, 3, 4]
    six = np.zeros(6 * 2, dtype=np.float32).tobytes()                     # 5.1 -> the first two channels
    assert len(A.to_s16(six, 2, 6, is_float=True, planar=False)) == 2 * 2 * 2
    assert A.to_s16(b"\0" * 8, 100, 2, is_float=True, planar=False) == b"\0" * 4   # missing data: what is there
    assert A.to_s16(b"", 0, 2, True, True) == b""


def test_level_and_packets():
    assert A.level_dbfs(b"") == -120.0 and A.level_dbfs(b"\0" * 400) == -120.0
    assert -10.0 < A.level_dbfs(_sine(amp=0.5)) < -8.0                    # a sine of amplitude 0.5 ≈ -9 dBFS
    assert A.describe_level(-120.0) == "silent" and A.describe_level(-23.4) == "-23 dBFS"
    assert A.describe_level(None) == "?"
    seq, pcm = A.unpack_audio(A.pack_audio(2 ** 32 + 7, b"abc"))
    assert seq == 7 and pcm == b"abc"


def test_queue_policy_prebuffers_rebuffers_and_trims():
    p = A.QueuePolicy()
    pkt = int(10 * BPM)
    assert p.on_packet(0, pkt) == "queue"                                 # 10 ms < 40 ms prebuffer
    assert p.on_packet(3 * pkt, pkt) == "start"
    assert p.on_packet(4 * pkt, pkt) == "queue"
    assert p.on_packet(0, pkt) == "rebuffer" and p.underruns == 1        # the queue had run dry
    assert p.on_packet(pkt, pkt) == "queue"
    assert p.on_packet(3 * pkt, pkt) == "start"
    assert p.on_packet(18 * pkt, pkt) == "trim" and p.trims == 1          # 190 ms > 180 ms limit
    assert p.playing
    p.idle()                                                              # the Mac is silent: not an underrun
    assert p.on_packet(0, pkt) == "queue" and p.underruns == 1
    assert round(p.ms(int(60 * BPM))) == 60


# ---- Windows side: AudioClient (with a fake channel and player) -----------------------------------------
class FakePlayer:
    """Stands in for the SDL audio queue: while playing, the queue drains in real time."""
    instances = []

    def __init__(self, rate, channels):
        self.rate, self.channels = rate, channels
        self.buf = 0.0
        self.total = 0
        self.playing = False
        self.t = time.monotonic()
        self.closed = False
        self.plays = []
        self.driver = "fake"
        FakePlayer.instances.append(self)

    def _drain(self):
        now = time.monotonic()
        if self.playing:
            self.buf = max(0.0, self.buf - (now - self.t) * A.bytes_per_ms(self.rate, self.channels) * 1000)
        self.t = now

    def queued(self):
        self._drain()
        return int(self.buf)

    def queue(self, pcm):
        self._drain()
        self.buf += len(pcm)
        self.total += len(pcm)

    def clear(self):
        self._drain()
        self.buf = 0.0

    def play(self, on=True):
        self._drain()
        self.playing = on
        self.plays.append(on)

    def close(self):
        self.closed = True


class FakeChannel:
    def __init__(self, msgs):
        self.q = queue.Queue()
        for m in msgs:
            self.q.put(m)
        self.sent = []
        self.closed = False

    def recv(self):
        while not self.closed:
            try:
                return self.q.get(timeout=0.05)
            except queue.Empty:
                pass
        raise ConnectionError("closed")

    def send_json(self, mtype, obj):
        self.sent.append((mtype, obj))

    def close(self):
        self.closed = True


def test_audio_client_plays_and_treats_idle_as_silence_not_underrun():
    from mirrorway.win.audio import AudioClient
    FakePlayer.instances.clear()
    info = (A.MSG_AUDIO_INFO, json.dumps({"rate": 48000, "channels": 2, "format": "s16le"}).encode())
    pkts = [(A.MSG_AUDIO, A.pack_audio(i, _sine(start=i * 480))) for i in range(5)]
    ch = FakeChannel([info] + pkts)
    c = AudioClient(control=None, player_factory=FakePlayer)
    c._connect = lambda: ch
    c.STATS_INTERVAL = 0.0
    c.set_state(True)
    try:
        deadline = time.time() + 5
        while c.packets < 5 and time.time() < deadline:
            time.sleep(0.01)
        (pl,) = FakePlayer.instances
        assert c.packets == 5 and pl.total == 5 * len(_sine())
        assert pl.plays and pl.plays[0] is True                           # started once 40 ms had built up
        time.sleep(0.1)                                                   # the queue drained (the Mac went quiet)
        ch.q.put((A.MSG_AUDIO_IDLE, b""))
        ch.q.put((A.MSG_AUDIO, A.pack_audio(9, _sine())))
        deadline = time.time() + 5
        while c.packets < 6 and time.time() < deadline:
            time.sleep(0.01)
        assert pl.plays[-1] is False                                      # buffering again
        assert c.policy.underruns == 0
        assert any(m == A.MSG_AUDIO_STATS and "queue_ms" in o for m, o in ch.sent)
    finally:
        c.stop()
    deadline = time.time() + 3
    while not pl.closed and time.time() < deadline:
        time.sleep(0.01)
    assert pl.closed and ch.closed and not c.running


def test_audio_client_reports_mac_error():
    from mirrorway.win.audio import AudioClient
    ch = FakeChannel([(A.MSG_AUDIO_INFO, json.dumps({"error": "cannot capture audio: no permission"}).encode())])
    c = AudioClient(control=None, player_factory=FakePlayer)
    c._connect = lambda: ch
    c.set_state(True)
    try:
        deadline = time.time() + 3
        while not c.last_error and time.time() < deadline:
            time.sleep(0.01)
        assert "no permission" in c.last_error
        deadline = time.time() + 3
        while c.running and time.time() < deadline:
            time.sleep(0.01)
        assert not c.running                                              # does not retry in vain
    finally:
        c.stop()


# ---- Mac side: muting the speakers ------------------------------------------------------------------------
def test_mac_mute_uses_osascript():
    from mirrorway.mac import audio as MA
    calls = []

    class R:
        def __init__(self, out, code=0):
            self.stdout, self.returncode = out, code

    def run(cmd, **kw):
        calls.append(cmd)
        return R("false\n")
    assert MA.output_muted(run) is False
    assert MA.set_output_muted(True, run) is True
    assert calls[0] == ["/usr/bin/osascript", "-e", "output muted of (get volume settings)"]
    assert calls[1] == ["/usr/bin/osascript", "-e", "set volume output muted true"]
    assert MA.output_muted(lambda *a, **k: R("missing value")) is None

    def boom(*a, **k):
        raise OSError("missing")
    assert MA.output_muted(boom) is None and MA.set_output_muted(False, boom) is False


class FakeVolume:
    def __init__(self, muted=False):
        self.muted = muted
        self.calls = []

    def get(self):
        return self.muted

    def set(self, muted):
        self.calls.append(muted)
        self.muted = muted
        return True


class FakeCapture:
    name = "fake"

    def __init__(self, fail=None):
        self.fail = fail
        self.stop_ev = threading.Event()
        self.started = False
        self.apps = None
        self.set_calls = []
        self.refreshes = 0

    def set_apps(self, apps):
        self.apps = apps
        self.set_calls.append(apps)

    def refresh(self, force=False):
        self.refreshes += 1
        return False

    def start(self, on_pcm):
        if self.fail:
            raise RuntimeError(self.fail)
        self.started = True

        def run():
            i = 0
            while not self.stop_ev.wait(0.01):
                on_pcm(_sine(start=i * 480), A.RATE, A.CHANNELS)
                i += 1
        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        self.stop_ev.set()


def test_volume_restored_after_crash():
    from mirrorway.mac.server import MirrorwayServer
    from test_router_remote import FakeMac, mac_cfg
    vol = FakeVolume(muted=True)
    srv = MirrorwayServer(mac_cfg(audio_muted_by_app=True), FakeMac(), beacon=False, volume=vol)
    srv.restore_volume_after_crash()
    assert vol.calls == [False] and srv.cfg.get("audio_muted_by_app") is False
    srv.restore_volume_after_crash()
    assert vol.calls == [False]                                           # once


def test_end_to_end_audio_on_off_and_mute(tmp_path):
    from mirrorway.geometry import Rect
    from mirrorway.mac.server import MirrorwayServer
    from mirrorway.win.client import ControlClient, Resolver
    from mirrorway.win.remote import RemoteControl
    from test_e2e import free_port, wait
    from test_router_remote import FakeMac, FakeWin, mac_cfg

    FakePlayer.instances.clear()
    port = free_port()
    caps, vol = [], FakeVolume()
    fail = {"msg": None}

    def cap_factory():
        caps.append(FakeCapture(fail["msg"]))
        return caps[-1]
    srv = MirrorwayServer(mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", audio=False), FakeMac(), beacon=False,
                      audio_capture_factory=cap_factory, volume=vol)
    srv.start()
    wcfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN")
    c = ControlClient(wcfg, Resolver(wcfg, None), RemoteControl(FakeWin(), lambda m, o: None),
                      netinfo_fn=lambda: None, monitors_fn=lambda: [Rect(0, 0, 1920, 1080)],
                      audio_player_factory=FakePlayer)
    c.start()
    try:
        wait(lambda: srv.connected() and c.auth_mode == "key" and srv.supports(A.FEATURE), timeout=20,
             msg="connection")
        time.sleep(0.2)
        assert not caps and srv.audio is None and not c.audio.running   # while off, no audio channel is opened

        srv.cfg.set("audio", True)
        srv.send_audio_state()
        wait(lambda: FakePlayer.instances and FakePlayer.instances[-1].total > 20 * len(_sine()), timeout=10,
             msg="audio playing on Windows")
        pl = FakePlayer.instances[-1]
        assert (pl.rate, pl.channels) == (48000, 2) and True in pl.plays
        st = srv.status()["audio"]
        assert st["enabled"] and st["active"] and st["supported"] and not st["error"]
        assert vol.muted and srv.cfg.get("audio_muted_by_app") is True  # the Mac's speakers were muted

        srv.cfg.set("audio_mute_mac", False)                             # turned off from the menu: unmuted at once
        srv.audio_mute_changed()
        assert vol.muted is False and srv.cfg.get("audio_muted_by_app") is False
        srv.cfg.set("audio_mute_mac", True)
        srv.audio_mute_changed()
        assert vol.muted is True

        # only the chosen apps (3.2.4): applied to the running capture at once
        assert srv.audio_apps() is None and caps[-1].apps is None
        srv.cfg.set("audio_apps_mode", "selected")
        srv.cfg.set("audio_apps", {"com.spotify.client": "Spotify", "com.google.Chrome": "Chrome"})
        srv.audio_apps_changed()
        wait(lambda: caps[-1].set_calls == [["com.google.Chrome", "com.spotify.client"]], timeout=5,
             msg="chosen apps applied")

        srv.cfg.set("audio", False)                                      # audio transfer turned off
        srv.send_audio_state()
        wait(lambda: srv.audio is None and caps[-1].stop_ev.is_set(), timeout=10, msg="audio stopped")
        wait(lambda: pl.closed and not c.audio.running, timeout=10, msg="Windows player closed")
        assert vol.muted is False and srv.cfg.get("audio_muted_by_app") is False
        assert srv.status()["audio"]["active"] is False

        fail["msg"] = "no Screen Recording permission"                   # if capture does not start, both sides show it
        srv.cfg.set("audio", True)
        srv.send_audio_state()
        wait(lambda: "Recording permission" in srv.status()["audio"]["error"] and "Recording permission" in c.audio.last_error,
             timeout=10, msg="error reported")
        assert vol.muted is False
        assert caps[-1].apps == ["com.google.Chrome", "com.spotify.client"]     # a new capture starts with them
    finally:
        c.stop()
        srv.stop()
    assert vol.muted is False


def test_audio_packets_are_small_enough_for_frames():
    one_sec = A.pack_audio(0, b"\0" * int(1000 * BPM))
    assert len(one_sec) < 32 * 1024 * 1024 and struct.unpack(">I", one_sec[:4]) == (0,)


def test_asbd_is_read_in_every_shape_pyobjc_gives():
    """On real macOS, pyobjc gave AudioStreamBasicDescription as a flat tuple of its fields; 2.8.0 could not read
    it and no audio went through (CI: 158 buffers, all read errors)."""
    from mirrorway.mac import audio as MA

    class S:
        mSampleRate, mFormatID, mFormatFlags, mChannelsPerFrame, mBitsPerChannel = 48000.0, 1819304813, 0x29, 2, 32
    flat = (48000.0, 1819304813, 0x29, 8, 1, 4, 2, 32, 0)                # the format on real macOS
    want = (48000, 2, 0x29, 32)
    assert MA.asbd_info(S()) == want
    assert MA.asbd_info([S()]) == want                                   # a struct pointer
    assert MA.asbd_info(flat) == want
    assert MA.asbd_info(None) is None and MA.asbd_info(3.0) is None and MA.asbd_info((48000.0,)) is None
    assert MA.asbd_info((0.0, 0, 0, 0, 0, 0, 0, 0, 0)) is None           # an empty format: falls back to the default
    rate, ch, flags, bits = MA.SCK_DEFAULT
    assert (rate, ch, bits) == (48000, 2, 32) and flags & MA.FLAG_FLOAT and flags & MA.FLAG_NON_INTERLEAVED


def test_level_meter_reports_music_between_silence():
    """3.2.4: a minute with 20 s of music and 40 s of silent packets is "level … while playing (33% …)", not
    "silent" (the old dB average was pulled down by the silent packets)."""
    m = A.LevelMeter()
    assert m.describe() == "no audio"
    silence = b"\0" * 3840
    for _ in range(100):
        m.add(silence)
    assert m.describe() == "silent" and m.level() is None
    music = _sine(amp=0.5)
    for _ in range(50):
        m.add(music)
    text = m.describe()
    assert text.startswith("level -9 dBFS while playing (33% of the time)") and text.endswith("peak -6 dBFS"), text
    m.reset()
    assert m.packets == 0 and m.describe() == "no audio"


def test_audio_counts_per_minute():
    from mirrorway.mac.server import _per_minute
    prev = {"underruns": 0}
    assert _per_minute({"underruns": 84}, prev, "underruns") == 84
    assert _per_minute({"underruns": 90}, prev, "underruns") == 6
    assert _per_minute({"underruns": 2}, prev, "underruns") == 2      # Windows started again
    assert _per_minute({}, prev, "trims") == "?"


def test_chosen_apps_and_settings_page():
    from conftest import DictCfg as Cfg
    from mirrorway import prefs as P
    from mirrorway.mac.audio import chosen_apps

    class App:
        def __init__(self, bid):
            self.bid = bid

        def bundleIdentifier(self):
            return self.bid
    running = [App("com.apple.Safari"), App("com.spotify.client"), App(None)]
    assert [a.bid for a in chosen_apps(running, ["com.spotify.client", "com.other"])] == ["com.spotify.client"]
    assert chosen_apps(running, []) == [] and chosen_apps(None, ["x"]) == []

    page = {p.id: p for p in P.mac_pages(Cfg(audio=True), {})}["audio"]
    assert page.item("audio_apps_mode").value == "all" and len(page.groups) == 2       # no app list
    cfg = Cfg(audio=True, audio_apps_mode="selected", audio_apps={"com.spotify.client": "Spotify",
                                                                   "com.old.app": "Old App"})
    page = {p.id: p for p in P.mac_pages(cfg, {}, {"audio_apps": [("com.apple.Safari", "Safari"),
                                                                    ("com.spotify.client", "Spotify")]})}["audio"]
    rows = page.groups[2].items
    assert [r.title for r in rows] == ["Old App", "Safari", "Spotify"]
    assert [r.value for r in rows] == [True, False, True] and rows[0].detail == "not running"
    assert "Only the chosen apps" in page.groups[1].note
    empty = {p.id: p for p in P.mac_pages(Cfg(audio_apps_mode="selected"), {})}["audio"]
    assert empty.groups[2].items[0].key == "no_audio_apps"
