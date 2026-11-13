"""CI (GitHub Windows runner with a virtual sound card, VB-CABLE): Windows' sound to the Mac for real (3.4).

  1. Process loopback captures a tone another program plays (excluding an unrelated process tree).
  2. It does not capture Mirrorway's own process tree (the tone played by a child process is left out).
  3. With the speakers muted, process loopback still captures the tone (Mirrorway mutes Windows' speakers while its
     sound plays on the Mac).
  4. The speakers' loopback (older Windows) captures the tone too.
  5. End to end: a Mac server on this machine (stand-in platform) with the route "all on the Mac", Windows'
     Mirrorway client with the real capture and the real speaker mute: the Mac side receives the tone, the
     speakers are muted meanwhile and unmuted afterwards.
No sound card on the runner (the driver did not install): reported only.
Run: python tests/ci_audio_windows.py
"""
import math
import os
import struct
import subprocess
import sys
import tempfile
import time
import wave

if not os.environ.get("CI") or sys.platform != "win32":
    sys.exit("This script is for the Windows CI only.")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.environ.setdefault("MIRRORWAY_HOME", tempfile.mkdtemp(prefix="mirrorway-ci-audio-"))

import logging  # noqa: E402

from mirrorway import audio as A  # noqa: E402
from mirrorway.win import loopback as L  # noqa: E402

logging.basicConfig(level=logging.INFO, format="  %(name)s: %(message)s", stream=sys.stdout)
failures = []


def check(name, cond, info=""):
    print(("[OK]   " if cond else "[FAIL] ") + name + (f": {info}" if info else ""), flush=True)
    if not cond:
        failures.append(name)


def tone_file(seconds=3.0) -> str:
    path = os.path.join(tempfile.mkdtemp(prefix="mirrorway-tone-"), "tone.wav")
    n = int(48000 * seconds)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        frames = bytearray()
        for i in range(n):
            v = int(12000 * math.sin(2 * math.pi * 1000 * i / 48000))
            frames += struct.pack("<hh", v, v)
        w.writeframes(bytes(frames))
    return path


TONE = tone_file()


def play_tone() -> subprocess.Popen:
    """A child process plays the tone through the default speakers (winsound / waveOut)."""
    return subprocess.Popen([sys.executable, "-c", "import sys, winsound; winsound.PlaySound(sys.argv[1], "
                             "winsound.SND_FILENAME)", TONE])


def unrelated_process() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])


def capture(cap, seconds=2.5, tone=True):
    """Captures while the tone plays. Returns (level dBFS or None, packets, silent packets)."""
    meter = A.LevelMeter()
    counts = {"packets": 0, "silent": 0}

    def on_pcm(pcm, silent):
        counts["packets"] += 1
        if silent:
            counts["silent"] += 1
        else:
            meter.add(pcm)
    cap.start(on_pcm)
    proc = play_tone() if tone else None
    try:
        time.sleep(seconds)
    finally:
        cap.stop()
        if proc is not None:
            proc.wait(10)
    return meter.level(), counts["packets"], counts["silent"], meter.describe()


def describe(r):
    level, packets, silent, desc = r
    return f"{packets} packets ({silent} marked silent), {desc}"


def speakers_present() -> bool:
    try:
        L._co_init()
        v = L._default_device(L.IID_IAudioEndpointVolume)
        L.release(v)
        return True
    except Exception as exc:
        print(f"[WARN] No default speakers on this runner: {exc}")
        return False


print(f"Windows build {L.windows_build()}, process loopback available: {L.process_loopback_available()}")
if not speakers_present():
    print("RESULT: PASSED (no sound card: reported only)")
    sys.exit(0)

spk = L.SpeakerMute()
was_muted = spk.get()
check("the speakers' mute can be read", was_muted is not None, str(was_muted))
other = unrelated_process()
try:
    # 1. process loopback, everything except an unrelated process tree: the tone (our child) is captured
    r = capture(L.WasapiLoopback("process", exclude_pid=other.pid))
    check("process loopback captures another program's sound", r[0] is not None and r[0] > -50, describe(r))

    # 2. excluding our own tree: the tone our child plays is left out
    r = capture(L.WasapiLoopback("process", exclude_pid=os.getpid()))
    check("process loopback leaves out Mirrorway's own process tree", r[0] is None, describe(r))

    # 3. speakers muted: the capture still hears it
    muted = spk.set(True)
    check("the speakers can be muted", muted and spk.get() is True, str(spk.get()))
    try:
        r = capture(L.WasapiLoopback("process", exclude_pid=other.pid))
        check("process loopback captures with the speakers muted", r[0] is not None and r[0] > -50, describe(r))
        r = capture(L.WasapiLoopback("endpoint"))
        print(f"[INFO] the speakers' loopback with the speakers muted: {describe(r)}")
    finally:
        spk.set(bool(was_muted))

    # 4. the speakers' loopback (older Windows)
    cap = L.WasapiLoopback("endpoint")
    r = capture(cap)
    check("the speakers' loopback captures the sound", r[0] is not None and r[0] > -50,
          f"{cap.rate} Hz, {describe(r)}")
