# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for KaproVPN.

Build locally:
    pip install pyinstaller
    pyinstaller KaproVPN.spec

Output: dist/KaproVPN.exe — single-file, GUI-only (no console window),
ships with the bundled brand icons + default sites list. The xray and
tun2socks binaries are NOT bundled — they're still downloaded on first
launch into %LOCALAPPDATA%\\KaproVPN\\. Keeps the exe small (~40-60 MB
instead of ~80 MB) and lets us pick up upstream xray fixes without
re-shipping.
"""

a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=[
        # Bundle our brand assets + default sites list. Relative path
        # inside the bundle mirrors the source layout, so
        # `Path(__file__).resolve().parent.parent / "data" / ...` keeps
        # working both in dev and in the frozen exe.
        ('kapro_vpn/data', 'kapro_vpn/data'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim Python stdlib + third-party libs we never import. PyInstaller
        # is conservative by default — these excludes shave ~15 MB off.
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
    upx=False,                      # don't UPX — false positive AV hits
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                  # no console window — pure GUI
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='kapro_vpn/data/icon.ico',
)
