"""Rich clipboard: text, HTML, RTF, images; chunked transfer; format conversions on Windows and the Mac."""
import io
import struct
import threading
import time

import pytest
from conftest import DictCfg

from mirrorway import richclip as RC
from mirrorway.clipsync import ClipboardSync


def _png(w=40, h=24, rgba=(200, 30, 60, 255)):
    from PIL import Image
    img = Image.new("RGBA", (w, h), rgba)
    for x in range(w):
        img.putpixel((x, h // 2), (0, 0, 0, 255))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def _pixels(png):
    from PIL import Image
    return Image.open(io.BytesIO(png)).convert("RGB")


SAMPLE = {RC.TEXT: "Hello world — ğüşiöç".encode(), RC.HTML: "<b>Bold</b> and <i>italic</i>".encode(),
          RC.RTF: b"{\\rtf1\\ansi {\\b Kal\\'fdn}}", RC.PNG: _png()}


# ---- bundle, chunks ----------------------------------------------------------------------
def test_bundle_roundtrip_and_digest():
    data = RC.pack_bundle(SAMPLE)
    assert RC.unpack_bundle(data) == SAMPLE
    assert RC.digest(SAMPLE) == RC.digest(dict(reversed(list(SAMPLE.items()))))
    assert RC.digest(SAMPLE) != RC.digest({**SAMPLE, RC.TEXT: b"x"})
    assert "image" in RC.summary(SAMPLE) and "text" in RC.summary(SAMPLE)
    with pytest.raises(ValueError):
        RC.unpack_bundle(data[:-5])


def test_limits(monkeypatch):
    monkeypatch.setattr(RC, "MAX_BUNDLE_BYTES", 1000)
    out = RC.limit({RC.TEXT: b"abc", RC.PNG: b"x" * 2000})
    assert out == {RC.TEXT: b"abc"}                          # the image was dropped, the text stayed
    monkeypatch.setattr(RC, "MAX_TEXT_BYTES", 2)
    assert RC.limit({RC.TEXT: b"abc"}) == {}


def test_chunks_and_assembler():
    data = RC.pack_bundle(SAMPLE)
    parts = list(RC.chunks(7, data, size=50))
    assert len(parts) > 4
    asm = RC.Assembler()
    got = [asm.feed(p) for p in parts]
    assert got[:-1] == [None] * (len(parts) - 1) and got[-1] == data
    # when a new clipboard starts, the partial one is discarded
    asm = RC.Assembler()
    for p in parts[:2]:
        asm.feed(p)
    other = list(RC.chunks(8, b"short", size=100))
    assert asm.feed(other[0]) == b"short"
    # if the sequence skips, it waits (a broken bundle is not applied)
    asm = RC.Assembler()
    assert asm.feed(parts[0]) is None and asm.feed(parts[2]) is None and asm.feed(parts[-1]) is None
    # too large
    asm = RC.Assembler(max_bytes=120)
    assert all(asm.feed(p) is None for p in parts)


def test_clip_sender_rich_and_text_fallback():
    sent, texts = [], []
    rich = {"on": False}
    s = RC.ClipSender(sent.append, lambda: rich["on"], texts.append, chunk_bytes=64)
    s.send(SAMPLE)
    assert texts == ["Hello world — ğüşiöç"] and not sent         # an old peer: text only
    rich["on"] = True
    s.send(SAMPLE)
    deadline = time.time() + 5
    while time.time() < deadline:
        asm = RC.Assembler()
        out = [asm.feed(p) for p in list(sent)]
        if out and out[-1]:
            break
        time.sleep(0.02)
    assert RC.unpack_bundle(out[-1]) == SAMPLE


def test_clip_sender_drops_stale_bundle():
    started, release = threading.Event(), threading.Event()
    sent = []

    def slow(part):
        sent.append(part)
        started.set()
        release.wait(2)

    s = RC.ClipSender(slow, lambda: True, lambda t: None, chunk_bytes=16)
    s.send({RC.TEXT: b"x" * 200})                          # 13+ chunks
    assert started.wait(2)
    s.send({RC.TEXT: b"new"})                              # the old one must be abandoned halfway
    release.set()
    time.sleep(0.5)
    asm, done = RC.Assembler(), []
    for p in sent:
        r = asm.feed(p)
        if r is not None:
            done.append(RC.unpack_bundle(r))
    assert done == [{RC.TEXT: b"new"}] and len(sent) < 10


# ---- HTML <-> CF_HTML -------------------------------------------------------------------------
def test_cf_html_roundtrip_with_offsets():
    html = "<p>Café <b>naïve</b> — ğüş 😀</p>"
    data = RC.to_cf_html(html)
    head = data[:200].decode("ascii", "replace")
    s = int(head.split("StartFragment:")[1][:10])
    e = int(head.split("EndFragment:")[1][:10])
    assert data[s:e].decode("utf-8") == html                   # byte offsets are right (UTF-8)
    assert RC.from_cf_html(data + b"\0") == html
    full = '<html><head><meta charset="utf-8"></head><body class="x"><p>A</p></body></html>'
    assert RC.from_cf_html(RC.to_cf_html(full)) == "<p>A</p>"
    assert RC.html_document("<p>A</p>").startswith("<html><head><meta charset=\"utf-8\">")
    assert RC.html_document(full) == full


def test_cf_html_from_browser_sample():
    frag = '<span style="color: red">red</span>'
    body = ("<html>\r\n<body>\r\n<!--StartFragment-->" + frag + "<!--EndFragment-->\r\n</body>\r\n</html>").encode()
    head_tmpl = ("Version:0.9\r\nStartHTML:{:010d}\r\nEndHTML:{:010d}\r\nStartFragment:{:010d}\r\n"
                 "EndFragment:{:010d}\r\nSourceURL:https://example.com/\r\n")
    hl = len(head_tmpl.format(0, 0, 0, 0).encode())
    sf = hl + body.index(b"<!--StartFragment-->") + len(b"<!--StartFragment-->")
    ef = hl + body.index(b"<!--EndFragment-->")
    data = head_tmpl.format(hl, hl + len(body), sf, ef).encode() + body
    assert RC.from_cf_html(data) == frag


# ---- PNG <-> DIB ----------------------------------------------------------------------------
def test_png_dib_roundtrip_flattens_alpha_on_white():
    png = _png(rgba=(0, 0, 255, 0))                          # fully transparent blue
    dib = RC.png_to_dib(png)
    assert struct.unpack_from("<I", dib, 0)[0] == 40 and struct.unpack_from("<H", dib, 14)[0] == 24
    back = _pixels(RC.dib_to_png(dib))
    assert back.size == (40, 24) and back.getpixel((0, 0)) == (255, 255, 255) and back.getpixel((3, 12)) == (0, 0, 0)


def _dib32(w, h, bgra, header=40):
    rows = b"".join(bytes(bgra) * w for _ in range(h))
    if header == 40:                                         # BITMAPINFOHEADER + BI_BITFIELDS masks
        hdr = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 32, 3, len(rows), 2835, 2835, 0, 0)
        return hdr + struct.pack("<III", 0x00FF0000, 0x0000FF00, 0x000000FF) + rows
    hdr = struct.pack("<IiiHHIIiiII", 124, w, h, 1, 32, 3, len(rows), 2835, 2835, 0, 0)
    hdr += struct.pack("<IIII", 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)      # BITMAPV5HEADER
    hdr += struct.pack("<I", 0x73524742) + b"\0" * 36 + b"\0" * 12 + struct.pack("<IIII", 4, 0, 0, 0)
    assert len(hdr) == 124
    return hdr + rows


@pytest.mark.parametrize("header", [40, 124])
def test_dib_bitfields_and_v5_to_png(header):
    png = RC.dib_to_png(_dib32(6, 4, (30, 20, 200, 255), header))      # BGRA: reddish
    assert _pixels(png).getpixel((2, 2)) == (200, 20, 30)


# ---- Windows clipboard (with a fake Win32 layer) ---------------------------------------------------
class FakeWin32:
    def __init__(self):
        self.store, self.names, self.n, self.opened = {}, {}, 0, False

    def seq(self):
        return self.n

    def register(self, name):
        return self.names.setdefault(name, 0xC000 + len(self.names))

    def open(self):
        assert not self.opened
        self.opened = True
        return True

    def close(self):
        self.opened = False

    def empty(self):
        assert self.opened
        self.store, self.n = {}, self.n + 1

    def available(self, fmt):
        return fmt in self.store

    def get(self, fmt):
        assert self.opened
        return self.store.get(fmt)

    def set(self, fmt, data):
        assert self.opened
        self.store[fmt] = bytes(data)
        return True


def test_windows_rich_clipboard_roundtrip():
    from mirrorway.win import clipboard as WC
    api = FakeWin32()
    cb = WC.WinRichClipboard(api)
    cb.set_formats(SAMPLE)
    st = api.store
    assert st[WC.CF_UNICODETEXT].endswith(b"\0\0") and st[WC.CF_UNICODETEXT].decode("utf-16-le").startswith("Hello")
    assert st[cb.cf_html].startswith(b"Version:0.9") and b"<!--StartFragment--><b>" in st[cb.cf_html]
    assert st[cb.cf_png] == SAMPLE[RC.PNG] and WC.CF_DIB in st and st[cb.cf_rtf].endswith(b"\0")
    got = cb.get_formats()
    assert got[RC.TEXT] == SAMPLE[RC.TEXT] and got[RC.HTML] == SAMPLE[RC.HTML] and got[RC.RTF] == SAMPLE[RC.RTF]
    assert got[RC.PNG] == SAMPLE[RC.PNG] and not api.opened
    assert cb.get_text() == "Hello world — ğüşiöç"


def test_windows_dib_only_clipboard_becomes_png():
    from mirrorway.win import clipboard as WC
    api = FakeWin32()
    cb = WC.WinRichClipboard(api)
    api.store[WC.CF_DIB] = _dib32(3, 2, (0, 255, 0, 255))
    got = cb.get_formats()
    assert set(got) == {RC.PNG} and _pixels(got[RC.PNG]).getpixel((1, 1)) == (0, 255, 0)


def test_windows_clipboard_busy_raises():
    from mirrorway.win import clipboard as WC

    class Busy(FakeWin32):
        def open(self):
            return False
    cb = WC.WinRichClipboard(Busy())
    with pytest.raises(OSError):
        cb.get_formats()


# ---- Mac clipboard (with a fake NSPasteboard) -------------------------------------------------------
def test_mac_rich_clipboard_with_stub():
    from test_platform_glue import run_script
    out = run_script("""
        import stubs, io
        Q, AK, R, pb = stubs.install_mac()
        from mirrorway.mac.clipboard import MacClipboard
        from mirrorway import richclip as RC
        from PIL import Image
        c = MacClipboard()
        b = io.BytesIO(); Image.new('RGB', (4, 3), (1, 2, 3)).save(b, 'PNG'); png = b.getvalue()
        c.set_formats({RC.TEXT: 'şık'.encode(), RC.HTML: '<i>x</i>'.encode(), RC.RTF: b'{\\\\rtf1 x}', RC.PNG: png})
        assert pb.items['public.utf8-plain-text'] == 'şık'
        assert pb.items['public.html'].startswith('<html><head><meta charset="utf-8">')
        assert pb.items['public.png'] == png and pb.items['public.rtf'] == b'{\\\\rtf1 x}'
        got = c.get_formats()
        assert got[RC.TEXT] == 'şık'.encode() and got[RC.HTML] == b'<i>x</i>' and got[RC.PNG] == png
        # TIFF only (e.g. older apps): converted to PNG
        b = io.BytesIO(); Image.new('RGB', (5, 2), (9, 8, 7)).save(b, 'TIFF')
        pb.clearContents(); pb.setData_forType_(b.getvalue(), 'public.tiff')
        got = c.get_formats()
        assert set(got) == {RC.PNG} and Image.open(io.BytesIO(got[RC.PNG])).size == (5, 2)
        print('OK')
    """)
    assert "OK" in out


# ---- sync and end to end ----------------------------------------------------------------
class FakeRich:
    def __init__(self):
        self.count, self.formats = 0, {}

    def change_count(self):
        return self.count

    def get_formats(self):
        return dict(self.formats)

    def set_formats(self, f):
        self.formats, self.count = dict(f), self.count + 1

    def user_copies(self, f):
        self.set_formats(f)


def test_sync_sends_rich_and_suppresses_echo():
    a = FakeRich()
    sent = []
    s = ClipboardSync(a, sent.append, lambda: True)
    a.user_copies(SAMPLE)
    s.poll_once()
    assert sent == [SAMPLE]
    s.apply_remote({RC.TEXT: b"remote"})
    s.poll_once()
    assert a.formats == {RC.TEXT: b"remote"} and len(sent) == 1        # what we wrote was not sent back
    s.apply_remote("old text")                                        # an old peer
    s.poll_once()
    assert a.formats == {RC.TEXT: "old text".encode()}


def test_end_to_end_rich_clipboard_both_ways():
    from mirrorway.mac.server import MirrorwayServer
    from mirrorway.win.client import ControlClient, Resolver
    from mirrorway.win.remote import RemoteControl
    from test_e2e import free_port, wait
    from test_router_remote import FakeMac, FakeWin, mac_cfg
    port = free_port()
    mac_clip, win_clip = FakeRich(), FakeRich()
    srv = MirrorwayServer(mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", clipboard=True), FakeMac(),
                      clipboard_backend=mac_clip, beacon=False)
    srv.clip.interval = 0.05
    srv.start()
    wcfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN", clipboard=True)
    holder = {}

    def factory(send_fn):
        sync = ClipboardSync(win_clip, send_fn, lambda: True, interval=0.05)
        return sync
    c = ControlClient(wcfg, Resolver(wcfg, None), RemoteControl(FakeWin(), lambda m, o: holder["c"].send_json(m, o)),
                      clipsync_factory=factory, netinfo_fn=lambda: None)
    holder["c"] = c
    c.start()
    try:
        wait(lambda: srv.connected() and c.auth_mode == "key" and srv.supports(RC.FEATURE), timeout=20,
             msg="connection", state=lambda: (f"mac connected={srv.connected()} win={c.state}/{c.auth_mode} "
                                           f"features={(srv.control.hello.get('features') if srv.control else None)} "
                                           f"error={srv.last_error!r} devices={len(srv.devices())}"))
        big = _png(1200, 900)
        mac_clip.user_copies({RC.TEXT: b"screen", RC.PNG: big, RC.HTML: b"<p>x</p>"})
        wait(lambda: win_clip.formats.get(RC.PNG) == big, timeout=10, msg="image Mac -> Windows")
        assert win_clip.formats[RC.HTML] == b"<p>x</p>"
        win_clip.user_copies({RC.TEXT: "Windows'tan".encode(), RC.RTF: b"{\\rtf1 w}"})
        wait(lambda: mac_clip.formats.get(RC.RTF) == b"{\\rtf1 w}", timeout=10, msg="RTF Windows -> Mac")
        assert mac_clip.formats[RC.TEXT] == "Windows'tan".encode()
    finally:
        c.stop()
        srv.stop()
