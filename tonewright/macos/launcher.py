"""Entry point of the packaged macOS app (PyInstaller starts this file)."""
import sys

from tonewright.app import main

if __name__ == "__main__":
    sys.exit(main())
