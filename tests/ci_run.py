"""CI helper: runs a command and shows its output unchanged; if it fails, the end of the output is also written
as a GitHub Actions annotation, so the error can be read (through the Checks API) without downloading the
job log.

Usage: python tests/ci_run.py [--retry N] "<title>" <command> [arguments...]
--retry: for environment-dependent runs (Xvfb smoke tests; end-to-end tests sensitive to timing on slow real-platform
machines). Every attempt shows up in the log. When pytest is retried only the failed tests run (--lf); a test that
fails twice still fails.
"""
import collections
import os
import subprocess
import sys

TAIL_LINES = 200
MAX_CHARS = 60000


def _escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")


def _escape_prop(text: str) -> str:
    return _escape(text).replace(":", "%3A").replace(",", "%2C")


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    args = sys.argv[1:]
    retries = 0
    if args[0] == "--retry":
        retries, args = int(args[1]), args[2:]
    title, cmd = args[0], args[1:]
    tail = collections.deque(maxlen=TAIL_LINES)
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    for attempt in range(retries + 1):
        if attempt:
            print(f"::warning title={_escape_prop(title)}::failed, retrying ({attempt}/{retries})")
        run = cmd + (["--lf"] if attempt and any("pytest" in c for c in cmd) else [])
        proc = subprocess.Popen(run, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                text=True, encoding="utf-8", errors="replace")
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            tail.append(line.rstrip("\n"))
        code = proc.wait()
        if code == 0:
            break
    if code != 0:
        body = "\n".join(tail)[-MAX_CHARS:]
        print(f"::error title={_escape_prop(f'{title} (exit {code})')}::{_escape(body)}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
