"""Installs and removes lock screen support (run elevated: Settings › General starts it with "runas").

  python -m mirrorway.win.svcinstall install --config <the user's win.json>
  python -m mirrorway.win.svcinstall uninstall [--config …]
  python -m mirrorway.win.svcinstall status

install: copies this Python and the mirrorway package to Program Files (only administrators can change them there),
writes the trust file (this PC's device key and the paired Mac's public key, from the user's settings) next to them,
readable only by SYSTEM and administrators, lets services simulate Ctrl+Alt+Del (SoftwareSASGeneration = 1; the old
value is kept to restore it) and creates and starts the service. uninstall undoes all of it. The outcome is written to
lockscreen-result.json next to the user's settings, for the Settings window.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from .. import __version__
from . import lockscreen as LS

log = logging.getLogger("mirrorway.svcinstall")

SC_MANAGER_CONNECT, SC_MANAGER_ALL_ACCESS = 0x0001, 0xF003F
SERVICE_QUERY_STATUS, SERVICE_ALL_ACCESS = 0x0004, 0xF01FF
SERVICE_WIN32_OWN_PROCESS, SERVICE_AUTO_START, SERVICE_ERROR_NORMAL = 0x10, 2, 1
SERVICE_CONFIG_DESCRIPTION, SERVICE_CONFIG_FAILURE_ACTIONS = 1, 2
SC_ACTION_RESTART = 1
SERVICE_CONTROL_STOP = 1
SERVICE_STOPPED, SERVICE_RUNNING = 1, 4
ERROR_SERVICE_MARKED_FOR_DELETE = 1072
SID_SYSTEM, SID_ADMINS = "*S-1-5-18", "*S-1-5-32-544"
STUB = ('import os, sys\n'
        'sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n'
        'from mirrorway.win.{module} import main\n'
        'sys.exit(main())\n')


class SC_ACTION(ctypes.Structure):
    _fields_ = [("Type", ctypes.c_int), ("Delay", wintypes.DWORD)]


class SERVICE_FAILURE_ACTIONSW(ctypes.Structure):
    _fields_ = [("dwResetPeriod", wintypes.DWORD), ("lpRebootMsg", wintypes.LPWSTR), ("lpCommand", wintypes.LPWSTR),
                ("cActions", wintypes.DWORD), ("lpsaActions", ctypes.POINTER(SC_ACTION))]


class SERVICE_DESCRIPTIONW(ctypes.Structure):
    _fields_ = [("lpDescription", wintypes.LPWSTR)]


def _advapi():
    from .service import SERVICE_STATUS
    a = ctypes.WinDLL("advapi32", use_last_error=True)
    H, D, B, S = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR
    for name, args, res in (
            ("OpenSCManagerW", [S, S, D], H),
            ("OpenServiceW", [H, S, D], H),
            ("CreateServiceW", [H, S, S, D, D, D, D, S, S, ctypes.POINTER(D), S, S, S], H),
            ("ChangeServiceConfig2W", [H, D, wintypes.LPVOID], B),
            ("StartServiceW", [H, D, wintypes.LPVOID], B),
            ("ControlService", [H, D, ctypes.POINTER(SERVICE_STATUS)], B),
            ("QueryServiceStatus", [H, ctypes.POINTER(SERVICE_STATUS)], B),
            ("DeleteService", [H], B),
            ("CloseServiceHandle", [H], B)):
        fn = getattr(a, name)
        fn.argtypes, fn.restype = args, res
    return a, SERVICE_STATUS


def service_command(program: Path) -> str:
    return f'"{program / "python" / "python.exe"}" -I "{program / "mirrorway_service.py"}"'


# ---- status (no administrator rights needed) ------------------------------------------------------------------
def status() -> dict:
    """{"installed", "running", "version"} of lock screen support."""
    out = {"installed": False, "running": False, "version": "", "protocol": 0}
    if sys.platform != "win32":
        return out
    try:
        a, SERVICE_STATUS = _advapi()
        scm = a.OpenSCManagerW(None, None, SC_MANAGER_CONNECT)
        if not scm:
            return out
        try:
            svc = a.OpenServiceW(scm, LS.SERVICE_NAME, SERVICE_QUERY_STATUS)
            if not svc:
                return out
            try:
                st = SERVICE_STATUS()
                out["installed"] = True
                if a.QueryServiceStatus(svc, ctypes.byref(st)):
                    out["running"] = st.dwCurrentState == SERVICE_RUNNING
            finally:
                a.CloseServiceHandle(svc)
        finally:
            a.CloseServiceHandle(scm)
        try:
            lines = (LS.program_dir() / "VERSION").read_text("utf-8").split()
            out["version"] = lines[0] if lines else ""
            out["protocol"] = int(lines[1]) if len(lines) > 1 else 0
        except (OSError, ValueError):
            pass
    except Exception as exc:
        log.debug("status: %s", exc)
    return out


# ---- the parts ---------------------------------------------------------------------------------------------------
def python_home() -> Path:
    """The Python that runs Mirrorway (the installer's own copy); a virtual environment cannot be copied."""
    if sys.prefix != sys.base_prefix:
        raise RuntimeError("Lock screen support needs Mirrorway from the installer (setup.exe), "
                           "not a virtual environment")
    return Path(sys.base_prefix)


def copy_files(program: Path, source_code: Path, python: Path) -> None:
    if program.exists():
        shutil.rmtree(program)
    program.mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(python, program / "python", ignore=ignore)
    shutil.copytree(source_code / "mirrorway", program / "mirrorway", ignore=ignore)
    (program / "mirrorway_service.py").write_text(STUB.format(module="service"), "utf-8")
    (program / "mirrorway_helper.py").write_text(STUB.format(module="secure_helper"), "utf-8")
    (program / "VERSION").write_text(f"{__version__}\n{LS.SERVICE_PROTOCOL}\n", "utf-8")


def lock_down(path: Path, run=subprocess.run) -> None:
    """Only SYSTEM and administrators may read or change it."""
    run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{SID_SYSTEM}:(OI)(CI)F", f"{SID_ADMINS}:(OI)(CI)F"],
        check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def trust_from_settings(config_path: Path) -> dict:
    """The device key and the Mac's public key from the user's settings (DPAPI: this runs as that user)."""
    from ..devkeys import b64d, default_store
    cfg = json.loads(Path(config_path).read_text("utf-8"))
    mac_pub = str(cfg.get("mac_pub") or "")
    if not mac_pub:
        raise RuntimeError("Windows is not paired with a Mac yet: pair it first")

    class _Cfg(dict):
        def set(self, k, v, save=True):
            self[k] = v
    key = default_store().load(_Cfg(cfg), "device_key")
    if not key:
        raise RuntimeError("This PC's device key could not be read")
    return LS.make_trust(key, b64d(mac_pub), str(cfg.get("mac_name") or "Mac"), str(config_path),
                         int(cfg.get("port") or 24850))


def write_trust(trust: dict, data: Path) -> None:
    data.mkdir(parents=True, exist_ok=True)
    lock_down(data)
    p = data / "trust.json"
    p.write_text(json.dumps(trust), "utf-8")


def set_sas_policy(data: Path) -> None:
    """Let services simulate Ctrl+Alt+Del; remember the old value (once) to restore it on removal."""
    import winreg
    state_p = data / "state.json"
    state = json.loads(state_p.read_text("utf-8")) if state_p.exists() else {}
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, LS.SAS_POLICY_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as k:
        try:
            old, _t = winreg.QueryValueEx(k, LS.SAS_POLICY_VALUE)
        except OSError:
            old = None
        if "sas_policy_before" not in state:
            state["sas_policy_before"] = old
            state_p.write_text(json.dumps(state), "utf-8")
        if old not in (1, 3):                           # 1: services, 3: services and Ease of Access apps
            winreg.SetValueEx(k, LS.SAS_POLICY_VALUE, 0, winreg.REG_DWORD, 1)


def restore_sas_policy(data: Path) -> None:
    import winreg
    state_p = data / "state.json"
    if not state_p.exists():
        return
    before = json.loads(state_p.read_text("utf-8")).get("sas_policy_before", "missing")
    if before == "missing":
        return
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, LS.SAS_POLICY_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as k:
        if before is None:
            try:
                winreg.DeleteValue(k, LS.SAS_POLICY_VALUE)
            except OSError:
                pass
        else:
            winreg.SetValueEx(k, LS.SAS_POLICY_VALUE, 0, winreg.REG_DWORD, int(before))


def remove_service(wait: float = 20.0) -> None:
    a, SERVICE_STATUS = _advapi()
    scm = a.OpenSCManagerW(None, None, SC_MANAGER_ALL_ACCESS)
    if not scm:
        raise OSError(f"OpenSCManager failed ({ctypes.get_last_error()})")
    try:
        svc = a.OpenServiceW(scm, LS.SERVICE_NAME, SERVICE_ALL_ACCESS)
        if not svc:
            return
        try:
            st = SERVICE_STATUS()
            a.ControlService(svc, SERVICE_CONTROL_STOP, ctypes.byref(st))
            end = time.monotonic() + wait
            while time.monotonic() < end and a.QueryServiceStatus(svc, ctypes.byref(st)) \
                    and st.dwCurrentState != SERVICE_STOPPED:
                time.sleep(0.3)
            a.DeleteService(svc)
        finally:
            a.CloseServiceHandle(svc)
    finally:
        a.CloseServiceHandle(scm)
    time.sleep(0.5)                                   # the service's process lets go of its files


def create_service(program: Path) -> None:
    a, _ = _advapi()
    scm = a.OpenSCManagerW(None, None, SC_MANAGER_ALL_ACCESS)
    if not scm:
        raise OSError(f"OpenSCManager failed ({ctypes.get_last_error()})")
    try:
        for _ in range(20):                              # a service just deleted may still be "marked for deletion"
            svc = a.CreateServiceW(scm, LS.SERVICE_NAME, LS.SERVICE_DISPLAY, SERVICE_ALL_ACCESS,
                                   SERVICE_WIN32_OWN_PROCESS, SERVICE_AUTO_START, SERVICE_ERROR_NORMAL,
                                   service_command(program), None, None, None, None, None)
            err = ctypes.get_last_error()
            if svc or err != ERROR_SERVICE_MARKED_FOR_DELETE:
                break
            time.sleep(0.5)
        if not svc:
            raise OSError(f"CreateService failed ({err})")
        try:
            desc = SERVICE_DESCRIPTIONW("Lets the Mac's keyboard and mouse use Windows' lock screen, Ctrl+Alt+Del "
                                        "and UAC prompts (Mirrorway).")
            a.ChangeServiceConfig2W(svc, SERVICE_CONFIG_DESCRIPTION, ctypes.byref(desc))
            acts = (SC_ACTION * 3)(SC_ACTION(SC_ACTION_RESTART, 5000), SC_ACTION(SC_ACTION_RESTART, 15000),
                                   SC_ACTION(SC_ACTION_RESTART, 60000))
            fa = SERVICE_FAILURE_ACTIONSW(86400, None, None, 3, acts)
            a.ChangeServiceConfig2W(svc, SERVICE_CONFIG_FAILURE_ACTIONS, ctypes.byref(fa))
            if not a.StartServiceW(svc, 0, None):
                raise OSError(f"StartService failed ({ctypes.get_last_error()})")
        finally:
            a.CloseServiceHandle(svc)
    finally:
        a.CloseServiceHandle(scm)


def install(config: Optional[Path], trust: Optional[dict] = None, source: Optional[Path] = None) -> None:
    program, data = LS.program_dir(), LS.data_dir()
    trust = trust if trust is not None else trust_from_settings(Path(config))
    remove_service()                                     # an older version
    for attempt in range(20):                            # its processes may still hold their files a moment
        try:
            copy_files(program, Path(source) if source else Path(__file__).resolve().parents[2], python_home())
            break
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.5)
    write_trust(trust, data)
    set_sas_policy(data)
    create_service(program)
    log.info("Lock screen support %s installed", __version__)


