"""In-app updates on the Mac: checks the signed releases of the public GitHub repository, downloads and installs them.

Order: ask for the latest release -> download the manifest and its signature -> verify the signature with the public
key in the code -> download the package, verify its size + SHA-256 + the version inside -> extract it into a temporary
folder -> (if needed) install new packages pinned by hash -> test the new code by importing it in a separate process
-> replace ~/.mirrorway/app/mirrorway (the old one stays in app.prev) -> restart Mirrorway.
If any step fails, the installed version is left untouched. Windows is updated from the Mac as usual the next time
it connects after the Mac has been updated.
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional

from .. import __version__
from .. import release as R
from ..i18n import T
from ..protocol import parse_version

log = logging.getLogger("mirrorway.selfupdate")

REPO = "fatihtanrivverdi/mirrorway"
API = "https://api.github.com"
CHECK_INTERVAL_S = 24 * 3600
RETRY_AFTER_FAILURE_S = 3600          # wait after a failed automatic check
STAGE = "app.new"
PREV = "app.prev"
REQ_FILE = "requirements-mac.txt"

_SYSTEM_CA_FILES = ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem")
_ssl_ctx = None


class UpdateError(Exception):
    pass


def _macos_root_pem() -> str:
    try:
        return subprocess.run(["/usr/bin/security", "find-certificate", "-a", "-p",
                               "/System/Library/Keychains/SystemRootCertificates.keychain"],
                              capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return ""


def ssl_context():
    """A verifying TLS context. python.org's macOS Python does not see the system certificates (unless "Install
    Certificates" was run); in that case macOS's root certificates are loaded. Verification is never turned off."""
    global _ssl_ctx
    if _ssl_ctx is not None:
        return _ssl_ctx
    import ssl
    ctx = ssl.create_default_context()
    if sys.platform == "darwin" and not ctx.cert_store_stats().get("x509_ca"):
        loaded = False
        for path in _SYSTEM_CA_FILES:
            if os.path.exists(path):
                try:
                    ctx.load_verify_locations(cafile=path)
                    loaded = True
                    break
                except Exception as exc:
                    log.debug("Could not load the certificate file (%s): %s", path, exc)
        if not loaded:
            pem = _macos_root_pem()
            if pem.strip():
                ctx.load_verify_locations(cadata=pem)
                loaded = True
        log.info("TLS root certificates: %s", "macOS system store" if loaded else "not found")
    _ssl_ctx = ctx
    return ctx


def _urlopen(req, timeout=30):
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())


@dataclass
class ReleaseInfo:
    version: str
    tag: str
    assets: Dict[str, int]                 # name -> asset id
    url: str = ""
    published: str = ""
    notes: str = ""
    manifest: Dict = field(default_factory=dict)

    def newer_than(self, current: str) -> bool:
        return parse_version(self.version) > parse_version(current)


# ---- GitHub ---------------------------------------------------------------------------
class GitHub:
    """Read-only access to the latest release and its files (no account or token needed: the repository is public)."""

    def __init__(self, repo: str = REPO, opener: Optional[Callable] = None, timeout: float = 30):
        self.repo, self.timeout = repo, timeout
        self.open = opener or _urlopen

    def _req(self, url: str, accept: str = "application/vnd.github+json"):
        req = urllib.request.Request(url)
        req.add_header("Accept", accept)
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", f"Mirrorway/{__version__}")
        try:
            return self.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise UpdateError(T("No release has been published yet"))
            if e.code in (403, 429):
                raise UpdateError(T("GitHub is limiting requests; try again later"))
            raise UpdateError(T("GitHub replied with HTTP {code}").format(code=e.code))
        except urllib.error.URLError as e:
            raise UpdateError(T("Could not reach GitHub: {reason}").format(reason=e.reason))

    def latest(self) -> ReleaseInfo:
        with self._req(f"{API}/repos/{self.repo}/releases/latest") as r:
            d = json.loads(r.read().decode("utf-8"))
        tag = str(d.get("tag_name") or "")
        version = tag[1:] if tag.startswith("v") else tag
        assets = {a["name"]: int(a["id"]) for a in d.get("assets", []) if "name" in a and "id" in a}
        return ReleaseInfo(version=version, tag=tag, assets=assets, url=d.get("html_url", ""),
                           published=d.get("published_at", ""), notes=str(d.get("body") or "")[:4000])

    def asset(self, asset_id: int, limit: int = R.MAX_ZIP_BYTES + 1) -> bytes:
        with self._req(f"{API}/repos/{self.repo}/releases/assets/{asset_id}", "application/octet-stream") as r:
            data = r.read(limit)
        if len(data) >= limit:
            raise UpdateError("the downloaded file is too large")
        return data


