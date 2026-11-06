"""In-app updates: signed releases, verification, download, installation, rollback, menu."""
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import urllib.error
import zipfile
from pathlib import Path

import pytest
from conftest import DictCfg

from mirrorway import __version__
from mirrorway import release as R
from mirrorway.mac import selfupdate as U

ROOT = Path(__file__).resolve().parent.parent


def _keypair():
    from cryptography.hazmat.primitives import serialization as S
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.generate()
    priv = base64.b64encode(k.private_bytes(S.Encoding.Raw, S.PrivateFormat.Raw, S.NoEncryption())).decode()
    pub = base64.b64encode(k.public_key().public_bytes(S.Encoding.Raw, S.PublicFormat.Raw)).decode()
    return priv, pub


def _fake_app(tmp: Path, version: str, extra: str = "") -> Path:
    """A small app tree with a changed version (a copy of the real mirrorway package)."""
    import shutil
    root = tmp / f"src-{version}"
    shutil.copytree(ROOT / "mirrorway", root / "mirrorway", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    init = root / "mirrorway" / "__init__.py"
    init.write_text(init.read_text(encoding="utf-8").replace(f'__version__ = "{__version__}"',
                                                             f'__version__ = "{version}"') + extra, encoding="utf-8")
    for name in R.APP_EXTRA_FILES:
        shutil.copyfile(ROOT / name, root / name)
    return root


# ---- package and signature ----------------------------------------------------------------------
def test_zip_is_deterministic_and_contains_app():
    a, b = R.build_app_zip(ROOT), R.build_app_zip(ROOT)
    assert a == b
    names = zipfile.ZipFile(io.BytesIO(a)).namelist()
    assert "mirrorway/mac/selfupdate.py" in names and "requirements-mac.txt" in names
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
    assert R.version_in_zip(a) == __version__


def test_sign_and_verify(monkeypatch):
    priv, pub = _keypair()
    data = R.build_app_zip(ROOT)
    m = R.make_manifest(__version__, data, "notes", created=1)
    sig = R.sign(m, priv)
    assert R.public_key_of(priv) == pub
    got = R.verify_manifest(m, sig, keys=[pub])
    assert got["version"] == __version__ and got["sha256"] == hashlib.sha256(data).hexdigest()
    R.verify_zip(got, data)
    # wrong key, modified manifest, modified package
    other = _keypair()[1]
    with pytest.raises(R.ReleaseError):
        R.verify_manifest(m, sig, keys=[other])
    with pytest.raises(R.ReleaseError):
        R.verify_manifest(m.replace(b'"size"', b'"siz3"'), sig, keys=[pub])
    with pytest.raises(R.ReleaseError):
        R.verify_zip(got, data[:-1] + bytes([data[-1] ^ 1]))
    with pytest.raises(R.ReleaseError):
        R.verify_manifest(m, "broken!!", keys=[pub])
    # the embedded key list contains the real key
    assert len(base64.b64decode(R.PUBLIC_KEYS[0])) == 32


def test_changelog_section():
    text = "# D\n\n## 2.3.0 — 5 November\n- a\n- b\n\n## 2.2.0 — x\n- c\n"
    assert R.changelog_section(text, "2.3.0") == "- a\n- b"
    assert R.changelog_section(text, "9.9.9") == ""
    assert "## " not in R.changelog_section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), __version__)


def test_make_release_script(tmp_path):
    priv, pub = _keypair()
    env = dict(os.environ, MIRRORWAY_SIGNING_KEY=priv)
    # an unknown key is refused
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "make_release.py"), "--tag", f"v{__version__}",
                        "--out", str(tmp_path)], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "does not match" in r.stderr
    # wrong tag
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "make_release.py"), "--tag", "v0.0.1",
                        "--out", str(tmp_path)], env=env, capture_output=True, text=True)
    assert r.returncode == 1
    # once added to the key list it builds (a wrapper extends the list in the subprocess)
    code = (f"import sys, runpy; sys.path.insert(0, {str(ROOT)!r}); import mirrorway.release as R; "
            f"R.PUBLIC_KEYS.append({pub!r}); sys.argv=['m','--tag','v{__version__}','--out',{str(tmp_path)!r}]; "
            f"runpy.run_path({str(ROOT / 'scripts' / 'make_release.py')!r}, run_name='__main__')")
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    m = (tmp_path / R.manifest_name(__version__)).read_bytes()
    sig = (tmp_path / R.sig_name(__version__)).read_text()
    got = R.verify_manifest(m, sig, keys=[pub])
    R.verify_zip(got, (tmp_path / R.zip_name(__version__)).read_bytes())


