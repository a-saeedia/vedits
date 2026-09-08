#!/usr/bin/env python3
"""Convenience entry point so `python run.py ...` works without installing."""
import sys
from vedits.cli import main

if __name__ == "__main__":
    sys.exit(main())
