"""CI: uploads the signed release files to GitHub Releases (with GITHUB_TOKEN).

Usage: GH_TOKEN=... GITHUB_REPOSITORY=owner/repo python scripts/publish_release.py --tag v3.0.0 --dir dist
If a release for the tag already exists, the missing files are added (safe to run again).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"


def call(method, url, token, data=None, ctype="application/json"):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"message": body[:300].decode("utf-8", "replace")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--dir", default="dist")
    a = ap.parse_args()
    token = os.environ["GH_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    d = Path(a.dir)
    version = a.tag[1:] if a.tag.startswith("v") else a.tag
    notes = (d / "notes.md").read_text(encoding="utf-8") if (d / "notes.md").exists() else ""
    st, rel = call("POST", f"{API}/repos/{repo}/releases", token, json.dumps({
        "tag_name": a.tag, "name": f"Mirrorway {version}", "body": notes, "draft": False, "prerelease": False,
    }).encode())
    if st == 422:
        st, rel = call("GET", f"{API}/repos/{repo}/releases/tags/{urllib.parse.quote(a.tag)}", token)
    if st not in (200, 201):
        print(f"Could not create the release: {st} {rel.get('message')}", file=sys.stderr)
        return 1
    have = {x["name"] for x in rel.get("assets", [])}
    upload = rel["upload_url"].split("{", 1)[0]
    # the signed update package (read by the app's updater) and the download packages for people
    names = [f"mirrorway-app-{version}.zip", f"mirrorway-{version}.json", f"mirrorway-{version}.json.sig",
             f"Mirrorway-{version}-mac.zip", f"Mirrorway-{version}-windows.zip"]
    for name in names:
        if name in have:
            print(f"already there: {name}")
            continue
        data = (d / name).read_bytes()
        ctype = "application/zip" if name.endswith(".zip") else "application/octet-stream"
        st, res = call("POST", f"{upload}?name={urllib.parse.quote(name)}", token, data, ctype)
        if st not in (200, 201):
            print(f"Could not upload {name}: {st} {res.get('message')}", file=sys.stderr)
            return 1
        print(f"uploaded: {name} ({len(data)} bytes)")
    print(rel.get("html_url", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
