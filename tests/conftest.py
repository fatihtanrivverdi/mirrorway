import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MIRRORWAY_HOME", tempfile.mkdtemp(prefix="mirrorway-test-"))
os.environ.setdefault("MIRRORWAY_LEGACY_HOME", tempfile.mkdtemp(prefix="mirrorway-test-legacy-"))   # never read ~/.kopru
os.environ["MIRRORWAY_LANG"] = "en"                                                            # tests expect English

# The real macOS/Windows CI machines (virtual, few cores) are too slow for tests that measure speed;
# those tests are skipped there and run on Linux CI and locally.
SLOW_CI_PLATFORM = bool(os.environ.get("CI")) and sys.platform in ("darwin", "win32")
SLOW_CI_REASON = "sensitive to CPU speed; skipped on real-platform CI machines (runs on Linux)"


class DictCfg:
    def __init__(self, **kw):
        self.d = dict(kw)

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, k, v, save=True):
        self.d[k] = v


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_device_keys(monkeypatch):
    """In tests, device keys live in the settings dict (PlainStore). On macOS the default store is the Keychain,
    which uses a single item (app.mirrorway/device_key); the fake Mac and the fake Windows in one test would
    overwrite each other's key (seen on real macOS CI). The Keychain is tested separately."""
    from mirrorway import devkeys
    monkeypatch.setattr(devkeys, "default_store", lambda: devkeys.PlainStore())


TEST_NETWORK = {"id": "gw:02:00:00:00:00:01", "weak": "ip:192.168.1.1|192.168.1.255", "gateway": "192.168.1.1",
                "ifname": "en0", "source": "test"}


@pytest.fixture(autouse=True)
def _isolated_network(monkeypatch):
    """Tests never look at the real network identity: if the CI machine's network changed during a test (while the
    network signature was re-read on macOS CI), the server could drop connections. Network lock tests bring their own probes."""
    from mirrorway import netlock
    monkeypatch.setattr(netlock.NetworkLock, "_probe", lambda self: [dict(TEST_NETWORK)])
