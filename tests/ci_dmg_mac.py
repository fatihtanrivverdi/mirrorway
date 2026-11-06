"""CI (GitHub macOS runner): tries the built disk image the way a person would use it, in a separate home folder.

  1. Mounts the .dmg: Mirrorway.app and the Applications shortcut are there; the app's signature is valid.
  2. Copies the app out (like dragging it to Applications) and starts it through its real launcher in check mode
     (MIRRORWAY_CHECK=1: start.sh does all its setup, then only test-imports the app instead of starting it, so the
     Keychain and the permissions are not touched): the code goes to ~/.mirrorway/app and the app's own Python
     runs it.
  3. An older installed version is replaced by the app's; a newer one (an in-app update) stays.
  4. Changed requirements (an update that needs a new package): an environment on top of the app's Python is made
     and used.
Run: python tests/ci_dmg_mac.py packages
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if not os.environ.get("CI") or sys.platform != "darwin":
    sys.exit("This script is for the macOS CI only.")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mirrorway import __version__  # noqa: E402

failures = []


def check(name, cond, info=""):
    print(("[OK]   " if cond else "[FAIL] ") + name + (f": {info}" if info else ""))
    if not cond:
        failures.append(name)


pkg = Path(sys.argv[1] if len(sys.argv) > 1 else "packages")
dmg = pkg / f"Mirrorway-{__version__}-mac.dmg"
if not dmg.exists():
    sys.exit(f"No disk image: {dmg}")
print(f"Disk image: {dmg.name} ({dmg.stat().st_size // (1024 * 1024)} MB)")
tmp = Path(tempfile.mkdtemp(prefix="mirrorway-dmg-"))
mnt, apps, home = tmp / "mnt", tmp / "Applications", tmp / "home"
for d in (mnt, apps, home):
    d.mkdir()

subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mnt), str(dmg)], check=True,
               stdout=subprocess.DEVNULL)
try:
    check("the image holds Mirrorway.app", (mnt / "Mirrorway.app" / "Contents" / "MacOS" / "Mirrorway").exists())
    check("the image has the Applications shortcut", os.path.islink(mnt / "Applications")
          and os.readlink(mnt / "Applications") == "/Applications")
    subprocess.run(["ditto", str(mnt / "Mirrorway.app"), str(apps / "Mirrorway.app")], check=True)
finally:
    subprocess.run(["hdiutil", "detach", str(mnt)], stdout=subprocess.DEVNULL)

app = apps / "Mirrorway.app"
r = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], capture_output=True, text=True)
check("signature valid after copying", r.returncode == 0, r.stderr.strip()[-300:])
bundled_py = app / "Contents" / "Resources" / "python" / "bin" / "python3"
check("the app has its own Python", bundled_py.exists())
mhome = home / ".mirrorway"
installed = mhome / "app" / "mirrorway" / "__init__.py"


def start(tag):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV", "MIRRORWAY"))}
    env.update(HOME=str(home), MIRRORWAY_CHECK="1", PATH="/usr/bin:/bin:/usr/sbin:/sbin")
    r = subprocess.run([str(app / "Contents" / "MacOS" / "Mirrorway")], env=env, capture_output=True, text=True,
                       timeout=900)
    out = (r.stdout or "").strip().splitlines()
    last = out[-1] if out else ""
    if r.returncode != 0:
        log = (mhome / "install.log")
        print(f"    {tag}: exit {r.returncode}\n    {r.stderr[-1500:]}")
        if log.exists():
            print("    install.log: " + log.read_text("utf-8", "replace")[-1500:])
    return r.returncode, last


def installed_version():
    m = re.search(r'__version__ = "([^"]+)"', installed.read_text("utf-8")) if installed.exists() else None
    return m.group(1) if m else None


code, out = start("first start")
check("first start: the code is installed", installed_version() == __version__, str(installed_version()))
check("first start: the app's own Python runs it", code == 0 and out.startswith(__version__)
      and "/Contents/Resources/python/" in out, out)

installed.write_text(installed.read_text("utf-8").replace(f'"{__version__}"', '"0.0.1"'), "utf-8")
code, out = start("older installed")
check("an older installed version is replaced", code == 0 and installed_version() == __version__, out)

installed.write_text(installed.read_text("utf-8").replace(f'"{__version__}"', '"99.0.0"'), "utf-8")
code, out = start("newer installed")
check("a newer installed version (an in-app update) stays", code == 0 and installed_version() == "99.0.0"
      and out.startswith("99.0.0"), out)

req = mhome / "app" / "requirements-mac.txt"
req.write_text(req.read_text("utf-8") + "\n# a changed requirements file\n", "utf-8")
code, out = start("changed requirements")
venv_py = mhome / "venv" / "bin" / "python"
check("changed requirements: an environment on top of the app's Python", code == 0 and venv_py.exists()
      and "/.mirrorway/venv/" in out and "include-system-site-packages = true"
      in (mhome / "venv" / "pyvenv.cfg").read_text(), out)

shutil.rmtree(tmp, ignore_errors=True)
print("RESULT: " + ("PASSED" if not failures else "FAILED: " + ", ".join(failures)))
sys.exit(1 if failures else 0)
