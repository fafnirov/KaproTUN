# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for KaproTUN — cross-platform.

Build locally on whichever OS you're targeting:
    pip install pyinstaller
    pyinstaller KaproTUN.spec

Outputs:
    Windows: dist/KaproTUN.exe              (single-file GUI)
    macOS:   dist/KaproTUN.app              (bundle in a folder)
    Linux:   dist/KaproTUN                  (single-file ELF)

xray and tun2socks binaries are NOT bundled — they're downloaded on
first launch into the per-OS app data directory. Keeps the bundle
slim (~50 MB instead of ~90 MB) and lets us pick up upstream Xray
fixes without re-shipping.
"""
import os
import sys

_is_windows = sys.platform == "win32"
_is_macos   = sys.platform == "darwin"

# Pick the right brand icon for each OS. PyInstaller is strict on macOS
# BUNDLE() — it rejects .png and demands .icns. CI generates icon.icns
# from icon.png via sips+iconutil before invoking PyInstaller; if it's
# missing (local dev build without that step), fall back to None so the
# build still completes, just without a Dock icon.
if _is_windows:
    _icon = 'kapro_tun/data/icon.ico'
elif _is_macos:
    if os.path.exists('kapro_tun/data/icon.icns'):
        _icon = 'kapro_tun/data/icon.icns'
    else:
        _icon = None  # build proceeds, app gets generic Python icon
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
        ('kapro_tun/data', 'kapro_tun/data'),
    ],
    hiddenimports=[
        # PySocks (`socks` module) is imported dynamically by urllib3 only
        # when a SOCKS5 proxy URL is actually used at runtime. PyInstaller's
        # static analysis misses it, and the public-IP probe in HTTP-proxy
        # mode (which routes through socks5h://127.0.0.1:2081) would crash
        # with "Missing dependencies for SOCKS support" — silently, because
        # the probe wraps everything in try/except. Result: empty IP label.
        # Forcing the import here makes PyInstaller bundle PySocks.
        'socks',
        # psutil (v1.15.4) — used by xray_stats.query_tun_iface_stats to
        # read live byte counters off the KaproTun interface. PyInstaller
        # finds the top-level psutil import in xray_stats.py, but psutil's
        # platform backend (psutil._psutil_windows) is loaded via C ext
        # and the analyzer needs the hint to bundle the compiled .pyd.
        'psutil',
        # cryptography (v1.16.12) — AES-256-GCM for config encryption on
        # macOS/Linux. secrets_store imports it lazily inside functions;
        # naming it here guarantees PyInstaller's cryptography hook pulls
        # in the Rust backend (_rust). Harmless on Windows (DPAPI path).
        'cryptography.hazmat.primitives.ciphers.aead',
        # zxing-cpp + Pillow (v3.1.9) — optional QR decoding for subscription /
        # config import. core/qr.py imports them lazily with a graceful
        # fallback, so a build that omits them just disables the QR button;
        # naming them here makes the default build bundle the decoder. PIL is
        # un-excluded below for the same reason. numpy stays excluded — zxing-cpp
        # decodes a PIL image without it.
        'zxingcpp',
        'PIL.Image',
    ],
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
    name='KaproTUN',
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
        name='KaproTUN.app',
        icon=_icon,
        bundle_identifier='com.kaprotun.app',
        info_plist={
            'CFBundleName': 'KaproTUN',
            'CFBundleDisplayName': 'KaproTUN',
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
