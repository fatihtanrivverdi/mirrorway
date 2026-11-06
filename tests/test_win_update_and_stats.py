"""Automatic updates, latency measurement, frame2 + statistics."""
import hashlib
import socket
import subprocess
import sys
import time
import zipfile
import io
from pathlib import Path

import pytest
from conftest import DictCfg

import mirrorway
from mirrorway import protocol as P
from mirrorway.mac.server import MirrorwayServer
from mirrorway.win import client as client_mod
from mirrorway.win.client import ControlClient, Resolver, VideoClient
from mirrorway.win.remote import RemoteControl
from mirrorway.win.updater import UpdateError, apply_update
from mirrorway.winbundle import build_windows_bundle
from test_e2e import FakeCapture, free_port, wait
from test_router_remote import FakeMac, FakeWin, mac_cfg

ROOT = Path(__file__).resolve().parent.parent


def make_server(**kw):
    port = free_port()
    cfg = mac_cfg(port=port, pair_code="ABCD-EFGH-JKMN", screen_share=True, display_index=0,
                  quality="low", clipboard=False, **kw)
    srv = MirrorwayServer(cfg, FakeMac(), capture_factory=lambda i, p: FakeCapture(), beacon=False)
    srv.start()
    return srv, port


def test_protocol_helpers():
    assert P.parse_version("1.3.0") > P.parse_version("1.2.9") > P.parse_version("1.2")
    assert P.parse_version("1.10.0") > P.parse_version("1.9.9")
    meta, data = P.unpack_update(P.pack_update({"version": "9.9.9"}, b"zipdata"))
    assert meta == {"version": "9.9.9"} and data == b"zipdata"
    assert P.unpack_echo(P.pack_echo(12.5, 3.25)) == (12.5, 3.25)
    w, h, seq, enc, flags, jpeg = P.unpack_frame2(P.pack_frame2(800, 500, 7, 12.7, b"J"))
    assert (w, h, seq, enc, flags, jpeg) == (800, 500, 7, 12, 0, b"J")


def test_bundle_excludes_mac_and_is_deterministic():
    v, data, sha = build_windows_bundle(ROOT)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert v == mirrorway.__version__ and sha == hashlib.sha256(data).hexdigest()
    assert "mirrorway/win/app.py" in names and "mirrorway/__init__.py" in names
    assert "mirrorway/assets/mirrorway.ico" in names                      # the shortcuts point at it
    assert not any(n.startswith("mirrorway/mac/") or "__pycache__" in n for n in names)
    assert "requirements-windows.txt" in names


def _install_old(tmp_path: Path) -> Path:
    root = tmp_path / "Mirrorway"
    (root / "mirrorway" / "win").mkdir(parents=True)
    (root / "mirrorway" / "__init__.py").write_text('__version__ = "1.2.0"\n')
    (root / "mirrorway" / "win" / "__init__.py").write_text("")
    (root / "requirements-windows.txt").write_bytes((ROOT / "requirements-windows.txt").read_bytes())
    return root


def test_apply_update_swaps_and_keeps_backup(tmp_path):
    root = _install_old(tmp_path)
    v, data, sha = build_windows_bundle(ROOT)
    got = apply_update({"version": v, "sha256": sha}, data, root, python_exe=sys.executable)
    assert got == v
    assert f'"{v}"' in (root / "mirrorway" / "__init__.py").read_text()
    assert (root / "mirrorway.old" / "__init__.py").read_text().strip() == '__version__ = "1.2.0"'
    assert not (root / "_update").exists()


def test_apply_update_rejects_bad_hash_and_bad_code(tmp_path):
    root = _install_old(tmp_path)
    v, data, sha = build_windows_bundle(ROOT)
    with pytest.raises(UpdateError):
        apply_update({"version": v, "sha256": "00"}, data, root)
    # the test import fails -> the installed version is untouched
    fail = lambda args, timeout: subprocess.CompletedProcess(args, 1, "", "SyntaxError")
    with pytest.raises(UpdateError):
        apply_update({"version": v, "sha256": sha}, data, root, runner=fail)
    assert (root / "mirrorway" / "__init__.py").read_text().strip() == '__version__ = "1.2.0"'
    # a harmful path
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.py", "x")
    bad = buf.getvalue()
    with pytest.raises(UpdateError):
        apply_update({"version": v, "sha256": hashlib.sha256(bad).hexdigest()}, bad, root)


