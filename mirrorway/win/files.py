"""File copy and paste on the Windows side: download what was copied on the Mac, send what was copied on Windows.

Windows opens the connection (ROLE_FILE, with the device key). The transfer runs on its own thread; a new copy in the
same direction abandons the old one. When done, the files are put on the clipboard (CF_HDROP) and a notification appears.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
from pathlib import Path
from typing import Callable, Optional

from .. import __version__
from .. import filexfer as FX
from .. import richclip as RC
from ..i18n import T
from ..protocol import ROLE_FILE, tune_socket

log = logging.getLogger("mirrorway.files")

NOTIFY_START_BYTES = 100 * 1024 * 1024      # transfers larger than this are also announced when they start


class FileClient:
    def __init__(self, control, base_dir, notify: Optional[Callable[[str, str], None]] = None,
                 max_total: int = FX.MAX_TOTAL_DEFAULT):
        self.control = control                  # ControlClient: creds, file_target, server_features, clip
        self.base_dir = Path(base_dir)
        self.notify = notify or (lambda title, msg: None)
        self.max_total = max_total
        self.progress: Optional[FX.Progress] = None
        self._lock = threading.Lock()
        self._jobs = {}                         # direction -> (cancel event, channel box)

    # ---- shared ----------------------------------------------------------------------------
    def supported(self) -> bool:
        c = self.control
        return FX.FEATURE in (c.server_features() or []) and c.creds.paired() and c.file_target is not None

    def enabled(self) -> bool:
        """File copy and paste is on in this PC's settings (the Files tile in the panel)."""
        cfg = getattr(self.control, "cfg", None)
        return cfg is None or bool(cfg.get("file_clipboard", True))

    def _connect(self):
        from .client import open_channel
        host, port = self.control.file_target
        sock = socket.create_connection((host, port), timeout=10)
        try:
            tune_socket(sock)
            FX.tune_file_socket(sock)
            ch, _hello, mode = open_channel(self.control.creds, sock, ROLE_FILE,
                                            {"name": socket.gethostname(), "version": __version__})
        except Exception:
            sock.close()
            raise
        if mode != "key":
            ch.close()
            raise FX.TransferError(T("file transfer needs a device key"))
        ch.sock.settimeout(60)
        return ch

    def _start(self, direction: str, target, *args) -> None:
        with self._lock:
            old = self._jobs.get(direction)
            if old is not None:
                old[0].set()
                if old[1].get("ch") is not None:
                    old[1]["ch"].close()          # a new copy: abandon the old one
            cancel, box = threading.Event(), {}
            self._jobs[direction] = (cancel, box)
        threading.Thread(target=target, args=args + (cancel, box), daemon=True,
                         name=f"mirrorway-files-{direction}").start()

    # ---- Mac -> Windows -------------------------------------------------------------------
    def on_offer(self, info: dict) -> None:
        """Files were copied on the Mac: download them."""
        if not self.supported():
            return
        if not self.enabled():
            log.info("Files copied on the Mac were not received: file copy and paste is off on this PC")
            return
        if int(info.get("total") or 0) > self.max_total:
            log.info("The files copied on the Mac are too large, not received: %s", FX.human_size(int(info["total"])))
            return
        self._start("in", self._download, dict(info))

    def _download(self, info: dict, cancel: threading.Event, box: dict) -> None:
        ch = None
        prog = None
        summary = FX.describe(info.get("names") or [], int(info.get("count") or 0), int(info.get("total") or 0))
        try:
            ch = box["ch"] = self._connect()
            ch.send_json(FX.MSG_FILE_REQ, {"op": "get", "id": info.get("id"), "pack": True})
            if int(info.get("total") or 0) >= NOTIFY_START_BYTES:
                self.notify(T("Receiving files from the Mac"), summary)
            dest = FX.new_staging(self.base_dir)

            def factory(m):
                nonlocal prog
                prog = self.progress = FX.Progress("in", summary, m["total"])
                return prog
            roots, man = FX.recv_files(ch, dest, windows=True, max_total=self.max_total,
                                        progress_factory=factory, cancel=cancel)
            ch.send_json(FX.MSG_FILE_RESULT, {"ok": True, "count": len(roots)})
            clip = self.control.clip
            if clip is not None:
                clip.apply_remote({RC.FILES: FX.paths_to_bytes(roots)})
            if prog is not None:
                prog.finish("done")
            log.info("Files arrived from the Mac: %s, %.1f MB/s — ready to paste in Explorer (%s)", summary,
                     (prog.speed() if prog else 0) / 1048576, man["stats"].text())
            self.notify(T("Files from the Mac are ready"), T("{summary}. Paste them in Explorer with Ctrl+V.").format(summary=summary))
        except Exception as exc:
            if prog is not None:
                prog.finish("cancelled" if cancel.is_set() else "failed", str(exc))
            if not cancel.is_set():
                log.info("Could not receive files from the Mac: %s", exc)
                try:
                    if ch is not None:
                        ch.send_json(FX.MSG_FILE_RESULT, {"ok": False, "error": str(exc)[:200]})
                except Exception:
                    pass
        finally:
            if ch is not None:
                ch.close()

    # ---- Windows -> Mac -------------------------------------------------------------------
    def upload(self, paths) -> bool:
        """Files were copied on Windows (from the clipboard sync). True: the transfer took over."""
        if any(FX.inside(p, FX.staging_root(self.base_dir)) for p in paths):
            return True                          # files that came from the Mac and were put on the clipboard: do not send back
        if not self.enabled():
            return False
        if not self.supported():
            log.info("The copied files were not sent to the Mac (%s): the Mac is not connected or does not support "
                     "file transfer", FX.names_of(paths))
            return False
        try:
            man = FX.build_manifest(paths)
        except FX.TransferError as exc:
            log.info("Could not send the copied files to the Mac (%s): %s", FX.names_of(paths), exc)
            self.notify(T("Files were not sent to the Mac"), f"{FX.names_of(paths)}: {exc}")
            return True
        if man.total > self.max_total:
            log.info("The copied files are too large, not sent to the Mac: %s", man.summary())
            self.notify(T("Files were not sent"), T("{summary} — limit {limit}").format(summary=man.summary(), limit=FX.human_size(self.max_total)))
            return True
        log.info("Files copied, sending them to the Mac: %s", man.summary())
        self._start("out", self._upload, man)
        return True

    def _upload(self, man: FX.Manifest, cancel: threading.Event, box: dict) -> None:
        ch = None
        prog = self.progress = FX.Progress("out", man.summary(), man.total)
        try:
            ch = box["ch"] = self._connect()
            ch.send_json(FX.MSG_FILE_REQ, {"op": "put", "id": man.id, "total": man.total, "count": man.count})
            mtype, payload = ch.recv()
            res = json.loads(payload.decode("utf-8")) if mtype == FX.MSG_FILE_RESULT else {}
            if not res.get("ok"):
                raise FX.TransferError(res.get("error") or "the Mac did not accept it")
            if man.total >= NOTIFY_START_BYTES:
                self.notify(T("Sending files to the Mac"), man.summary())
            packed = FX.FEATURE_PACK in (self.control.server_features() or [])
            stats = FX.send_files(ch, man, prog, cancel, packed=packed)
            mtype, payload = ch.recv()
            res = json.loads(payload.decode("utf-8")) if mtype == FX.MSG_FILE_RESULT else {}
            if not res.get("ok"):
                raise FX.TransferError(res.get("error") or T("the Mac could not receive the files"))
            prog.finish("done")
            log.info("Files sent to the Mac: %s, %.1f MB/s (%s)", man.summary(), prog.speed() / 1048576,
                     stats.text())
        except Exception as exc:
            prog.finish("cancelled" if cancel.is_set() else "failed", str(exc))
            if not cancel.is_set():
                log.info("Could not send the files to the Mac: %s", exc)
                self.notify(T("Could not send the files to the Mac"), str(exc)[:200])
        finally:
            if ch is not None:
                ch.close()
