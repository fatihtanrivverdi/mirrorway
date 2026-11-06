"""Builds Mirrorway.app: the launcher (macapp/launcher.c) + Info.plist + icon + start.sh.

Usage:
  python3 macapp/build_app.py OUT_DIR                        compile launcher.c, then build the bundle
  python3 macapp/build_app.py OUT_DIR LAUNCHER [LAUNCHER…]   use ready Mach-O launchers (one per architecture)

Compiling: on macOS with the Xcode command line tools (cc, a universal arm64 + x86_64 binary); elsewhere with zig
(`pip install ziglang`). Signing: on macOS ad hoc with codesign; elsewhere with rcodesign when it is installed.
Only the Python standard library is used, so the system python3 on macOS can run this.
"""
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "Mirrorway"
BUNDLE_ID = "app.mirrorway.mac"
# The launcher (Mirrorway.app bundle) version is deliberately separate from the app version
# (mirrorway.__version__): if the bundle's signature changes, macOS resets the permissions granted to it. Code
# updates go to ~/.mirrorway/app; the bundle is rebuilt only when the launcher itself changes.
LAUNCHER_VERSION = "1.0.0"
MIN_MACOS = "11.0"


def make_fat(slices, out):
    """Writes a universal (fat) Mach-O file from single-architecture Mach-O files."""
    datas = [open(p, "rb").read() for p in slices]
    align = 14
    header = struct.pack(">II", 0xCAFEBABE, len(datas))
    layout = []
    cur = 1 << align
    for d in datas:
        cpu, sub = struct.unpack_from("<ii", d, 4)
        cur = (cur + (1 << align) - 1) & ~((1 << align) - 1)
        layout.append((cpu, sub, cur, len(d)))
        cur += len(d)
    entries = b"".join(struct.pack(">iiIII", cpu, sub, off, size, align) for cpu, sub, off, size in layout)
    out_bytes = bytearray(header + entries)
    for (_cpu, _sub, off, _size), d in zip(layout, datas):
        out_bytes.extend(b"\0" * (off - len(out_bytes)))
        out_bytes.extend(d)
    with open(out, "wb") as fh:
        fh.write(out_bytes)
    os.chmod(out, 0o755)


def compile_launcher(out):
    """Compiles launcher.c into a universal binary at `out`."""
    src = os.path.join(HERE, "launcher.c")
    if sys.platform == "darwin":
        base = ["cc", "-O2", "-Wall", "-Wl,-S", f"-mmacosx-version-min={MIN_MACOS}", "-o", out, src]
        try:
            subprocess.run(base[:1] + ["-arch", "arm64", "-arch", "x86_64"] + base[1:], check=True)
        except subprocess.CalledProcessError:
            subprocess.run(base, check=True)             # an old toolchain: this Mac's architecture only
        os.chmod(out, 0o755)
        return
    tmp = tempfile.mkdtemp(prefix="mirrorway-launcher-")
    try:
        slices = []
        for arch in ("aarch64", "x86_64"):
            o = os.path.join(tmp, arch)
            subprocess.run([sys.executable, "-m", "ziglang", "cc", "-target", f"{arch}-macos.{MIN_MACOS}", "-O2", "-s",
                            "-o", o, src], check=True)
            slices.append(o)
        make_fat(slices, out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def sign(app):
    """Ad hoc signature (no developer certificate); returns how it was signed."""
    if sys.platform == "darwin":
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", app], check=True)
        return "codesign (ad hoc)"
    tool = os.environ.get("RCODESIGN") or shutil.which("rcodesign")
    if tool:
        subprocess.run([tool, "sign", app], check=True, stdout=subprocess.DEVNULL)
        return "rcodesign (ad hoc)"
    return "not signed (no codesign or rcodesign)"


def info_plist():
    return {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": APP_NAME,
        "CFBundleIconFile": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": LAUNCHER_VERSION,
        "CFBundleVersion": LAUNCHER_VERSION,
        "LSMinimumSystemVersion": MIN_MACOS,
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Mirrorway — share a Mac's keyboard, mouse and screen with Windows. MIT License.",
    }


def build(out_dir, launchers=(), do_sign=True):
    app = os.path.join(out_dir, f"{APP_NAME}.app")
    if os.path.exists(app):
        shutil.rmtree(app)
    macos = os.path.join(app, "Contents", "MacOS")
    res = os.path.join(app, "Contents", "Resources")
    os.makedirs(macos)
    os.makedirs(res)
    exe = os.path.join(macos, APP_NAME)
    if launchers:
        make_fat(list(launchers), exe)
    else:
        compile_launcher(exe)
    shutil.copy(os.path.join(HERE, "start.sh"), os.path.join(res, "start.sh"))
    os.chmod(os.path.join(res, "start.sh"), 0o755)
    shutil.copy(os.path.join(HERE, f"{APP_NAME}.icns"), os.path.join(res, f"{APP_NAME}.icns"))
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as fh:
        plistlib.dump(info_plist(), fh)
    with open(os.path.join(app, "Contents", "PkgInfo"), "w") as fh:
        fh.write("APPL????")
    how = sign(app) if do_sign else "not signed"
    return app, how


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path, how = build(sys.argv[1], sys.argv[2:])
    print(f"{path} — {how}")