# ---- checking and downloading ------------------------------------------------------------------
def check(gh: GitHub, current: str = __version__) -> ReleaseInfo:
    """Fetches the latest release and its signed manifest (signature verified). The package is not downloaded."""
    info = gh.latest()
    need = (R.manifest_name(info.version), R.sig_name(info.version), R.zip_name(info.version))
    missing = [n for n in need if n not in info.assets]
    if missing:
        raise UpdateError(f"{info.tag} is missing files: {', '.join(missing)}")
    manifest = gh.asset(info.assets[need[0]], 256 * 1024)
    sig = gh.asset(info.assets[need[1]], 4096).decode("ascii", "replace")
    try:
        m = R.verify_manifest(manifest, sig)
    except R.ReleaseError as exc:
        raise UpdateError(f"{info.tag}: {exc}")
    if m.get("version") != info.version or m.get("file") != need[2]:
        raise UpdateError(f"{info.tag}: the manifest does not belong to this release")
    info.manifest = m
    return info


def download(gh: GitHub, info: ReleaseInfo) -> bytes:
    data = gh.asset(info.assets[R.zip_name(info.version)])
    try:
        R.verify_zip(info.manifest, data)
    except R.ReleaseError as exc:
        raise UpdateError(str(exc))
    return data


# ---- installation ----------------------------------------------------------------------------
def app_root() -> Path:
    import mirrorway
    return Path(mirrorway.__file__).resolve().parent.parent


def _safe_extract(data: bytes, dest: Path) -> None:
    allowed = set(R.APP_EXTRA_FILES)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            name = info.filename
            p = Path(name)
            if p.is_absolute() or ".." in p.parts or name.startswith(("/", "\\")):
                raise UpdateError(f"Invalid file path: {name}")
            if not (name.startswith("mirrorway/") or name in allowed):
                raise UpdateError(f"Unexpected file: {name}")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise UpdateError(f"Symbolic link in the package: {name}")
        z.extractall(dest)


def _run(args, timeout: float):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def install(data: bytes, version: str, root: Optional[Path] = None, python_exe: Optional[str] = None,
            runner: Callable = _run) -> Path:
    """Installs a verified package; returns the backup folder. On error UpdateError (the installed version stays)."""
    root = Path(root) if root else app_root()
    python_exe = python_exe or sys.executable
    if not (root / "mirrorway").is_dir():
        raise UpdateError(f"Installed code not found: {root}")
    base = root.parent
    stage, prev = base / STAGE, base / PREV
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    try:
        _safe_extract(data, stage)
        if not (stage / "mirrorway" / "mac" / "app.py").exists():
            raise UpdateError("The package has no Mac app")
        new_req, old_req = stage / REQ_FILE, root / REQ_FILE
        if new_req.exists() and (not old_req.exists() or new_req.read_bytes() != old_req.read_bytes()):
            log.info("Installing new dependencies")
            r = runner([python_exe, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                        "--require-hashes", "-r", str(new_req)], timeout=900)
            if r.returncode != 0:
                raise UpdateError("Could not install packages: " + (r.stderr or r.stdout or "")[-400:])
        probe = ("import sys; sys.path.insert(0, sys.argv[1]); import mirrorway, mirrorway.mac.server, "
                 "mirrorway.mac.selfupdate, mirrorway.release; print(mirrorway.__version__)")
        r = runner([python_exe, "-I", "-c", probe, str(stage)], timeout=120)
        if r.returncode != 0:
            raise UpdateError("The new version failed its test import: " + (r.stderr or "")[-400:])
        got = ((r.stdout or "").strip().splitlines() or [""])[-1]
        if got != version:
            raise UpdateError(f"Version mismatch: package {version}, code {got}")
        # Swap: the old version goes to app.prev (the backup before it is removed)
        shutil.rmtree(prev, ignore_errors=True)
        prev.mkdir(parents=True)
        (root / "mirrorway").rename(prev / "mirrorway")
        try:
            (stage / "mirrorway").rename(root / "mirrorway")
        except Exception:
            (prev / "mirrorway").rename(root / "mirrorway")
            raise
        for name in R.APP_EXTRA_FILES:
            if (root / name).exists():
                shutil.copyfile(root / name, prev / name)
            if (stage / name).exists():
                shutil.copyfile(stage / name, root / name)
        (prev / "VERSION").write_text(__version__, encoding="utf-8")
        log.info("Update installed: %s -> %s (previous version: %s)", __version__, version, prev)
        return prev
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def previous_version(root: Optional[Path] = None) -> Optional[str]:
    root = Path(root) if root else app_root()
    prev = root.parent / PREV
    if not (prev / "mirrorway").is_dir():
        return None
    try:
        return (prev / "VERSION").read_text(encoding="utf-8").strip() or "?"
    except OSError:
        return "?"


