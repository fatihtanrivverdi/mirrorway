"""CI: pinned CI dependencies, actions, and keeping secret files out of the repository."""
import os
import re

from mirrorway import devkeys as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


def test_ci_requirements_pinned_with_hashes():
    for name in ("requirements-ci-linux.txt", "requirements-dev.txt", "requirements-release.txt"):
        text = _read(name)
        blocks = [b for b in re.split(r"\n(?=\S)", text) if b.strip() and not b.startswith("#")]
        assert blocks, name
        for b in blocks:
            assert re.match(r"[A-Za-z0-9_.-]+==\d", b), (name, b[:40])
            assert re.search(r"--hash=sha256:[0-9a-f]{64}", b), (name, b[:40])
    assert "pytest==" in _read("requirements-dev.txt") and "av==" in _read("requirements-ci-linux.txt")


def test_workflow_is_locked_down():
    wf = _read(".github", "workflows", "ci.yml")
    uses = re.findall(r"uses:\s*(\S+)", wf)
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u) for u in uses), uses
    assert re.search(r"^permissions:\n  contents: read", wf, re.M)
    assert wf.count("persist-credentials: false") == wf.count("actions/checkout@")
    assert "--require-hashes" in wf
    jobs = re.split(r"\n  (?=[a-z][\w-]*:\n)", wf.split("\njobs:\n", 1)[1])
    by_name = {j.split(":", 1)[0]: j for j in jobs}
    # Jobs that may write to the repository: only the tagged release (release) and failure logs (report)
    writers = sorted(n for n, j in by_name.items() if "contents: write" in j)
    assert writers == ["release", "report"], writers
    # The secret key only in the signing step of the release job
    assert re.findall(r"secrets\.(\w+)", wf) == ["MIRRORWAY_SIGNING_KEY"]
    rel = by_name["release"]
    assert "startsWith(github.ref, 'refs/tags/v')" in rel and "needs: [linux, macos, windows, windows-sound]" in rel
    steps = rel.split("      - ")
    sign = [st for st in steps if "secrets.MIRRORWAY_SIGNING_KEY" in st]
    assert len(sign) == 1 and "make_release.py" in sign[0] and "publish_release" not in sign[0]
    # The virtual sound card's driver (an outside action) never runs where release packages are built
    snd = by_name["windows-sound"]
    assert "LABSN/sound-ci-helpers@" in snd and "upload-artifact" not in snd and "secrets." not in snd
    assert "uses: LABSN" not in by_name["windows"] and "uses: LABSN" not in by_name["macos"]
    rep = by_name["report"]
    assert "uses:" not in rep and "pytest" not in rep and "pip install" not in rep
    assert ("if: ${{ failure() || (always() && (startsWith(github.ref, 'refs/tags/v') || "
            "github.event_name == 'workflow_dispatch')) }}") in rep
    assert "git push -q -f" in rep and " ci-logs" in rep and "main" not in rep.split("git push", 1)[1]
    # macOS/Windows only by hand or on a release tag
    for job in ("macos", "windows", "windows-sound"):
        block = re.split(r"\n  (?=\S)", wf.split(f"\n  {job}:", 1)[1], maxsplit=1)[0]
        assert "workflow_dispatch" in block and "refs/tags/v" in block, job


def test_gitignore_keeps_secrets_out():
    gi = _read(".gitignore").splitlines()
    for pat in ("github-token", "mac.json", "win.json", "*.zip", "*.log"):
        assert pat in gi, pat


def test_keychain_service_is_configurable():
    calls = []

    class R:
        returncode, stdout, stderr = 0, "", ""

    class Cfg(dict):
        def set(self, k, v, save=True):
            self[k] = v

    st = D.KeychainStore(runner=lambda args, **kw: (calls.append(args), R())[1], service="app.mirrorway.ci-test")
    st.save(Cfg(), "k", b"x")
    assert calls[0][calls[0].index("-s") + 1] == "app.mirrorway.ci-test"
    assert D.KeychainStore().service == "app.mirrorway"


def test_log_forwarding_survives_reconnect():
    """An old sender whose connection was renewed while it slept must neither take nor lose the queued record."""
    import collections

    from mirrorway.win.client import ControlClient

    class Ch:
        def __init__(self, closed=False, fail=False):
            self.closed, self.fail, self.sent = closed, fail, []

        def send_json(self, mtype, obj):
            if self.fail:
                raise ConnectionError("Channel closed")
            self.sent.append(obj)

        def send(self, *a):
            pass

    c = ControlClient.__new__(ControlClient)
    c._log_queue = collections.deque([{"msg": "a"}])
    c.monitors_fn = lambda: []
    import threading
    c._stop_evt = threading.Event()
    old = Ch(closed=False, fail=True)
    import mirrorway.win.client as C
    orig_sleep = C.time.sleep
    try:
        C.time.sleep = lambda s: setattr(old, "closed", True)   # the connection closed while sleeping
        c._pinger(old)
        assert list(c._log_queue) == [{"msg": "a"}]
        broken = Ch(fail=True)
        C.time.sleep = lambda s: None
        c._pinger(broken)                       # send error: the record goes back to the queue
        assert list(c._log_queue) == [{"msg": "a"}]
    finally:
        C.time.sleep = orig_sleep
