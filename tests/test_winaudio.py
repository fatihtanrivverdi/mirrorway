"""3.4: where sound plays (each on its own / all on Windows / all on the Mac) and Windows' sound on the Mac
(WASAPI loopback -> ROLE_WINAUDIO -> AudioQueue)."""
import ctypes
import json
import threading
import time

from conftest import DictCfg
from test_audio import FakeChannel, FakePlayer, FakeVolume, _sine

from mirrorway import audio as A


# ---- the shared receiver ------------------------------------------------------------------------------------
def test_receiver_opens_plays_rebuffers_and_reports():
    FakePlayer.instances.clear()
    opened = []
    rx = A.Receiver(FakePlayer, on_open=lambda r, c, p: opened.append((r, c)))
    rx.handle(A.MSG_AUDIO, A.pack_audio(0, _sine()))                 # before the format: ignored
    assert rx.packets == 0 and not FakePlayer.instances
    assert rx.handle(A.MSG_AUDIO_INFO, json.dumps({"rate": 44100, "channels": 2}).encode())
    assert opened == [(44100, 2)]
    for i in range(5):
        rx.handle(A.MSG_AUDIO, A.pack_audio(i, _sine(start=i * 480)))
    (pl,) = FakePlayer.instances
    assert rx.packets == 5 and pl.total == 5 * len(_sine()) and pl.plays[0] is True
    st = rx.stats()
    assert set(st) == {"queue_ms", "underruns", "trims"} and st["underruns"] == 0
    rx.handle(A.MSG_AUDIO_INFO, json.dumps({"rate": 44100, "channels": 2}).encode())   # same format: same device
    assert len(FakePlayer.instances) == 1
    rx.handle(A.MSG_AUDIO_INFO, json.dumps({"rate": 48000, "channels": 2}).encode())   # new format: new device
    assert pl.closed and len(FakePlayer.instances) == 2
    assert rx.handle(99, b"") is False
    try:
        rx.handle(A.MSG_AUDIO_INFO, json.dumps({"error": "no permission"}).encode())
        raise AssertionError("expected SourceError")
    except A.SourceError as exc:
        assert "no permission" in str(exc)
    rx.close()
    assert FakePlayer.instances[-1].closed


# ---- the Mac's player: the byte ring between the network and the AudioQueue callback ----------------------
def test_player_ring_pads_with_silence_and_counts_underruns():
    from mirrorway.mac.player import BUFFER_MS, Ring
    r = Ring()
    r.add(b"\x01" * 6)
    assert len(r) == 6
    got = r.take(4)
    assert got == b"\x01" * 4 and len(r) == 2 and r.underruns == 0
    got = r.take(4)
    assert got == b"\x01\x01\x00\x00" and len(r) == 0 and r.underruns == 1
    r.add(b"\x02" * 8)
    r.clear()
    assert len(r) == 0 and r.take(2) == b"\x00\x00"
    assert BUFFER_MS * 3 <= 40                       # the AudioQueue adds at most ~30 ms of latency


def test_player_structs_match_core_audio():
    from mirrorway.mac import player as P
    assert ctypes.sizeof(P.AudioStreamBasicDescription) == 40
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert P.AudioQueueBuffer.mAudioData.offset == 8 and P.AudioQueueBuffer.mAudioDataByteSize.offset == 16


# ---- Windows: WASAPI pieces that can be checked anywhere ---------------------------------------------------
def test_loopback_structs_and_activation_params():
    from mirrorway.win import loopback as L
    assert ctypes.sizeof(L.WAVEFORMATEX) == 18
    assert ctypes.sizeof(L.AUDIOCLIENT_ACTIVATION_PARAMS) == 12
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(L.PROPVARIANT) == 24 and L.PROPVARIANT.blob.offset == 8
    f = L.pcm_format()
    assert (f.wFormatTag, f.nChannels, f.nSamplesPerSec, f.wBitsPerSample, f.nBlockAlign, f.nAvgBytesPerSec) == \
        (1, 2, 48000, 16, 4, 192000)
    pv, params = L.activation_params(1234)
    assert pv.vt == L.VT_BLOB and pv.blob.cbSize == 12 and pv.blob.pBlobData == ctypes.addressof(params)
    assert params.ActivationType == 1 and params.ProcessLoopbackParams.TargetProcessId == 1234
    assert params.ProcessLoopbackParams.ProcessLoopbackMode == L.EXCLUDE_TARGET_PROCESS_TREE
    g = L.guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2")
    assert (g.Data1, g.Data2, g.Data3, bytes(g.Data4)) == (0x1CB9AD4C, 0xDBFA, 0x4C32,
                                                          bytes.fromhex("B178C2F568A703B2"))
    assert L.process_loopback_available(26100) and L.process_loopback_available(20348)
    assert not L.process_loopback_available(19045)                    # Windows 10 22H2: the speakers' loopback
    try:
        L.check(-2147467262, "QueryInterface")
        raise AssertionError
    except L.ComError as exc:
        assert "0x80004002" in str(exc)


