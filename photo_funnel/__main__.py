"""
    python3 -m photo_funnel                 # serve on 127.0.0.1:8770
    python3 -m photo_funnel --port 9000
    python3 -m photo_funnel --no-browser
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .server import PORT, serve


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="photo_funnel",
                                 description="Review a shoot in twelve steps.")
    ap.add_argument("--version", action="version", version=f"photo_funnel {__version__}")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    return serve(port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
