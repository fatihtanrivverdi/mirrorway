"""The Mirrorway service (LocalSystem, "MirrorwaySecure"): keeps the lock screen helper running in the session at the
console and sends Ctrl+Alt+Del for it. Plain ctypes, no pywin32.

  - The helper is started with this service's own SYSTEM token moved to the console session (CreateProcessAsUser), so
    it can open the Winlogon desktop there; when the console session changes (a user logs on, fast user switching)
    or the helper ends, it is started again.
  - The helper's standard output is a pipe only this service reads: a line "SAS" calls SendSAS(FALSE) (only a service
    may, and only when the SoftwareSASGeneration policy allows services; the installer sets it). Other lines go to
    this service's log.
  - The service has no network connection and takes no other commands.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from ctypes import wintypes
from typing import Callable

from . import lockscreen as LS

log = logging.getLogger("mirrorway.service")

SERVICE_WIN32_OWN_PROCESS = 0x10
SERVICE_STOPPED, SERVICE_START_PENDING, SERVICE_STOP_PENDING, SERVICE_RUNNING = 1, 2, 3, 4
SERVICE_ACCEPT_STOP, SERVICE_ACCEPT_SHUTDOWN, SERVICE_ACCEPT_SESSIONCHANGE = 0x1, 0x4, 0x80
SERVICE_CONTROL_STOP, SERVICE_CONTROL_INTERROGATE, SERVICE_CONTROL_SHUTDOWN = 1, 4, 5
SERVICE_CONTROL_SESSIONCHANGE = 0x0E
NO_ERROR, ERROR_CALL_NOT_IMPLEMENTED, ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 0, 120, 1063
NO_SESSION = 0xFFFFFFFF
WAIT_TIMEOUT = 0x102


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [("dwServiceType", wintypes.DWORD), ("dwCurrentState", wintypes.DWORD),
                ("dwControlsAccepted", wintypes.DWORD), ("dwWin32ExitCode", wintypes.DWORD),
                ("dwServiceSpecificExitCode", wintypes.DWORD), ("dwCheckPoint", wintypes.DWORD),
                ("dwWaitHint", wintypes.DWORD)]


_FN = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)          # CFUNCTYPE: importable (and testable) elsewhere
LPSERVICE_MAIN = _FN(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))
HANDLER_EX = _FN(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID)


class SERVICE_TABLE_ENTRYW(ctypes.Structure):
    _fields_ = [("lpServiceName", wintypes.LPWSTR), ("lpServiceProc", LPSERVICE_MAIN)]


# ---- keeping the helper running (platform independent, tested with a fake launcher) --------------------------------
class Supervisor:
    """Starts the helper in the console session and starts it again when it ends or the session changes."""

    def __init__(self, launch: Callable[[int, Callable[[str], None]], object], session_fn: Callable[[], int],
                 sas_fn: Callable[[], None], max_backoff: float = 30.0):
        self.launch = launch
        self.session_fn = session_fn
        self.sas_fn = sas_fn
        self.max_backoff = max_backoff
        self.child = None
        self.starts = 0
        self.fails = 0
        self._wake = threading.Event()
        self._stop = threading.Event()

    def session_changed(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def on_line(self, line: str) -> None:
        if line == "SAS":
            log.info("Ctrl+Alt+Del asked by the helper")
            try:
                self.sas_fn()
            except Exception:
                log.exception("SendSAS failed")
        elif line:
            log.info("helper: %s", line[:500])

    def step(self) -> float:
        """One round; returns how long to wait before the next one."""
        sid = self.session_fn()
        c = self.child
        if c is not None and (not c.alive() or c.session != sid):
            log.info("Helper %s (session %s, console now %s)", "ended" if not c.alive() else "moves", c.session, sid)
            c.terminate()
            self.child = c = None
        if c is None and sid != NO_SESSION:
            try:
                self.child = self.launch(sid, self.on_line)
                self.starts += 1
                self.fails = 0
                log.info("Helper started in session %s (pid %s)", sid, getattr(self.child, "pid", "?"))
                return 2.0
            except Exception as exc:
                self.fails += 1
                log.warning("Could not start the helper in session %s: %s", sid, exc)
                return min(2.0 ** self.fails, self.max_backoff)
        return 2.0

    def run(self) -> None:
        while not self._stop.is_set():
            wait = self.step()
            self._wake.wait(wait)
            self._wake.clear()
        c, self.child = self.child, None
        if c is not None:
            c.terminate()


# ---- Windows: the helper process ------------------------------------------------------------------------------
class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)), ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD)]


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", wintypes.LPVOID),
                ("bInheritHandle", wintypes.BOOL)]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", wintypes.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]


TOKEN_ASSIGN_PRIMARY, TOKEN_DUPLICATE, TOKEN_QUERY = 0x1, 0x2, 0x8
TOKEN_ADJUST_PRIVILEGES, TOKEN_ADJUST_DEFAULT, TOKEN_ADJUST_SESSIONID = 0x20, 0x80, 0x100
MAXIMUM_ALLOWED = 0x02000000
SECURITY_IMPERSONATION, TOKEN_PRIMARY, TOKEN_SESSION_ID = 2, 1, 12
SE_PRIVILEGE_ENABLED = 0x2
STARTF_USESTDHANDLES = 0x100
CREATE_NO_WINDOW = 0x08000000
HANDLE_FLAG_INHERIT = 0x1

_api_cache: dict = {}


def _api():
    if _api_cache:
        return _api_cache["k"], _api_cache["a"]
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    a = ctypes.WinDLL("advapi32", use_last_error=True)
    H, D, B = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
    sig = {
        (k, "WTSGetActiveConsoleSessionId"): ([], D),
        (k, "GetCurrentProcess"): ([], H),
        (k, "CreatePipe"): ([ctypes.POINTER(H), ctypes.POINTER(H), ctypes.POINTER(SECURITY_ATTRIBUTES), D], B),
        (k, "SetHandleInformation"): ([H, D, D], B),
        (k, "CloseHandle"): ([H], B),
        (k, "WaitForSingleObject"): ([H, D], D),
        (k, "TerminateProcess"): ([H, wintypes.UINT], B),
        (a, "OpenProcessToken"): ([H, D, ctypes.POINTER(H)], B),
        (a, "DuplicateTokenEx"): ([H, D, wintypes.LPVOID, ctypes.c_int, ctypes.c_int, ctypes.POINTER(H)], B),
        (a, "SetTokenInformation"): ([H, ctypes.c_int, wintypes.LPVOID, D], B),
        (a, "LookupPrivilegeValueW"): ([wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(LUID)], B),
        (a, "AdjustTokenPrivileges"): ([H, B, ctypes.POINTER(TOKEN_PRIVILEGES), D, wintypes.LPVOID, wintypes.LPVOID],
                                       B),
        (a, "CreateProcessAsUserW"): ([H, wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.LPVOID, wintypes.LPVOID, B, D,
                                       wintypes.LPVOID, wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
                                       ctypes.POINTER(PROCESS_INFORMATION)], B),
        (a, "StartServiceCtrlDispatcherW"): ([ctypes.POINTER(SERVICE_TABLE_ENTRYW)], B),
        (a, "RegisterServiceCtrlHandlerExW"): ([wintypes.LPCWSTR, HANDLER_EX, wintypes.LPVOID], H),
        (a, "SetServiceStatus"): ([H, ctypes.POINTER(SERVICE_STATUS)], B),
    }
    for (dll, name), (args, res) in sig.items():
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, res
    _api_cache.update(k=k, a=a)
    return k, a


def _check(ok, what: str):
    if not ok:
        raise OSError(f"{what} failed ({ctypes.get_last_error()})")
    return ok


def enable_privilege(name: str) -> None:
    k, a = _api()
    tok = wintypes.HANDLE()
    _check(a.OpenProcessToken(k.GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(tok)),
           "OpenProcessToken")
    try:
        tp = TOKEN_PRIVILEGES(1)
        _check(a.LookupPrivilegeValueW(None, name, ctypes.byref(tp.Privileges[0].Luid)), "LookupPrivilegeValue")
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        _check(a.AdjustTokenPrivileges(tok, False, ctypes.byref(tp), 0, None, None), "AdjustTokenPrivileges")
    finally:
        k.CloseHandle(tok)


class HelperProcess:
    def __init__(self, session: int, h_process, pid: int):
        self.session, self._h, self.pid = session, h_process, pid

    def alive(self) -> bool:
        k, _a = _api()
        return self._h is not None and k.WaitForSingleObject(self._h, 0) == WAIT_TIMEOUT

    def terminate(self) -> None:
        k, _a = _api()
        h, self._h = self._h, None
        if h:
            k.TerminateProcess(h, 1)
            k.CloseHandle(h)


def helper_command() -> tuple:
    """(python.exe, command line, folder) of the helper: the same Python as this service, in Program Files."""
    root = os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv and sys.argv[0] else str(LS.program_dir())
    python = sys.executable
    script = os.path.join(root, "mirrorway_helper.py")
    return python, f'"{python}" -I "{script}"', root


def launch_helper(session: int, on_line: Callable[[str], None]) -> HelperProcess:
    """Starts the helper as SYSTEM in `session` with its output piped to on_line."""
    import msvcrt
    k, a = _api()
    tok, dup = wintypes.HANDLE(), wintypes.HANDLE()
    rd, wr = wintypes.HANDLE(), wintypes.HANDLE()
    _check(a.OpenProcessToken(k.GetCurrentProcess(), TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY
                              | TOKEN_ADJUST_DEFAULT | TOKEN_ADJUST_SESSIONID, ctypes.byref(tok)), "OpenProcessToken")
    try:
        _check(a.DuplicateTokenEx(tok, MAXIMUM_ALLOWED, None, SECURITY_IMPERSONATION, TOKEN_PRIMARY,
                                  ctypes.byref(dup)), "DuplicateTokenEx")
        sid = wintypes.DWORD(session)
        _check(a.SetTokenInformation(dup, TOKEN_SESSION_ID, ctypes.byref(sid), ctypes.sizeof(sid)),
               "SetTokenInformation(TokenSessionId)")
        sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
        _check(k.CreatePipe(ctypes.byref(rd), ctypes.byref(wr), ctypes.byref(sa), 0), "CreatePipe")
        k.SetHandleInformation(rd, HANDLE_FLAG_INHERIT, 0)
        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(STARTUPINFOW)
        si.lpDesktop = "winsta0\\default"
        si.dwFlags = STARTF_USESTDHANDLES
        si.hStdOutput = wr
        si.hStdError = wr
        pi = PROCESS_INFORMATION()
        python, cmd, cwd = helper_command()
        buf = ctypes.create_unicode_buffer(cmd)
        ok = a.CreateProcessAsUserW(dup, python, buf, None, None, True, CREATE_NO_WINDOW, None, cwd,
                                    ctypes.byref(si), ctypes.byref(pi))
        err = ctypes.get_last_error()
        k.CloseHandle(wr)
        wr = None
        if not ok:
            k.CloseHandle(rd)
            raise OSError(f"CreateProcessAsUser failed ({err})")
        k.CloseHandle(pi.hThread)
    finally:
        for h in (dup, tok):
            if h:
                k.CloseHandle(h)
    stream = os.fdopen(msvcrt.open_osfhandle(rd.value, os.O_RDONLY), "rb", buffering=0)

    def reader():
        with stream:
            for raw in iter(stream.readline, b""):
                on_line(raw.decode("utf-8", "replace").strip())
    threading.Thread(target=reader, daemon=True, name="mirrorway-helper-out").start()
    return HelperProcess(session, pi.hProcess, int(pi.dwProcessId))


def send_sas() -> None:
    ctypes.WinDLL("sas").SendSAS(False)


# ---- the service itself --------------------------------------------------------------------------------------------
_state: dict = {}


def _set_status(state: int, accept: int = 0) -> None:
    _k, a = _api()
    st = SERVICE_STATUS(SERVICE_WIN32_OWN_PROCESS, state, accept, NO_ERROR, 0, 0, 3000 if state in
                        (SERVICE_START_PENDING, SERVICE_STOP_PENDING) else 0)
    a.SetServiceStatus(_state["handle"], ctypes.byref(st))


@HANDLER_EX
def _handler(control, event_type, event_data, context):
    sup = _state.get("sup")
    if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
        log.info("Stopping")
        _set_status(SERVICE_STOP_PENDING)
        if sup is not None:
            sup.stop()
        return NO_ERROR
    if control == SERVICE_CONTROL_SESSIONCHANGE:
        if sup is not None:
            sup.session_changed()
        return NO_ERROR
    if control == SERVICE_CONTROL_INTERROGATE:
        return NO_ERROR
    return ERROR_CALL_NOT_IMPLEMENTED


@LPSERVICE_MAIN
def _service_main(argc, argv):
    try:
        k, a = _api()
        _state["handle"] = _check(a.RegisterServiceCtrlHandlerExW(LS.SERVICE_NAME, _handler, None),
                                  "RegisterServiceCtrlHandlerEx")
        try:
            enable_privilege("SeTcbPrivilege")          # to move a token to another session
        except Exception as exc:
            log.warning("SeTcbPrivilege: %s", exc)
        sup = _state["sup"] = Supervisor(launch_helper, lambda: int(k.WTSGetActiveConsoleSessionId()), send_sas)
        _set_status(SERVICE_RUNNING, SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN | SERVICE_ACCEPT_SESSIONCHANGE)
        log.info("Mirrorway service running")
        sup.run()
    except Exception:
        log.exception("service failed")
    finally:
        if "handle" in _state:
            _set_status(SERVICE_STOPPED)
        log.info("Mirrorway service stopped")


def main(argv=None) -> int:
    from logging.handlers import RotatingFileHandler
    d = LS.data_dir()
    d.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(d / "service.log", maxBytes=512 * 1024, backupCount=1, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("mirrorway")
    root.setLevel(logging.INFO)
    root.addHandler(h)
    _k, a = _api()
    table = (SERVICE_TABLE_ENTRYW * 2)(SERVICE_TABLE_ENTRYW(LS.SERVICE_NAME, _service_main),
                                       SERVICE_TABLE_ENTRYW(None, LPSERVICE_MAIN()))
    if not a.StartServiceCtrlDispatcherW(table):
        err = ctypes.get_last_error()
        if err == ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
            print("This is the Mirrorway service; Windows starts it (Settings › General in Mirrorway installs it).")
            return 1
        log.error("StartServiceCtrlDispatcher failed (%s)", err)
        return 1
    return 0
