# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for KaproTUN-Setup.exe — the branded installer.

Build pipeline:
    1. pyinstaller KaproTUN.spec        # builds dist/KaproTUN.exe (~57 MB)
    2. pyinstaller KaproTUN-Setup.spec  # embeds dist/KaproTUN.exe → dist/KaproTUN-Setup.exe

Result: a single ~110 MB Setup.exe the user downloads, runs, and gets a
custom amber-on-dark installer flow (Welcome → Progress → Done) that
copies KaproTUN.exe to %LOCALAPPDATA%\\Programs\\KaproTUN, creates Start
Menu + Desktop shortcuts, registers an uninstaller. No admin required.
"""
import os

# A release installer must know the digest of the KaproTUN.exe it will
# install (installer/payload_digest.py, filled in by
# packaging/write_payload_digest.py between the two builds). Building it
# without one in CI would silently ship an installer that has to ask GitHub
# what to trust — so there, that is an error.
if os.environ.get("GITHUB_ACTIONS") == "true":
    import re as _re
    _src = open(os.path.join("installer", "payload_digest.py"), encoding="utf-8").read()
    if not _re.search(r'^MAIN_EXE_SHA256 = "[0-9a-f]{64}"$', _src, _re.M):
        raise SystemExit("installer/payload_digest.py is empty — run "
                         "packaging/write_payload_digest.py after building KaproTUN.exe")

# Note: we deliberately do NOT embed dist/KaproTUN.exe here any more.
# Doing so doubled the installer's download size to ~100 MB. Instead,
# the installer downloads KaproTUN.exe from the matching GitHub release
# at install time (see installer/operations.py). Keeps the Setup.exe
# under ~45 MB at the cost of requiring internet during install.
# (Acceptable — VPN client; if you can't reach github.com you wouldn't
# get far anyway.)

a = Analysis(
    ['installer_run.py'],
    pathex=[],
    binaries=[],
    datas=[
        # Reuse main-app brand assets (the window icon) inside the installer.
        # Its screens themselves are drawn in code: no pictures to bundle.
        ('kapro_tun/data', 'kapro_tun/data'),
    ],
    hiddenimports=[
        # Reached only via lazy imports in installer/operations.py, so
        # PyInstaller's static analysis can't see them: the graceful-quit
        # pipe (QtNetwork + singleton) and the uninstall network-cleanup
        # safety net (system proxy + firewall-rule modules).
        'PySide6.QtNetwork',
        'kapro_tun.gui.singleton',
        'kapro_tun.core.system_proxy',
        'kapro_tun.core.storage',
        'kapro_tun.core.killswitch',
        'kapro_tun.core.ipv6_block',
        'kapro_tun.core.webrtc_block',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'matplotlib', 'numpy', 'scipy', 'PIL',
        'pytest', 'unittest', 'doctest', 'pydoc',
        'PySide6.QtBluetooth', 'PySide6.Qt3DCore', 'PySide6.Qt3DRender',
        'PySide6.Qt3DAnimation', 'PySide6.QtCharts',
        'PySide6.QtDataVisualization', 'PySide6.QtMultimedia',
        'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
        'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtTest',
        'PySide6.QtPositioning', 'PySide6.QtLocation',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='KaproTUN-Setup',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='kapro_tun/data/icon.ico',
)