finally:
    other.kill()

# 5. end to end through Mirrorway: route "all on the Mac"
import json  # noqa: E402
import socket  # noqa: E402

from conftest import DictCfg  # noqa: E402
from mirrorway.geometry import Rect  # noqa: E402
from mirrorway.mac.server import MirrorwayServer  # noqa: E402
from mirrorway.win.client import ControlClient, Resolver  # noqa: E402
from mirrorway.win.remote import RemoteControl  # noqa: E402
from test_router_remote import FakeMac, FakeWin  # noqa: E402


class Recorder:
    """The Mac's player: records what arrives (no Mac here)."""
    instances = []

    def __init__(self, rate, channels):
        self.rate, self.channels = rate, channels
        self.meter = A.LevelMeter()
        self.first_sound = None
        self.buf = 0
        self.closed = False
        Recorder.instances.append(self)

    def queued(self):
        return 0

    def queue(self, pcm):
        self.meter.add(pcm)
        if self.first_sound is None and self.meter.level() is not None:
            self.first_sound = time.monotonic()

    def clear(self):
        pass

    def play(self, on=True):
        pass

    def close(self):
        self.closed = True


class MacVolume:
    def get(self):
        return False

    def set(self, muted):
        return True


def wait(cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


port = free_port()
mcfg = DictCfg(port=port, side="right", speed=1.0, scroll_speed=1.0, edge_switch=True, clipboard=False,
               pair_code="ABCD-EFGH-JKMN", audio=False, audio_from_windows=False)
srv = MirrorwayServer(mcfg, FakeMac(), beacon=False, files_dir=tempfile.mkdtemp(), volume=MacVolume(),
                      winaudio_player_factory=Recorder)
srv.start()
other = unrelated_process()
wcfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN", audio_muted_by_app=False)
client = ControlClient(wcfg, Resolver(wcfg, None), RemoteControl(FakeWin(), lambda m, o: None),
                       netinfo_fn=lambda: None, monitors_fn=lambda: [Rect(0, 0, 1920, 1080)],
                       audio_capture_factory=lambda: L.WasapiLoopback("auto", exclude_pid=other.pid),
                       speakers=L.SpeakerMute())
client.start()
before = spk.get()
try:
    ok = wait(lambda: srv.connected() and client.auth_mode == "key" and srv.winaudio_supported(), 30)
    check("Windows connects with its device key and can send its sound", ok)
    srv.set_audio_route("mac", "CI")
    ok = wait(lambda: client.sending_audio and srv.winaudio is not None, 15)
    check("route 'all on the Mac': Windows opens its sound channel", ok, client.winaudio.last_error)
    check("Windows' speakers are muted meanwhile", wait(lambda: spk.get() is True, 5), str(spk.get()))
    t0 = time.monotonic()
    proc = play_tone()
    ok = wait(lambda: Recorder.instances and Recorder.instances[-1].first_sound is not None, 10)
    rec = Recorder.instances[-1] if Recorder.instances else None
    proc.wait(10)
    check("the Mac side receives the tone", ok, (f"first sound after {(rec.first_sound - t0) * 1000:.0f} ms "
                                                  f"(includes starting the player process); {rec.meter.describe()}")
          if ok else (rec.meter.describe() if rec else "nothing arrived"))
    st = srv.status()["audio"]
    check("the Mac's status shows it", st["route"] == "mac" and st["from_windows"], json.dumps(st))
    srv.set_audio_route("own", "CI")
    ok = wait(lambda: not client.sending_audio and srv.winaudio is None, 15)
    check("route 'each on its own': Windows stops sending", ok)
    check("Windows' speakers are as before", wait(lambda: spk.get() == before, 5), f"{spk.get()} (was {before})")
    check("the muted flag is cleared", wcfg.get("audio_muted_by_app") is False)
finally:
    client.stop()
    srv.stop()
    other.kill()
    spk.set(bool(was_muted))

print("RESULT: " + ("PASSED" if not failures else "FAILED: " + ", ".join(failures)))
sys.exit(1 if failures else 0)
