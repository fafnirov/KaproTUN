"""KaproTUN-Setup.exe entry point.

Modes (CLI flags):
  default             → auto-detect:
                          - clean machine → install flow
                          - existing install detected → maintenance UI
                            (Reinstall / Uninstall choice)
  --install           → force install flow even if already installed
  --uninstall         → confirm + uninstall flow (used by the Programs
                        & Features "Uninstall" button — we register the
                        uninstaller command with this flag)
  --silent            → headless update install: tiny progress indicator,
                        run install_everything(), launch installed app,
                        exit. Used by the in-app auto-updater.
  --selftest <folder> → draw every screen once, save the pictures and a
                        report, install nothing. Used by the release build.
"""
from __future__ import annotations

import sys

from . import paths
from .gui import run, run_selftest, run_silent


def main() -> int:
    if "--selftest" in sys.argv:
        rest = [a for a in sys.argv[1:] if not a.startswith("--")]
        return run_selftest(rest[0] if rest else "installer-selftest")
    if "--silent" in sys.argv:
        return run_silent()
    if "--uninstall" in sys.argv:
        return run(mode="uninstall")
    if "--install" in sys.argv:
        return run(mode="install")
    # No flag: pick the right mode based on whether KaproTUN is already
    # on disk. Running Setup.exe a second time should NOT silently
    # re-install — it should ask. Same UX as Telegram, Slack, etc.
    if paths.installed_exe_path().is_file():
        return run(mode="maintenance")
    return run(mode="install")


if __name__ == "__main__":
    sys.exit(main())
