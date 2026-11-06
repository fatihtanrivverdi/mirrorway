"""Builds the download packages for a release (CI: the macOS job, after building Mirrorway.app).

  Mirrorway-<version>-mac.zip       Mirrorway.app + install-mac.command + the code
  Mirrorway-<version>-windows.zip   install-windows.bat/.ps1 + the code

Usage: python scripts/make_packages.py --app build/Mirrorway.app --out dist
Each zip holds one folder (Mirrorway-<version>-mac/ or …-windows/); file modes are kept (the launcher and the
.command files stay executable), dates are fixed so the same input gives the same zip.
"""
import argparse
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mirrorway import __version__  # noqa: E402
from mirrorway import release as R  # noqa: E402

COMMON = ("README.md", "LICENSE", "CHANGELOG.md")
MAC_FILES = COMMON + ("install-mac.command", "run-mac.command", "requirements-mac.txt")
WIN_FILES = COMMON + ("install-windows.bat", "install-windows.ps1", "run-windows.bat", "debug-windows.bat",
                      "requirements-windows.txt")
FIXED_DATE = (2020, 1, 1, 0, 0, 0)


def mac_name(version: str) -> str:
    return f"Mirrorway-{version}-mac.zip"


def windows_name(version: str) -> str:
    return f"Mirrorway-{version}-windows.zip"


def _code_files():
    """(archive path, file) for the mirrorway package, as in the update package."""
    return [(arc, p) for arc, p in R.app_files(ROOT) if arc.startswith("mirrorway/")]


def _add(z: zipfile.ZipFile, arc: str, path: Path) -> None:
    mode = 0o755 if os.access(path, os.X_OK) else 0o644
    zi = zipfile.ZipInfo(arc, date_time=FIXED_DATE)
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.create_system = 3                                   # Unix: external_attr carries the file mode
    zi.external_attr = (0o100000 | mode) << 16
    z.writestr(zi, path.read_bytes())


def _tree(base: Path):
    for p in sorted(base.rglob("*")):
        if p.is_symlink():
            raise SystemExit(f"symbolic link in the package: {p}")
        if p.is_file() and "__pycache__" not in p.parts and p.name != ".DS_Store":
            yield p


def build(out_dir: Path, app: Path, version: str = __version__) -> list:
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    top = f"Mirrorway-{version}-mac/"
    path = out_dir / mac_name(version)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in _tree(app):
            _add(z, top + "Mirrorway.app/" + p.relative_to(app).as_posix(), p)
        for name in MAC_FILES:
            _add(z, top + name, ROOT / name)
        for arc, p in _code_files():
            _add(z, top + arc, p)
    made.append(path)
    top = f"Mirrorway-{version}-windows/"
    path = out_dir / windows_name(version)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in WIN_FILES:
            _add(z, top + name, ROOT / name)
        for arc, p in _code_files():
            _add(z, top + arc, p)
    made.append(path)
    return made


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True, help="the built Mirrorway.app")
    ap.add_argument("--out", default="dist")
    a = ap.parse_args()
    app = Path(a.app)
    if not (app / "Contents" / "MacOS" / "Mirrorway").is_file():
        print(f"Not a Mirrorway.app: {app}", file=sys.stderr)
        return 1
    for p in build(Path(a.out), app):
        print(f"{p.name}: {p.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
