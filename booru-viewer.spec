# -*- mode: python ; coding: utf-8 -*-

import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

hiddenimports = [
    *collect_submodules('booru_viewer'),
    'httpx',
    'httpx._transports',
    'httpx._transports.default',
    'h2',
    'hpack',
    'hyperframe',
    'PIL',
    'PIL.Image',
    'PIL.JpegImagePlugin',
    'PIL.PngImagePlugin',
    'PIL.GifImagePlugin',
    'PIL.WebPImagePlugin',
    'PIL.BmpImagePlugin',
]

a = Analysis(
    ['booru_viewer/main_gui.py'],
    pathex=[],
    binaries=[],
    datas=[('icon.png', '.'), ('booru_viewer/gui/custom_css_guide.txt', 'booru_viewer/gui')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['textual', 'tkinter', 'unittest'],
    noarchive=False,
    optimize=0,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='booru-viewer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon='icon.ico',
)
