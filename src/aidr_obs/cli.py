"""aidr-obs command line: ingest, serve, tap install|uninstall|status, prune."""

import argparse
import json
import sys
from pathlib import Path

from . import store, tapinstall


def _print(obj) -> None:
    print(json.dumps(obj, indent=2))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="aidr-obs", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest", help="read the Noma debug logs and capture.jsonl into SQLite")
    serve = sub.add_parser("serve", help="ingest, then serve the dashboard on 127.0.0.1")
    serve.add_argument("--port", type=int, default=8770)
    serve.add_argument("--no-ingest", action="store_true", help="skip the startup ingest")
    tap = sub.add_parser("tap", help="wire the capture tap into Claude Code and/or Cursor")
    tap.add_argument("action", choices=["install", "uninstall", "status"])
    tap.add_argument("--agent", choices=["all", "claude-code", "cursor"], default="all")
    tap.add_argument(
        "--cursor-hook",
        type=Path,
        help="path to Noma Cursor hook.py (else: previous install, then ~/.cursor/plugins/)",
    )
    prune = sub.add_parser("prune", help="drop captures and stored log lines older than N days")
    prune.add_argument("--days", type=int, required=True)
    ship = sub.add_parser("ship", help="push captures and Noma logs to a remote aidr-obs serve")
    ship.add_argument("action", choices=["run", "once", "install", "uninstall", "status"])
    ship.add_argument(
        "--from-end",
        action="store_true",
        help="treat everything already in the files as shipped (after migrating aidr.db to the server)",
    )
    args = parser.parse_args(argv)

    if args.cmd == "ingest":
        _print(store.ingest(store.connect()))
    elif args.cmd == "serve":
        from . import jevreview, server

        jevreview.load_env_file()
        try:
            server.serve(args.port, ingest_first=not args.no_ingest)
        except ValueError as e:
            print("not serving: " + str(e), file=sys.stderr)
            return 1
    elif args.cmd == "tap":
        if args.action == "install":
            try:
                _print(tapinstall.install(args.agent, args.cursor_hook))
            except tapinstall.SelfTestFailed as e:
                print("not installed: " + str(e), file=sys.stderr)
                return 1
        elif args.action == "uninstall":
            _print(tapinstall.uninstall(args.agent))
        else:
            _print(tapinstall.status())
    elif args.cmd == "ship":
        from . import ship as shipper

        try:
            if args.action in ("run", "once"):
                return shipper.run(from_end=args.from_end, once=args.action == "once")
            if args.action == "install":
                _print(shipper.install(from_end=args.from_end))
            elif args.action == "uninstall":
                _print(shipper.uninstall())
            else:
                _print(shipper.status())
        except shipper.ShipError as e:
            print("ship: " + str(e), file=sys.stderr)
            return 1
    elif args.cmd == "prune":
        if args.days < 0:
            parser.error("--days must be >= 0")
        _print(store.prune(store.connect(), args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