def test_activation_handler_is_a_working_com_object():
    """The completion handler is called through its vtable like Windows does: QueryInterface (IAgileObject too),
    then ActivateCompleted, which asks the operation for its result."""
    from mirrorway.win import loopback as L
    h = L.ActivationHandler()
    qi = L.method(h.ptr, 0, ctypes.c_long, ctypes.POINTER(L.GUID), ctypes.POINTER(ctypes.c_void_p))
    out = ctypes.c_void_p()
    assert qi(ctypes.byref(L.IID_IAgileObject), ctypes.byref(out)) == 0 and out.value == h.ptr
    assert qi(ctypes.byref(L.IID_IActivateAudioInterfaceCompletionHandler), ctypes.byref(out)) == 0
    assert qi(ctypes.byref(L.IID_IAudioClient), ctypes.byref(out)) == L.E_NOINTERFACE and not out.value
    assert L.method(h.ptr, 1, ctypes.c_uint32)() >= 1

    # a stand-in IActivateAudioInterfaceAsyncOperation: GetActivateResult -> (S_OK, a client pointer)
    fake_client = ctypes.c_int(7)

    @L._FN(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_long), ctypes.POINTER(ctypes.c_void_p))
    def get_result(this, phr, punk):
        phr[0] = 0
        punk[0] = ctypes.addressof(fake_client)
        return 0
    noop = L._FN(ctypes.c_uint32, ctypes.c_void_p)(lambda this: 1)
    vtbl = (ctypes.c_void_p * 4)(None, ctypes.cast(noop, ctypes.c_void_p), ctypes.cast(noop, ctypes.c_void_p),
                                 ctypes.cast(get_result, ctypes.c_void_p))
    op = ctypes.c_void_p(ctypes.addressof(vtbl))
    done = L.method(h.ptr, 3, ctypes.c_long, ctypes.c_void_p)
    assert done(ctypes.addressof(op)) == 0
    assert h.done.is_set() and h.hr == 0 and h.client.value == ctypes.addressof(fake_client)


# ---- Windows: the sender (silence, idle, speakers) ----------------------------------------------------------
class FakeLoopback:
    """Stands in for WasapiLoopback: a tone, then silence (as WASAPI reports it), on its own thread."""
    name = "fake loopback"
    rate, channels = A.RATE, A.CHANNELS

    def __init__(self, mutes_ok=True, sound_packets=10, silent_packets=60, fail=None):
        self.mutes_ok = mutes_ok
        self.sound, self.silent = sound_packets, silent_packets
        self.fail = fail
        self.error = None
        self.stop_ev = threading.Event()
        self.started = False

    def start(self, on_pcm):
        if self.fail:
            raise RuntimeError(self.fail)
        self.started = True

        def run():
            i = 0
            while not self.stop_ev.wait(0.01):
                if i < self.sound:
                    on_pcm(_sine(start=i * 480), False)
                elif i < self.sound + self.silent:
                    on_pcm(bytes(len(_sine())), True)
                i += 1
        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        self.stop_ev.set()


class SendChannel(FakeChannel):
    def __init__(self, msgs=()):
        super().__init__(msgs)
        self.out = []

    def send(self, mtype, payload=b""):
        if self.closed:
            raise ConnectionError("closed")
        self.out.append((mtype, payload))


