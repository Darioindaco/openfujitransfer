"""
Command line-versie.

    python3 -m open_fujitransfer.cli                 # alles nieuw ophalen
    python3 -m open_fujitransfer.cli --list          # alleen tonen
    python3 -m open_fujitransfer.cli --latest 20 --only jpg
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import APP_NAME, __version__
from .protocol import CAMERA_IP, Cancelled, FujiError
from .sync import DEFAULT_OUT, SyncListener, SyncOptions, human_size, run_sync


class TerminalListener(SyncListener):
    def __init__(self, list_only: bool):
        self.list_only = list_only

    def status(self, message):
        print(message)

    def files_found(self, files):
        if self.list_only:
            for i in files:
                print(f"  {i.date_folder}  {i.filename}")

    def scan_progress(self, done, total):
        sys.stdout.write(f"\r  kaart uitlezen: {done}/{total} ")
        sys.stdout.flush()
        if done == total:
            print()

    def file_progress(self, info, done, total, bytes_per_sec):
        pct = done / total if total else 1
        bar = "#" * int(pct * 24)
        sys.stdout.write(f"\r  {info.filename:<14} [{bar:<24}] {pct:4.0%}  "
                         f"{human_size(bytes_per_sec)}/s ")
        sys.stdout.flush()

    def file_finished(self, info, outcome):
        if outcome == "nieuw":
            print()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="open-fujitransfer",
                                description=f"{APP_NAME}: foto's van je Fujifilm via WiFi naar je Mac.")
    p.add_argument("--out", default=str(DEFAULT_OUT), help="doelmap (per dag een submap)")
    p.add_argument("--list", action="store_true", help="alleen tonen, niets downloaden")
    p.add_argument("--latest", type=int, metavar="N", help="alleen de N nieuwste bestanden")
    p.add_argument("--only", metavar="EXT", help="alleen deze extensies, bv. jpg of jpg,raf")
    p.add_argument("--host", default=CAMERA_IP, help=f"IP van de camera (standaard {CAMERA_IP})")
    p.add_argument("--verbose", "-v", action="store_true", help="toon alle pakketten")
    p.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    p.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    if args.selftest:  # gebruikt door de build: zit alles in de app?
        import tkinter  # noqa: F401
        from PIL import Image, ImageTk  # noqa: F401
        print(f"{APP_NAME} {__version__}: tkinter en Pillow aanwezig")
        return 0

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="  [debug] %(message)s")

    opts = SyncOptions(
        out_dir=Path(args.out).expanduser(),
        only=tuple(e for e in (args.only or "").split(",") if e),
        latest=args.latest,
        list_only=args.list,
        host=args.host,
    )
    try:
        result = run_sync(opts, TerminalListener(args.list))
        if not args.list:
            print(f"Map: {opts.out_dir}")
        return 0 if result.failed == 0 else 1
    except FujiError as e:
        print(f"\nFout: {e}", file=sys.stderr)
        return 1
    except (Cancelled, KeyboardInterrupt):
        print("\nAfgebroken.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
