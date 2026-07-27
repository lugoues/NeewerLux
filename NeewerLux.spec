# -*- mode: python ; coding: utf-8 -*-
# NeewerLux PyInstaller spec file
# Build with:  pyinstaller NeewerLux.spec
# Produces:    dist/NeewerLux/  (--onedir mode)
#
# NeewerLux is a headless service: it serves the HTTP API and web dashboard and has
# no GUI, so this builds a console application with no Qt in it.

import sys
from PyInstaller.utils.hooks import collect_all

block_cipher = None

# Collect all bleak subpackages/data (WinRT backends, etc.)
bleak_datas, bleak_binaries, bleak_hiddenimports = collect_all('bleak')

a = Analysis(
    ['NeewerLux.py'],
    pathex=[],
    binaries=bleak_binaries,
    datas=[
        ('com.github.poizenjam.NeewerLux.ico', '.'),
        ('com.github.poizenjam.NeewerLux.png', '.'),
    ] + bleak_datas,
    hiddenimports=[
        'bleak',
        'bleak.backends.winrt',
        'neewerlux_webui',
    ] + bleak_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='NeewerLux',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,            # Headless service, so the console is the only output
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='com.github.poizenjam.NeewerLux.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='NeewerLux',
)