def test_sender_sends_sound_then_goes_idle_and_mutes_meanwhile(monkeypatch):
    from mirrorway.win.audio import WinAudioSender
    monkeypatch.setattr(A, "IDLE_EVERY_S", 0.2)
    ch = SendChannel()
    cap = FakeLoopback()
    spk = FakeVolume()
    cfg = DictCfg(audio_muted_by_app=False)
    s = WinAudioSender(control=None, capture_factory=lambda: cap, speakers=spk, cfg=cfg)
    s._connect = lambda: ch
    s.set_state(True)
    try:
        deadline = time.time() + 5
        while not any(m == A.MSG_AUDIO_IDLE for m, _ in ch.out) and time.time() < deadline:
            time.sleep(0.02)
        info = [o for m, o in ch.sent if m == A.MSG_AUDIO_INFO]
        assert info == [{"rate": 48000, "channels": 2, "format": "s16le", "source": "fake loopback"}]
        audio = [p for m, p in ch.out if m == A.MSG_AUDIO]
        assert 10 <= len(audio) <= 10 + 40          # the tone, then silence only for a short tail (0.3 s)
        assert [A.unpack_audio(p)[0] for p in audio] == list(range(len(audio)))
        assert any(m == A.MSG_AUDIO_IDLE for m, _ in ch.out)
        assert spk.muted is True and cfg.get("audio_muted_by_app") is True
        assert s.running and s.source == "fake loopback"
    finally:
        s.stop()
    deadline = time.time() + 3
    while (s.running or spk.muted) and time.time() < deadline:
        time.sleep(0.02)
    assert not s.running and cap.stop_ev.is_set() and ch.closed
    assert spk.muted is False and cfg.get("audio_muted_by_app") is False


def test_sender_leaves_speakers_on_without_process_loopback_and_stops_on_mac_error():
    from mirrorway.win.audio import WinAudioSender
    ch = SendChannel([(A.MSG_AUDIO_INFO, json.dumps({"error": "not set to play on the Mac"}).encode())])
    spk = FakeVolume()
    s = WinAudioSender(control=None, capture_factory=lambda: FakeLoopback(mutes_ok=False), speakers=spk)
    s._connect = lambda: ch
    s.set_state(True)
    deadline = time.time() + 5
    while s.running and time.time() < deadline:
        time.sleep(0.02)
    assert not s.running and "not set to play on the Mac" in s.last_error     # no retry in vain
    assert spk.calls == []                                                    # the speakers were never touched


def test_sender_reads_the_macs_refusal_even_if_sending_fails_first():
    """On Windows a send to a channel the Mac already closed fails at once: the refusal must still be read."""
    from mirrorway.win.audio import WinAudioSender

    class ResetChannel(SendChannel):
        def send_json(self, mtype, obj):
            time.sleep(0.05)                          # the reader gets the Mac's message meanwhile
            raise ConnectionResetError("reset by peer")
    ch = ResetChannel([(A.MSG_AUDIO_INFO, json.dumps({"error": "not set to play on the Mac"}).encode())])
    s = WinAudioSender(control=None, capture_factory=lambda: FakeLoopback(), speakers=FakeVolume())
    s._connect = lambda: ch
    s.set_state(True)
    deadline = time.time() + 5
    while s.running and time.time() < deadline:
        time.sleep(0.02)
    assert not s.running and "not set to play on the Mac" in s.last_error


def test_mac_player_keeps_running_through_short_gaps(monkeypatch):
    """Rebuffering only stops taking from the queue; the AudioQueue is paused only after IDLE_PAUSE_S."""
    from mirrorway.mac import player as P
    calls = []

    class AT:
        def AudioQueueStart(self, aq, ts):
            calls.append("start")
            return 0

        def AudioQueuePause(self, aq):
            calls.append("pause")
            return 0
    monkeypatch.setattr(P, "_toolbox", lambda: AT())
    monkeypatch.setattr(P, "IDLE_PAUSE_S", 0.2)
    pl = P.AudioQueuePlayer.__new__(P.AudioQueuePlayer)
    pl.playing = pl.running = pl.closed = False
    pl.starts, pl._idle_since, pl._lock, pl.aq = 0, None, threading.Lock(), None
    pl.play(True)
    pl.play(False)
    pl.play(True)                                   # a short gap: no pause, no second start
    time.sleep(0.3)
    assert calls == ["start"] and pl.running and pl.starts == 1
    pl.play(False)
    time.sleep(0.4)                                 # silence for longer: the hardware is paused
    assert calls == ["start", "pause"] and not pl.running
    pl.play(True)
    assert calls == ["start", "pause", "start"] and pl.starts == 2


