"""Platform-independent clipboard sync (text, HTML, RTF, images).

backend: change_count() -> int and either
  - rich: get_formats() -> {"text"|"html"|"rtf"|"png": bytes}, set_formats(dict)
  - text only (old): get_text() -> Optional[str], set_text(str)
send_fn(formats: dict) sends to the other side; apply_remote(formats | str) writes incoming content.
Every incoming copy is logged with its formats and sizes (never the content); richclip.ClipSender logs outgoing ones.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Dict, List, Optional, Union

from . import richclip as RC

log = logging.getLogger("mirrorway.clipboard")

MAX_CLIP_BYTES = RC.MAX_TEXT_BYTES
READ_RETRIES = 5          # if another app holds the clipboard, the copy is not lost: it is read again on the next rounds


def _read(backend) -> Dict[str, bytes]:
    if hasattr(backend, "get_formats"):
        return backend.get_formats() or {}
    text = backend.get_text()
    return {RC.TEXT: text.encode("utf-8")} if text else {}


def _write(backend, formats: Dict[str, bytes]) -> None:
    if hasattr(backend, "set_formats"):
        backend.set_formats(formats)
        return
    text = formats.get(RC.TEXT)
    if text:
        backend.set_text(text.decode("utf-8", "replace"))


class ClipboardSync(threading.Thread):
    def __init__(self, backend, send_fn: Callable[[Dict[str, bytes]], None], enabled_fn: Callable[[], bool],
                 interval: float = 0.4, peer: Optional[str] = None,
                 files_fn: Optional[Callable[[List[str]], bool]] = None):
        super().__init__(daemon=True, name="mirrorway-clipboard")
        self.backend = backend
        self.send_fn = send_fn
        self.enabled_fn = enabled_fn
        self.interval = interval
        self.peer = peer
        self.files_fn = files_fn          # copied files: returns True if the transfer took over (mirrorway.filexfer)
        self._read_failures = 0
        self._incoming: "queue.Queue[Dict[str, bytes]]" = queue.Queue()
        self._stop_evt = threading.Event()
        self._last_digest: Optional[str] = None
        try:
            self._last_count = backend.change_count()
        except Exception:
            self._last_count = None

    def stop(self) -> None:
        self._stop_evt.set()

    def apply_remote(self, content: Union[str, Dict[str, bytes]]) -> None:
        """Content from the other side (may be called from any thread)."""
        if isinstance(content, str):
            content = {RC.TEXT: content.encode("utf-8")}
        self._incoming.put(content)

    def poll_once(self) -> None:
        # 1) Apply incoming content (only the latest matters)
        latest = None
        while True:
            try:
                latest = self._incoming.get_nowait()
            except queue.Empty:
                break
        if latest and self.enabled_fn():
            try:
                self._last_digest = RC.digest(latest)
                _write(self.backend, latest)
                self._last_count = self.backend.change_count()
                log.info("Clipboard received %s: %s", RC.from_peer(self.peer), RC.summary(latest))
            except Exception as exc:
                log.warning("Could not write the clipboard received %s (%s): %s", RC.from_peer(self.peer),
                            RC.summary(latest), exc)
        # 2) Send the local change
        try:
            count = self.backend.change_count()
        except Exception:
            return
        if count == self._last_count:
            return
        try:
            formats = _read(self.backend)
        except Exception as exc:
            self._read_failures += 1
            if self._read_failures < READ_RETRIES:
                log.debug("Could not read the clipboard, will retry: %s", exc)
                return                                 # _last_count unchanged: read again on the next round
            log.warning("Could not read the clipboard (%d attempts), this copy was not sent: %s", self._read_failures, exc)
            self._read_failures = 0
            self._last_count = count
            return
        self._read_failures = 0
        self._last_count = count
        if formats.get(RC.FILES):
            formats = self._files(formats)
            if formats is None:
                return
        formats = RC.limit(formats)
        if not formats:
            return
        d = RC.digest(formats)
        if d == self._last_digest:
            return
        self._last_digest = d
        if self.enabled_fn():
            try:
                self.send_fn(formats)
            except Exception:
                pass

    def _files(self, formats: Dict[str, bytes]) -> Optional[Dict[str, bytes]]:
        """Files were copied: hand them to the file transfer (if the other side supports it). Otherwise the names
        go as text; the file icon image Finder adds is not sent."""
        d = RC.digest(formats)
        if d == self._last_digest:
            return None
        self._last_digest = d
        paths = [p for p in bytes(formats[RC.FILES]).decode("utf-8", "replace").split("\n") if p]
        if self.enabled_fn() and self.files_fn is not None and paths:
            try:
                if self.files_fn(paths):
                    return None
            except Exception as exc:
                log.warning("Could not start the file transfer: %s", exc)
        rest = {k: v for k, v in formats.items() if k not in (RC.FILES, RC.PNG)}
        return rest or None

    def run(self) -> None:
        while not self._stop_evt.is_set():
            self.poll_once()
            self._stop_evt.wait(self.interval)
