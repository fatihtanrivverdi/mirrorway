"""CI: builds and signs the release package.

Usage: MIRRORWAY_SIGNING_KEY=<base64> python scripts/make_release.py --tag v3.0.0 --out dist
The tag must match the version in mirrorway/__init__.py. Stops if the signing key's public key is not in the
PUBLIC_KEYS list in the code (a release signed with the wrong key is never published).
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mirrorway import __version__  # noqa: E402
from mirrorway import release as R  # noqa: E402


def download_notes(version: str) -> str:
    return (f"**Windows:** download `Mirrorway-{version}-windows-setup.exe` and double-click it; it installs and "
            "starts Mirrorway (no Python needed). The first time Windows may say \"Windows protected your PC\": "
            "click **More info › Run anyway**.\n\n"
            f"**Mac (Apple silicon):** download `Mirrorway-{version}-mac.dmg`, open it and drag Mirrorway into "
            "Applications, then open it there. The first time macOS may refuse to open it: open **System Settings "
            "› Privacy & Security** and click **Open Anyway**. Intel Macs: `Mirrorway-{version}-mac.zip` with "
            "`install-mac.command` (see the README).\n\n"
            "Installed copies update themselves: the Mac from here, Windows from the Mac.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", default="dist")
    a = ap.parse_args()
    version = a.tag[1:] if a.tag.startswith("v") else a.tag
    if version != __version__:
        print(f"The tag ({a.tag}) differs from the code version ({__version__})", file=sys.stderr)
        return 1
    key = os.environ.get("MIRRORWAY_SIGNING_KEY", "").strip()
    if not key:
        print("MIRRORWAY_SIGNING_KEY is not set", file=sys.stderr)
        return 1
    if R.public_key_of(key) not in R.PUBLIC_KEYS:
        print("The signing key does not match the public keys in the code", file=sys.stderr)
        return 1
    data = R.build_app_zip(ROOT)
    if len(data) > R.MAX_ZIP_BYTES:
        print("The package is too large", file=sys.stderr)
        return 1
    notes = R.changelog_section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    manifest = R.make_manifest(version, data, notes)
    sig = R.sign(manifest, key)
    m = R.verify_manifest(manifest, sig)          # verify itself
    R.verify_zip(m, data)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / R.zip_name(version)).write_bytes(data)
    (out / R.manifest_name(version)).write_bytes(manifest)
    (out / R.sig_name(version)).write_text(sig + "\n", encoding="ascii")
    (out / "notes.md").write_text(download_notes(version) + "\n\n" + (notes or f"Mirrorway {version}"),
                                  encoding="utf-8")
    print(f"Mirrorway {version}: {len(data)} bytes, sha256 {m['sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
