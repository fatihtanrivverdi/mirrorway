"""The one-click Windows installer: the folder it installs and the Inno Setup script (the build itself runs on
the Windows CI, which also installs, starts, updates and uninstalls it)."""
import io
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_windows_installer as B  # noqa: E402

from mirrorway import __version__  # noqa: E402


def _fake_python(path: Path, extra=None) -> Path:
    with tarfile.open(path, "w:gz") as t:
        for name, data in [("python/python.exe", b"MZ"), ("python/pythonw.exe", b"MZ"),
                           ("python/Lib/site.py", b"")] + (extra or []):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return path


def test_stage_layout(tmp_path):
    root = B.stage(tmp_path / "work", _fake_python(tmp_path / "py.tar.gz"), pip=False)
    assert (root / "python" / "pythonw.exe").exists()
    assert (root / "mirrorway" / "win" / "app.py").exists() and not (root / "mirrorway" / "mac").exists()
    assets = root / "mirrorway" / "assets"
    assert (assets / "mirrorway.ico").exists() and (assets / "logo.png").exists()
    assert (root / "requirements-windows.txt").exists() and (root / "LICENSE").exists()
    bat = (root / "debug.bat").read_bytes()
    assert b"python\\python.exe\" -m mirrorway.win --debug" in bat and b"\r\n" in bat
    (root / "stale").write_text("x")
    again = B.stage(tmp_path / "work", tmp_path / "py.tar.gz", pip=False)      # a second build starts clean
    assert again == root and not (root / "stale").exists()


def test_stage_refuses_unsafe_archives(tmp_path):
    with pytest.raises(SystemExit):
        B.stage(tmp_path / "w1", _fake_python(tmp_path / "a.tar.gz", [("../evil.txt", b"x")]), pip=False)
    with pytest.raises(SystemExit):
        B.stage(tmp_path / "w2", _fake_python(tmp_path / "b.tar.gz", [("other/x.txt", b"x")]), pip=False)


def test_pinned_python_and_names():
    assert len(B.PYTHON_SHA256) == 64 and B.PYTHON_URL.startswith("https://github.com/astral-sh/")
    assert "3.12" in B.PYTHON_VERSION and "cpython-3.12" in B.PYTHON_URL
    assert B.setup_name("3.1.5") == "Mirrorway-3.1.5-windows-setup.exe"
    assert B.setup_name() == f"Mirrorway-{__version__}-windows-setup.exe"


def test_inno_script():
    raw = (ROOT / "wininstaller" / "Mirrorway.iss").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "Inno Setup reads non-ASCII text as UTF-8 only with the BOM"
    iss = raw.decode("utf-8-sig")
    for line in ("PrivilegesRequired=lowest", "DefaultDirName={localappdata}\\Mirrorway",
                 "OutputBaseFilename=Mirrorway-{#MyVersion}-windows-setup", "DisableReadyPage=yes",
                 "DisableFinishedPage=yes", "ArchitecturesAllowed=x64compatible"):
        assert line in iss, line
    # shortcuts and the start after installing use the private Python
    assert iss.count('Filename: "{app}\\python\\pythonw.exe"; Parameters: "-m mirrorway.win"') == 4
    assert 'Name: "{userstartup}\\Mirrorway"' in iss and "Check: WantStartup" in iss
    # the old installer's virtual environment and Köprü go; a running Mirrorway is stopped, but not the
    # PowerShell that stops it
    assert 'Name: "{app}\\.venv"' in iss and 'Name: "{localappdata}\\Kopru"' in iss and "Köprü.lnk" in iss
    assert "$_.ProcessId -ne $PID" in iss and "*mirrorway.win*" in iss and "*kopru.win*" in iss
    assert "function PrepareToInstall" in iss and "function InitializeUninstall" in iss
    # the settings folder is not touched
    assert ".mirrorway" not in iss.split("[UninstallDelete]", 1)[1].split("[Code]", 1)[0]
