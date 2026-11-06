"""CI (GitHub Windows runner): tries the built installer the way a person would use it.

  1. Installs silently; checks the files, the private Python's packages and the shortcuts.
  2. Starts Mirrorway and checks that it keeps running (no crash while starting: tray menu, windows, shortcuts).
  3. Installs again while Mirrorway runs (an update): the installer must stop it and replace the files.
  4. Uninstalls; the program folder and the shortcuts must be gone (the settings folder stays).
Run: python tests/ci_installer_windows.py packages
"""
import os
import subprocess
import sys
import time
from pathlib import Path

if not os.environ.get("CI") or sys.platform != "win32":
    sys.exit("This script is for the Windows CI only.")

failures = []


def check(name, cond, info=""):
    print(("[OK]   " if cond else "[FAIL] ") + name + (f": {info}" if info else ""))
    if not cond:
        failures.append(name)


pkg = Path(sys.argv[1] if len(sys.argv) > 1 else "packages")
setups = sorted(pkg.glob("Mirrorway-*-windows-setup.exe"))
if not setups:
    sys.exit(f"No installer in {pkg}")
setup = setups[-1]
app_dir = Path(os.environ["LOCALAPPDATA"]) / "Mirrorway"
home = Path(os.environ["USERPROFILE"])
log_file = home / ".mirrorway" / "mirrorway-win.log"
links = {name: Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / sub / "Mirrorway.lnk"
         for name, sub in (("start menu", ""), ("startup", "Startup"))}
links["desktop"] = home / "Desktop" / "Mirrorway.lnk"


def install(tag):
    t0 = time.monotonic()
    r = subprocess.run([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={pkg / (tag + '.log')}"],
                       timeout=600)
    check(f"{tag}: installer finished", r.returncode == 0, f"exit {r.returncode}, {time.monotonic() - t0:.0f} s")


def app_processes():
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*mirrorway.win*' "
                          "-and $_.ProcessId -ne $PID } | ForEach-Object { $_.ProcessId }"],
                         capture_output=True, text=True, timeout=60).stdout
    return [int(x) for x in out.split() if x.strip().isdigit()]


print(f"Installer: {setup.name} ({setup.stat().st_size // (1024 * 1024)} MB)")
install("install")
py = app_dir / "python" / "python.exe"
check("files in place", py.exists() and (app_dir / "python" / "pythonw.exe").exists()
      and (app_dir / "mirrorway" / "win" / "app.py").exists() and not (app_dir / "mirrorway" / "mac").exists()
      and (app_dir / "debug.bat").exists())
r = subprocess.run([str(py), "-c", "import tkinter, PIL, pystray, av, sdl2, cryptography, spake2, numpy; "
                                  "import mirrorway; import mirrorway.win.app; print(mirrorway.__version__)"],
                   cwd=str(app_dir), capture_output=True, text=True, timeout=120)
check("private Python loads the app", r.returncode == 0, (r.stdout.strip() or r.stderr.strip()[-300:]))
for name, p in links.items():
    check(f"{name} shortcut", p.exists(), str(p))

before = log_file.read_text("utf-8", "replace") if log_file.exists() else ""
proc = subprocess.Popen([str(app_dir / "python" / "pythonw.exe"), "-m", "mirrorway.win"], cwd=str(app_dir))
time.sleep(15)
alive = proc.poll() is None
text = log_file.read_text("utf-8", "replace")[len(before):] if log_file.exists() else ""
check("Mirrorway keeps running after starting", alive and "The app crashed" not in text,
      "running" if alive else f"exit {proc.returncode}; log: {text[-1500:]}")

install("update")                              # while Mirrorway runs
time.sleep(2)
check("the update stopped the running Mirrorway", proc.poll() is not None and not app_processes())
check("files in place after the update", py.exists() and (app_dir / "mirrorway" / "win" / "app.py").exists())
if proc.poll() is None:
    proc.kill()

uninstaller = app_dir / "unins000.exe"
check("uninstaller present", uninstaller.exists())
if uninstaller.exists():
    subprocess.run([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=300)
    deadline = time.monotonic() + 90
    while (app_dir / "python").exists() and time.monotonic() < deadline:
        time.sleep(1)
    check("uninstall removed the program", not (app_dir / "python").exists() and not (app_dir / "mirrorway").exists())
    check("uninstall removed the shortcuts", not any(p.exists() for p in links.values()))
    check("settings folder kept", (home / ".mirrorway").exists())

print("RESULT: " + ("PASSED" if not failures else "FAILED: " + ", ".join(failures)))
sys.exit(1 if failures else 0)
