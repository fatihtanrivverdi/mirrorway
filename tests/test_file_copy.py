"""File copy and paste: files copied with Finder ⌘C / Explorer Ctrl+C are transferred to the other side."""
import os
import queue
import threading
import time
import unicodedata

import pytest
from conftest import DictCfg

from mirrorway import filexfer as FX
from mirrorway import richclip as RC
from mirrorway.clipsync import ClipboardSync


def _tree(root):
    out = {}
    for cur, dirs, files in os.walk(root):
        rel = os.path.relpath(cur, root)
        for d in dirs:
            out[os.path.normpath(os.path.join(rel, d))] = None
        for f in files:
            with open(os.path.join(cur, f), "rb") as fh:
                out[os.path.normpath(os.path.join(rel, f))] = fh.read()
    return out


@pytest.fixture
def sample(tmp_path):
    src = tmp_path / "source"
    (src / "Report" / "sub" / "empty").mkdir(parents=True)
    (src / "Report" / "a.txt").write_bytes(b"goodbye")
    (src / "Report" / "sub" / "größe.bin").write_bytes(os.urandom(FX.CHUNK * 2 + 123))
    (src / "Report" / "sub" / "zéro.txt").write_bytes(b"")
    (src / "single.csv").write_bytes(b"a,b\n1,2\n")
    try:
        os.symlink(src / "single.csv", src / "Report" / "link.csv")      # links are skipped
    except (OSError, NotImplementedError):
        pass
    return src


# ---- names and paths -----------------------------------------------------------------------------
def test_names_are_cleaned_for_target_system():
    assert FX.clean_component('a<b>:c"d|e?f*g', windows=True) == "a_b__c_d_e_f_g"
    assert FX.clean_component("CON.txt", windows=True) == "_CON.txt"
    assert FX.clean_component("dot. ", windows=True) == "dot"
    assert FX.clean_component("a:b", windows=False) == "a:b"
    nfd = unicodedata.normalize("NFD", "café")
    assert FX.clean_component(nfd, windows=True) == "café"              # the Mac's NFD names become NFC
    for bad in ("../x", "/etc/passwd", "a/../b", "C:/Windows", "a//b", "", ".", "\\\\server\\share"):
        assert FX.safe_relpath(bad, windows=True) is None, bad
    assert FX.safe_relpath("Report/sub/a?.txt", windows=True) == "Report/sub/a_.txt"


def test_manifest_walks_folders_and_skips_links(sample):
    man = FX.build_manifest([str(sample / "Report"), str(sample / "single.csv")])
    paths = [i["path"] for i in man.items]
    assert man.roots == ["Report", "single.csv"]
    assert "Report/sub/empty" in paths and "Report/sub/größe.bin" in paths and "Report/link.csv" not in paths
    assert paths.index("Report") < paths.index("Report/a.txt")           # a folder before its contents
    assert man.total == 7 + FX.CHUNK * 2 + 123 + 8
    dup = FX.build_manifest([str(sample / "single.csv"), str(sample / "single.csv")])
    assert dup.roots == ["single.csv", "single (2).csv"]
    with pytest.raises(FX.TransferError):
        FX.build_manifest([str(sample / "Report")], max_items=3)


# ---- transfer (a pair of in-memory channels) ------------------------------------------------------------------
class Pipe:
    """Stands in for SecureChannel: a two-ended queue."""

    def __init__(self, inbox, outbox):
        self.inbox, self.outbox = inbox, outbox
        self.closed = False

    def send(self, mtype, payload=b""):
        if self.closed:
            raise ConnectionError("closed")
        self.outbox.put((mtype, bytes(payload)))

    def send_json(self, mtype, obj):
        import json
        self.send(mtype, json.dumps(obj).encode())

    def recv(self):
        item = self.inbox.get(timeout=5)
        if item is None:
            raise ConnectionError("closed")
        return item

    def close(self):
        self.closed = True
        self.outbox.put(None)


def pipes():
    a, b = queue.Queue(), queue.Queue()
    return Pipe(a, b), Pipe(b, a)


def test_send_and_receive_roundtrip(sample, tmp_path):
    man = FX.build_manifest([str(sample / "Report"), str(sample / "single.csv")])
    tx, rx = pipes()
    progs = []

    def factory(info):
        progs.append(FX.Progress("in", "x", info["total"]))
        return progs[-1]
    t = threading.Thread(target=FX.send_files, args=(tx, man))
    t.start()
    roots, info = FX.recv_files(rx, tmp_path / "target", windows=True, progress_factory=factory)
    t.join()
    assert [r.name for r in roots] == ["Report", "single.csv"]
    got = _tree(tmp_path / "target")
    want = {k: v for k, v in _tree(sample).items() if "link" not in k}
    assert got == want
    assert progs[0].done == progs[0].total == man.total and info["total"] == man.total


