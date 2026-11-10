"""Speed test for copying a folder of many small files (Kapı 6): the 3.1 way and the 3.2 way, through a real encrypted
connection on this machine (localhost).

The folder looks like a real one (a Python app folder: 3,641 files, half under 4 KB, 94% under 64 KB, a few large
ones). After a warm-up, both ways run in turn (--rounds, the order swapped every round) and the median time of each
counts. The 3.2 way has to be at least --min-ratio times as fast, otherwise the exit code is 1.

--link N also receives the folder over an N MB/s link (the sender in its own process), with the socket set up the
3.2.0 way (Python timeout) and the current way (blocking, whole frames).

Run: python tests/bench_files.py [--files 5000] [--min-ratio 1.5] [--link 40] [--dir DIR]
"""
import argparse
import json
import os
import random
import shutil
import statistics
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mirrorway import filexfer as FX  # noqa: E402
from mirrorway.protocol import SecureChannel, tune_socket  # noqa: E402

BUCKETS = ((0.48, 1, 4096), (0.28, 4097, 16384), (0.18, 16385, 65536), (0.05, 65537, 262144),
           (0.01, 262145, 1048576))


def make_folder(root: Path, n: int, seed: int = 1) -> int:
    rnd = random.Random(seed)
    noise = os.urandom(1 << 21)
    root.mkdir(parents=True)
    dirs = [root]
    total = 0
    for i in range(n):
        if i % 12 == 0:
            parent = rnd.choice(dirs[-30:]) if len(dirs) > 1 and rnd.random() < 0.6 else root
            d = parent / f"pkg{len(dirs)}"
            d.mkdir()
            dirs.append(d)
        r, acc = rnd.random(), 0.0
        lo, hi = 1, 4096
        for p, a, b in BUCKETS:
            acc += p
            if r <= acc:
                lo, hi = a, b
                break
        size = rnd.randint(lo, hi)
        off = rnd.randint(0, len(noise) - size)
        (dirs[-1] / f"m{i:05d}.py").write_bytes(noise[off:off + size])
        total += size
    for k, mb in enumerate((8, 12)):                         # a few large files, like .dll/.pyd
        (root / f"large{k}.bin").write_bytes(os.urandom(mb << 20))
        total += mb << 20
    return total


def _prepare(ch, way: str) -> None:
    """The socket set up as that version does it: 3.1 / 3.2.0 a Python timeout, 3.2.1+ prepare_file_channel."""
    if way in ("3.1", "3.2.0"):
        FX.tune_file_socket(ch.sock)
        ch.sock.settimeout(60)
    else:
        FX.prepare_file_channel(ch)


def channel_pair(way: str = "3.2"):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    a = socket.create_connection(srv.getsockname())
    b, _ = srv.accept()
    srv.close()
    k1, k2 = os.urandom(32), os.urandom(32)
    pair = (SecureChannel(a, send_key=k1, recv_key=k2, role=b"F"),
            SecureChannel(b, send_key=k2, recv_key=k1, role=b"F"))
    for ch in pair:
        tune_socket(ch.sock)
        _prepare(ch, way)
    return pair


# ---- a link of a given speed (Wi-Fi): the sender in its own process, sending at most `rate` bytes/s ----------
class Paced:
    """Stands in for the sender's socket: sends in 16 KB pieces, no faster than `rate`."""

    def __init__(self, sock, rate: float):
        self.sock, self.rate = sock, rate
        self.t0 = None
        self.sent = 0

    def sendall(self, data) -> None:
        mv = memoryview(data)
        if self.t0 is None:
            self.t0 = time.monotonic()
        for i in range(0, len(mv), 16384):
            part = mv[i:i + 16384]
            self.sock.sendall(part)
            self.sent += len(part)
            ahead = self.sent / self.rate - (time.monotonic() - self.t0)
            if ahead > 0.002:
                time.sleep(ahead)

    def shutdown(self, how) -> None:
        self.sock.shutdown(how)

    def close(self) -> None:
        self.sock.close()


def paced_sender(src: str, port: int, k1: bytes, k2: bytes, rate: float) -> None:
    sock = socket.create_connection(("127.0.0.1", port))
    tune_socket(sock)
    FX.tune_file_socket(sock)
    ch = SecureChannel(Paced(sock, rate), send_key=k1, recv_key=k2, role=b"F")
    FX.send_files(ch, FX.build_manifest([src]), packed=True)
    sock.shutdown(socket.SHUT_WR)
    while sock.recv(65536):
        pass
    sock.close()


def run_paced(way: str, src: Path, dest: Path, rate: float) -> float:
    """Receiving speed (MB/s) over a `rate` link, the receiving socket set up as `way` does it."""
    import multiprocessing as mp
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    k1, k2 = os.urandom(32), os.urandom(32)
    proc = mp.get_context("spawn").Process(target=paced_sender, args=(str(src), srv.getsockname()[1], k1, k2, rate))
    proc.start()
    conn, _ = srv.accept()
    srv.close()
    tune_socket(conn)
    rx = SecureChannel(conn, send_key=k2, recv_key=k1, role=b"F")
    _prepare(rx, way)
    try:
        _roots, info = FX.recv_files(rx, dest, windows=os.name == "nt")
    finally:
        rx.close()
        proc.join(60)
    st = info["stats"]
    speed = st.bytes / st.elapsed / 1048576
    print(f"  {way} socket: {speed:.1f} MB/s ({st.text()})", flush=True)
    return speed


