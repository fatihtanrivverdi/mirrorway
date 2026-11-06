"""The drag-and-drop Mac download: what goes into Mirrorway.app (the disk image itself is made and tried on the
macOS CI: copied out, started in a separate home folder, updated)."""
import hashlib
import io
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_mac_dmg as D  # noqa: E402

from mirrorway import __version__  # noqa: E402


def _fake_python(path: Path, links=(("python/bin/python3", "python3.14"),), extra=()) -> Path:
    with tarfile.open(path, "w:gz") as t:
        for name, data in [("python/bin/python3.14", b"\xcf\xfa\xed\xfe"), ("python/lib/python3.14/os.py", b"")] \
                + list(extra):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            t.addfile(info, io.BytesIO(data))
        for name, target in links:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            t.addfile(info)
    return path


def _fake_app(tmp: Path) -> Path:
    app = tmp / "built" / "Mirrorway.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources").mkdir()
    (app / "Contents" / "MacOS" / "Mirrorway").write_bytes(b"launcher")
    (app / "Contents" / "Resources" / "start.sh").write_text((ROOT / "macapp" / "start.sh").read_text())
    return app


def test_assemble(tmp_path):
    app = D.assemble(_fake_app(tmp_path), tmp_path / "work", _fake_python(tmp_path / "py.tar.gz"), pip=False)
    res = app / "Contents" / "Resources"
    assert (res / "python" / "bin" / "python3").is_symlink() and (res / "python" / "bin" / "python3.14").exists()
    want = hashlib.sha1((ROOT / "requirements-mac.txt").read_bytes()).hexdigest()
    assert (res / "python" / ".req").read_text().strip() == want             # start.sh: the packages match
    assert (res / "app" / "mirrorway" / "mac" / "app.py").exists() and (res / "app" / "requirements-mac.txt").exists()
    assert f'__version__ = "{__version__}"' in (res / "app" / "mirrorway" / "__init__.py").read_text()
    assert not list((res / "app").rglob("__pycache__"))
    assert (app / "Contents" / "MacOS" / "Mirrorway").read_bytes() == b"launcher"


def test_python_archive_must_stay_inside(tmp_path):
    for i, kw in enumerate([dict(links=(("python/bin/python3", "/usr/bin/python3"),)),
                            dict(links=(("python/bin/evil", "../../../etc"),)),
                            dict(extra=(("other/file", b"x"),)),
                            dict(extra=(("python/../escape", b"x"),))]):
        with pytest.raises(SystemExit):
            D.assemble(_fake_app(tmp_path / str(i)), tmp_path / f"w{i}", _fake_python(tmp_path / f"{i}.tgz", **kw),
                       pip=False)


def test_pins_and_names():
    assert len(D.PYTHON_SHA256) == 64 and "aarch64-apple-darwin" in D.PYTHON_URL and "3.14" in D.PYTHON_VERSION
    assert D.dmg_name("3.1.6") == "Mirrorway-3.1.6-mac.dmg"


def test_start_script_uses_the_apps_python_and_code():
    s = (ROOT / "macapp" / "start.sh").read_text()
    assert 'BPY="$RES/python/bin/python3"' in s and 'BAPP="$RES/app"' in s
    assert '/usr/bin/ditto --noqtn "$BAPP" "$APP.new"' in s and 'newer "$BVER" "$IVER"' in s
    assert '"$(cat "$RES/python/.req" 2>/dev/null)" = "$REQ_HASH"' in s and '--system-site-packages' in s
    assert 'xattr -dr com.apple.quarantine "$BUNDLE"' in s and 'MIRRORWAY_CHECK' in s