def test_receiver_rejects_traversal_and_oversize(tmp_path):
    for items, err in (([{"path": "../escape.txt", "size": 1, "dir": False}], "invalid path"),
                       ([{"path": "a.bin", "size": 10 ** 12, "dir": False}], "too large")):
        tx, rx = pipes()
        tx.send_json(FX.MSG_FILE_MANIFEST, {"id": "x", "items": items})
        with pytest.raises(FX.TransferError, match=err):
            FX.recv_files(rx, tmp_path / "h", windows=False, max_total=10 ** 9)
        assert not (tmp_path / "escape.txt").exists()
    # more data than the file size: treated as corrupt, the partial folder is deleted
    tx, rx = pipes()
    tx.send_json(FX.MSG_FILE_MANIFEST, {"id": "x", "items": [{"path": "a.txt", "size": 2, "dir": False}]})
    tx.send(FX.MSG_FILE_DATA, b"extra data")
    with pytest.raises(FX.TransferError, match="corrupt"):
        FX.recv_files(rx, tmp_path / "h2", windows=False)
    assert not (tmp_path / "h2").exists()
    # the other side refused
    tx, rx = pipes()
    tx.send_json(FX.MSG_FILE_RESULT, {"ok": False, "error": "this copy is no longer valid"})
    with pytest.raises(FX.TransferError, match="no longer valid"):
        FX.recv_files(rx, tmp_path / "h3", windows=False)


def test_staging_keeps_last_few(tmp_path):
    made = []
    for _ in range(5):
        made.append(FX.new_staging(tmp_path))
    left = sorted(p.name for p in FX.staging_root(tmp_path).iterdir())
    assert len(left) == FX.KEEP_STAGING and made[-1].name in left
    assert FX.inside(str(made[-1] / "x.txt"), FX.staging_root(tmp_path))
    assert not FX.inside(str(tmp_path / "other"), FX.staging_root(tmp_path))


# ---- clipboard ---------------------------------------------------------------------------------------------
def test_dropfiles_roundtrip():
    paths = [r"C:\Users\Alex\Documents\größe.xlsx", r"D:\Dossier é"]
    data = RC.paths_to_dropfiles(paths)
    assert data[:4] == (20).to_bytes(4, "little")
    assert RC.dropfiles_to_paths(data) == paths


def test_windows_clipboard_files():
    from mirrorway.win import clipboard as WC
    from test_rich_clipboard import FakeWin32
    api = FakeWin32()
    cb = WC.WinRichClipboard(api=api)
    cb.set_formats({RC.FILES: "C:\\a\\b.txt\nC:\\c".encode()})
    assert RC.dropfiles_to_paths(api.store[WC.CF_HDROP]) == ["C:\\a\\b.txt", "C:\\c"]
    assert api.store[cb.cf_dropeffect] == (1).to_bytes(4, "little")      # copy (not move)
    assert cb.get_formats()[RC.FILES] == "C:\\a\\b.txt\nC:\\c".encode()


def test_mac_clipboard_files_with_stub():
    from test_platform_glue import run_script
    out = run_script("""
        import stubs
        Q, AK, R, pb = stubs.install_mac()
        from mirrorway.mac.clipboard import MacClipboard
        from mirrorway import richclip as RC
        c = MacClipboard()
        c.set_formats({RC.FILES: '/Users/f/.mirrorway/incoming/1/Report\\n/Users/f/.mirrorway/incoming/1/a.txt'.encode()})
        assert [u.path() for u in pb.items['public.file-url']] == ['/Users/f/.mirrorway/incoming/1/Report',
                                                                   '/Users/f/.mirrorway/incoming/1/a.txt']
        assert 'public.utf8-plain-text' not in pb.items       # so Finder does not create a text clipping
        got = c.get_formats()
        assert got[RC.FILES].decode().split('\\n') == ['/Users/f/.mirrorway/incoming/1/Report', '/Users/f/.mirrorway/incoming/1/a.txt']
        print('OK')
    """)
    assert "OK" in out


class FakeRich:
    def __init__(self):
        self.count, self.formats = 0, {}

    def change_count(self):
        return self.count

    def get_formats(self):
        return dict(self.formats)

    def set_formats(self, f):
        self.formats, self.count = dict(f), self.count + 1


