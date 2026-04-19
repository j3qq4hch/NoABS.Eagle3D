# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

SOFTWARE_DIR = Path(SPECPATH)

a = Analysis(
    [str(SOFTWARE_DIR / 'eagle2gltf.py')],
    pathex=[str(SOFTWARE_DIR)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=['cadquery', 'OCC', 'OCP', 'tkinter', 'numpy', 'scipy', 'earcut'],
    noarchive=False,
)

# Убираем OpenSSL DLL — они нужны только для HTTPS, мы не делаем сетевых запросов
a.binaries = [b for b in a.binaries
              if not b[0].lower().startswith(('libcrypto', 'libssl', '_ssl'))]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='eagle2gltf',
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
    name='eagle2gltf',
)
