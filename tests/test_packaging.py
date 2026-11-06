"""The logo assets, Mirrorway.app, the download packages and the menu bar / tray icons."""
import os
import plistlib
import struct
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from mirrorway import __version__
from mirrorway import release as R

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "macapp"))
sys.path.insert(0, str(ROOT / "scripts"))

import build_app  # noqa: E402
import make_packages  # noqa: E402


def _fake_macho(path: Path, cpu: int) -> Path:
    path.write_bytes(struct.pack("<Iii", 0xFEEDFACF, cpu, 0) + b"\0" * 64)
    return path


@pytest.fixture
def app(tmp_path):
    launchers = [_fake_macho(tmp_path / "arm64", 0x0100000C), _fake_macho(tmp_path / "x86_64", 0x01000007)]
    path, how = build_app.build(str(tmp_path / "build"), launchers, do_sign=False)
    assert how == "not signed"
    return Path(path)


def test_assets():
    menubar = Image.open(ROOT / "mirrorway" / "assets" / "menubar.png")
    assert menubar.size == (40, 40) and menubar.mode == "RGBA"
    # a template image: black, the shape is in the alpha channel only
    px = menubar.load()
    assert {px[x, y][:3] for x in range(40) for y in range(40) if px[x, y][3]} <= {(0, 0, 0)}
    ico = Image.open(ROOT / "mirrorway" / "assets" / "mirrorway.ico")
    assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= set(ico.info["sizes"])
    icns = Image.open(ROOT / "macapp" / "Mirrorway.icns")
    assert icns.format == "ICNS" and (512, 512, 2) in icns.info["sizes"]
    for name in ("logo.svg", "logo-mono.svg"):
        assert "<svg" in (ROOT / "assets" / name).read_text(encoding="utf-8")
    # the icons travel with the code, so in-app updates carry them too
    arcs = [arc for arc, _ in R.app_files(ROOT)]
    assert "mirrorway/assets/menubar.png" in arcs and "mirrorway/assets/mirrorway.ico" in arcs


def test_build_app_bundle(app):
    c = app / "Contents"
    info = plistlib.loads((c / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == "app.mirrorway.mac" and info["CFBundleExecutable"] == "Mirrorway"
    assert info["CFBundleName"] == "Mirrorway" and info["CFBundleDevelopmentRegion"] == "en" and info["LSUIElement"]
    exe = (c / "MacOS" / "Mirrorway").read_bytes()
    assert struct.unpack(">II", exe[:8]) == (0xCAFEBABE, 2)                  # universal: two architectures
    assert os.access(c / "MacOS" / "Mirrorway", os.X_OK) and os.access(c / "Resources" / "start.sh", os.X_OK)
    assert (c / "Resources" / "Mirrorway.icns").read_bytes() == (ROOT / "macapp" / "Mirrorway.icns").read_bytes()
    start = (c / "Resources" / "start.sh").read_text(encoding="utf-8")
    assert 'MHOME="$HOME/.mirrorway"' in start and "-m mirrorway.mac" in start
    launcher = (ROOT / "macapp" / "launcher.c").read_text(encoding="utf-8")
    assert '"MIRRORWAY_APP"' in launcher and '"MIRRORWAY_BUNDLE"' in launcher


def test_download_packages(app, tmp_path):
    mac, win = make_packages.build(tmp_path / "dist", app)
    assert mac.name == f"Mirrorway-{__version__}-mac.zip" and win.name == f"Mirrorway-{__version__}-windows.zip"
    top = f"Mirrorway-{__version__}-mac/"
    with zipfile.ZipFile(mac) as z:
        infos = {i.filename: i for i in z.infolist()}
        assert all(n.startswith(top) for n in infos)
        mode = {n[len(top):]: (i.external_attr >> 16) & 0o777 for n, i in infos.items()}
        assert mode["Mirrorway.app/Contents/MacOS/Mirrorway"] == 0o755
        assert mode["Mirrorway.app/Contents/Resources/start.sh"] == 0o755
        assert mode["install-mac.command"] == 0o755 and mode["mirrorway/__init__.py"] == 0o644
        assert {"README.md", "LICENSE", "requirements-mac.txt", "mirrorway/mac/app.py"} <= set(mode)
        assert not any("__pycache__" in n or n.endswith(".pyc") for n in mode)
    with zipfile.ZipFile(win) as z:
        names = {n.split("/", 1)[1] for n in z.namelist()}
        assert {"install-windows.bat", "install-windows.ps1", "requirements-windows.txt", "mirrorway/win/app.py",
                "mirrorway/assets/mirrorway.ico", "LICENSE"} <= names
        assert not any(n.startswith("Mirrorway.app") for n in names)
    again, _ = make_packages.build(tmp_path / "again", app)
    assert again.read_bytes() == mac.read_bytes()                            # same input, same bytes


def test_publish_uploads_download_packages():
    text = (ROOT / "scripts" / "publish_release.py").read_text(encoding="utf-8")
    assert 'f"Mirrorway-{version}-mac.zip"' in text and 'f"Mirrorway-{version}-windows.zip"' in text
    wf = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    mac_job = wf.split("\n  macos:", 1)[1].split("\n  windows:", 1)[0]
    assert "macapp/build_app.py build" in mac_job and "make_packages.py --app build/Mirrorway.app" in mac_job
    assert "codesign --verify" in mac_job and "name: packages" in mac_job
    rel = wf.split("\n  release:", 1)[1].split("\n  report:", 1)[0]
    assert rel.index("download-artifact") < rel.index("make_release.py") < rel.index("publish_release.py")


def test_mac_menu_bar_uses_the_logo():
    from test_platform_glue import run_script
    out = run_script("""
        import stubs, os
        Q, AK, R, pb = stubs.install_mac()
        from mirrorway.config import Config, MAC_DEFAULTS
        from mirrorway.mac import app as A
        a = A.MirrorwayMacApp(Config('mac-test-icon', MAC_DEFAULTS))
        assert a.icon == A.MENUBAR_ICON and os.path.exists(a.icon) and a.template is True and a.title is None
        a.server.load_identity()
        a._tick(None)
        assert a.title is None, a.title                    # waiting: the icon alone
        a._set_title('•')
        assert a.title == '•'
        a._has_icon = False                                # without the icon file: the old text title
        a._set_title('')
        assert a.title == '⇆'
        print('OK')
    """)
    assert "OK" in out


def test_tray_icon_draws_the_monogram():
    from test_platform_glue import run_script
    out = run_script("""
        import sys, types
        tk = types.ModuleType('tkinter'); tk.ttk = types.ModuleType('tkinter.ttk')
        sys.modules.setdefault('tkinter', tk); sys.modules.setdefault('tkinter.ttk', tk.ttk)
        import stubs
        stubs.install_windows()
        from mirrorway.win import app as A
        import os
        assert os.path.exists(A.ICON_FILE)
        for name, color in A.COLORS.items():
            img = A.make_icon(color)
            assert img.size == (64, 64) and img.mode == 'RGBA'
            assert img.getpixel((32, 4))[:3] == color or abs(img.getpixel((32, 4))[0] - color[0]) < 8
            assert min(img.getpixel((16, 22))[:3]) > 240               # the M's left stem is white
            assert img.getpixel((0, 0))[3] == 0                          # transparent corner
        print('OK')
    """)
    assert "OK" in out