# ---- 3.1 (one file after the other on both sides) ------------------------------------------------------------
def old_send(ch, man, chunk=256 * 1024):
    ch.send_json(FX.MSG_FILE_MANIFEST, {"id": man.id, "items": man.items, "total": man.total})
    for item, src in zip(man.items, man.sources):
        if item["dir"]:
            continue
        left = item["size"]
        with open(src, "rb") as f:
            while left > 0:
                block = f.read(min(chunk, left))
                ch.send(FX.MSG_FILE_DATA, block)
                left -= len(block)
    ch.send_json(FX.MSG_FILE_END, {"ok": True})


def old_recv(ch, dest: Path):
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
                f.write(block)
                left -= len(block)
    ch.recv()


def run(way: str, src: Path, dest: Path, readers: int = FX.READERS, writers: int = FX.WRITERS) -> float:
    man = FX.build_manifest([str(src)])
    tx, rx = channel_pair(way)
    err = []

    def sender():
        try:
            if way == "3.1":
                old_send(tx, man)
            else:
                FX.send_files(tx, man, packed=True, readers=readers)
        except Exception as exc:                             # noqa: BLE001
            err.append(exc)
    t0 = time.monotonic()
    th = threading.Thread(target=sender)
    th.start()
    if way == "3.1":
        old_recv(rx, dest)
        stats = ""
    else:
        _roots, info = FX.recv_files(rx, dest, windows=os.name == "nt", writers=writers)
        stats = info["stats"].text()
    th.join()
    took = time.monotonic() - t0
    tx.close()
    rx.close()
    if err:
        raise err[0]
    if (readers, writers) != (FX.READERS, FX.WRITERS):
        way += f" ({readers} readers, {writers} writers)"
    print(f"  {way}: {took:.2f} s, {man.total / took / 1048576:.1f} MB/s {stats}", flush=True)
    return took


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=5000)
    ap.add_argument("--min-ratio", type=float, default=1.0)
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--sweep", action="store_true", help="also try other numbers of reader and writer threads")
    ap.add_argument("--link", type=float, default=0, help="also receive over a link of this many MB/s (Wi-Fi), "
                                                           "with the 3.2.0 socket and the current one")
    ap.add_argument("--dir", help="where to make the test folder (default: a temporary folder)")
    a = ap.parse_args()
    base = Path(tempfile.mkdtemp(prefix="mirrorway-bench-", dir=a.dir))
    try:
        src = base / "src" / "project"
        total = make_folder(src, a.files)
        print(f"Test folder: {a.files} small files + 2 large, {total / 1048576:.1f} MB")
        times = {"3.1": [], "3.2": []}
        for rnd in range(a.rounds + 1):                      # in turn, the order swapped every round
            if rnd == 0:
                print("Warm-up (not counted):")
            for way in (("3.1", "3.2") if rnd % 2 == 0 else ("3.2", "3.1")):
                dest = base / f"{way}-{rnd}"
                took = run(way, src, dest)
                if rnd:
                    times[way].append(took)
                shutil.rmtree(dest, ignore_errors=True)
                time.sleep(1)                                # let the disk settle a little
        if a.sweep:
            print("Threads:")
            for readers, writers in ((4, 4), (8, 6), (8, 12), (16, 12), (16, 24)):
                ts = []
                for k in range(3):
                    dest = base / f"sweep-{readers}-{writers}-{k}"
                    ts.append(run("3.2", src, dest, readers, writers))
                    shutil.rmtree(dest, ignore_errors=True)
                    time.sleep(1)
                print(f"  {readers} readers, {writers} writers: median {total / statistics.median(ts) / 1048576:.1f} MB/s")
        if a.link:
            print(f"Over a {a.link:.0f} MB/s link (the sender in its own process):")
            speeds = {"3.2.0": [], "3.2.1": []}
            for rnd in range(2):
                for way in (("3.2.0", "3.2.1") if rnd % 2 == 0 else ("3.2.1", "3.2.0")):
                    dest = base / f"link-{way}-{rnd}"
                    speeds[way].append(run_paced(way, src, dest, a.link * 1048576))
                    shutil.rmtree(dest, ignore_errors=True)
                    time.sleep(1)
            print(f"LINK: 3.2.0 socket {max(speeds['3.2.0']):.1f} MB/s, current {max(speeds['3.2.1']):.1f} MB/s "
                  f"(link {a.link:.0f} MB/s)")
        best = {way: statistics.median(ts) for way, ts in times.items()}
        ratio = best["3.1"] / best["3.2"]
        print(f"RESULT: 3.1 {total / best['3.1'] / 1048576:.1f} MB/s, 3.2 {total / best['3.2'] / 1048576:.1f} MB/s, "
              f"{ratio:.2f}× (needed {a.min_ratio:.2f}×)")
        return 0 if ratio >= a.min_ratio else 1
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
