"""
Command line entry point.

    python3 -m photo_studio
    python3 -m photo_studio --port 9000
    python3 -m photo_studio --no-browser
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import __version__
from .server import PORT, serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="photo_studio",
        description="A website for reviewing a shoot: ranked, diagnosed, "
                    "and compared side by side.",
    )
    parser.add_argument("--version", action="version",
                        version=f"photo_studio {__version__}")
    parser.add_argument("--port", type=int, default=PORT,
                        help="port to serve on (default: %(default)s)")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser window")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return serve(port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
