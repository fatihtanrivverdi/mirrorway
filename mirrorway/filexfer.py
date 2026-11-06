"""File copy and paste (the platform-independent, testable part).

When files or folders are copied on one side (Finder ⌘C / Explorer Ctrl+C), their content is transferred right away
over a separate encrypted connection (ROLE_FILE) and written to a hidden "incoming" folder on the other side. When the
transfer finishes, those files are put on the other side's clipboard; ⌘V in Finder / Ctrl+V in Explorer pastes as usual.

Windows always opens the connection:
  Mac -> Windows: the Mac sends MSG_FILES_OFFER on the control channel; Windows opens ROLE_FILE and asks {"op": "get"}.
  Windows -> Mac: Windows opens ROLE_FILE and sends with {"op": "put"}.
On the file channel: MANIFEST (JSON) -> DATA chunks for each file in turn -> END (JSON) -> RESULT (JSON).
Incoming paths are always sanitised: absolute paths, "..", separators and (on Windows) forbidden characters are refused.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .i18n import T

log = logging.getLogger("mirrorway.files")

FEATURE = "files"
CHUNK = 256 * 1024
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
        for cur, dirs, files in os.walk(p, followlinks=False):
            dirs.sort()
            rel_cur = os.path.relpath(cur, p)
            prefix = name if rel_cur == "." else name + "/" + "/".join(
                unicodedata.normalize("NFC", c) for c in rel_cur.split(os.sep))
            for d in list(dirs):
                full = os.path.join(cur, d)
                if os.path.islink(full):
                    dirs.remove(d)
                    continue
                add(prefix + "/" + unicodedata.normalize("NFC", d), None, 0, True)
            for f in sorted(files):
                full = os.path.join(cur, f)
                try:
                    fst = os.lstat(full)
                except OSError:
                    continue
                if not stat.S_ISREG(fst.st_mode):
                    continue
                add(prefix + "/" + unicodedata.normalize("NFC", f), full, fst.st_size, False)
    if not roots:
        raise TransferError(T("no files to send"))
    return Manifest(items, sources, roots)


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


def send_files(ch, man: Manifest, progress: Optional[Progress] = None,
               cancel: Optional[threading.Event] = None) -> None:
    ch.send_json(MSG_FILE_MANIFEST, {"id": man.id, "items": man.items, "total": man.total})
    for item, src in zip(man.items, man.sources):
        if item["dir"]:
            continue
        left = item["size"]
        with open(src, "rb") as f:
            while left > 0:
                if cancel is not None and cancel.is_set():
                    raise TransferError(T("cancelled"))
                block = f.read(min(CHUNK, left))
                if not block:
                    raise TransferError(T("a file got shorter while it was being sent: {path}").format(path=item['path']))
                ch.send(MSG_FILE_DATA, block)
                left -= len(block)
                if progress is not None:
                    progress.add(len(block))
    ch.send_json(MSG_FILE_END, {"ok": True})


def recv_files(ch, dest: Path, windows: bool, max_total: int = MAX_TOTAL_DEFAULT,
               progress_factory: Optional[Callable[[dict], Progress]] = None,
               cancel: Optional[threading.Event] = None) -> Tuple[List[Path], dict]:
    """Writes the files under dest; returns (paths of the top-level items, manifest). On error dest is removed."""
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
    dest.mkdir(parents=True, exist_ok=True)
    try:
        for path, size, is_dir in plan:
            if is_dir:
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            left = size
            with open(path, "wb") as f:
                while left > 0:
                    if cancel is not None and cancel.is_set():
                        raise TransferError(T("cancelled"))
                    mtype, block = ch.recv()
                    if mtype != MSG_FILE_DATA or len(block) > left:
                        raise TransferError(T("corrupt file data"))
                    f.write(block)
                    left -= len(block)
                    if progress is not None:
                        progress.add(len(block))
        mtype, payload = ch.recv()
        if mtype != MSG_FILE_END:
            raise TransferError(T("the transfer did not finish"))
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return [dest / r for r in roots], dict(man, total=total, roots=roots)


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