def test_sync_hands_files_to_transfer_or_falls_back_to_names():
    b, sent, handed = FakeRich(), [], []
    s = ClipboardSync(b, sent.append, lambda: True, files_fn=lambda p: handed.append(p) or True)
    b.set_formats({RC.FILES: b"/a/Report\n/a/x.txt", RC.TEXT: b"Report\nx.txt", RC.PNG: b"icon"})
    s.poll_once()
    assert handed == [["/a/Report", "/a/x.txt"]] and sent == []
    s.poll_once()
    assert len(handed) == 1                                           # the same copy once
    # the other side does not support it: names go as text, Finder's icon image does not
    b2, sent2 = FakeRich(), []
    s2 = ClipboardSync(b2, sent2.append, lambda: True, files_fn=lambda p: False)
    b2.set_formats({RC.FILES: b"/a/x.txt", RC.TEXT: b"x.txt", RC.PNG: b"icon"})
    s2.poll_once()
    assert sent2 == [{RC.TEXT: b"x.txt"}]
    assert "1 files/folders" in RC.summary({RC.FILES: b"/a/x.txt"})


# ---- end to end: Mac server + Windows client, a real file channel ---------------------------------------------
def test_end_to_end_file_copy_both_ways(sample, tmp_path, monkeypatch):
    from mirrorway.geometry import Rect
    from mirrorway.mac.server import MirrorwayServer
    from mirrorway.win.client import ControlClient, Resolver
    from mirrorway.win.remote import RemoteControl
    from test_e2e import free_port, wait
    from test_router_remote import FakeMac, FakeWin, mac_cfg

    sends = []
    real_send = FX.send_files

    def spy(*args, **kw):
        sends.append(kw.get("packed"))
        return real_send(*args, **kw)
    monkeypatch.setattr(FX, "send_files", spy)
    port = free_port()
    mac_clip, win_clip = FakeRich(), FakeRich()
    srv = MirrorwayServer(mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", clipboard=True), FakeMac(),
                      clipboard_backend=mac_clip, beacon=False, files_dir=tmp_path / "mac")
    srv.clip.interval = 0.05
    srv.start()
    wcfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN", clipboard=True)
    notes = []
    holder = {}

    def factory(send_fn):
        return ClipboardSync(win_clip, send_fn, lambda: True, interval=0.05, peer="Mac",
                             files_fn=lambda p: holder["c"].files.upload(p))
    c = ControlClient(wcfg, Resolver(wcfg, None), RemoteControl(FakeWin(), lambda m, o: None),
                      clipsync_factory=factory, netinfo_fn=lambda: None, monitors_fn=lambda: [Rect(0, 0, 1920, 1080)],
                      files_dir=tmp_path / "win", on_notify=lambda t, m: notes.append((t, m)))
    holder["c"] = c
    c.start()
    try:
        wait(lambda: srv.connected() and c.auth_mode == "key" and srv.supports(FX.FEATURE), timeout=20,
             msg="connection")
        # Finder ⌘C on the Mac -> files on the Windows clipboard
        mac_clip.set_formats({RC.FILES: FX.paths_to_bytes([sample / "Report", sample / "single.csv"]),
                              RC.TEXT: b"Report\nsingle.csv"})
        wait(lambda: win_clip.formats.get(RC.FILES), timeout=10, msg="files on the Windows clipboard")
        got = FX.paths_from_bytes(win_clip.formats[RC.FILES])
        assert [os.path.basename(p) for p in got] == ["Report", "single.csv"]
        assert all(FX.inside(p, FX.staging_root(tmp_path / "win")) for p in got)
        assert _tree(got[0]) == {k[len("Report") + 1:]: v for k, v in _tree(sample).items()
                                 if k.startswith("Report" + os.sep) and "link" not in k}
        wait(lambda: any("Files from the Mac are ready" in t for t, _ in notes), msg="Windows notification")
        time.sleep(0.3)
        assert not srv.file_progress or srv.file_progress.state == "done"
        mac_stage = FX.staging_root(tmp_path / "mac")
        assert not mac_stage.exists() or not any(mac_stage.iterdir())   # not sent back (no echo)

        # Explorer Ctrl+C on Windows -> files on the Mac clipboard
        win_clip.set_formats({RC.FILES: FX.paths_to_bytes([sample / "single.csv"])})
        wait(lambda: FX.paths_from_bytes(mac_clip.formats.get(RC.FILES) or b"") and
             FX.paths_from_bytes(mac_clip.formats[RC.FILES])[0].endswith("single.csv")
             and FX.inside(FX.paths_from_bytes(mac_clip.formats[RC.FILES])[0], FX.staging_root(tmp_path / "mac")),
             timeout=10, msg="file on the Mac clipboard")
        p = FX.paths_from_bytes(mac_clip.formats[RC.FILES])[0]
        assert open(p, "rb").read() == b"a,b\n1,2\n"
        wait(lambda: srv.file_event is not None or srv.status()["files"]["state"] == "done", msg="Mac notification")
        st = srv.status()["files"]
        assert st["dir"] == "in" and st["state"] == "done" and st["percent"] == 100
        assert sends == [True, True]                         # both 3.2: small files packed both ways

        # if file copy and paste is off on the Mac, files from Windows are refused
        srv.cfg.set("file_clipboard", False)
        win_clip.set_formats({RC.FILES: FX.paths_to_bytes([sample / "Report"])})
        wait(lambda: any("Could not send the files" in t for t, _ in notes), timeout=10, msg="refusal notification")
    finally:
        c.stop()
        srv.stop()


