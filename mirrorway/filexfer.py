"""File copy and paste (the platform-independent, testable part).

When files or folders are copied on one side (Finder ⌘C / Explorer Ctrl+C), their content is transferred right away
over a separate encrypted connection (ROLE_FILE) and written to a hidden "incoming" folder on the other side. When the
transfer finishes, those files are put on the other side's clipboard; ⌘V in Finder / Ctrl+V in Explorer pastes as usual.

Windows always opens the connection:
  Mac -> Windows: the Mac sends MSG_FILES_OFFER on the control channel; Windows opens ROLE_FILE and asks {"op": "get"}.
  Windows -> Mac: Windows opens ROLE_FILE and sends with {"op": "put"}.
On the file channel: MANIFEST (JSON) -> the files' contents in manifest order -> END (JSON) -> RESULT (JSON).
Contents go as DATA blocks (one file at a time, never across a file boundary) or, when the receiver understands it
(3.2+: "pack" in the get request / FEATURE_PACK in the Mac's features), small files go several to a PACK frame.

Folders of many small files are slow when every file waits for the one before it (opening a file costs more than its
content, above all on Windows where Defender scans it), so the sender reads files ahead with several threads and the
receiver writes them with several threads; the network never waits for a single file.
Incoming paths are always sanitised: absolute paths, "..", separators and (on Windows) forbidden characters are refused.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import re
import shutil
import socket
import stat
import struct
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .i18n import T

log = logging.getLogger("mirrorway.files")

FEATURE = "files"
FEATURE_PACK = "files2"            # the receiver understands PACK frames
CHUNK = 1024 * 1024                # a DATA block of a large file
SMALL_FILE = 256 * 1024            # files up to this size are read whole (and packed when the receiver allows)
PACK_BYTES = 1024 * 1024           # a PACK frame is sent when it holds this much…
PACK_FILES = 1024                  # …or this many files
READERS = 8                        # threads reading files ahead (sender)
WRITERS = 6                        # threads writing files (receiver)
READ_AHEAD = 32 * 1024 * 1024      # at most this much read but not yet sent
READ_AHEAD_UNITS = 256
GROUP_FILES = 16                   # a reader/writer thread handles small files this many at a time…
GROUP_BYTES = 256 * 1024           # …or this much
WRITE_BEHIND = 64 * 1024 * 1024    # at most this much received but not yet written
SOCKET_BUFFER = 4 * 1024 * 1024
MAX_TOTAL_DEFAULT = 4 * 1024 ** 3
MAX_ITEMS = 100000                 # most items (files + folders) in one copy
KEEP_STAGING = 3                    # keep this many recent incoming transfers (so they are not deleted mid-paste)

# control channel
MSG_FILES_OFFER = 32               # s->c JSON {"id", "count", "total", "names"}
# file channel (ROLE_FILE)
MSG_FILE_REQ = 70                  # c->s JSON {"op": "get"|"put", "id", "count"?, "total"?}
MSG_FILE_MANIFEST = 71             # sender -> receiver JSON {"id", "items": [{"path", "size", "dir"}], "total"}
MSG_FILE_DATA = 72                 # sender -> receiver, raw bytes (in order, never across a file boundary)
MSG_FILE_END = 73                  # sender -> receiver JSON {"ok": true}
MSG_FILE_RESULT = 74               # JSON {"ok", "error"?, "count"?}
MSG_FILE_PACK = 75                 # sender -> receiver: u32 n, n × u32 size, the n files' contents (the next non-empty
                                   # files of the manifest, whole)
_U32 = struct.Struct(">I")


class TransferError(Exception):
    pass


# ---- names and paths ------------------------------------------------------------------------------
_WIN_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def clean_component(name: str, windows: bool) -> str:
    """A single path component: a valid, harmless name on the target system."""
    name = unicodedata.normalize("NFC", str(name))
    if windows:
        name = _WIN_FORBIDDEN.sub("_", name).rstrip(" .")
        if name.split(".")[0].upper() in _WIN_RESERVED:
            name = "_" + name
    else:
        name = name.replace("/", "_").replace("\x00", "_")
    if name in ("", ".", ".."):
        name = "_"
    return name[:255]


def safe_relpath(rel: str, windows: bool) -> Optional[str]:
    """A relative path from the other side, as "a/b/c", sanitised; None if it cannot be accepted."""
    if not isinstance(rel, str) or not rel or rel.startswith(("/", "\\")) or "\x00" in rel:
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts) or len(parts) > 64:
        return None
    if re.match(r"^[A-Za-z]:", parts[0]):
        return None
    return "/".join(clean_component(p, windows) for p in parts)


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.0f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def names_of(paths: Sequence) -> str:
    """Names of the copied items (short, for logs and notifications)."""
    names = [os.path.basename(str(p).rstrip("/\\")) for p in paths]
    more = T("and {n} more").format(n=len(names) - 3)
    return ", ".join(names[:3]) + (f" {more}" if len(names) > 3 else "")


def describe(names: Sequence[str], count: int, total: int) -> str:
    first = ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
    return T("{n} items, {size} ({names})").format(n=count, size=human_size(total), names=first)


# ---- what to send --------------------------------------------------------------------------------
class Manifest:
    """Items to send: items (what goes to the other side), sources (local path of every file item)."""

    def __init__(self, items: List[dict], sources: List[Optional[str]], roots: List[str]):
        self.items, self.sources, self.roots = items, sources, roots
        self.total = sum(i["size"] for i in items if not i["dir"])
        self.count = len(items)
        self.id = uuid.uuid4().hex[:12]

    def summary(self) -> str:
        return describe(self.roots, len(self.roots), self.total)


def build_manifest(paths: Sequence[str], max_items: int = MAX_ITEMS) -> Manifest:
    """The copied files and folders (including subfolders). Symbolic links and special files are skipped."""
    items: List[dict] = []
    sources: List[Optional[str]] = []
    roots: List[str] = []
    used = set()

    def add(rel: str, src: Optional[str], size: int, is_dir: bool):
        if len(items) >= max_items:
            raise TransferError(T("too many items (at most {n})").format(n=f"{max_items:,}"))
        items.append({"path": rel, "size": int(size), "dir": bool(is_dir)})
        sources.append(src)

    for p in paths:
        p = os.path.abspath(str(p))
        try:
            st = os.lstat(p)
        except OSError as exc:
            raise TransferError(T("could not read {path} ({error})").format(path=p, error=exc))
        if stat.S_ISLNK(st.st_mode) or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
            continue
        base = unicodedata.normalize("NFC", os.path.basename(p.rstrip("/\\")) or "item")
        name, k = base, 2
        while name.lower() in used:                      # two items with the same name: "name (2)"
            stem, ext = os.path.splitext(base)
            name, k = f"{stem} ({k}){ext}", k + 1
        used.add(name.lower())
        roots.append(name)
        if stat.S_ISREG(st.st_mode):
            add(name, p, st.st_size, False)
            continue
        add(name, None, 0, True)
        # depth first, a folder's subfolders and then its files (sorted by name) before going into the subfolders;
        # the sizes come from the folder listing itself (on Windows, a separate lstat per file would be slow)
        stack = [(p, name)]
        while stack:
            cur, prefix = stack.pop()
            try:
                with os.scandir(cur) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue                                   # an unreadable subfolder is skipped (as os.walk did)
            subdirs = []
            for e in entries:
                try:
                    if e.is_symlink() or _is_junction(e) or not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                rel = prefix + "/" + unicodedata.normalize("NFC", e.name)
                add(rel, None, 0, True)
                subdirs.append((e.path, rel))
            for e in entries:
                try:
                    fst = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(fst.st_mode):
                    continue
                add(prefix + "/" + unicodedata.normalize("NFC", e.name), e.path, fst.st_size, False)
            stack.extend(reversed(subdirs))
    if not roots:
        raise TransferError(T("no files to send"))
    return Manifest(items, sources, roots)


def _is_junction(entry) -> bool:
    fn = getattr(entry, "is_junction", None)            # Python 3.12+: Windows junctions are skipped like links
    try:
        return bool(fn()) if fn is not None else False
    except OSError:
        return True


# ---- sending / receiving (over a SecureChannel) -----------------------------------------------------------
class Progress:
    """Progress: done/total bytes, direction and name; safe to read from the UI thread."""

    def __init__(self, direction: str, summary: str, total: int, on_change: Optional[Callable] = None):
        self.direction, self.summary, self.total = direction, summary, int(total)
        self.done = 0
        self.state = "running"               # running / done / failed / cancelled
        self.error = ""
        self.started = time.monotonic()
        self.finished = 0.0
        self._on_change = on_change
        self._last = 0.0

    def add(self, n: int) -> None:
        self.done += n
        now = time.monotonic()
        if self._on_change is not None and now - self._last >= 0.5:
            self._last = now
            self._on_change(self)

    def finish(self, state: str, error: str = "") -> None:
        self.state, self.error, self.finished = state, error, time.monotonic()
        if self._on_change is not None:
            self._on_change(self)

    def percent(self) -> int:
        return 100 if self.total <= 0 else int(100 * self.done / self.total)

    def speed(self) -> float:
        span = (self.finished or time.monotonic()) - self.started
        return self.done / span if span > 0 else 0.0


class Stats:
    """Where a transfer's time went (for the log): waiting for the disk (sender: reading, receiver: writing) and for
    the network (sender: sending, receiver: waiting for data)."""

    def __init__(self, packed: bool = False):
        self.packed = packed
        self.files = self.dirs = self.bytes = self.frames = 0
        self.disk = self.net = 0.0
        self.started = time.monotonic()
        self.elapsed = 0.0

    def done(self) -> "Stats":
        self.elapsed = time.monotonic() - self.started
        return self

    def speed(self) -> float:
        return self.bytes / self.elapsed if self.elapsed > 0 else 0.0

    def text(self) -> str:
        return (f"{self.files} files, {self.dirs} folders in {self.elapsed:.1f} s, {self.frames} frames"
                f"{' (packed)' if self.packed else ''}; waiting for the disk {self.disk:.1f} s, "
                f"for the network {self.net:.1f} s")


def tune_file_socket(sock) -> None:
    """Larger socket buffers for the file channel (only raised, never lowered)."""
    for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
        try:
            if sock.getsockopt(socket.SOL_SOCKET, opt) < SOCKET_BUFFER:
                sock.setsockopt(socket.SOL_SOCKET, opt, SOCKET_BUFFER)
        except (OSError, AttributeError):
            pass


def prepare_file_channel(ch, timeout: float = 60.0) -> None:
    """The file channel's socket: larger buffers, blocking with the system's own timeouts, whole frames per read.
    With a Python timeout (settimeout) every few KB that arrive is a separate wait and read, each taking the GIL
    back; while the writer threads were busy that halved the speed of a folder over Wi-Fi (20 vs 44 MB/s)."""
    sock = ch.sock
    tune_file_socket(sock)
    if not hasattr(socket, "MSG_WAITALL"):
        sock.settimeout(timeout)
        return
    try:
        if os.name == "nt":
            val = int(timeout * 1000)                                  # DWORD milliseconds
        else:
            val = struct.pack("@ll", int(timeout), int(timeout % 1 * 1e6))   # struct timeval
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO, val)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, val)
    except (OSError, AttributeError, struct.error):
        sock.settimeout(timeout)
        return
    sock.settimeout(None)
    ch.waitall = True


def _open_read(path):
    return open(path, "rb")


def _open_write(path):
    return open(path, "wb")


def _shorter(path: str) -> TransferError:
    return TransferError(T("a file got shorter while it was being sent: {path}").format(path=path))


def _read_whole(opener, src, size: int, rel: str) -> bytes:
    with opener(src) as f:
        data = f.read(size)
    if len(data) != size:
        raise _shorter(rel)
    return data


def _open_large(opener, src, size: int, rel: str):
    f = opener(src)
    try:
        first = f.read(min(CHUNK, size))
    except BaseException:
        f.close()
        raise
    if not first:
        f.close()
        raise _shorter(rel)
    return f, first


def _read_group(opener, group) -> List[bytes]:
    return [_read_whole(opener, src, item["size"], item["path"]) for item, src in group]


def send_files(ch, man: Manifest, progress: Optional[Progress] = None,
               cancel: Optional[threading.Event] = None, packed: bool = False, readers: int = READERS,
               opener: Callable = _open_read) -> Stats:
    """Sends the manifest and the files' contents. packed: the receiver understands PACK frames (3.2+).
    Files are read ahead by `readers` threads (small files a group at a time) and sent in manifest order."""
    st = Stats(packed)
    st.dirs = sum(1 for i in man.items if i["dir"])
    st.files = man.count - st.dirs
    head = {"id": man.id, "items": man.items, "total": man.total}
    if packed:
        head["pack"] = True
    ch.send_json(MSG_FILE_MANIFEST, head)
    units = _units(man)                                    # (group of small files | large file, cost)
    pool = ThreadPoolExecutor(max_workers=max(1, readers), thread_name_prefix="mirrorway-read")
    pending: collections.deque = collections.deque()      # (unit, future, cost), in manifest order
    state = {"next": 0, "ahead": 0}
    pack: List[bytes] = []
    sizes: List[int] = []

    def send(mtype: int, payload: bytes, content: int) -> None:
        t = time.monotonic()
        ch.send(mtype, payload)
        st.net += time.monotonic() - t
        st.frames += 1
        st.bytes += content
        if progress is not None:
            progress.add(content)

    def flush() -> None:
        if pack:
            n = state["packed"]
            send(MSG_FILE_PACK, struct.pack(f">{len(sizes) + 1}I", len(sizes), *sizes) + b"".join(pack), n)
            pack.clear()
            sizes.clear()
            state["packed"] = 0

    def fill() -> None:
        while state["next"] < len(units) and len(pending) < READ_AHEAD_UNITS and \
                (not pending or state["ahead"] < READ_AHEAD):
            unit, cost = units[state["next"]]
            state["next"] += 1
            if isinstance(unit, list):
                fut = pool.submit(_read_group, opener, unit)
            else:
                item, src = unit
                fut = pool.submit(_open_large, opener, src, item["size"], item["path"])
            pending.append((unit, fut, cost))
            state["ahead"] += cost

    state["packed"] = 0
    try:
        fill()
        while pending:
            if cancel is not None and cancel.is_set():
                raise TransferError(T("cancelled"))
            unit, fut, cost = pending.popleft()
            t = time.monotonic()
            try:
                res = fut.result()
            finally:
                st.disk += time.monotonic() - t
                state["ahead"] -= cost
            fill()
            if isinstance(unit, list):
                for data in res:
                    if packed:
                        pack.append(data)
                        sizes.append(len(data))
                        state["packed"] += len(data)
                        if state["packed"] >= PACK_BYTES or len(sizes) >= PACK_FILES:
                            flush()
                    else:
                        send(MSG_FILE_DATA, data, len(data))
                continue
            flush()
            item = unit[0]
            f, block = res
            with f:
                left = item["size"]
                while True:
                    send(MSG_FILE_DATA, block, len(block))
                    left -= len(block)
                    if left <= 0:
                        break
                    if cancel is not None and cancel.is_set():
                        raise TransferError(T("cancelled"))
                    t = time.monotonic()
                    block = f.read(min(CHUNK, left))
                    st.disk += time.monotonic() - t
                    if not block:
                        raise _shorter(item["path"])
        flush()
        ch.send_json(MSG_FILE_END, {"ok": True})
    finally:
        for _unit, fut, _cost in pending:
            fut.cancel()
        pool.shutdown(wait=True)
        for unit, fut, _cost in pending:                  # large files opened ahead but not sent
            if not isinstance(unit, list) and fut.done() and not fut.cancelled() and fut.exception() is None:
                fut.result()[0].close()
    return st.done()


def _units(man: Manifest) -> list:
    """What the reader threads read: consecutive small files in groups, large files one by one."""
    units: list = []
    group: list = []
    gbytes = 0
    for item, src in zip(man.items, man.sources):
        size = item["size"]
        if item["dir"] or size <= 0:
            continue
        if size > SMALL_FILE:
            if group:
                units.append((group, gbytes))
                group, gbytes = [], 0
            units.append(((item, src), min(size, CHUNK)))
            continue
        group.append((item, src))
        gbytes += size
        if len(group) >= GROUP_FILES or gbytes >= GROUP_BYTES:
            units.append((group, gbytes))
            group, gbytes = [], 0
    if group:
        units.append((group, gbytes))
    return units


class _Writers:
    """Writes files on a few threads; at most `budget` bytes wait to be written (the receiver then waits)."""

    def __init__(self, n: int, budget: int, st: Stats, cancel: Optional[threading.Event]):
        self.pool = ThreadPoolExecutor(max_workers=max(1, n), thread_name_prefix="mirrorway-write")
        self.budget, self.st, self.cancel = budget, st, cancel
        self.cv = threading.Condition()
        self.pending = 0
        self.error: Optional[BaseException] = None

    def submit(self, fn, cost: int, *args) -> None:
        with self.cv:
            t = time.monotonic()
            while self.pending > 0 and self.pending + cost > self.budget and self.error is None:
                if self.cancel is not None and self.cancel.is_set():
                    raise TransferError(T("cancelled"))
                self.cv.wait(0.2)
            self.st.disk += time.monotonic() - t
            if self.error is not None:
                raise self.error
            self.pending += cost
        self.pool.submit(self._run, fn, cost, args)

    def _run(self, fn, cost: int, args) -> None:
        try:
            fn(*args)
        except BaseException as exc:
            with self.cv:
                if self.error is None:
                    self.error = exc
        finally:
            with self.cv:
                self.pending -= cost
                self.cv.notify_all()

    def finish(self) -> None:
        t = time.monotonic()
        self.pool.shutdown(wait=True)
        self.st.disk += time.monotonic() - t
        if self.error is not None:
            raise self.error

    def abort(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=True)


def _write_group(opener, group) -> None:
    for path, data in group:
        with opener(path) as f:
            if len(data):
                f.write(data)


def _unpack(payload: bytes):
    """A PACK frame's sizes and contents (a memoryview, nothing copied)."""
    if len(payload) < 4:
        raise TransferError(T("corrupt file data"))
    (n,) = _U32.unpack_from(payload, 0)
    if not 1 <= n <= PACK_FILES or len(payload) < 4 + 4 * n:
        raise TransferError(T("corrupt file data"))
    sizes = struct.unpack_from(f">{n}I", payload, 4)
    body = memoryview(payload)[4 + 4 * n:]
    if sum(sizes) != len(body):
        raise TransferError(T("corrupt file data"))
    return sizes, body


