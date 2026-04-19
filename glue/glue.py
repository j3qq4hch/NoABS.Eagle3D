"""
library_tool.py
---------------
Запускает утилиту сопоставления Eagle футпринтов с 3D-моделями.

Зависимости:
    pip install pywebview

Использование:
    python library_tool.py
    python library_tool.py path/to/library.lbr
"""

import sys
import json
import base64
import shutil
import threading
import configparser
from pathlib import Path

import webview

from lbr_parser import LbrLibrary
from step_tessellator import get_glb_bytes, tessellate, HAS_DRAWEXE

# RESOURCE_DIR — где лежат ui/, bundled файлы (в сборке: _internal/)
# DATA_DIR     — где лежит glue.ini, рядом с exe (записываемое место)
if getattr(sys, 'frozen', False):
    RESOURCE_DIR = Path(sys._MEIPASS)
    DATA_DIR     = Path(sys.executable).parent
else:
    RESOURCE_DIR = Path(__file__).parent
    DATA_DIR     = Path(__file__).parent
TOOL_DIR = RESOURCE_DIR  # обратная совместимость для url window
INI_PATH = DATA_DIR / "glue.ini"


# ══════════════════════════════════════════════════════════
#  Конфигурация
# ══════════════════════════════════════════════════════════

def _read_ini() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read(INI_PATH, encoding="utf-8")
    return cfg

def _write_ini(cfg: configparser.ConfigParser):
    with open(INI_PATH, "w", encoding="utf-8") as f:
        cfg.write(f)

def _save_last_library(path: str):
    cfg = _read_ini()
    if "general" not in cfg:
        cfg["general"] = {}
    cfg["general"]["last_library"] = path
    _write_ini(cfg)

def _read_step_dir() -> str:
    cfg = _read_ini()
    return cfg.get("general", "step_dir", fallback="")

def _save_step_dir(path: str):
    cfg = _read_ini()
    if "general" not in cfg:
        cfg["general"] = {}
    cfg["general"]["step_dir"] = path
    _write_ini(cfg)


# ══════════════════════════════════════════════════════════
#  Python API — вызывается из JavaScript через pywebview
# ══════════════════════════════════════════════════════════

