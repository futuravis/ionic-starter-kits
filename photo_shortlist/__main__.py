"""
Command line entry point.

    python3 -m photo_shortlist                 # serve the web app
    python3 -m photo_shortlist --port 9000
    python3 -m photo_shortlist --no-browser

    python3 -m photo_shortlist pick FOLDER     # shortlist without a browser
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from wedding_album import pipeline, scanner

from . import __version__, shortlist
from .app import DEFAULT_WANT, MAX_WANT, MIN_WANT, PORT, serve


def cmd_pick(args: argparse.Namespace) -> int:
    """Shortlist a folder on the terminal, for a quick check or a script."""
    source = os.path.expanduser(args.source)

    try:
        images = scanner.find_images(source)
    except NotADirectoryError as exc:
        print(f"error: {exc}")
        return 1

    if not images:
        print(f"No images found under {source}")
        return 1

    print(f"Shortlisting {len(images)} photo(s) from {source}\n")

    last = {"stage": ""}

    def progress(stage: str, done: int, total: int) -> None:
        if stage != last["stage"]:
            last["stage"] = stage
            print(f"  {stage}...")

    result = shortlist.run(
        images,
        want=max(MIN_WANT, min(MAX_WANT, args.want)),
        workers=args.workers,
        progress=progress,
        skip_duplicates=not args.keep_duplicates,
    )

    print(f"\n{result.summary()}\n")
    print(f"  {'#':>2}  {'score':>6}  photo")
    print(f"  {'-' * 2}  {'-' * 6}  {'-' * 40}")
    for candidate in result.picks:
        print(f"  {candidate.rank:>2}  {candidate.percent:>6}  {candidate.name}")
        print(f"      {'':>6}  {candidate.reason}")

    if result.failures:
        print(f"\n  {len(result.failures)} file(s) could not be read:")
        for metrics in result.failures[:5]:
            print(f"    {os.path.basename(metrics.filepath)}: {metrics.error}")

    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    return serve(port=args.port, open_browser=not args.no_browser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="photo_shortlist",
        description="Drop in 20-50 photos, get back the best few.",
    )
    parser.add_argument("--version", action="version",
                        version=f"photo_shortlist {__version__}")

    # Serving is the default, so the bare module call just works.
    parser.add_argument("--port", type=int, default=PORT,
                        help="port to serve on (default: %(default)s)")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser window")
    parser.set_defaults(func=cmd_serve)

    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="run the web app (default)")
    p_serve.add_argument("--port", type=int, default=PORT,
                         help="port to serve on (default: %(default)s)")
    p_serve.add_argument("--no-browser", action="store_true",
                         help="do not open a browser window")
    p_serve.set_defaults(func=cmd_serve)

    p_pick = sub.add_parser("pick", help="shortlist a folder on the terminal")
    p_pick.add_argument("source", help="folder holding the photos")
    p_pick.add_argument("--want", type=int, default=DEFAULT_WANT,
                        help="how many photos to pick (default: %(default)s)")
    p_pick.add_argument("--workers", type=int, default=pipeline.default_workers(),
                        help="parallel worker processes (default: %(default)s)")
    p_pick.add_argument("--keep-duplicates", action="store_true",
                        help="allow near-identical frames to both be picked")
    p_pick.set_defaults(func=cmd_pick)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