def recv_files(ch, dest: Path, windows: bool, max_total: int = MAX_TOTAL_DEFAULT,
               progress_factory: Optional[Callable[[dict], Progress]] = None,
               cancel: Optional[threading.Event] = None, writers: int = WRITERS,
               opener: Callable = _open_write) -> Tuple[List[Path], dict]:
    """Writes the files under dest; returns (paths of the top-level items, manifest + "stats"). On error dest is
    removed. Accepts both DATA blocks (every version) and PACK frames; files are written by `writers` threads."""
    mtype, payload = ch.recv()
    if mtype == MSG_FILE_RESULT:                     # the other side refused (e.g. the copy is no longer current)
        raise TransferError(json.loads(payload.decode("utf-8")).get("error") or T("the other side refused"))
    if mtype != MSG_FILE_MANIFEST:
        raise TransferError(f"unexpected message {mtype}")
    man = json.loads(payload.decode("utf-8"))
    items = man.get("items") or []
    total = sum(int(i.get("size") or 0) for i in items if not i.get("dir"))
    if len(items) > MAX_ITEMS:
        raise TransferError(T("too many items"))
    if total > max_total:
        raise TransferError(T("too large ({size}; limit {limit})").format(size=human_size(total), limit=human_size(max_total)))
    plan: List[Tuple[Path, int, bool]] = []
    seen = set()
    roots: List[str] = []
    for it in items:
        rel = safe_relpath(it.get("path"), windows)
        size = int(it.get("size") or 0)
        if rel is None or size < 0 or rel.lower() in seen:
            raise TransferError(T("invalid path: {path}").format(path=repr(it.get('path'))))
        seen.add(rel.lower())
        top = rel.split("/")[0]
        if top not in roots:
            roots.append(top)
        plan.append((dest.joinpath(*rel.split("/")), size, bool(it.get("dir"))))
    progress = progress_factory(dict(man, total=total, roots=roots)) if progress_factory else None
    st = Stats(bool(man.get("pack")))
    files = [(path, size) for path, size, is_dir in plan if not is_dir]
    st.files, st.dirs = len(files), len(plan) - len(files)
    dest.mkdir(parents=True, exist_ok=True)
    w = _Writers(writers, WRITE_BEHIND, st, cancel)
    cur = None                                       # [open file, bytes left]: a file arriving in DATA blocks
    idx = 0                                          # the next file whose content is expected
    batch: list = []                                 # whole files waiting to be handed to a writer thread
    bbytes = 0

    def corrupt() -> TransferError:
        return TransferError(T("corrupt file data"))

    def submit_batch() -> None:
        nonlocal batch, bbytes
        if batch:
            w.submit(_write_group, bbytes, opener, batch)
            batch, bbytes = [], 0

    def whole(path: Path, data) -> None:
        nonlocal bbytes
        batch.append((path, data))
        bbytes += len(data)
        if len(batch) >= GROUP_FILES or bbytes >= GROUP_BYTES:
            submit_batch()

    def next_file() -> None:
        nonlocal idx
        idx += 1
        while idx < len(files) and files[idx][1] == 0:     # empty files have no content on the wire
            whole(files[idx][0], b"")
            idx += 1

    try:
        t = time.monotonic()
        made = set()
        for path, _size, is_dir in plan:             # all folders first (a folder comes before its contents)
            d = path if is_dir else path.parent
            if d not in made:
                d.mkdir(parents=True, exist_ok=True)
                made.add(d)
        st.disk += time.monotonic() - t
        idx = -1
        next_file()
        while idx < len(files):
            submit_batch()                           # nothing waits for the next frame
            if cancel is not None and cancel.is_set():
                raise TransferError(T("cancelled"))
            t = time.monotonic()
            mtype, block = ch.recv()
            st.net += time.monotonic() - t
            st.frames += 1
            if mtype == MSG_FILE_DATA:
                path, size = files[idx]
                if cur is None and len(block) == size:          # a whole file in one block
                    whole(path, block)
                    next_file()
                else:
                    if not block or len(block) > (size if cur is None else cur[1]):
                        raise corrupt()
                    if cur is None:
                        cur = [opener(path), size]
                    cur[0].write(block)
                    cur[1] -= len(block)
                    if cur[1] == 0:
                        f, cur = cur[0], None
                        w.submit(f.close, 0)                    # closing can be slow (a virus scan): not here
                        next_file()
                n = len(block)
            elif mtype == MSG_FILE_PACK:
                if cur is not None:
                    raise corrupt()
                sizes, body = _unpack(block)
                off = 0
                for sz in sizes:
                    if idx >= len(files) or files[idx][1] != sz:
                        raise corrupt()
                    whole(files[idx][0], body[off:off + sz])
                    off += sz
                    next_file()
                n = off
            else:
                raise corrupt()
            st.bytes += n
            if progress is not None:
                progress.add(n)
        submit_batch()
        mtype, payload = ch.recv()
        if mtype != MSG_FILE_END:
            raise TransferError(T("the transfer did not finish"))
        w.finish()
    except BaseException:
        if cur is not None:
            cur[0].close()
        w.abort()
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return [dest / r for r in roots], dict(man, total=total, roots=roots, stats=st.done())


