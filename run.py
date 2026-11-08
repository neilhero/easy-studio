#!/usr/bin/env python3
"""Start the local Lazy Studio viewer; forwards --root, --port and --open."""
import pathlib
import runpy
import sys

if __name__ == "__main__":
    entry = pathlib.Path(__file__).resolve().parent / "studio.py"
    sys.argv[0] = str(entry)
    runpy.run_path(str(entry), run_name="__main__")
