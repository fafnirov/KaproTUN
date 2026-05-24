"""PyInstaller entry script for KaproVPN-Setup.exe."""
import sys

from installer.main import main

if __name__ == "__main__":
    sys.exit(main())
