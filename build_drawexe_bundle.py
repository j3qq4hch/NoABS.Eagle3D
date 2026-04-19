"""
build_drawexe_bundle.py
-----------------------
Собирает минимальный автономный дистрибутив DRAWEXE из установленного OpenCASCADE.
Результат: папка drawexe_bundle/ рядом со скриптом.

Использование:
    python build_drawexe_bundle.py
"""

import shutil
from pathlib import Path

OCCT_ROOT = Path(r"D:\OpenCascade\opencascade-7.9.3-vc14-64")
THIRD     = Path(r"D:\OpenCascade\3rdparty-vc14-64")
OUT       = Path(__file__).parent / "glue" / "drawexe_bundle"

OCCT_BIN  = OCCT_ROOT / "win64" / "vc14" / "bin"
OCCT_SRC  = OCCT_ROOT / "src"   # CSF_OCCTResourcePath

# Конкретные DLL из 3rdparty (проверены экспериментом)
THIRD_DLLS = [
    THIRD / "tcltk-8.6.15-x64"      / "bin"       / "tcl86.dll",
    THIRD / "tcltk-8.6.15-x64"      / "bin"       / "tk86.dll",
    THIRD / "tcltk-8.6.15-x64"      / "bin"       / "zlib1.dll",
    THIRD / "freeimage-3.18.0-x64"  / "bin"       / "FreeImage.dll",
    THIRD / "freetype-2.13.3-x64"   / "bin"       / "freetype.dll",
    THIRD / "tbb-2021.13.0-x64"     / "bin"       / "tbb12.dll",
    THIRD / "jemalloc-vc14-64"      / "bin"       / "jemalloc.dll",
    THIRD / "openvr-1.14.15-64"     / "bin/win64" / "openvr_api.dll",
]

# FFmpeg: берём все DLL из папки (имена версионированы, не угадываем)
FFMPEG_BIN = THIRD / "ffmpeg-3.3.4-64" / "bin"

# Ресурсные папки из src/ (нужны для CSF_* переменных)
RESOURCE_DIRS = [
    "DrawResources",
    "XSTEPResource",
    "XmlOcafResource",
    "StdResource",
    "SHMessage",
    "Shaders",
    "Textures",
    "XSMessage",
    "TObj",
]


def _copy_file(src: Path, dst_dir: Path):
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst_dir / src.name)
    print(f"  + {src.name}")


def _copy_dir(src: Path, dst: Path):
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    print(f"  + {src.name}/  ({sum(1 for _ in src.rglob('*'))} файлов)")


def main():
    print(f"Выходная папка: {OUT}\n")

    if OUT.exists():
        shutil.rmtree(OUT)

    bin_dir = OUT / "bin"
    res_dir = OUT / "res"

    # ── DRAWEXE + все OCCT DLL ──────────────────────────────────────────────
    print("=== OCCT bin ===")
    _copy_file(OCCT_BIN / "DRAWEXE.exe", bin_dir)
    for dll in sorted(OCCT_BIN.glob("*.dll")):
        _copy_file(dll, bin_dir)

    # ── 3rdparty DLL (проверенный минимум) ──────────────────────────────────
    print("\n=== 3rdparty DLL ===")
    missing = []
    for f in THIRD_DLLS:
        if f.exists():
            _copy_file(f, bin_dir)
        else:
            print(f"  MISSING: {f}")
            missing.append(f)

    # ── FFmpeg (все DLL из папки) ────────────────────────────────────────────
    print("\n=== FFmpeg DLL ===")
    if FFMPEG_BIN.exists():
        for dll in sorted(FFMPEG_BIN.glob("*.dll")):
            _copy_file(dll, bin_dir)
    else:
        print(f"  MISSING папка: {FFMPEG_BIN}")

    # ── Ресурсные папки OCCT ─────────────────────────────────────────────────
    print("\n=== Ресурсы OCCT ===")
    for name in RESOURCE_DIRS:
        src = OCCT_SRC / name
        if src.exists():
            _copy_dir(src, res_dir / name)
        else:
            print(f"  MISSING: {name}/")

    # ── Итог ─────────────────────────────────────────────────────────────────
    total_mb = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file()) / 1024**2
    total_files = sum(1 for f in OUT.rglob("*") if f.is_file())
    print(f"\n{'='*50}")
    print(f"Готово: {total_files} файлов, {total_mb:.1f} MB")
    if missing:
        print(f"Не найдено: {len(missing)} файлов — проверь пути в скрипте")
    print(f"Папка: {OUT}")


if __name__ == "__main__":
    main()