def test_refused_copy_is_reported(monkeypatch, tmp_path):
    from mirrorway.mac.server import MirrorwayServer
    from test_router_remote import FakeMac, mac_cfg
    assert FX.names_of(["/a/Report", "/b/x.txt"]) == "Report, x.txt"
    assert FX.names_of([f"/a/{i}" for i in range(5)]) == "0, 1, 2 and 2 more"
    srv = MirrorwayServer(mac_cfg(clipboard=True), FakeMac(), clipboard_backend=FakeRich(), beacon=False,
                      files_dir=tmp_path)
    monkeypatch.setattr(srv, "supports", lambda f: True)

    def too_many(paths, max_items=FX.MAX_ITEMS):
        raise FX.TransferError("too many items (at most 100,000)")
    monkeypatch.setattr(FX, "build_manifest", too_many)
    assert srv.offer_files(["/Users/f/project"]) is True               # names do not go as text either
    assert srv.file_event["title"] == "Files were not sent to Windows"
    assert "project" in srv.file_event["msg"] and "100,000" in srv.file_event["msg"]


def test_mac_notification_uses_osascript():
    from mirrorway.mac import notify as N
    calls = []
    assert N.notify("Mirrorway", 'the "Report" folder\nis ready', subtitle="Files arrived from Windows",
                    popen=lambda args, **kw: calls.append(args))
    args = calls[0]
    assert args[:2] == ["/usr/bin/osascript", "-e"]
    assert args[2] == ('display notification "the \\"Report\\" folder is ready" with title "Mirrorway" '
                       'subtitle "Files arrived from Windows"')
    assert N.quote('a\\b') == '"a\\\\b"'

    def boom(*a, **k):
        raise OSError("missing")
    assert N.notify("x", "y", popen=boom) is False


# ---- 3.2: files read ahead and written by several threads, small files packed ------------------------------------
def _many(root, n=300, seed=7):
    import random
    rnd = random.Random(seed)
    root.mkdir(parents=True)
    for i in range(n):
        d = root / f"pkg{i % 7}" / f"sub{i % 3}"
        d.mkdir(parents=True, exist_ok=True)
        size = rnd.choice([0, 1, 300, 4096, 9000, 70000, FX.SMALL_FILE, FX.SMALL_FILE + 1])
        (d / f"f{i:04d}.py").write_bytes(rnd.randbytes(size))
    (root / "big.bin").write_bytes(os.urandom(FX.CHUNK * 3 + 17))
    (root / "empty_dir").mkdir()
    return root


class Recorder(Pipe):
    """Counts the frames the sender sends."""

    def __init__(self, inbox, outbox):
        super().__init__(inbox, outbox)
        self.types = []

    def send(self, mtype, payload=b""):
        self.types.append(mtype)
        super().send(mtype, payload)


def _transfer(man, dest, packed, readers=FX.READERS, writers=FX.WRITERS, ropen=FX._open_read, wopen=FX._open_write):
    a, b = queue.Queue(), queue.Queue()
    tx, rx = Recorder(a, b), Pipe(b, a)
    out = {}
    t = threading.Thread(target=lambda: out.update(st=FX.send_files(tx, man, packed=packed, readers=readers,
                                                                     opener=ropen)))
    t.start()
    roots, info = FX.recv_files(rx, dest, windows=False, writers=writers, opener=wopen)
    t.join()
    return roots, info, out["st"], tx.types


