"""Mirrorway's lock screen helper: LocalSystem in the user's session, started and kept running by the Mirrorway service.

It connects to the paired Mac on its own (ROLE_SECURE, with the device key from the trust file; the Mac must prove
its identity with the key the trust file holds), reports which desktop gets the input ("Default", or "Winlogon" for
the lock screen, Ctrl+Alt+Del and UAC prompts), and while a secure desktop is up the Mac sends the keyboard and mouse
here instead of to the Mirrorway app. It types with SendInput from a thread attached to the input desktop.
Ctrl+Alt+Del cannot be typed: it writes "SAS" to the service, which calls SendSAS.

It accepts input only from that Mac connection; nothing on this PC can make it type.
"""
from __future__ import annotations

import json
import logging
import socket
import sys
import threading
import time
from typing import Callable, Optional

from .. import __version__
from ..protocol import (MSG_BUTTON, MSG_ECHO, MSG_ECHO_REPLY, MSG_ENTER, MSG_EXIT, MSG_KEY, MSG_LOG, MSG_MOVE,
                        MSG_RELEASE_ALL, MSG_SAS, MSG_SCROLL, MSG_SECURE_STATE, MSG_TEXT, ROLE_SECURE, tune_socket,
                        unpack_button, unpack_key, unpack_move, unpack_scroll)
from . import lockscreen as LS
from .securedesk import is_secure

log = logging.getLogger("mirrorway.secure")

INPUT = {MSG_MOVE, MSG_KEY, MSG_BUTTON, MSG_SCROLL, MSG_ENTER, MSG_EXIT, MSG_TEXT, MSG_RELEASE_ALL}


class _MacLog(logging.Handler):
    """The helper's log lines also go to the Mac's log (it has no window; its own file is only for administrators)."""

    def __init__(self, helper: "SecureHelper"):
        super().__init__(logging.INFO)
        self.helper = helper

    def emit(self, record) -> None:
        ch = self.helper.ch
        if ch is None or getattr(ch, "closed", False):
            return
        try:
            ch.send_json(MSG_LOG, {"level": record.levelname, "msg": "[lock screen] " + record.getMessage()})
        except Exception:
            pass


class SecureHelper:
    POLL_S = 0.15            # how often the input desktop is checked
    FOLLOW_S = 0.1           # the input thread re-attaches to the input desktop at most this often

    def __init__(self, trust: dict, backend=None, desktop_fn: Optional[Callable] = None,
                 follow_fn: Optional[Callable] = None, sas_fn: Optional[Callable] = None,
                 address_fn: Optional[Callable] = None, connect_fn: Optional[Callable] = None):
        from .remote import RemoteControl
        self.trust = trust
        if backend is None:
            from .backend import WinInputBackend
            backend = WinInputBackend()
        if desktop_fn is None:
            from .securedesk import input_desktop as desktop_fn
        if follow_fn is None:
            from .securedesk import ThreadDesktop
            follow_fn = ThreadDesktop().follow
        self.desktop_fn = desktop_fn
        self.follow_fn = follow_fn
        self.sas_fn = sas_fn or _ask_service_for_sas
        self.address_fn = address_fn or (lambda: LS.mac_address(trust))
        self.connect_fn = connect_fn or self._connect
        self.remote = RemoteControl(backend, self._send_json)
        self.ch = None
        self.desktop: Optional[str] = "?"
        self._stop = threading.Event()
        self._follow_failed = False

    def stop(self) -> None:
        self._stop.set()
        ch = self.ch
        if ch is not None:
            ch.close()

    def _send_json(self, mtype: int, obj) -> None:
        ch = self.ch
        if ch is not None:
            ch.send_json(mtype, obj)

    def _connect(self, addr):
        from ..secure import client_connect
        sock = socket.create_connection(addr, timeout=10)
        try:
            tune_socket(sock)
            ch, _hello, _ = client_connect(sock, self.trust["identity"], self.trust["mac_pub"], ROLE_SECURE,
                                           {"name": socket.gethostname(), "version": __version__, "role": "secure",
                                            "protocol": LS.SERVICE_PROTOCOL})
        except Exception:
            sock.close()
            raise
        ch.sock.settimeout(30)                       # the Mac sends an echo every 2 s
        return ch

    def run(self) -> None:
        while not self._stop.is_set():
            addr = None
            try:
                addr = self.address_fn()
            except Exception:
                pass
            if addr is None:
                self._stop.wait(3)
                continue
            try:
                self.session(self.connect_fn(addr))
            except Exception as exc:
                if not self._stop.is_set():
                    log.info("Connection to the Mac ended (%s): %s", addr[0], exc)
            self._stop.wait(3)

    def session(self, ch) -> None:
        self.ch = ch
        self.desktop = "?"
        log.info("Connected to the Mac (lock screen helper %s)", __version__)
        threading.Thread(target=self._watch, args=(ch,), daemon=True, name="mirrorway-secure-desktop").start()
        try:
            self._recv_loop(ch)
        finally:
            self.ch = None
            ch.close()
            try:
                self.remote.release_all()
            except Exception:
                pass

    def _watch(self, ch) -> None:
        """Reports the input desktop whenever it changes (and once at the start)."""
        while not ch.closed and not self._stop.is_set():
            try:
                name = self.desktop_fn()
            except Exception:
                name = None
            if name != self.desktop:
                self.desktop = name
                try:
                    ch.send_json(MSG_SECURE_STATE, {"desktop": name or "?", "secure": is_secure(name)})
                except Exception:
                    return
                log.info("Input desktop: %s", name or "? (cannot be opened)")
            time.sleep(self.POLL_S)

    def _recv_loop(self, ch) -> None:
        r = self.remote
        last_follow = -1e9
        while not self._stop.is_set():
            mtype, payload = ch.recv()
            if mtype in INPUT:
                now = time.monotonic()
                if mtype == MSG_ENTER or now - last_follow >= self.FOLLOW_S:
                    last_follow = now
                    try:
                        self.follow_fn()
                        self._follow_failed = False
                    except Exception as exc:
                        if not self._follow_failed:
                            log.warning("Cannot reach the input desktop: %s", exc)
                        self._follow_failed = True
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
            elif mtype == MSG_TEXT:
                r.on_text(payload.decode("utf-8", "replace"))
            elif mtype == MSG_RELEASE_ALL:
                r.release_all()
            elif mtype == MSG_ECHO:
                ch.send(MSG_ECHO_REPLY, payload)
            elif mtype == MSG_SAS:
                log.info("Ctrl+Alt+Del from the Mac")
                try:
                    self.sas_fn()
                except Exception:
                    log.exception("could not ask for Ctrl+Alt+Del")


def _ask_service_for_sas() -> None:
    """The service reads the helper's output: one "SAS" line = Ctrl+Alt+Del (only a service may call SendSAS)."""
    sys.stdout.write("SAS\n")
    sys.stdout.flush()


def main(argv=None) -> int:
    from logging.handlers import RotatingFileHandler
    d = LS.data_dir()
    d.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(d / "helper.log", maxBytes=512 * 1024, backupCount=1, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("mirrorway")
    root.setLevel(logging.INFO)
    root.addHandler(h)
    try:
        trust = LS.read_trust()
    except Exception as exc:
        log.error("No trust file, nothing to do: %s", exc)
        return 2
    from .winapi import set_dpi_aware
    set_dpi_aware()
    log.info("Lock screen helper %s started for %s", __version__, trust["mac_name"])
    helper = SecureHelper(trust)
    root.addHandler(_MacLog(helper))
    helper.run()
    return 0
