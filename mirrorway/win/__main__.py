"""Start the Windows side:  python -m mirrorway.win  [--debug]"""
from __future__ import annotations

import argparse
import logging
import sys

from ..config import WIN_DEFAULTS, Config, setup_logging
from ..i18n import set_language


def main() -> None:
    if sys.platform != "win32":
        sys.exit("This part is for Windows. On the Mac:  python3 -m mirrorway.mac")
    ap = argparse.ArgumentParser(description="Mirrorway — Windows client")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--host", help="Mac address (skips discovery)")
    ap.add_argument("--code", help="pairing code")
    ap.add_argument("--viewer", action="store_true", help="show the Mac's screen at startup")
    args = ap.parse_args()
    log_path = setup_logging("mirrorway-win", args.debug)
    logging.getLogger("mirrorway").info("Log: %s", log_path)
    cfg = Config("windows", WIN_DEFAULTS)
    set_language(cfg.get("language"))
    if args.host is not None:
        cfg.set("host", args.host)
    if args.code:
        cfg.set("pair_code", args.code.upper())
    from .app import WinApp
    try:
        WinApp(cfg, open_viewer=args.viewer).run()
    except Exception:
        logging.getLogger("mirrorway").exception("The app crashed")
        raise


if __name__ == "__main__":
    main()