def test_speakers_unmuted_after_a_crash():
    from mirrorway.win.audio import WinAudioSender
    spk = FakeVolume(muted=True)
    cfg = DictCfg(audio_muted_by_app=True)
    s = WinAudioSender(control=None, speakers=spk, cfg=cfg)
    s.restore_speakers()
    assert spk.calls == [False] and cfg.get("audio_muted_by_app") is False
    s.restore_speakers()
    assert spk.calls == [False]


# ---- end to end: the Mac's choice decides which way sound goes ------------------------------------------------
def test_end_to_end_route_mac_windows_own(tmp_path):
    from mirrorway.geometry import Rect
    from mirrorway.mac.server import MirrorwayServer
    from mirrorway.win.client import ControlClient, Resolver
    from mirrorway.win.remote import RemoteControl
    from test_audio import FakeCapture
    from test_e2e import free_port, wait
    from test_router_remote import FakeMac, FakeWin, mac_cfg

    FakePlayer.instances.clear()
    port = free_port()
    mac_players, win_caps = [], []

    def mac_player(rate, channels):
        mac_players.append(FakePlayer(rate, channels))
        return mac_players[-1]

    def win_cap():
        win_caps.append(FakeLoopback(sound_packets=10_000, silent_packets=0))
        return win_caps[-1]
    mac_vol, win_spk = FakeVolume(), FakeVolume()
    srv = MirrorwayServer(mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", audio=False), FakeMac(), beacon=False,
                          audio_capture_factory=FakeCapture, volume=mac_vol, winaudio_player_factory=mac_player)
    srv.start()
    wcfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN", audio_muted_by_app=False)
    c = ControlClient(wcfg, Resolver(wcfg, None), RemoteControl(FakeWin(), lambda m, o: None),
                      netinfo_fn=lambda: None, monitors_fn=lambda: [Rect(0, 0, 1920, 1080)],
                      audio_player_factory=FakePlayer, audio_capture_factory=win_cap, speakers=win_spk)
    c.start()
    try:
        wait(lambda: srv.connected() and c.auth_mode == "key" and srv.winaudio_supported(), timeout=20,
             msg="connection")
        wait(lambda: c.audio_route == "own", timeout=5, msg="route told on connect")
        assert c.can_route_audio() and not win_caps and srv.audio_route() == "own"

        # all on the Mac (from Windows' settings)
        assert c.set_audio_route("mac")
        wait(lambda: mac_players and mac_players[-1].total > 20 * len(_sine()), timeout=10,
             msg="Windows' sound playing on the Mac")
        assert srv.cfg.get("audio_from_windows") is True and srv.cfg.get("audio") is False
        assert win_spk.muted is True and wcfg.get("audio_muted_by_app") is True
        st = srv.status()["audio"]
        assert st["route"] == "mac" and st["from_windows"] and st["from_windows_supported"] and not st["active"]
        assert c.sending_audio and c.audio_route == "mac" and not c.audio.running
        wait(lambda: c.winaudio.mac_stats.get("queue_ms") is not None, timeout=10, msg="the Mac's queue reported")

        # the quick toggle: off (each on its own), then back to the last direction
        c.toggle_audio()
        wait(lambda: srv.audio_route() == "own" and srv.winaudio is None and not c.sending_audio, timeout=10,
             msg="sharing off")
        wait(lambda: win_spk.muted is False, timeout=5, msg="speakers back on")
        assert mac_players[-1].closed and win_caps[-1].stop_ev.is_set()
        srv.toggle_audio("test")
        wait(lambda: srv.audio_route() == "mac" and c.sending_audio and srv.winaudio is not None, timeout=10,
             msg="back to the Mac")

        # all on Windows: the other way round, never both
        srv.set_audio_route("windows", "test")
        wait(lambda: FakePlayer.instances and any(p.total for p in FakePlayer.instances if p not in mac_players),
             timeout=10, msg="the Mac's sound on Windows")
        wait(lambda: not c.sending_audio and srv.winaudio is None, timeout=10, msg="Windows stopped sending")
        assert win_spk.muted is False and mac_vol.muted is True
        assert srv.cfg.get("audio") is True and srv.cfg.get("audio_from_windows") is False

        # an older Windows panel's request still works
        srv.action_from_windows({"audio": False})
        wait(lambda: srv.audio_route() == "own" and not c.audio.running, timeout=10, msg="off from old request")
        assert srv.cfg.get("audio_route_last") == "windows"
    finally:
        c.stop()
        srv.stop()
    assert win_spk.muted is False and mac_vol.muted is False


