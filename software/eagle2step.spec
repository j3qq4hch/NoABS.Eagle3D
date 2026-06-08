# -*- mode: python ; coding: utf-8 -*-
# One-DIR build: eagle2step/ folder (exe + _internal) — no per-run extraction,
# fast startup. Lands in release_artifacts/software/eagle2step/.
# drawexe_bundle/ is NOT bundled — it stays a sibling folder, invoked as a subprocess.
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
    excludes=['cadquery', 'OCC', 'OCP', 'tkinter', 'scipy'],
    noarchive=False,
)

a.binaries = [b for b in a.binaries
              if not b[0].lower().startswith(('libcrypto', 'libssl', '_ssl'))]

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
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='eagle2step',
)