@pytest.mark.parametrize("packed", [True, False])
def test_many_small_files_roundtrip(tmp_path, packed):
    src = _many(tmp_path / "src" / "project")
    man = FX.build_manifest([str(src)])
    roots, info, st, types = _transfer(man, tmp_path / "dst", packed)
    assert _tree(roots[0]) == _tree(src)
    assert st.bytes == man.total == info["stats"].bytes and info["stats"].packed is packed
    assert st.files == info["stats"].files == sum(1 for i in man.items if not i["dir"])
    data = types.count(FX.MSG_FILE_DATA)
    if packed:
        large = [i["size"] for i in man.items if not i["dir"] and i["size"] > FX.SMALL_FILE]
        assert data == sum(-(-s // FX.CHUNK) for s in large)            # only the large files go in blocks
        assert types.count(FX.MSG_FILE_PACK) < 40 and "packed" in st.text()
    else:
        assert FX.MSG_FILE_PACK not in types and data > 250
    assert types[0] == FX.MSG_FILE_MANIFEST and types[-1] == FX.MSG_FILE_END


def _old_recv(ch, dest):
    """The receiver of 3.1 and earlier (DATA blocks only, one file at a time)."""
    import json
    man = json.loads(ch.recv()[1].decode())
    for it in man["items"]:
        path = dest.joinpath(*it["path"].split("/"))
        if it["dir"]:
            path.mkdir(parents=True, exist_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        left = it["size"]
        with open(path, "wb") as f:
            while left > 0:
                mtype, block = ch.recv()
                assert mtype == FX.MSG_FILE_DATA and 0 < len(block) <= left
                f.write(block)
                left -= len(block)
    assert ch.recv()[0] == FX.MSG_FILE_END


def _old_send(ch, man, chunk=256 * 1024):
    """The sender of 3.1 and earlier."""
    ch.send_json(FX.MSG_FILE_MANIFEST, {"id": man.id, "items": man.items, "total": man.total})
    for item, src in zip(man.items, man.sources):
        if item["dir"]:
            continue
        with open(src, "rb") as f:
            while True:
                block = f.read(chunk)
                if not block:
                    break
                ch.send(FX.MSG_FILE_DATA, block)
    ch.send_json(FX.MSG_FILE_END, {"ok": True})


def test_compatible_with_older_versions(tmp_path):
    src = _many(tmp_path / "src" / "project", n=120)
    man = FX.build_manifest([str(src)])
    # a 3.2 sender to a 3.1 receiver (no "pack" asked for: DATA blocks only)
    tx, rx = pipes()
    t = threading.Thread(target=FX.send_files, args=(tx, man), kwargs={"packed": False})
    t.start()
    _old_recv(rx, tmp_path / "old")
    t.join()
    assert _tree(tmp_path / "old" / "project") == _tree(src)
    # a 3.1 sender to a 3.2 receiver
    tx, rx = pipes()
    t = threading.Thread(target=_old_send, args=(tx, man))
    t.start()
    roots, info = FX.recv_files(rx, tmp_path / "new", windows=True)
    t.join()
    assert _tree(roots[0]) == _tree(src) and info["stats"].packed is False


def test_pack_frames_are_checked(tmp_path):
    import struct
    items = [{"path": "d", "size": 0, "dir": True}, {"path": "d/a", "size": 3, "dir": False},
             {"path": "d/b", "size": 2, "dir": False}, {"path": "d/c", "size": FX.CHUNK + 1, "dir": False}]

    def pack(sizes, body):
        return struct.pack(f">{len(sizes) + 1}I", len(sizes), *sizes) + body
    bad = [
        [(FX.MSG_FILE_PACK, pack([3, 3], b"abcxyz"))],                     # b is 2 bytes, not 3
        [(FX.MSG_FILE_PACK, pack([3, 2], b"abcxyz"))],                     # more content than the sizes
        [(FX.MSG_FILE_PACK, pack([], b""))],                               # an empty pack
        [(FX.MSG_FILE_PACK, b"\x00\x00")],                                 # cut off
        [(FX.MSG_FILE_PACK, pack([3, 2], b"abcxy")), (FX.MSG_FILE_DATA, b"x"),
         (FX.MSG_FILE_PACK, pack([1], b"y"))],                             # a pack in the middle of a file
        [(FX.MSG_FILE_PACK, pack([3, 2], b"abcxy")), (FX.MSG_FILE_PACK, pack([FX.CHUNK + 1], bytes(FX.CHUNK + 1))),
         (FX.MSG_FILE_PACK, pack([1], b"z"))],                             # content past the last file
        [(FX.MSG_FILE_DATA, b"")],                                         # an empty block
    ]
    for k, frames in enumerate(bad):
        tx, rx = pipes()
        tx.send_json(FX.MSG_FILE_MANIFEST, {"id": "x", "items": items, "pack": True})
        for mtype, payload in frames:
            tx.send(mtype, payload)
        tx.send_json(FX.MSG_FILE_END, {"ok": True})
        dest = tmp_path / f"bad{k}"
        with pytest.raises(FX.TransferError):
            FX.recv_files(rx, dest, windows=False)
        assert not dest.exists(), k
    tx, rx = pipes()
    tx.send_json(FX.MSG_FILE_MANIFEST, {"id": "x", "items": items, "pack": True})
    tx.send(FX.MSG_FILE_PACK, pack([3, 2], b"abcxy"))
    tx.send(FX.MSG_FILE_DATA, b"1" * FX.CHUNK)
    tx.send(FX.MSG_FILE_DATA, b"2")
    tx.send_json(FX.MSG_FILE_END, {"ok": True})
    roots, _info = FX.recv_files(rx, tmp_path / "good", windows=False)
    assert _tree(roots[0]) == {"a": b"abc", "b": b"xy", "c": b"1" * FX.CHUNK + b"2"}


def test_parallel_reads_and_writes_hide_slow_file_opening(tmp_path):
    """Opening a file costs ~3 ms here (like a virus scan on Windows): with several threads that cost overlaps."""
    src = tmp_path / "src" / "many"
    src.mkdir(parents=True)
    for i in range(150):
        (src / f"{i:03d}.txt").write_bytes(b"x" * (i * 37))
    man = FX.build_manifest([str(src)])

    def slow(fn):
        def opener(path):
            time.sleep(0.003)
            return fn(path)
        return opener
    times = {}
    for name, threads in (("one", 1), ("many", 8)):
        t = time.monotonic()
        roots, info, st, _ = _transfer(man, tmp_path / name, True, readers=threads, writers=threads,
                                       ropen=slow(FX._open_read), wopen=slow(FX._open_write))
        times[name] = time.monotonic() - t
        assert _tree(roots[0]) == _tree(src)
    assert times["many"] * 2.5 < times["one"], times


def test_sender_errors_and_cancel(tmp_path):
    src = _many(tmp_path / "src" / "p", n=40)
    man = FX.build_manifest([str(src)])
    victim = next(s for i, s in zip(man.items, man.sources) if not i["dir"] and i["size"] > 100)
    with open(victim, "r+b") as f:
        f.truncate(10)                                                 # got shorter after the copy
    tx, _rx = pipes()
    with pytest.raises(FX.TransferError, match="shorter"):
        FX.send_files(tx, man, packed=True)
    os.remove(victim)
    with pytest.raises(OSError):
        FX.send_files(pipes()[0], man, packed=True)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(FX.TransferError, match="cancelled"):
        FX.send_files(pipes()[0], FX.build_manifest([str(src)]), cancel=cancel, packed=True)
    tx, rx = pipes()
    FX.send_files(tx, FX.build_manifest([str(src)]), packed=True)
    with pytest.raises(FX.TransferError, match="cancelled"):
        FX.recv_files(rx, tmp_path / "c", windows=False, cancel=cancel)
    assert not (tmp_path / "c").exists()


def test_manifest_order_matches_a_folder_walk(tmp_path):
    src = _many(tmp_path / "src" / "w", n=60)
    (src / "pkg1" / "sub0" / "deeper" / "deepest").mkdir(parents=True)
    (src / "pkg1" / "sub0" / "deeper" / "z.txt").write_bytes(b"z")
    man = FX.build_manifest([str(src)])
    want = ["w"]
    for cur, dirs, files in os.walk(src):
        dirs.sort()
        rel = os.path.relpath(cur, src)
        prefix = "w" if rel == "." else "w/" + rel.replace(os.sep, "/")
        want += [prefix + "/" + d for d in dirs] + [prefix + "/" + f for f in sorted(files)]
    assert [i["path"] for i in man.items] == want
    sizes = {i["path"]: i["size"] for i in man.items if not i["dir"]}
    assert sizes["w/big.bin"] == FX.CHUNK * 3 + 17
