"""CI (GitHub Windows runner, administrator): lock screen support for real.

  1. A Mac server on this machine (the Mac's side of Mirrorway with a stand-in platform) with a paired device key.
  2. Installs lock screen support with that key and the server's identity (python -m mirrorway.win.svcinstall
     install --trust-file …): files under Program Files, the trust file readable only by SYSTEM and administrators,
     the Ctrl+Alt+Del policy, the service.
  3. The service starts the helper as SYSTEM in the console session; the helper connects to the server with the
     device key and reports the input desktop ("Default").
  4. Ctrl+Alt+Del from the "Mac": the service calls SendSAS, the helper reports the secure desktop ("Winlogon") and
     Escape typed through the helper closes it again. (Only reported if the runner has no console session for it.)
  5. Removes it again: service gone, files gone, the policy as before.
Run: python tests/ci_lockscreen_windows.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if not os.environ.get("CI") or sys.platform != "win32":
    sys.exit("This script is for the Windows CI only.")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.environ.setdefault("MIRRORWAY_HOME", tempfile.mkdtemp(prefix="mirrorway-ci-ls-"))

from conftest import DictCfg  # noqa: E402
from mirrorway import protocol as PR  # noqa: E402
from mirrorway.devkeys import Identity  # noqa: E402
from mirrorway.geometry import Rect  # noqa: E402
from mirrorway.mac.server import MirrorwayServer  # noqa: E402
from mirrorway.win import lockscreen as LS  # noqa: E402
from mirrorway.win import svcinstall as SI  # noqa: E402

failures = []


def check(name, cond, info=""):
    print(("[OK]   " if cond else "[FAIL] ") + name + (f": {info}" if info else ""), flush=True)
    if not cond:
        failures.append(name)


def wait(cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.2)
    return cond()


class StandInMac:
    """The Mac's platform for the server (no real Mac here)."""

    def displays(self):
        return [Rect(0, 0, 1440, 900)]

    def main_display(self):
        return Rect(0, 0, 1440, 900)

    def warp(self, x, y):
        pass

    def hide_cursor(self):
        pass

    def show_cursor(self):
        pass

    def cursor_pos(self):
        return (720.0, 450.0)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def show_logs():
    for name in ("service.log", "helper.log"):
        p = LS.data_dir() / name
        if p.exists():
            print(f"----- {name} -----")
            print(p.read_text("utf-8", "replace")[-4000:])


def sas_policy():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, LS.SAS_POLICY_KEY) as k:
            return winreg.QueryValueEx(k, LS.SAS_POLICY_VALUE)[0]
    except OSError:
        return None


port = free_port()
cfg = DictCfg(port=port, side="bottom", speed=1.0, scroll_speed=1.0, edge_switch=True, clipboard=False)
srv = MirrorwayServer(cfg, StandInMac(), beacon=False, files_dir=Path(tempfile.mkdtemp()))
srv.start()
device = Identity.generate()
srv.add_device("ci-pc", device.public, "windows")
work = Path(tempfile.mkdtemp(prefix="mirrorway-ci-trust-"))
user_cfg = work / "win.json"
user_cfg.write_text(json.dumps({"host": f"127.0.0.1:{port}"}), "utf-8")
trust_file = work / "trust.json"
trust_file.write_text(json.dumps(LS.make_trust(device.private_bytes(), srv.identity.public, "CI Mac", str(user_cfg),
                                               port, plain=True)), "utf-8")
policy_before = sas_policy()
installed = False
try:
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-m", "mirrorway.win.svcinstall", "install", "--trust-file", str(trust_file)],
                       cwd=ROOT, capture_output=True, text=True, timeout=600)
    print(r.stdout[-2000:], r.stderr[-2000:])
    installed = r.returncode == 0
    check("installed (files, trust, policy, service)", installed, f"{time.monotonic() - t0:.0f} s")
    check("the service runs", wait(lambda: SI.status()["running"], 30), json.dumps(SI.status()))
    check("the Ctrl+Alt+Del policy allows services", sas_policy() in (1, 3), str(sas_policy()))
    acl = subprocess.run(["icacls", str(LS.data_dir())], capture_output=True, text=True).stdout
    check("the trust folder is for SYSTEM and administrators only",
          not any(w in acl for w in ("S-1-5-32-545", "BUILTIN\\Users", "Authenticated Users", "Everyone")),
          acl.strip().replace("\n", " | ")[:300])
    ok = wait(lambda: srv.secure_connected(), 60)
    check("the helper (SYSTEM, console session) connects with the device key", ok)
    check("it reports the user's desktop", wait(lambda: srv.secure_desktop_name.lower() == "default", 10),
          srv.secure_desktop_name)
    if ok:
        sess = srv.secure
        srv.send_sas()
        if wait(lambda: srv.secure_desktop, 15):
            print(f"[OK]   Ctrl+Alt+Del opened the secure desktop: {srv.secure_desktop_name}", flush=True)
            sess.send_json(PR.MSG_ENTER, {"from": "center", "ratio": 0.5, "exit": "top"})
            for _ in range(3):
                sess.send(PR.MSG_KEY, PR.pack_key(True, 0, 0x01, 0x1B))      # Escape
                sess.send(PR.MSG_KEY, PR.pack_key(False, 0, 0x01, 0x1B))
                if wait(lambda: not srv.secure_desktop, 5):
                    break
            check("Escape typed by the helper closes it", not srv.secure_desktop, srv.secure_desktop_name)
        else:
            print("[WARN] Ctrl+Alt+Del did not open a secure desktop on this runner (reported only)", flush=True)
finally:
    show_logs()
    if installed or SI.status()["installed"]:
        r = subprocess.run([sys.executable, "-m", "mirrorway.win.svcinstall", "uninstall"], cwd=ROOT,
                           capture_output=True, text=True, timeout=300)
        print(r.stdout[-1500:], r.stderr[-1500:])
        check("removed", r.returncode == 0 and not SI.status()["installed"], json.dumps(SI.status()))
        check("its files are gone", not LS.program_dir().exists() and not LS.data_dir().exists())
        check("the policy is as before", sas_policy() == policy_before, f"{sas_policy()} (was {policy_before})")
    srv.stop()

print("RESULT: " + ("PASSED" if not failures else "FAILED: " + ", ".join(failures)))
sys.exit(1 if failures else 0)