# ---- fake GitHub -------------------------------------------------------------------------
class FakeResp(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeGitHub:
    """Stands in for urlopen: /releases/latest and /releases/assets/<id>. The repository is public, so requests
    must carry no credentials at all."""

    def __init__(self, files: dict, version: str, status: int = 200):
        self.files, self.version, self.status = files, version, status
        self.ids = {name: 100 + i for i, name in enumerate(files)}
        self.calls = []

    def __call__(self, req, timeout=30):
        url = req.full_url
        self.calls.append((url, req.get_header("Accept")))
        assert "Authorization" not in req.headers and "Authorization" not in req.unredirected_hdrs
        assert req.get_header("User-agent", "").startswith("Mirrorway/")
        if self.status != 200:
            raise urllib.error.HTTPError(url, self.status, "error", {}, None)
        if url.endswith("/releases/latest"):
            assert "/repos/fatihtanrivverdi/mirrorway/" in url
            body = {"tag_name": f"v{self.version}", "html_url": "https://github.com/x", "body": "notes",
                    "assets": [{"name": n, "id": i} for n, i in self.ids.items()]}
            return FakeResp(json.dumps(body).encode())
        aid = int(url.rsplit("/", 1)[1])
        assert req.get_header("Accept") == "application/octet-stream"
        name = {i: n for n, i in self.ids.items()}[aid]
        return FakeResp(self.files[name])


def _release_files(tmp_path, version, priv):
    data = R.build_app_zip(_fake_app(tmp_path, version))
    m = R.make_manifest(version, data, "- new things")
    return {R.zip_name(version): data, R.manifest_name(version): m,
            R.sig_name(version): (R.sign(m, priv) + "\n").encode()}


@pytest.fixture
def signed(monkeypatch):
    priv, pub = _keypair()
    monkeypatch.setattr(R, "PUBLIC_KEYS", [pub])
    return priv


def test_check_and_download(tmp_path, signed):
    files = _release_files(tmp_path, "9.1.0", signed)
    fake = FakeGitHub(files, "9.1.0")
    gh = U.GitHub(opener=fake)
    info = U.check(gh, current=__version__)
    assert info.version == "9.1.0" and info.newer_than(__version__) and info.manifest["notes"] == "- new things"
    data = U.download(gh, info)
    assert R.version_in_zip(data) == "9.1.0"
    # the package was modified
    files[R.zip_name("9.1.0")] = files[R.zip_name("9.1.0")] + b"x"
    with pytest.raises(U.UpdateError, match="size"):
        U.download(gh, info)


def test_check_rejects_unsigned_or_wrong_key(tmp_path, signed):
    other_priv, _ = _keypair()
    fake = FakeGitHub(_release_files(tmp_path, "9.1.0", other_priv), "9.1.0")
    with pytest.raises(U.UpdateError, match="signature"):
        U.check(U.GitHub(opener=fake))
    files = _release_files(tmp_path, "9.2.0", signed)
    del files[R.sig_name("9.2.0")]
    fake = FakeGitHub(files, "9.2.0")
    with pytest.raises(U.UpdateError, match="missing"):
        U.check(U.GitHub(opener=fake))
    # the manifest belongs to another version (attack: an old signed manifest under a new tag)
    old = _release_files(tmp_path, "9.0.0", signed)
    files = {R.zip_name("9.3.0"): old[R.zip_name("9.0.0")], R.manifest_name("9.3.0"): old[R.manifest_name("9.0.0")],
             R.sig_name("9.3.0"): old[R.sig_name("9.0.0")]}
    fake = FakeGitHub(files, "9.3.0")
    with pytest.raises(U.UpdateError, match="does not belong"):
        U.check(U.GitHub(opener=fake))


def test_github_errors_are_readable():
    for status, text in ((404, "No release has been published yet"), (403, "limiting requests"),
                         (429, "limiting requests"), (500, "HTTP 500")):
        with pytest.raises(U.UpdateError, match=text):
            U.GitHub(opener=FakeGitHub({}, "9.1.0", status=status)).latest()


def test_updater_needs_no_token():
    assert not hasattr(U, "save_token") and not hasattr(U, "TokenError")
    assert U.REPO == "fatihtanrivverdi/mirrorway"


# ---- installation and rollback ------------------------------------------------------------------
def _installed_tree(tmp_path) -> Path:
    import shutil
    base = tmp_path / "home"
    root = base / "app"
    src = _fake_app(tmp_path, __version__)
    shutil.copytree(src, root)
    return root


def _ok_runner(calls):
    def run(args, timeout):
        calls.append(args)

        class Rr:
            returncode = 0
            stderr = ""
            stdout = "9.1.0\n" if "-I" in args else ""
        return Rr()
    return run


def test_install_swaps_and_keeps_previous(tmp_path, signed):
    root = _installed_tree(tmp_path)
    data = _release_files(tmp_path, "9.1.0", signed)[R.zip_name("9.1.0")]
    calls = []
    prev = U.install(data, "9.1.0", root=root, python_exe="py", runner=_ok_runner(calls))
    assert '__version__ = "9.1.0"' in (root / "mirrorway" / "__init__.py").read_text(encoding="utf-8")
    assert (prev / "VERSION").read_text() == __version__ and (prev / "mirrorway" / "mac" / "app.py").exists()
    assert not (root.parent / U.STAGE).exists()
    assert not any("pip" in a for a in calls)                       # same dependencies: pip did not run
    assert U.previous_version(root) == __version__
    # roll back
    assert U.rollback(root) == __version__
    assert f'__version__ = "{__version__}"' in (root / "mirrorway" / "__init__.py").read_text(encoding="utf-8")


def test_install_runs_pip_when_requirements_change(tmp_path, signed):
    root = _installed_tree(tmp_path)
    src = _fake_app(tmp_path, "9.1.0")
    (src / "requirements-mac.txt").write_text((src / "requirements-mac.txt").read_text() + "\n# changed\n")
    data = R.build_app_zip(src)
    calls = []
    U.install(data, "9.1.0", root=root, python_exe="py", runner=_ok_runner(calls))
    pip = [a for a in calls if "pip" in a]
    assert pip and "--require-hashes" in pip[0]
    assert "# changed" in (root / "requirements-mac.txt").read_text()


def test_install_on_the_apps_own_python(tmp_path, signed):
    """The .dmg's app: new packages go into ~/.mirrorway/venv (on top of the app's Python), never into the bundle,
    and start.sh is told (venv/.req) to use that environment."""
    import hashlib
    root = _installed_tree(tmp_path)
    src = _fake_app(tmp_path, "9.1.0")
    (src / "requirements-mac.txt").write_text((src / "requirements-mac.txt").read_text() + "\n# new package\n")
    data = R.build_app_zip(src)
    calls = []
    bundle_py = "/Applications/Mirrorway.app/Contents/Resources/python/bin/python3"
    prefix = "/Applications/Mirrorway.app/Contents/Resources/python"
    assert U.bundled_python(prefix) and not U.bundled_python("/opt/homebrew/opt/python@3.14")
    U.install(data, "9.1.0", root=root, python_exe=bundle_py, runner=_ok_runner(calls), prefix=prefix)
    venv = root.parent / "venv"
    made = [a for a in calls if "venv" in a and "-m" in a]
    assert made and made[0][0] == bundle_py and "--system-site-packages" in made[0]
    pip = [a for a in calls if "pip" in a]
    assert pip and pip[0][0] == str(venv / "bin" / "python")          # not the bundle's Python
    probe = [a for a in calls if "-I" in a]
    assert probe and probe[0][0] == str(venv / "bin" / "python")
    want = hashlib.sha1((root / "requirements-mac.txt").read_bytes()).hexdigest()
    assert (venv / ".req").read_text().strip() == want
    # the same dependencies: nothing is installed, the app's Python is used as it is
    calls.clear()
    (tmp_path / "again").mkdir()
    src2 = _fake_app(tmp_path / "again", "9.1.0")
    (src2 / "requirements-mac.txt").write_bytes((root / "requirements-mac.txt").read_bytes())
    U.install(R.build_app_zip(src2), "9.1.0", root=root, python_exe=bundle_py, runner=_ok_runner(calls), prefix=prefix)
    assert not any("pip" in a for a in calls)


def test_install_failure_leaves_current_version(tmp_path, signed):
    root = _installed_tree(tmp_path)
    data = _release_files(tmp_path, "9.1.0", signed)[R.zip_name("9.1.0")]

    def bad(args, timeout):
        class Rr:
            returncode, stdout, stderr = 1, "", "ImportError: missing"
        return Rr()
    with pytest.raises(U.UpdateError, match="failed its test import"):
        U.install(data, "9.1.0", root=root, python_exe="py", runner=bad)
    assert f'__version__ = "{__version__}"' in (root / "mirrorway" / "__init__.py").read_text(encoding="utf-8")
    assert U.previous_version(root) is None


def test_install_rejects_bad_paths(tmp_path):
    root = _installed_tree(tmp_path)
    for name in ("../evil.py", "/etc/x", "other/file.txt"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(name, "x")
        with pytest.raises(U.UpdateError):
            U.install(buf.getvalue(), "9.1.0", root=root, python_exe="py", runner=_ok_runner([]))


def test_real_probe_imports_new_code(tmp_path, signed):
    """The new code is tested by importing it in a real subprocess (a broken package is not installed)."""
    root = _installed_tree(tmp_path)
    good = R.build_app_zip(_fake_app(tmp_path, "9.1.0"))
    U.install(good, "9.1.0", root=root)
    assert U.previous_version(root) == __version__
    broken_src = _fake_app(tmp_path, "9.2.0", extra="\nimport no_such_module\n")
    with pytest.raises(U.UpdateError, match="failed its test import"):
        U.install(R.build_app_zip(broken_src), "9.2.0", root=root)
    assert '__version__ = "9.1.0"' in (root / "mirrorway" / "__init__.py").read_text(encoding="utf-8")


def test_restart_command(monkeypatch, tmp_path):
    monkeypatch.delenv("MIRRORWAY_BUNDLE", raising=False)
    assert U.restart_command() is None and U.spawn_restart(popen=lambda *a, **k: None) is False
    monkeypatch.setenv("MIRRORWAY_BUNDLE", str(tmp_path))
    cmd = U.restart_command()
    assert cmd[0] == "/bin/sh" and cmd[-1] == str(tmp_path) and "/usr/bin/open -n" in cmd[2]
    got = []
    assert U.spawn_restart(popen=lambda c, **k: got.append((c, k))) and got[0][1]["start_new_session"]


# ---- state machine ------------------------------------------------------------------------------
def test_updater_flow(tmp_path, signed):
    cfg = DictCfg()
    files = _release_files(tmp_path, "9.1.0", signed)
    fake = FakeGitHub(files, "9.1.0")
    installed = []
    t = {"now": 1_793_900_000.0}
    up = U.Updater(cfg, github_factory=lambda: U.GitHub(opener=fake), clock=lambda: t["now"],
                   installer=lambda data, ver, root=None: installed.append((ver, len(data))))
    assert up.due()
    assert up.check() == "available" and up.info.version == "9.1.0" and not up.due()
    assert up.text == "New version: 9.1.0"
    assert up.install_available() == "installed" and installed == [("9.1.0", len(files[R.zip_name("9.1.0")]))]
    # up to date
    fake.version = __version__
    fake.files = _release_files(tmp_path, __version__, signed)
    fake.ids = {n: 200 + i for i, n in enumerate(fake.files)}
    assert up.check() == "uptodate" and up.info is None and __version__ in up.text
    t["now"] += U.CHECK_INTERVAL_S + 1
    assert up.due()
    cfg.set("auto_update_check", False)
    assert not up.due()


def test_mac_menu_update_flow(tmp_path):
    from test_platform_glue import run_script
    priv, pub = _keypair()
    out = run_script(f"""
        import stubs, os, sys
        Q, AK, R, pb = stubs.install_mac()
        sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})
        from mirrorway.config import Config, MAC_DEFAULTS
        cfg = Config('mac-test-updates', MAC_DEFAULTS)
        import mirrorway.release as REL
        REL.PUBLIC_KEYS[:] = [{pub!r}]
        from mirrorway.mac import app as A, selfupdate as U
        import test_mac_updates as T
        from pathlib import Path
        files = T._release_files(Path({str(tmp_path)!r}), '9.1.0', {priv!r})
        fake = T.FakeGitHub(files, '9.1.0')
        a = A.MirrorwayMacApp(cfg)
        installed = []
        a.updater.github_factory = lambda: U.GitHub(opener=fake)
        a.updater.installer = lambda data, ver, root=None: installed.append(ver)
        a._bg = lambda fn, name: fn()                       # run at once instead of in the background
        import mirrorway.mac.notify as N
        notes = []
        N.notify = lambda *args, **kw: notes.append((args, kw)) or True
        assert not any('token' in (i.title or '').lower() for i in a.m_update._order if i is not None)
        a._tick(None)                                       # the daily check is due: runs and finds 9.1.0
        a._tick(None)
        assert a.m_update.title == 'Update Available: 9.1.0 ●', a.m_update.title
        assert a.mi_upd_install.title == 'Install 9.1.0 and Restart' and len(notes) == 1
        # install: the confirmation returns 1 (Install) -> installation and restart
        restarted = []
        U.spawn_restart = lambda: restarted.append(1) or True
        quit = []
        a.quit_app = lambda s: quit.append(1)
        a.install_update(None)
        assert installed == ['9.1.0'] and a._restart_pending
        a._tick(None)
        assert restarted and quit
        print('OK')
    """)
    assert "OK" in out


def test_paired_device_registered_before_confirmation():
    """The device must be registered before the pairing confirmation reaches Windows (otherwise the keyed
    connection that follows at once was refused as 'unpaired device' and Windows waited 15 s — caught on real CI)."""
    import socket
    import threading

    from mirrorway import devkeys as K
    from mirrorway import secure as S
    from mirrorway.protocol import recv_exact
    a, b = socket.socketpair()
    a.settimeout(5)
    b.settimeout(5)
    mac, win = K.Identity.generate(), K.Identity.generate()
    order = []

    def server():
        assert recv_exact(b, 6) == S.MAGIC2 and recv_exact(b, 1) == S.ROLE_PAIR
        S.server_pair(b, "123456", mac, "Mac", on_success=lambda info: order.append(("registered", info["pub"])))
        order.append(("server done", None))

    t = threading.Thread(target=server, daemon=True)
    t.start()
    info = S.client_pair(a, "123456", win, "PC")
    order.append(("client confirmed", None))
    t.join(5)
    assert order[0] == ("registered", win.public) and info["pub"] == mac.public


# ---- TLS root certificates and backing off after a failed check -------------------
def test_ssl_context_loads_system_roots_on_macos_python(monkeypatch, tmp_path):
    import ssl
    bundle = next((p for p in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt")
                   if os.path.exists(p)), None)
    if bundle is None:
        pytest.skip("no system CA file")
    empty = lambda: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)        # like python.org's macOS Python: no roots
    monkeypatch.setattr(U.sys, "platform", "darwin")
    monkeypatch.setattr(ssl, "create_default_context", empty)
    monkeypatch.setattr(U, "_ssl_ctx", None)
    monkeypatch.setattr(U, "_SYSTEM_CA_FILES", (str(tmp_path / "missing.pem"), bundle))
    ctx = U.ssl_context()
    assert ctx.cert_store_stats()["x509_ca"] > 0 and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    # without a file: roots exported from the macOS keychain
    monkeypatch.setattr(U, "_ssl_ctx", None)
    monkeypatch.setattr(U, "_SYSTEM_CA_FILES", ())
    monkeypatch.setattr(U, "_macos_root_pem", lambda: open(bundle, encoding="ascii", errors="ignore").read())
    assert U.ssl_context().cert_store_stats()["x509_ca"] > 0
    monkeypatch.setattr(U, "_ssl_ctx", None)


def test_failed_check_backs_off_and_warns_once(caplog):
    cfg = DictCfg()
    t = {"now": 1_000_000.0}

    class Down:
        def latest(self):
            raise U.UpdateError("Could not reach GitHub: CERTIFICATE_VERIFY_FAILED")

    up = U.Updater(cfg, github_factory=lambda: Down(), clock=lambda: t["now"])
    assert up.due()
    with caplog.at_level("WARNING", logger="mirrorway.selfupdate"):
        assert up.check() == "error"
        assert not up.due()                                   # not retried at once
        t["now"] += 60
        assert not up.due()
        up.check()                                            # a manual check still runs
        t["now"] += U.RETRY_AFTER_FAILURE_S + 1
        assert up.due()
    assert sum("Could not check for updates" in r.getMessage() for r in caplog.records) == 1


def test_login_item_follows_the_app(tmp_path, monkeypatch):
    from mirrorway.mac import loginitem as L
    monkeypatch.setattr(L.Path, "home", staticmethod(lambda: tmp_path))
    assert not L.follow("/Applications/Mirrorway.app")                     # off: stays off
    L.enable(str(tmp_path / "Applications" / "Mirrorway.app"))           # the zip's app (~/Applications)
    assert L.follow("/Applications/Mirrorway.app") and L.target() == "/Applications/Mirrorway.app"
    assert not L.follow("/Applications/Mirrorway.app")                     # already right
    for temp in ("/Volumes/Mirrorway/Mirrorway.app",
                 "/private/var/folders/x/AppTranslocation/ABC/d/Mirrorway.app"):
        assert L.temporary(temp) and not L.follow(temp)
    assert L.target() == "/Applications/Mirrorway.app" and not L.temporary(None)