def rollback(root: Optional[Path] = None) -> str:
    """Returns to the version in app.prev (the current one moves to app.prev). Returns the version restored."""
    root = Path(root) if root else app_root()
    prev = root.parent / PREV
    ver = previous_version(root)
    if ver is None:
        raise UpdateError(T("There is no previous version to return to"))
    tmp = root.parent / "app.swap"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    (root / "mirrorway").rename(tmp / "mirrorway")
    (prev / "mirrorway").rename(root / "mirrorway")
    (tmp / "mirrorway").rename(prev / "mirrorway")
    for name in R.APP_EXTRA_FILES:
        a, b = root / name, prev / name
        if b.exists():
            data_b = b.read_bytes()
            if a.exists():
                shutil.copyfile(a, b)
            a.write_bytes(data_b)
    (prev / "VERSION").write_text(__version__, encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    log.info("Returned to the previous version: %s (the current %s is kept as the backup)", ver, __version__)
    return ver


def restart_command() -> Optional[list]:
    """The command that reopens Mirrorway.app (None when not running from the app bundle)."""
    bundle = os.environ.get("MIRRORWAY_BUNDLE")
    if not bundle or not os.path.isdir(bundle):
        return None
    return ["/bin/sh", "-c", 'sleep 1.5; exec /usr/bin/open -n "$1"', "mirrorway-restart", bundle]


def spawn_restart(popen=subprocess.Popen) -> bool:
    cmd = restart_command()
    if cmd is None:
        return False
    popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL, close_fds=True)
    return True


# ---- app state ---------------------------------------------------------------------
class Updater:
    """State machine for the menu: checking/downloading/installing run in the background; read `state` and `text`."""

    def __init__(self, cfg, github_factory: Optional[Callable[[], GitHub]] = None, clock=time.time,
                 root: Optional[Path] = None, installer: Callable = install):
        self.cfg, self.clock, self.root = cfg, clock, root
        self.github_factory = github_factory or GitHub
        self.installer = installer
        self.state = "idle"        # idle|checking|uptodate|available|downloading|installed|error
        self.text = ""
        self.info: Optional[ReleaseInfo] = None
        self.busy = False
        self._last_warning = ""
        self._lock = threading.Lock()

    def auto_enabled(self) -> bool:
        return bool(self.cfg.get("auto_update_check", True))

    def due(self) -> bool:
        if not self.auto_enabled() or self.busy:
            return False
        now = self.clock()
        last_ok = float(self.cfg.get("update_last_check") or 0)
        last_try = float(self.cfg.get("update_last_attempt") or 0)
        return now - last_ok >= CHECK_INTERVAL_S and now - last_try >= RETRY_AFTER_FAILURE_S

    def check(self, current: str = __version__) -> str:
        with self._lock:
            if self.busy:
                return self.state
            self.busy = True
        try:
            self.state, self.text = "checking", T("Checking…")
            gh = self.github_factory()
            self.cfg.set("update_last_attempt", int(self.clock()))
            info = check(gh, current)
            self.cfg.set("update_last_check", int(self.clock()))
            if info.newer_than(current):
                self.info = info
                self.state, self.text = "available", T("New version: {version}").format(version=info.version)
                log.info("New version available: %s (installed %s)", info.version, current)
            else:
                self.info = None
                self.state, self.text = "uptodate", T("Mirrorway is up to date ({version})").format(version=current)
                log.info("Update check: up to date (%s; latest release %s, signature verified)",
                         current, info.version)
            self._last_warning = ""
        except Exception as exc:
            self.state, self.text = "error", T("Could not check: {error}").format(error=exc)
            self._warn(f"Could not check for updates: {exc}")
        finally:
            self.busy = False
        return self.state

    def _warn(self, msg: str) -> None:
        if msg != self._last_warning:
            self._last_warning = msg
            log.warning("%s", msg)

    def install_available(self) -> str:
        with self._lock:
            if self.busy or self.info is None:
                return self.state
            self.busy = True
        info = self.info
        try:
            self.state, self.text = "downloading", T("Downloading {version}…").format(version=info.version)
            data = download(self.github_factory(), info)
            self.state, self.text = "downloading", T("Installing {version}…").format(version=info.version)
            self.installer(data, info.version, root=self.root)
            self.state, self.text = "installed", T("{version} installed — restarting Mirrorway").format(version=info.version)
        except Exception as exc:
            self.state, self.text = "error", T("Could not install: {error}").format(error=exc)
            log.warning("Could not install the update: %s", exc)
        finally:
            self.busy = False
        return self.state
