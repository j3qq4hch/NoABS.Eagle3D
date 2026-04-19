"""
step_tessellator.py
-------------------
Тесселирует STEP → GLB через DRAWEXE.exe из drawexe_bundle/.

Бандл ищется автоматически рядом с этим файлом.
Ручная настройка не требуется.
"""

import os
import subprocess
import tempfile
import time
from pathlib import Path

import sys as _sys
if getattr(_sys, 'frozen', False):
    _TOOL_DIR = Path(_sys.executable).parent
else:
    _TOOL_DIR = Path(__file__).resolve().parent
BUNDLE_DIR = _TOOL_DIR.parent / "drawexe_bundle"
_DRAWEXE   = BUNDLE_DIR / "bin" / "DRAWEXE.exe"
_RES_DIR   = BUNDLE_DIR / "res"

HAS_DRAWEXE: bool = _DRAWEXE.exists()


def _build_env() -> dict:
    """Строит env с PATH и CSF_* переменными, указывающими на бандл."""
    env = os.environ.copy()
    env["PATH"] = str(BUNDLE_DIR / "bin") + os.pathsep + env.get("PATH", "")

    def r(name: str) -> str:
        return str(_RES_DIR / name).replace("\\", "/")

    env["CSF_LANGUAGE"]              = "us"
    env["MMGT_CLEAR"]                = "1"
    env["DRAWHOME"]                  = r("DrawResources")
    env["DRAWDEFAULT"]               = r("DrawResources/DrawDefault")
    env["CSF_DrawPluginDefaults"]    = r("DrawResources")
    env["CSF_SHMessage"]             = r("SHMessage")
    env["CSF_MDTVTexturesDirectory"] = r("Textures")
    env["CSF_ShadersDirectory"]      = r("Shaders")
    env["CSF_XSMessage"]             = r("XSMessage")
    env["CSF_TObjMessage"]           = r("TObj")
    env["CSF_StandardDefaults"]      = r("StdResource")
    env["CSF_PluginDefaults"]        = r("StdResource")
    env["CSF_XCAFDefaults"]          = r("StdResource")
    env["CSF_TObjDefaults"]          = r("StdResource")
    env["CSF_StandardLiteDefaults"]  = r("StdResource")
    env["CSF_IGESDefaults"]          = r("XSTEPResource")
    env["CSF_STEPDefaults"]          = r("XSTEPResource")
    env["CSF_XmlOcafResource"]       = r("XmlOcafResource")
    env["CSF_MIGRATION_TYPES"]       = r("StdResource/MigrationSheet.txt")
    return env


def tessellate(step_path: Path, out_dir: Path) -> Path:
    if not HAS_DRAWEXE:
        raise RuntimeError(
            f"drawexe_bundle не найден: {BUNDLE_DIR}\n"
            "Запустите build_drawexe_bundle.py для сборки бандла."
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (step_path.stem + ".glb")

    def _tp(p: Path) -> str:
        return str(p.resolve()).replace("\\", "/")

    tcl = (
        f'pload XDE OCAF MODELING\n'
        f'ReadStep D "{_tp(step_path)}"\n'
        f'XGetOneShape s D\n'
        f'incmesh s 0.2\n'
        f'WriteGltf D "{_tp(out_path)}"\n'
        f'exit\n'
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".tcl",
                                     delete=False, encoding="utf-8") as f:
        tcl_file = Path(f.name)
        f.write(tcl)

    try:
        print(f"[tess] {step_path.name} → {out_path.name}")
        result = subprocess.run(
            [str(_DRAWEXE), "-f", str(tcl_file)],
            env=_build_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
        )
        print(f"[tess] exit code: {result.returncode}")
        if result.stdout:
            print(result.stdout, end="")
    finally:
        tcl_file.unlink(missing_ok=True)

    if not out_path.exists():
        raise RuntimeError(f"Тесселяция не удалась: {step_path.name}")

    print(f"[tess] ✓ {out_path.name} ({out_path.stat().st_size} bytes)")
    return out_path


def get_glb_bytes(step_path: Path, out_dir: Path) -> bytes:
    out_path = out_dir / (step_path.stem + ".glb")
    if (out_path.exists() and
            out_path.stat().st_mtime >= step_path.stat().st_mtime):
        print(f"[tess] cache: {out_path.name}")
        return out_path.read_bytes()
    return tessellate(step_path, out_dir).read_bytes()