def uninstall() -> None:
    program, data = LS.program_dir(), LS.data_dir()
    remove_service()
    restore_sas_policy(data)
    for p in (program, data):
        for _ in range(40):                              # the service's processes may still hold their files a moment
            if not p.exists():
                break
            shutil.rmtree(p, ignore_errors=True)
            if p.exists():
                time.sleep(0.5)
    try:
        program.parent.rmdir()                          # Program Files\Mirrorway, if nothing else is there
    except OSError:
        pass
    log.info("Lock screen support removed")


def _report(config: Optional[str], action: str, ok: bool, error: str = "") -> None:
    if not config:
        return
    try:
        p = Path(config).with_name("lockscreen-result.json")
        p.write_text(json.dumps({"action": action, "ok": ok, "error": error, "version": __version__,
                                 "time": time.time()}), "utf-8")
    except Exception:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mirrorway.win.svcinstall")
    ap.add_argument("action", choices=["install", "uninstall", "status"])
    ap.add_argument("--config", help="the user's win.json (install: where the pairing comes from)")
    ap.add_argument("--trust-file", help="CI: a ready trust file instead of the user's settings")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if a.action == "status":
        print(json.dumps(status()))
        return 0
    try:
        if a.action == "install":
            trust = json.loads(Path(a.trust_file).read_text("utf-8")) if a.trust_file else None
            install(Path(a.config) if a.config else None, trust)
        else:
            uninstall()
    except Exception as exc:
        log.error("Lock screen support: %s failed: %s", a.action, exc)
        _report(a.config, a.action, False, str(exc))
        return 1
    _report(a.config, a.action, True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
