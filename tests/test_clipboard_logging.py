"""Clipboard transfers are logged (format and size, not content); a locked clipboard is read again;
Windows's clipboard lines go to the Mac log."""
import collections
import logging
import threading
import time

from mirrorway import richclip as RC
from mirrorway.clipsync import READ_RETRIES, ClipboardSync

SECRET = "secret-content-ğüş"
SAMPLE = {RC.TEXT: SECRET.encode(), RC.HTML: f"<b>{SECRET}</b>".encode(), RC.PNG: b"\x89PNG" + b"0" * 3000}


def _wait(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "mirrorway.clipboard"]


def test_sender_logs_formats_and_sizes_not_content(caplog):
    caplog.set_level(logging.INFO, logger="mirrorway.clipboard")
    sent = []
    s = RC.ClipSender(sent.append, lambda: True, lambda t: None, chunk_bytes=1024, peer="Windows")
    s.send(SAMPLE)
    assert _wait(lambda: any("Clipboard sent" in m for m in _lines(caplog)))
    line = next(m for m in _lines(caplog) if "Clipboard sent" in m)
    assert line.startswith("Clipboard sent to Windows: text") and "HTML" in line and "image 3 KB" in line
    assert f"({len(sent)} chunks" in line
    assert SECRET not in caplog.text


def test_sender_text_only_peer_is_logged(caplog):
    caplog.set_level(logging.INFO, logger="mirrorway.clipboard")
    texts = []
    s = RC.ClipSender(lambda p: None, lambda: False, texts.append, peer="Mac")
    s.send(SAMPLE)
    assert texts == [SECRET]
    (line,) = _lines(caplog)
    assert line.startswith("Clipboard sent to Mac: text") and "text only" in line
    assert SECRET not in caplog.text


def test_sender_logs_abort_and_failure(caplog):
    caplog.set_level(logging.INFO, logger="mirrorway.clipboard")
    started, release = threading.Event(), threading.Event()

    def slow(part):
        started.set()
        release.wait(2)

    s = RC.ClipSender(slow, lambda: True, lambda t: None, chunk_bytes=16, peer="Windows")
    s.send({RC.TEXT: b"x" * 300})
    assert started.wait(2)
    s.send({RC.TEXT: b"news"})
    release.set()
    assert _wait(lambda: any("abandoned" in m for m in _lines(caplog)))
    assert _wait(lambda: any("sent to Windows: text 4 B" in m for m in _lines(caplog)))

    def broken(part):
        raise OSError("no connection")

    b = RC.ClipSender(broken, lambda: True, lambda t: None, peer="Mac")
    b.send({RC.TEXT: b"abc"})
    assert _wait(lambda: any(m.startswith("Clipboard could not be sent to Mac") and "no connection" in m
                             for m in _lines(caplog)))


class FakeRich:
    def __init__(self):
        self.count, self.formats, self.fail_reads = 0, {}, 0

    def change_count(self):
        return self.count

    def get_formats(self):
        if self.fail_reads:
            self.fail_reads -= 1
            raise OSError("could not open the clipboard (another app is using it)")
        return dict(self.formats)

    def set_formats(self, f):
        self.formats, self.count = dict(f), self.count + 1


def test_incoming_is_logged_with_direction(caplog):
    caplog.set_level(logging.INFO, logger="mirrorway.clipboard")
    s = ClipboardSync(FakeRich(), lambda f: None, lambda: True, peer="Windows")
    s.apply_remote(SAMPLE)
    s.poll_once()
    (line,) = _lines(caplog)
    assert line.startswith("Clipboard received from Windows: text") and "image 3 KB" in line
    assert SECRET not in caplog.text


def test_incoming_write_failure_is_a_warning(caplog):
    caplog.set_level(logging.INFO, logger="mirrorway.clipboard")

    class Broken(FakeRich):
        def set_formats(self, f):
            raise OSError("could not open the clipboard")

    s = ClipboardSync(Broken(), lambda f: None, lambda: True, peer="Mac")
    s.apply_remote({RC.TEXT: b"abc"})
    s.poll_once()
    rec = [r for r in caplog.records if r.name == "mirrorway.clipboard"][-1]
    assert rec.levelno == logging.WARNING and rec.getMessage().startswith("Could not write the clipboard received from Mac")


def test_locked_clipboard_is_read_again_instead_of_lost():
    b, sent = FakeRich(), []
    s = ClipboardSync(b, sent.append, lambda: True)
    b.set_formats({RC.TEXT: b"copy"})
    b.fail_reads = 2                                   # another app holds the clipboard for a moment
    s.poll_once()
    s.poll_once()
    assert sent == []
    s.poll_once()
    assert sent == [{RC.TEXT: b"copy"}]
    s.poll_once()
    assert len(sent) == 1                              # once


def test_unreadable_clipboard_gives_up_after_retries(caplog):
    caplog.set_level(logging.INFO, logger="mirrorway.clipboard")
    b, sent = FakeRich(), []
    s = ClipboardSync(b, sent.append, lambda: True)
    b.set_formats({RC.TEXT: b"copy"})
    b.fail_reads = 100
    for _ in range(READ_RETRIES + 3):
        s.poll_once()
    assert sent == []
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1 and "was not sent" in warns[0].getMessage()
    b.fail_reads = 0
    b.set_formats({RC.TEXT: b"next"})                 # the next copy goes again
    s.poll_once()
    assert sent == [{RC.TEXT: b"next"}]


def test_windows_forwards_clipboard_info_but_not_other_info():
    from mirrorway.win.client import _RemoteLogHandler
    sink = collections.deque()
    h = _RemoteLogHandler(sink)

    def rec(name, level, msg):
        return logging.LogRecord(name, level, __file__, 1, msg, None, None)

    h.handle(rec("mirrorway.clipboard", logging.INFO, "Clipboard received from Mac: image 240 KB"))
    h.handle(rec("mirrorway.client", logging.INFO, "ordinary info"))
    h.handle(rec("mirrorway.client", logging.WARNING, "a warning"))
    h.handle(rec("mirrorway.viewer", logging.DEBUG, "detail"))
    h.handle(rec("other.module", logging.ERROR, "unrelated"))
    assert [i["msg"] for i in sink] == ["[mirrorway.clipboard] Clipboard received from Mac: image 240 KB",
                                        "[mirrorway.client] a warning"]
    assert sink[0]["level"] == "INFO"


def test_server_and_client_name_their_peer():
    from conftest import DictCfg
    from mirrorway.mac.server import MirrorwayServer
    from mirrorway.win.client import ControlClient, Resolver
    from test_router_remote import FakeMac, mac_cfg

    srv = MirrorwayServer(mac_cfg(clipboard=True), FakeMac(), clipboard_backend=FakeRich(), beacon=False)
    assert srv._clip_sender.peer == "Windows" and srv.clip.peer == "Windows"
    wcfg = DictCfg(host="127.0.0.1", port=1, clipboard=True)
    cli = ControlClient(wcfg, Resolver(wcfg, None), remote=None, netinfo_fn=lambda: None)
    assert cli._clip_sender.peer == "Mac"


def test_windows_app_clipboard_sync_names_mac():
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1].joinpath("mirrorway", "win", "app.py").read_text("utf-8")
    assert 'peer="Mac"' in src
