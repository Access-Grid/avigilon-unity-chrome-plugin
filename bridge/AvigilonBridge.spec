# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — builds on macOS, Linux, and Windows.

import os
import sys

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'requests',
        'urllib3',
        'flask',
        'flask_cors',
        'cryptography',
        'PIL',
        'pystray',
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'xml.etree.ElementTree',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='AvigilonBridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# macOS: wrap in .app bundle
if sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='AvigilonBridge.app',
        bundle_identifier='com.accessgrid.avigilon-bridge',
        info_plist={
            'NSHighResolutionCapable': True,
            'NSPrincipalClass': 'NSApplication',
            'CFBundleShortVersionString': '1.0.0',
            'LSUIElement': True,  # Hide from dock (background app)
        },
    )