def test_apply_update_runs_pip_when_requirements_change(tmp_path):
    root = _install_old(tmp_path)
    (root / "requirements-windows.txt").write_text("Pillow\n")
    v, data, sha = build_windows_bundle(ROOT)
    calls = []

    def runner(args, timeout):
        calls.append(args)
        if "-m" in args and "pip" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

    apply_update({"version": v, "sha256": sha}, data, root, python_exe=sys.executable, runner=runner)
    assert any("pip" in a for a in calls[0])


def test_old_client_gets_update_and_restarts(monkeypatch):
    srv, port = make_server()
    monkeypatch.setattr(client_mod, "__version__", "1.2.0")
    applied, restarted = [], []
    cfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN", clipboard=False)
    holder = {}
    remote = RemoteControl(FakeWin(), lambda m, o: holder["c"].send_json(m, o))
    c = ControlClient(cfg, Resolver(cfg, None), remote, on_update=restarted.append,
                      apply_update_fn=lambda meta, data, root: applied.append((meta, len(data))))
    holder["c"] = c
    c.start()
    try:
        wait(lambda: restarted == [mirrorway.__version__], msg="update")
        assert applied[0][0]["version"] == mirrorway.__version__
        wait(lambda: srv.control and (srv.control.last_update_result or {}).get("ok"), msg="result")
        assert srv.status()["client_version"] == "1.2.0"
    finally:
        c.stop()
        srv.stop()


def test_failed_update_is_not_retried(monkeypatch):
    srv, port = make_server()
    monkeypatch.setattr(client_mod, "__version__", "1.2.0")
    cfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN", clipboard=False)
    tries = []

    def boom(meta, data, root):
        tries.append(1)
        raise UpdateError("test")

    c = ControlClient(cfg, Resolver(cfg, None), RemoteControl(FakeWin(), lambda m, o: None), apply_update_fn=boom)
    c.start()
    try:
        wait(lambda: cfg.get("update_failed") == mirrorway.__version__, msg="failure record")
        wait(lambda: srv.control and (srv.control.last_update_result or {}).get("ok") is False)
        c.kick()                     # reconnect: the same version must not be tried again
        time.sleep(1.5)
        assert len(tries) == 1
    finally:
        c.stop()
        srv.stop()


def test_rtt_and_video_stats():
    srv, port = make_server()
    cfg = DictCfg(host="127.0.0.1", port=port, pair_code="ABCD-EFGH-JKMN", clipboard=False)
    holder = {}
    c = ControlClient(cfg, Resolver(cfg, None), RemoteControl(FakeWin(), lambda m, o: holder["c"].send_json(m, o)))
    holder["c"] = c
    c.start()
    metas = []
    stats = {"fps": 24.0, "kbps": 900.0, "decode_ms": 6.0, "display_ms": 4.0, "latency_ms": 30.0}
    VideoClient.STATS_INTERVAL = 0.2
    vc = VideoClient(cfg, Resolver(cfg, None), on_frame=lambda img, meta: metas.append(meta),
                     on_status=lambda s: None, target_size=lambda: (640, 400), stats_fn=lambda: stats)
    vc.start()
    try:
        # a client connecting with the old code registers its key and reconnects with it; measurements come after that
        wait(lambda: srv.connected() and c.state == "connected" and c.auth_mode == "key", timeout=10,
             msg="connection with the key")
        wait(lambda: srv.control and srv.control.rtt_avg_ms is not None, timeout=10, msg="rtt")
        wait(lambda: c.last_rtt_ms is not None, timeout=10, msg="client rtt")
        wait(lambda: len(metas) >= 3, msg="frames")
        assert all("enc_ms" in m and "decode_ms" in m and m["bytes"] > 0 for m in metas)
        wait(lambda: srv.status()["client_stats"].get("latency_ms") == 30.0, msg="statistics")
        time.sleep(2.2)
        st = srv.status()
        assert st["video_kbps"] > 0 and st["rtt_ms"] is not None
        line = srv.stats_line()
        assert "latency" in line and "fps" in line and "Windows" in line
    finally:
        VideoClient.STATS_INTERVAL = 5.0
        vc.stop()
        c.stop()
        srv.stop()


def test_old_video_client_still_gets_plain_frames():
    srv, port = make_server()
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        ch, _ = P.client_handshake(sock, "ABCD-EFGH-JKMN", P.ROLE_VIDEO, {"name": "old"})
        sock.settimeout(5)
        seen = set()
        for _ in range(6):
            mtype, _payload = ch.recv()
            seen.add(mtype)
        assert P.MSG_FRAME in seen and P.MSG_FRAME2 not in seen
        ch.close()
    finally:
        srv.stop()
