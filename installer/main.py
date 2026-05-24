"""KaproVPN-Setup.exe entry point.

Run normally → install flow.
Run with --uninstall → confirm + uninstall flow.
"""
from __future__ import annotations

import sys

from .gui import run


def main() -> int:
    uninstall = "--uninstall" in sys.argv
    return run(uninstall=uninstall)


if __name__ == "__main__":
    sys.exit(main())
