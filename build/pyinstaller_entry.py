"""PyInstaller entry shim.

Wraps :func:`maverick.cli.main` so PyInstaller can produce a single-file
binary. Distributed via GitHub Releases for macOS / Linux / Windows.
"""
from maverick.cli import main

if __name__ == "__main__":
    main()
