# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for KaproVPN-Setup.exe — the branded installer.

Build pipeline:
    1. pyinstaller KaproVPN.spec        # builds dist/KaproVPN.exe (~57 MB)
    2. pyinstaller KaproVPN-Setup.spec  # embeds dist/KaproVPN.exe → dist/KaproVPN-Setup.exe

Result: a single ~110 MB Setup.exe the user downloads, runs, and gets a
custom amber-on-dark installer flow (Welcome → Progress → Done) that
copies KaproVPN.exe to %LOCALAPPDATA%\\Programs\\KaproVPN, creates Start
Menu + Desktop shortcuts, registers an uninstaller. No admin required.
"""
import glob
from pathlib import Path

ROOT = Path('.').resolve()

# The main KaproVPN.exe payload — must exist when this spec runs.
PAYLOAD = ROOT / 'dist' / 'KaproVPN.exe'
if not PAYLOAD.is_file():
    raise SystemExit(
        f"Missing {PAYLOAD}. Build it first with `pyinstaller KaproVPN.spec`."
    )

# Optional designer assets — bundled if present, fall back to app icons.
installer_pngs = [
    (p, 'installer') for p in glob.glob('installer/*.png')
]

a = Analysis(
    ['installer_run.py'],
    pathex=[],
    binaries=[],
    datas=[
        # The exe being installed.
        (str(PAYLOAD), 'payload'),

        # Reuse main-app brand assets (icon, splash) inside the installer
        # so the welcome page can display them without us duplicating files.
        ('kapro_vpn/data', 'kapro_vpn/data'),

        *installer_pngs,
    ],
    hiddenimports=[],
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
    name='KaproVPN-Setup',
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
    icon='kapro_vpn/data/icon.ico',
)
