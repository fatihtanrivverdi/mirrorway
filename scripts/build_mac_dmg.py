"""CI (macOS): builds the drag-and-drop Mac download, Mirrorway-<version>-mac.dmg (Apple silicon).

  1. Mirrorway.app as built by macapp/build_app.py (the launcher, start.sh, the icon).
  2. Its own Python in Contents/Resources/python: python-build-standalone, pinned by URL and SHA-256, with the
     packages from requirements-mac.txt (pinned versions + hashes). python/.req holds the requirements' SHA-1, so
     start.sh knows the packages match.
  3. The code in Contents/Resources/app; start.sh copies it to ~/.mirrorway/app on the first start (updates then
     go there, outside the bundle).
  4. Ad hoc signature, then a compressed disk image with an Applications shortcut to drag the app onto.

Usage: python scripts/build_mac_dmg.py --app build/Mirrorway.app --out packages [--python-archive file.tar.gz]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mirrorway import __version__  # noqa: E402
from mirrorway import release as R  # noqa: E402

PYTHON_VERSION = "3.14.10"          # the requirements-mac.txt wheels are for CPython 3.14
PYTHON_URL = ("https://github.com/astral-sh/python-build-standalone/releases/download/20261026/"
              "cpython-3.14.10%2B20261026-aarch64-apple-darwin-install_only.tar.gz")
PYTHON_SHA256 = "30a18bcb9ab75e485b4bf4e5d783dde165571e7e42c56dff47a2d2c5345183a4"
CHECK_IMPORTS = ("import objc, AppKit, Quartz, ScreenCaptureKit, CoreMedia, VideoToolbox, rumps, PIL, mss, "
                 "cryptography, numpy, simplejpeg, av, spake2")


def dmg_name(version: str = __version__) -> str:
    return f"Mirrorway-{version}-mac.dmg"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_python(dest: Path) -> Path:
    if not dest.exists() or sha256_of(dest) != PYTHON_SHA256:
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"Downloading Python {PYTHON_VERSION}…")
        with urllib.request.urlopen(PYTHON_URL, timeout=300) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    if sha256_of(dest) != PYTHON_SHA256:
        raise SystemExit("The Python archive is not the pinned one")
    return dest


def _extract_python(archive: Path, res: Path) -> None:
    with tarfile.open(archive) as t:
        for m in t.getmembers():
            p = Path(m.name)
            if p.is_absolute() or ".." in p.parts or not (p.parts and p.parts[0] == "python"):
                raise SystemExit(f"Unexpected path in the Python archive: {m.name}")
            if m.islnk() or (m.issym() and (os.path.isabs(m.linkname) or not _link_inside(m))):
                raise SystemExit(f"Link out of the Python folder: {m.name} -> {m.linkname}")
        if sys.version_info >= (3, 12):
            t.extractall(res, filter="tar")
        else:
            t.extractall(res)


def _link_inside(m: tarfile.TarInfo) -> bool:
    target = os.path.normpath(os.path.join(os.path.dirname(m.name), m.linkname))
    return target == "python" or target.startswith("python/")


def assemble(app_src: Path, work: Path, archive: Path, pip: bool = True) -> Path:
    """Mirrorway.app with its own Python and the code, ready to be signed."""
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    app = work / "Mirrorway.app"
    if app.exists():
        shutil.rmtree(app)
    shutil.copytree(app_src, app, symlinks=True)
    res = app / "Contents" / "Resources"
    _extract_python(Path(archive), res)
    py = res / "python" / "bin" / "python3"
    req = ROOT / "requirements-mac.txt"
    if pip:
        subprocess.run([str(py), "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
                        "--require-hashes", "-r", str(req)], check=True)
        subprocess.run([str(py), "-c", CHECK_IMPORTS], check=True)
    (res / "python" / ".req").write_text(hashlib.sha1(req.read_bytes()).hexdigest() + "\n", encoding="ascii")
    code = res / "app"
    for arc, p in R.app_files(ROOT):
        dest = code / arc
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dest)
    return app


def sign(app: Path) -> None:
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)], check=True)


def make_dmg(app: Path, out_dir: Path, version: str = __version__) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    root = app.parent / "dmgroot"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()
    subprocess.run(["ditto", str(app), str(root / "Mirrorway.app")], check=True)
    os.symlink("/Applications", root / "Applications")
    dmg = out_dir / dmg_name(version)
    if dmg.exists():
        dmg.unlink()
    subprocess.run(["hdiutil", "create", "-volname", "Mirrorway", "-srcfolder", str(root), "-fs", "HFS+",
                    "-format", "UDZO", "-imagekey", "zlib-level=9", "-ov", str(dmg)], check=True)
    return dmg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True, help="Mirrorway.app from macapp/build_app.py")
    ap.add_argument("--out", default="packages")
    ap.add_argument("--work", default="build/dmg")
    ap.add_argument("--python-archive")
    a = ap.parse_args()
    work = Path(a.work)
    archive = Path(a.python_archive) if a.python_archive else fetch_python(work / "python.tar.gz")
    if sha256_of(archive) != PYTHON_SHA256:
        raise SystemExit("The Python archive is not the pinned one")
    app = assemble(Path(a.app), work, archive)
    sign(app)
    dmg = make_dmg(app, Path(a.out))
    print(f"{dmg} ({dmg.stat().st_size // (1024 * 1024)} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
