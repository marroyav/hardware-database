#!/usr/bin/env python3
"""Compatibility entry point for the DAPHNE production CLI."""

from daphne_production.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
