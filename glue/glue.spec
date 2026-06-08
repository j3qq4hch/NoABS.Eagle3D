# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

GLUE_DIR = Path(SPECPATH)

import sys as _sys
_conda = Path(_sys.base_prefix)
_ssl_dlls = [
    (str(_conda / 'Library' / 'bin' / 'libssl-3-x64.dll'),    '.'),
    (str(_conda / 'Library' / 'bin' / 'libcrypto-3-x64.dll'), '.'),
]

a = Analysis(
    [str(GLUE_DIR / 'glue.py')],
    pathex=[str(GLUE_DIR)],
    binaries=_ssl_dlls,
    datas=[
        (str(GLUE_DIR / 'ui'),       'ui'),
        (str(GLUE_DIR / 'glue.ini'), '.'),
    ] + collect_data_files('pythonnet') + collect_data_files('clr_loader'),
    hiddenimports=['webview', 'webview.platforms.winforms', 'clr', 'clr_loader']
                + collect_submodules('clr_loader'),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['cadquery', 'OCC', 'OCP'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='glue',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=None,
)

coll = COLLECT(
    exe,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='glue',
)
