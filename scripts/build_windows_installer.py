"""CI (Windows): builds the one-click Windows installer, Mirrorway-<version>-windows-setup.exe.

  1. A private Python: python-build-standalone (it has Tk, unlike the embeddable package from python.org), pinned
     by URL and SHA-256, extracted to <stage>/python.
  2. The packages from requirements-windows.txt (pinned versions + hashes) installed into that Python.
  3. The code: the same files as the in-app Windows update (mirrorway/ without mac/, requirements, changelog),
     plus LICENSE and debug.bat.
  4. Inno Setup (preinstalled on GitHub's Windows runners) packs it: wininstaller/Mirrorway.iss.

Usage: python scripts/build_windows_installer.py --out packages [--python-archive file.tar.gz] [--no-compile]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mirrorway import __version__  # noqa: E402
from mirrorway.winbundle import build_windows_bundle  # noqa: E402

PYTHON_VERSION = "3.12.16"          # the requirements-windows.txt wheels are for CPython 3.12 (win_amd64)
PYTHON_URL = ("https://github.com/astral-sh/python-build-standalone/releases/download/20261026/"
              "cpython-3.12.16%2B20261026-x86_64-pc-windows-msvc-install_only.tar.gz")
PYTHON_SHA256 = "590dfcbe3a34c07301e6332e13e2188b98655cd072b08afa45a7998dc4c22bc5"
ISS = ROOT / "wininstaller" / "Mirrorway.iss"
DEBUG_BAT = '@echo off\r\ncd /d "%~dp0"\r\n"python\\python.exe" -m mirrorway.win --debug\r\npause\r\n'


def setup_name(version: str = __version__) -> str:
    return f"Mirrorway-{version}-windows-setup.exe"


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
    got = sha256_of(dest)
    if got != PYTHON_SHA256:
        raise SystemExit(f"The Python archive's SHA-256 is wrong: {got}")
    return dest


def _extract_python(archive: Path, stage: Path) -> None:
    with tarfile.open(archive) as t:
        for m in t.getmembers():
            p = Path(m.name)
            if p.is_absolute() or ".." in p.parts or not (p.parts and p.parts[0] == "python"):
                raise SystemExit(f"Unexpected path in the Python archive: {m.name}")
            if m.issym() or m.islnk():
                raise SystemExit(f"Link in the Python archive: {m.name}")
        if sys.version_info >= (3, 12):
            t.extractall(stage, filter="data")
        else:
            t.extractall(stage)


def stage(out_dir: Path, archive: Path, pip: bool = True) -> Path:
    """The folder the installer installs, as it will be on the PC."""
    root = Path(out_dir) / "stage"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    _extract_python(Path(archive), root)
    if not (root / "python" / "python.exe").exists():
        raise SystemExit("python.exe is missing from the Python archive")
    _version, data, _sha = build_windows_bundle(ROOT)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(root)
    shutil.copyfile(ROOT / "LICENSE", root / "LICENSE")
    (root / "debug.bat").write_bytes(DEBUG_BAT.encode("ascii"))
    if pip:
        py = str(root / "python" / "python.exe")
        subprocess.run([py, "-m", "pip", "install", "--disable-pip-version-check", "--no-warn-script-location",
                        "--require-hashes", "--only-binary=:all:", "-r", str(root / "requirements-windows.txt")],
                       check=True)
        subprocess.run([py, "-c", "import tkinter, PIL, pystray, pyperclip, cryptography, numpy, av, sdl2, "
                                  "spake2, simplejpeg; import mirrorway.win.app"], check=True, cwd=str(root))
    return root


def find_iscc() -> str:
    for c in (shutil.which("iscc"), shutil.which("ISCC"), r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
              r"C:\Program Files\Inno Setup 6\ISCC.exe"):
        if c and Path(c).exists():
            return c
    raise SystemExit("Inno Setup (ISCC.exe) was not found")


def compile_installer(root: Path, out_dir: Path, version: str = __version__) -> Path:
    out_dir = Path(out_dir).resolve()
    subprocess.run([find_iscc(), "/Q", f"/DMyVersion={version}", f"/DSourceDir={Path(root).resolve()}",
                    f"/DOutputDir={out_dir}", str(ISS)], check=True)
    exe = out_dir / setup_name(version)
    if not exe.exists():
        raise SystemExit(f"Inno Setup did not make {exe.name}")
    return exe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="packages")
    ap.add_argument("--work", default="build/wininstaller")
    ap.add_argument("--python-archive", help="a local copy of the pinned Python archive")
    ap.add_argument("--no-compile", action="store_true", help="only prepare the folder (no Inno Setup)")
    a = ap.parse_args()
    work = Path(a.work)
    archive = Path(a.python_archive) if a.python_archive else fetch_python(work / "python.tar.gz")
    if sha256_of(archive) != PYTHON_SHA256:
        raise SystemExit("The Python archive is not the pinned one")
    root = stage(work, archive)
    if a.no_compile:
        print(root)
        return 0
    exe = compile_installer(root, Path(a.out))
    print(f"{exe} ({exe.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
