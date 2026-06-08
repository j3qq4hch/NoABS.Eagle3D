# -*- mode: python ; coding: utf-8 -*-
# One-DIR build: eagle2gltf/ folder (exe + _internal) — no per-run extraction,
# fast startup. Lands in release_artifacts/software/eagle2gltf/.
from pathlib import Path

SOFTWARE_DIR = Path(SPECPATH)

a = Analysis(
    [str(SOFTWARE_DIR / 'eagle2gltf.py')],
    pathex=[str(SOFTWARE_DIR)],
    binaries=[],
    datas=[],
    # mapbox_earcut (C++ triangulation) + numpy are imported dynamically inside
    # _earcut.py, so PyInstaller can't see them — list them explicitly.
    hiddenimports=['mapbox_earcut', 'numpy'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['cadquery', 'OCC', 'OCP', 'tkinter', 'scipy', 'earcut'],
    noarchive=False,
)

# Drop OpenSSL DLLs — only needed for HTTPS, we make no network calls.
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
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='eagle2gltf',
)
