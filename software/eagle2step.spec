# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

SOFTWARE_DIR = Path(SPECPATH)

a = Analysis(
    [str(SOFTWARE_DIR / 'eagle2step.py')],
    pathex=[str(SOFTWARE_DIR)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=['cadquery', 'OCC', 'OCP', 'tkinter'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='eagle2step',
    debug=False,
    strip=False,
    upx=False,
    console=True,
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
    name='eagle2step',
)
