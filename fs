#!/usr/bin/env python3
"""Entry point for the `fs` Freshsales CLI."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from fs_cli.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
