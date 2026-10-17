#!/usr/bin/env python3
"""ClickerBridge - send a presentation clicker's buttons to OpenLP's HTTP API.

Run:            python clickerbridge.py
List devices:   python clickerbridge.py --list-devices
See README.md for per-platform setup (driver / permissions).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cb import __version__  # noqa: E402
from cb.backends import load_backend  # noqa: E402
from cb.backends.base import CaptureError  # noqa: E402
from cb.bridge import BridgeCore  # noqa: E402
from cb.openlp import OpenLPClient  # noqa: E402
from cb.settings import Settings  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="clickerbridge", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"ClickerBridge {__version__}")
    parser.add_argument("--list-devices", action="store_true", help="print the devices the capture backend sees and exit")
    parser.add_argument("--settings", metavar="FILE", help="use this settings.json instead of the default location")
    parser.add_argument("--minimized", action="store_true",
                        help="start with the window minimized (for use as a startup program)")
    parser.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    settings = Settings(Path(args.settings) if args.settings else None)
    backend = load_backend()

    if args.list_devices:
        try:
            devices = backend.list_devices()
        except CaptureError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        if not devices:
            print("No keyboard-like devices found.")
        for d in devices:
            flag = "[built-in] " if d.builtin else ""
            print(f"{flag}{d.name}\n    id: {d.id}\n    {d.details}")
        return 0

    o = settings.openlp
    openlp = OpenLPClient(o.get("host"), o.get("port"), o.get("username"), o.get("password"))
    core = BridgeCore(settings, backend, openlp)

    from cb.ui import App  # imported late so --list-devices works without a display

    app = App(settings, core, backend.name if backend.available else backend.unavailable_reason,
              start_minimized=args.minimized)
    if args.selftest:
        app.root.after(800, app.on_close)
        app.run()
        print("selftest ok")
        return 0
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