# ---- incoming folder ------------------------------------------------------------------------------
def staging_root(base: Path) -> Path:
    return Path(base) / "incoming"


def new_staging(base: Path) -> Path:
    root = staging_root(base)
    root.mkdir(parents=True, exist_ok=True)
    cleanup_staging(base, keep=KEEP_STAGING - 1)
    now = time.time()                    # name order = time order (cleanup removes the oldest)
    d = root / (time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1e6) % 1000000:06d}-"
                + uuid.uuid4().hex[:4])
    d.mkdir()
    return d


def cleanup_staging(base: Path, keep: int = KEEP_STAGING) -> None:
    root = staging_root(base)
    if not root.is_dir():
        return
    dirs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    for old in dirs[:max(0, len(dirs) - keep)]:
        shutil.rmtree(old, ignore_errors=True)


def inside(path: str, root: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(str(root)))
        return True
    except (ValueError, OSError):
        return False


def paths_from_bytes(data: bytes) -> List[str]:
    return [p for p in data.decode("utf-8", "replace").split("\n") if p]


def paths_to_bytes(paths: Sequence) -> bytes:
    return "\n".join(str(p) for p in paths).encode("utf-8")


def names_text(paths: Sequence) -> bytes:
    return "\n".join(os.path.basename(str(p).rstrip("/\\")) for p in paths).encode("utf-8")


class Offers:
    """The last copy the Mac offered to Windows (only the latest one can be downloaded)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.current: Optional[Manifest] = None

    def set(self, man: Manifest) -> None:
        with self._lock:
            self.current = man

    def get(self, offer_id: str) -> Optional[Manifest]:
        with self._lock:
            m = self.current
            return m if m is not None and m.id == offer_id else None


def offer_info(man: Manifest) -> Dict:
    return {"id": man.id, "count": len(man.roots), "items": man.count, "total": man.total, "names": man.roots[:20]}
