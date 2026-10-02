"""Allows `python3 -m wedding_album ...`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