class LibraryAPI:
    def __init__(self):
        self._lib: LbrLibrary | None = None
        self._lib_path: str = ""
        self._step_files: dict[str, Path] = {}   # pkg_name.lower() → .step path
        raw = _read_step_dir()
        self._step_dir: Path = Path(raw) if raw else Path()
        self._tess_dir: Path = self._step_dir / "tesselated" if raw else Path()

    # ── Вспомогательные ───────────────────────────────────────────────────

    @staticmethod
    def _pick_file(title="Open file", filetypes=("Eagle Library (*.lbr)",)):
        """Диалог выбора файла: pywebview → tkinter fallback."""
        try:
            result = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG,
                file_types=filetypes,
            )
            if result:
                return result[0]
        except Exception:
            pass
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            tk_types = [(t.split("(")[0].strip(), "*" + t.split("*")[1].rstrip(")"))
                        for t in filetypes if "(" in t]
            tk_types.append(("All files", "*.*"))
            p = filedialog.askopenfilename(title=title, filetypes=tk_types)
            root.destroy()
            return p or ""
        except Exception:
            return ""

    def _package_status(self, name):
        step_path = self._step_files.get(name.lower())
        orient    = self._lib.get_orientation(name)
        if step_path is None:
            status = "no_model"
        elif not orient["has_orientation"]:
            status = "no_orientation"
        else:
            status = "ok"
        return {
            "name": name,
            "status": status,
            **{k: orient[k] for k in
               ("tx","ty","tz","rx","ry","rz","drawing_url","model_url")},
        }

    # ── Загрузка библиотеки ────────────────────────────────────────────────

    def open_library(self, path: str = ""):
        """Открывает .lbr файл. Если path пустой — показывает диалог."""
        if not path:
            path = self._pick_file("Open Eagle Library",
                                   ("Eagle Library (*.lbr)",))
            if not path:
                return {"error": "cancelled"}

        try:
            self._lib = LbrLibrary(Path(path))
            self._lib_path = path
        except Exception as e:
            return {"error": str(e)}

        self._rescan_step_files()
        print(f"[lib] step_dir={self._step_dir}, found {len(self._step_files)} STEP files")

        packages = [self._package_status(n) for n in self._lib.package_names()]
        _save_last_library(path)
        return {"packages": packages, "library_path": path}

    # ── Геометрия футпринта ────────────────────────────────────────────────

    def get_footprint(self, pkg_name: str):
        if self._lib is None:
            return {"error": "no library"}
        geom   = self._lib.get_geometry(pkg_name)
        orient = self._lib.get_orientation(pkg_name)
        return {"geometry": geom, "orientation": orient}

    # ── GLB модели компонента ──────────────────────────────────────────────

    def get_component_glb(self, pkg_name: str):
        from step_tessellator import BUNDLE_DIR as _bd
        print(f"[lib] get_component_glb: pkg={pkg_name!r}, HAS_DRAWEXE={HAS_DRAWEXE}, BUNDLE_DIR={_bd}")
        if not HAS_DRAWEXE:
            return {"error": f"drawexe_bundle не найден: {_bd}"}

        key = pkg_name.lower()
        step_path = self._step_files.get(key)
        if step_path is None:
            return {"error": f"STEP-файл не найден: {pkg_name}"}

        try:
            glb = get_glb_bytes(step_path, self._tess_dir)
            return {"glb": base64.b64encode(glb).decode()}
        except Exception as e:
            import traceback; traceback.print_exc()
            return {"error": str(e)}

    # ── Перезагрузка модели (принудительная ретесселяция) ─────────────────

    def reload_model(self, pkg_name: str):
        """Удаляет кэш GLB и заново тесселирует из STEP-файла."""
        if not HAS_DRAWEXE:
            return {"error": "drawexe недоступен: env.bat не найден"}

        key = pkg_name.lower()
        step_path = self._step_files.get(key)
        if step_path is None:
            return {"error": f"STEP-файл не найден: {pkg_name}"}

        # Удаляем кэшированный GLB
        glb_path = self._tess_dir / (step_path.stem + ".glb")
        if glb_path.exists():
            glb_path.unlink()
            print(f"[lib] deleted GLB cache: {glb_path.name}")

        try:
            glb = get_glb_bytes(step_path, self._tess_dir)
            return {"glb": base64.b64encode(glb).decode()}
        except Exception as e:
            import traceback; traceback.print_exc()
            return {"error": str(e)}

    # ── Сохранение ────────────────────────────────────────────────────────

    def save_orientation(self, pkg_name: str,
                         tx, ty, tz, rx, ry, rz,
                         drawing_url: str, model_url: str):
        if self._lib is None:
            return {"error": "no library"}

        try:
            self._lib.save_orientation(
                pkg_name,
                float(tx), float(ty), float(tz),
                float(rx), float(ry), float(rz),
                drawing_url, model_url
            )
        except Exception as e:
            return {"error": f"Ошибка записи в lbr: {e}"}

        step_path = self._step_files.get(pkg_name.lower())
        if step_path and HAS_DRAWEXE:
            try:
                tessellate(step_path, self._tess_dir)
            except Exception as e:
                return {"ok": True, "warning": f"Тесселяция: {e}"}

        return {"ok": True}

    # ── Переименование футпринта ───────────────────────────────────────────

    def rename_package(self, old_name: str, new_name: str):
        if self._lib is None:
            return {"error": "no library"}
        try:
            self._lib.rename_package(old_name, new_name)
        except (ValueError, KeyError) as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e)}

        # Переименовываем STEP и GLB файлы
        key_old = old_name.lower()
        step_path = self._step_files.pop(key_old, None)
        if step_path and step_path.exists():
            new_step = step_path.parent / (new_name + step_path.suffix)
            step_path.rename(new_step)
            self._step_files[new_name.lower()] = new_step
            old_glb = self._tess_dir / (step_path.stem + ".glb")
            if old_glb.exists():
                old_glb.rename(self._tess_dir / (new_name + ".glb"))

        return {"ok": True}

    # ── Удаление футпринта ────────────────────────────────────────────────

    def delete_package(self, pkg_name: str):
        if self._lib is None:
            return {"error": "no library"}

        # Удаляем STEP-файл
        key = pkg_name.lower()
        step_path = self._step_files.pop(key, None)
        # Fallback: ищем файл напрямую в папке (на случай несоответствия имён)
        if step_path is None or not step_path.exists():
            for ext in ('.step', '.stp'):
                candidate = self._step_dir / (pkg_name + ext)
                if candidate.exists():
                    step_path = candidate
                    break
            if step_path is None or not step_path.exists():
                # Последний вариант: поиск без учёта регистра
                if self._step_dir.exists():
                    for f in self._step_dir.iterdir():
                        if f.stem.lower() == key and f.suffix.lower() in ('.step', '.stp'):
                            step_path = f
                            break
        if step_path and step_path.exists():
            step_path.unlink()
            print(f"[lib] deleted STEP: {step_path.name}")

        # Удаляем GLB-файл
        for glb_name in (pkg_name + ".glb",
                         (step_path.stem + ".glb") if step_path else None):
            if glb_name:
                glb_path = self._tess_dir / glb_name
                if glb_path.exists():
                    glb_path.unlink()
                    print(f"[lib] deleted GLB: {glb_path.name}")

        try:
            self._lib.delete_package(pkg_name)
        except Exception as e:
            return {"error": str(e)}

        return {"ok": True}

    # ── Дублирование футпринта ────────────────────────────────────────────

    def duplicate_package(self, src_name: str, new_name: str):
        if self._lib is None:
            return {"error": "no library"}
        try:
            self._lib.duplicate_package(src_name, new_name)
        except (ValueError, KeyError) as e:
            return {"error": str(e)}
        except Exception as e:
            return {"error": str(e)}
        return {"ok": True}

    # ── Загрузка STEP с диска ─────────────────────────────────────────────

    def load_step_file(self, pkg_name: str):
        """Диалог выбора STEP-файла, копирование в step/ с именем футпринта."""
        path = self._pick_file(
            "Load STEP model",
            ("STEP files (*.step;*.stp)",)
        )
        if not path:
            return {"error": "cancelled"}

        src = Path(path)
        self._step_dir.mkdir(parents=True, exist_ok=True)
        dst = self._step_dir / (pkg_name + ".step")
        try:
            shutil.copy2(src, dst)
        except Exception as e:
            return {"error": f"Ошибка копирования файла: {e}"}

        self._step_files[pkg_name.lower()] = dst

        if HAS_DRAWEXE:
            try:
                get_glb_bytes(dst, self._tess_dir)
            except Exception as e:
                return {"ok": True, "warning": f"Тесселяция: {e}"}

        return {"ok": True}

    # ── Удаление 3D-модели из футпринта ──────────────────────────────────

    def delete_step(self, pkg_name: str):
        """Удаляет STEP/GLB файлы и метаданные ориентации."""
        key = pkg_name.lower()
        step_path = self._step_files.pop(key, None)
        if step_path and step_path.exists():
            step_path.unlink()
            print(f"[lib] deleted STEP: {step_path.name}")
        # Ищем GLB по имени пакета (независимо от расширения исходного файла)
        for glb in self._tess_dir.glob(f"{pkg_name}.glb"):
            glb.unlink()
            print(f"[lib] deleted GLB: {glb.name}")

        if self._lib is not None:
            try:
                self._lib.remove_orientation(pkg_name)
            except Exception as e:
                return {"error": str(e)}

        return {"ok": True}

    # ── Ретесселяция всех STEP-файлов ────────────────────────────────────

    def retessellate_all(self):
        """Удаляет все GLB из tesselated/ и перетесселирует все STEP-файлы."""
        if not HAS_DRAWEXE:
            return {"error": "drawexe недоступен: env.bat не найден"}

        def _status(msg: str):
            webview.windows[0].evaluate_js(
                f'setStatus({json.dumps(msg)})'
            )

        # Удаляем все GLB из папки tesselated
        deleted = 0
        if self._tess_dir.exists():
            for glb in self._tess_dir.glob("*.glb"):
                glb.unlink()
                deleted += 1
        print(f"[lib] retessellate_all: удалено {deleted} GLB")

        # Пересканируем STEP-файлы
        self._rescan_step_files()

        errors = []
        done = 0
        total = len(self._step_files)
        for i, (key, step_path) in enumerate(self._step_files.items(), 1):
            _status(f"Tessellating model {i} of {total}: {step_path.name}")
            try:
                tessellate(step_path, self._tess_dir)
                done += 1
                print(f"[lib] retessellate_all: {i}/{total} {step_path.name}")
            except Exception as e:
                errors.append(f"{step_path.name}: {e}")
                print(f"[lib] retessellate_all ERROR {step_path.name}: {e}")

        result = {"done": done, "total": total}
        if errors:
            result["errors"] = errors
        return result

    # ── Step dir ──────────────────────────────────────────────────────────

    def get_step_dir(self) -> dict:
        raw = _read_step_dir()
        return {"step_dir": raw}

    def set_step_dir(self) -> dict:
        folder = self._pick_folder("Select STEP components folder")
        if not folder:
            return {"error": "cancelled"}
        self._step_dir  = Path(folder)
        self._tess_dir  = self._step_dir / "tesselated"
        _save_step_dir(folder)
        self._rescan_step_files()
        packages = ([self._package_status(n) for n in self._lib.package_names()]
                    if self._lib else [])
        return {"step_dir": folder, "packages": packages,
                "step_count": len(self._step_files)}

    def _rescan_step_files(self):
        self._step_files = {}
        if self._step_dir.exists():
            for f in self._step_dir.glob("*.step"):
                self._step_files[f.stem.lower()] = f
            for f in self._step_dir.glob("*.stp"):
                self._step_files[f.stem.lower()] = f

    # ── Повторное сканирование папки step ─────────────────────────────────

    def rescan_step(self):
        self._rescan_step_files()
        if self._lib is None:
            return {"packages": []}

        packages = [self._package_status(n) for n in self._lib.package_names()]
        return {"packages": packages, "step_count": len(self._step_files)}

    # ── Список пакетов (для обновления после операций) ────────────────────

    def get_packages(self):
        """Возвращает актуальный список пакетов со статусами."""
        if self._lib is None:
            return {"packages": []}
        packages = [self._package_status(n) for n in self._lib.package_names()]
        return {"packages": packages}

    # ── Downstream ────────────────────────────────────────────────────────────

    @staticmethod
    def _pick_folder(title="Select folder") -> str:
        try:
            result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
            if result:
                return result[0]
        except Exception:
            pass
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            p = filedialog.askdirectory(title=title)
            root.destroy()
            return p or ""
        except Exception:
            return ""

    def get_downstream_status(self):
        cfg = _read_ini()
        folder      = cfg.get("general", "libfolder", fallback="")
        lib_name    = Path(self._lib_path).name if self._lib_path else ""
        folder_name = Path(folder).name if folder else ""
        return {
            "has_lib":     self._lib is not None,
            "lib_name":    lib_name,
            "folder":      folder,
            "folder_name": folder_name,
        }

    def pick_lib_folder(self):
        folder = self._pick_folder("Select library folder for downstream")
        if not folder:
            return {"error": "cancelled"}
        cfg = _read_ini()
        if "general" not in cfg:
            cfg["general"] = {}
        cfg["general"]["libfolder"] = folder
        _write_ini(cfg)
        return {"folder": folder, "folder_name": Path(folder).name}

    def run_downstream(self):
        if self._lib is None:
            return {"error": "no library loaded"}
        cfg = _read_ini()
        folder = cfg.get("general", "libfolder", fallback="")
        if not folder or not Path(folder).is_dir():
            return {"error": f"Library folder not found: {folder}"}
        try:
            from downstream import process_libraries
            process_libraries(self._lib_path, folder)
            return {"ok": True}
        except Exception as e:
            return {"error": str(e)}

    # ── Диалог выбора файла (legacy) ──────────────────────────────────────

    def open_file_dialog(self):
        return self._pick_file()


# ══════════════════════════════════════════════════════════
#  Запуск
# ══════════════════════════════════════════════════════════

api = LibraryAPI()

def start():
    global window
    window = webview.create_window(
        title="NoABS.Eagle3D glue tool",
        url=str(TOOL_DIR / "ui" / "index.html"),
        js_api=api,
        width=1400, height=900,
        min_size=(900, 600),
    )

    if len(sys.argv) > 1:
        lbr_path = sys.argv[1]
    else:
        cfg = _read_ini()
        last = cfg.get("general", "last_library", fallback="")
        lbr_path = last if last and Path(last).is_file() else ""

    if lbr_path:
        def _auto_open():
            import time; time.sleep(0.8)
            window.evaluate_js(
                f'window.autoOpenLibrary({json.dumps(lbr_path)})'
            )
        threading.Thread(target=_auto_open, daemon=True).start()

    webview.start(debug=False)


if __name__ == "__main__":
    start()