"""Pins the dependencies by version + SHA-256 hash (requirements-mac.txt / requirements-windows.txt and others).

Usage: python scripts/pin_requirements.py
To change versions, edit the MAC / WIN lists below. Then verify for both platforms:
  pip install --dry-run --ignore-installed --target /tmp/x --platform win_amd64 --python-version 3.12 \
      --implementation cp --only-binary=:all: --require-hashes -r requirements-windows.txt
  (When run on Linux, pystray's Linux-only python-xlib dependency has to be added separately.)
"""
import json
import os
import urllib.request


def files(name, ver):
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{ver}/json", timeout=30) as r:
        return json.load(r)["urls"]


def keep(fn, plat):
    if fn.endswith(".tar.gz") or fn.endswith(".zip") or fn.endswith("-none-any.whl"):
        return True
    if plat == "mac":
        return "macosx" in fn and ("cp314" in fn or "abi3" in fn or "py3" in fn or "py2.py3" in fn) and "cp314t" not in fn
    if plat == "linux":     # CI: Ubuntu x86_64, Python 3.12 and 3.14
        return ("manylinux" in fn and "x86_64" in fn and "cp314t" not in fn
                and ("cp312" in fn or "cp314" in fn or "abi3" in fn or "py3" in fn or "py2.py3" in fn))
    if plat == "any":       # pure-Python test tools (every platform)
        return False
    return "win_amd64" in fn and ("cp312" in fn or "abi3" in fn or "py3" in fn or "py2.py3" in fn) and "cp315" not in fn


def gen(plat, pins, header):
    out = [header]
    for spec in pins:
        spec, _, marker = spec.partition(";")
        name, ver = spec.strip().split("==")
        fs = [f for f in files(name, ver) if keep(f["filename"], plat)]
        if not fs:
            raise SystemExit(f"{spec}: no files for {plat}")
        hs = sorted({f["digests"]["sha256"] for f in fs})
        head = f"{name}=={ver}" + (f" ;{marker}" if marker else "")
        out.append(f"{head} \\\n" + " \\\n".join(f"    --hash=sha256:{h}" for h in hs))
    return "\n".join(out) + "\n"


MAC = ["pyobjc-core==13.0","pyobjc-framework-Cocoa==13.0","pyobjc-framework-Quartz==13.0","pyobjc-framework-CoreText==13.0",
       "pyobjc-framework-ApplicationServices==13.0","pyobjc-framework-ScreenCaptureKit==13.0","pyobjc-framework-CoreMedia==13.0",
       "pyobjc-framework-VideoToolbox==13.0","pyobjc-framework-libdispatch==13.0","rumps==0.4.0","pillow==13.0.0","mss==10.2.0",
       "cryptography==50.0.2","cffi==2.1.1","pycparser==3.12","numpy==2.5.4","simplejpeg==1.9.0","av==19.0.2","spake2==0.9"]
WIN = ["pillow==13.0.0","pystray==0.19.5","six==1.17.0","pyperclip==1.11.0","cryptography==50.0.2","cffi==2.1.1","pycparser==3.12",
       "numpy==2.5.4","simplejpeg==1.9.0","av==19.0.2","pysdl2==0.9.17","pysdl2-dll==2.32.10","spake2==0.9"]
LINUX = ["pillow==13.0.0","cryptography==50.0.2","cffi==2.1.1","pycparser==3.12","numpy==2.5.4","simplejpeg==1.9.0",
         "av==19.0.2","spake2==0.9","pysdl2==0.9.17","pysdl2-dll==2.32.10"]
RELEASE = ["cryptography==50.0.2","cffi==2.1.1","pycparser==3.12"]
DEV = ["pytest==9.1.1","iniconfig==2.3.1","packaging==26.3","pluggy==1.6.0","pygments==2.21.0","pyflakes==4.0.3",
       "colorama==0.4.6; sys_platform == 'win32'"]
H = ("# Mirrorway — {} dependencies\n# Versions and SHA-256 hashes are pinned: pip installs exactly these packages\n"
     "# (even if a package is replaced on PyPI). To update: scripts/pin_requirements.py\n")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
open(os.path.join(ROOT, "requirements-mac.txt"), "w").write(gen("mac", MAC, H.format("Mac")))
open(os.path.join(ROOT, "requirements-windows.txt"), "w").write(gen("win", WIN, H.format("Windows")))
open(os.path.join(ROOT, "requirements-ci-linux.txt"), "w").write(
    gen("linux", LINUX, H.format("CI (Linux, Python 3.12/3.14)")))
open(os.path.join(ROOT, "requirements-release.txt"), "w").write(
    gen("linux", RELEASE, H.format("release build (CI, Linux)")))
open(os.path.join(ROOT, "requirements-dev.txt"), "w").write(gen("any", DEV, H.format("test tools (every platform)")))
print("ok")
