"""Start the Mac side:  python -m mirrorway.mac  [--debug] [--headless]"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from ..config import MAC_DEFAULTS, Config, setup_logging
from ..i18n import set_language


def main() -> None:
    if sys.platform != "darwin":
        sys.exit("This part is for macOS. On Windows:  python -m mirrorway.win")
    ap = argparse.ArgumentParser(description="Mirrorway — Mac server")
    ap.add_argument("--debug", action="store_true", help="verbose log")
    ap.add_argument("--headless", action="store_true", help="run without the menu bar (from a terminal)")
    args = ap.parse_args()
    log_path = setup_logging("mirrorway-mac", args.debug)
    log = logging.getLogger("mirrorway")
    cfg = Config("mac", MAC_DEFAULTS)
    set_language(cfg.get("language"))
    if cfg.get("legacy_enabled") is False and cfg.get("pair_code"):
        cfg.set("pair_code", "")           # the old long code is disabled; do not keep it in the file either
    log.info("Log: %s", log_path)

    if not args.headless:
        from .app import run
        run(cfg)
        return

    from .clipboard import MacClipboard
    from .server import MirrorwayServer
    from .tap import EventTap, MacPlatform, accessibility_trusted, request_input_monitoring
    server = MirrorwayServer(cfg, MacPlatform(), clipboard_backend=MacClipboard())
    server.start()
    request_input_monitoring()
    accessibility_trusted(prompt=True)
    tap = EventTap(server.router)
    tap.start()
    print(f"Address: {server.status()['ip']}:{cfg.get('port')}")
    if not server.devices():
        code = server.start_pairing()
        print(f"Pairing code (enter it on Windows): {code[:3]} {code[3:]}")
    print("Press Ctrl+C to quit")
    signal.signal(signal.SIGINT, lambda *_: (server.stop(), sys.exit(0)))
    while True:
        time.sleep(1)
        if not tap.is_alive() and tap.error:
            print("No event tap permission. System Settings > Privacy & Security > Accessibility / Input Monitoring")
            time.sleep(4)
            tap = EventTap(server.router)
            tap.start()


if __name__ == "__main__":
    main()