def test_mac_refuses_windows_sound_unless_chosen():
    from mirrorway.geometry import Rect
    from mirrorway.mac.server import MirrorwayServer
    from mirrorway.win.client import ControlClient, Resolver
    from mirrorway.win.remote import RemoteControl
    from test_e2e import free_port, wait
    from test_router_remote import FakeMac, FakeWin, mac_cfg

    port = free_port()
    srv = MirrorwayServer(mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN"), FakeMac(), beacon=False,
                          volume=FakeVolume(), winaudio_player_factory=FakePlayer)
    srv.start()
    wcfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN")
    caps = []
    c = ControlClient(wcfg, Resolver(wcfg, None), RemoteControl(FakeWin(), lambda m, o: None),
                      netinfo_fn=lambda: None, monitors_fn=lambda: [Rect(0, 0, 1920, 1080)],
                      audio_capture_factory=lambda: caps.append(FakeLoopback()) or caps[-1], speakers=FakeVolume())
    c.start()
    try:
        wait(lambda: srv.connected() and c.auth_mode == "key", timeout=20, msg="connection")
        # Windows opens its sound channel on its own (the sender itself follows the Mac's state, so it is opened by
        # hand here): the Mac says why not, and waits for Windows to close
        from mirrorway.protocol import ROLE_WINAUDIO
        from mirrorway.win.audio import open_audio_channel
        ch = open_audio_channel(c, ROLE_WINAUDIO)
        mtype, payload = ch.recv()
        assert mtype == A.MSG_AUDIO_INFO and "not set to play on the Mac" in json.loads(payload)["error"]
        wait(lambda: srv.winaudio is not None, timeout=5, msg="the Mac waits")
        ch.close()
        wait(lambda: srv.winaudio is None, timeout=10, msg="closed")
        assert not caps
    finally:
        c.stop()
        srv.stop()


# ---- the panels and settings ---------------------------------------------------------------------------------
def test_panels_and_settings_show_the_route():
    from mirrorway import prefs as P
    from mirrorway import quick as Q
    def tile(st, conn=True):
        return Q._audio_tile(st, conn, "a")
    assert tile({"audio": {"route": "own"}}).on is False
    assert tile({"audio": {"route": "windows", "active": True, "supported": True}}).detail == "All on Windows"
    assert tile({"audio": {"route": "mac", "from_windows": True}}).detail == "All on the Mac"
    assert tile({"audio": {"route": "mac", "from_windows_supported": False}}).detail == "Update needed"
    assert tile({"audio": {"route": "mac"}}, conn=False).detail == "Waiting"
    assert tile({"audio": {"enabled": True, "active": True, "supported": True}}).on is True    # older status

    cfg = DictCfg(clipboard=True)
    win = Q.win_panel(state="connected", status_text="", cfg=cfg, audio_supported=True, audio_route="mac",
                      sending_audio=True).tile("audio")
    assert win.title == "Sound" and win.detail == "All on the Mac" and win.on
    old = Q.win_panel(state="connected", status_text="", cfg=cfg, audio_supported=True, audio_on=True).tile("audio")
    assert old.title == "Mac's sound" and old.detail == "On"           # an older Mac: as before

    pages = {p.id: p for p in P.win_pages(cfg, {"state": "connected", "audio_supported": True,
                                               "route_supported": True, "audio_route": "mac",
                                               "sending_audio": True})}
    it = pages["audio"].item("audio_route")
    assert it.kind == "popup" and it.value == "mac" and [v for v, _ in it.options] == ["own", "windows", "mac"]
    assert "muted" in it.detail
    pages = {p.id: p for p in P.win_pages(cfg, {"state": "connected", "audio_supported": True, "audio_on": True,
                                               "audio_route": "windows", "route_supported": False})}
    assert pages["audio"].item("action:audio").button == "Turn off"

