# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for KaproVPN — cross-platform.

Build locally on whichever OS you're targeting:
    pip install pyinstaller
    pyinstaller KaproVPN.spec

Outputs:
    Windows: dist/KaproVPN.exe              (single-file GUI)
    macOS:   dist/KaproVPN.app              (bundle in a folder)
    Linux:   dist/KaproVPN                  (single-file ELF)

xray and tun2socks binaries are NOT bundled — they're downloaded on
first launch into the per-OS app data directory. Keeps the bundle
slim (~50 MB instead of ~90 MB) and lets us pick up upstream Xray
fixes without re-shipping.
"""
import sys

_is_windows = sys.platform == "win32"
_is_macos   = sys.platform == "darwin"

# Pick the right brand icon for each OS. PyInstaller accepts .ico on
# Windows, .icns on macOS, and ignores the icon parameter on Linux
# (we ship icon.png separately for desktop-entry use).
if _is_windows:
    _icon = 'kapro_vpn/data/icon.ico'
elif _is_macos:
    # PyInstaller falls back to a PNG if .icns isn't present — slightly
    # blurry but legal. Swap in icon.icns later when we generate one.
    _icon = 'kapro_vpn/data/icon.png'
else:
    _icon = None

a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=[
        # Brand assets + default sites list. Relative path inside the
        # bundle mirrors the source layout so
        # `Path(__file__).resolve().parent.parent / "data" / ...`
        # keeps working both in dev and in the frozen bundle.
        ('kapro_vpn/data', 'kapro_vpn/data'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim Python stdlib + third-party libs we never import.
        # PyInstaller is conservative by default; these excludes shave
        # ~15 MB off the bundle.
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'PIL',
        'pytest',
        'unittest',
        'doctest',
        'pydoc',
        'PySide6.QtBluetooth',
        'PySide6.Qt3DCore',
        'PySide6.Qt3DRender',
        'PySide6.Qt3DAnimation',
        'PySide6.QtCharts',
        'PySide6.QtDataVisualization',
        'PySide6.QtMultimedia',
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtPdf',
        'PySide6.QtPdfWidgets',
        'PySide6.QtTest',
        'PySide6.QtPositioning',
        'PySide6.QtLocation',
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
    name='KaproVPN',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                      # don't UPX — false-positive AV hits
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                  # no console window — pure GUI
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon,
)

# macOS — wrap the executable in a proper .app bundle so users can
# double-click it in Finder. Without BUNDLE() the binary still runs
# from the terminal but doesn't show a Dock icon or app menu.
if _is_macos:
    app = BUNDLE(
        exe,
        name='KaproVPN.app',
        icon=_icon,
        bundle_identifier='com.kaprovpn.app',
        info_plist={
            'CFBundleName': 'KaproVPN',
            'CFBundleDisplayName': 'KaproVPN',
            'CFBundleShortVersionString': '1.0.0',
            'CFBundleVersion': '1.0.0',
            # We toggle system proxy via networksetup, which doesn't
            # need an entitlement, but mark as agent so the app stays
            # tray-only without a Dock icon when in the tray.
            'LSUIElement': False,
            'NSHighResolutionCapable': True,
            'NSRequiresAquaSystemAppearance': False,
            'NSHumanReadableCopyright': 'GPL v3',
            'NSAppTransportSecurity': {
                'NSAllowsArbitraryLoads': True,
            },
        },
    )
