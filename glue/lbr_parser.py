"""
lbr_parser.py
-------------
Читает и пишет Eagle .lbr файлы.
Данные ориентации 3D-модели хранятся в <description> футпринта в конце:

  <существующий текст описания>
  <a href="drawing_url">Drawing</a> <a href="model_url">3D model</a>
  <!--3d:{"tx":0,"ty":0,"tz":0,"rx":0,"ry":0,"rz":0}-->

Пользовательское содержимое description сохраняется, метаданные добавляются в конец.
"""

import copy
import xml.etree.ElementTree as ET
import re
import json
from pathlib import Path

_RE_3D   = re.compile(r'<!--3d:(\{.*?\})-->', re.DOTALL)
_RE_DRAW = re.compile(r'<a\s+href="([^"]*)"[^>]*>Drawing</a>', re.IGNORECASE)
_RE_MDL  = re.compile(r'<a\s+href="([^"]*)"[^>]*>3D model</a>', re.IGNORECASE)


def _parse_description(desc_text):
    result = {
        "tx": 0.0, "ty": 0.0, "tz": 0.0,
        "rx": 0.0, "ry": 0.0, "rz": 0.0,
        "drawing_url": "",
        "model_url": "",
        "has_orientation": False,
    }
    if not desc_text:
        return result
    m3d = _RE_3D.search(desc_text)
    if m3d:
        try:
            data = json.loads(m3d.group(1))
            for k in ("tx", "ty", "tz", "rx", "ry", "rz"):
                result[k] = float(data.get(k, 0.0))
            result["has_orientation"] = True
        except (json.JSONDecodeError, ValueError):
            pass
    m_draw = _RE_DRAW.search(desc_text)
    if m_draw:
        result["drawing_url"] = m_draw.group(1)
    m_mdl = _RE_MDL.search(desc_text)
    if m_mdl:
        result["model_url"] = m_mdl.group(1)
    return result


def _strip_metadata(desc_text: str) -> str:
    """Удаляет блок 3D-метаданных из description, оставляя пользовательский текст."""
    if not desc_text:
        return ""
    text = desc_text
    text = _RE_3D.sub('', text)
    text = _RE_DRAW.sub('', text)
    text = _RE_MDL.sub('', text)
    return text.strip()


def _build_metadata(tx, ty, tz, rx, ry, rz, drawing_url, model_url) -> str:
    """Строит только блок метаданных (без пользовательского контента)."""
    parts = []
    if drawing_url:
        parts.append(f'<a href="{drawing_url}">Drawing</a>')
    if model_url:
        parts.append(f'<a href="{model_url}">3D model</a>')
    orient = {"tx": round(float(tx), 4), "ty": round(float(ty), 4),
              "tz": round(float(tz), 4), "rx": round(float(rx), 4),
              "ry": round(float(ry), 4), "rz": round(float(rz), 4)}
    parts.append(f'<!--3d:{json.dumps(orient, separators=(",", ":"))}-->')
    return " ".join(parts)


def _float(el, attr, default=0.0):
    v = el.get(attr)
    return float(v) if v is not None else default


def _parse_rot(rot_str):
    if not rot_str:
        return 0.0, False
    mirror = rot_str.startswith("M")
    num = rot_str.lstrip("MR")
    return (float(num) if num else 0.0), mirror


def extract_package_geometry(pkg_el):
    """Извлекает геометрию пакета для 3D-вьюера."""
    COPPER_LAYERS = {"1", "16", "18"}
    SILK_LAYERS   = {"21", "22"}
    ALL_LAYERS    = COPPER_LAYERS | SILK_LAYERS

    wires, smds, pads, circles, holes = [], [], [], [], []

    for w in pkg_el.findall("wire"):
        layer = w.get("layer", "")
        if layer not in ALL_LAYERS:
            continue
        wires.append({
            "layer": layer,
            "x1": _float(w,"x1"), "y1": _float(w,"y1"),
            "x2": _float(w,"x2"), "y2": _float(w,"y2"),
            "curve": _float(w,"curve",0.0),
            "width": _float(w,"width",0.0),
        })

    for s in pkg_el.findall("smd"):
        angle, _ = _parse_rot(s.get("rot",""))
        smds.append({
            "x": _float(s,"x"), "y": _float(s,"y"),
            "dx": _float(s,"dx"), "dy": _float(s,"dy"),
            "rot": angle,
            "layer": s.get("layer","1"),
        })

    for p in pkg_el.findall("pad"):
        drill = p.get("drill")
        if drill is None:
            continue
        angle, _ = _parse_rot(p.get("rot",""))
        pads.append({
            "x": _float(p,"x"), "y": _float(p,"y"),
            "drill": float(drill), "rot": angle,
        })

    for c in pkg_el.findall("circle"):
        layer = c.get("layer","")
        if layer not in ALL_LAYERS:
            continue
        circles.append({
            "layer": layer,
            "x": _float(c,"x"), "y": _float(c,"y"),
            "radius": _float(c,"radius"),
            "width": _float(c,"width",0.0),
        })

    for h in pkg_el.findall("hole"):
        drill = h.get("drill")
        if drill:
            holes.append({
                "x": _float(h,"x"), "y": _float(h,"y"),
                "drill": float(drill),
            })

    return {"wires": wires, "smds": smds, "pads": pads,
            "circles": circles, "holes": holes}


class LbrLibrary:
    def __init__(self, path):
        self.path = Path(path)
        self._tree = ET.parse(self.path)
        self._root = self._tree.getroot()
        self._packages = {}
        self._load()

    def _load(self):
        self._packages = {}
        for pkg in self._root.iter("package"):
            name = pkg.get("name", "")
            desc_el = pkg.find("description")
            self._packages[name] = (pkg, desc_el)

    def _save(self):
        ET.indent(self._tree, space="  ")
        self._tree.write(str(self.path), encoding="utf-8", xml_declaration=True)

    def package_names(self):
        return sorted(self._packages.keys())

    def get_orientation(self, pkg_name):
        _, desc_el = self._packages.get(pkg_name, (None, None))
        desc_text = (desc_el.text or "") if desc_el is not None else ""
        return _parse_description(desc_text)

    def get_geometry(self, pkg_name):
        pkg_el, _ = self._packages.get(pkg_name, (None, None))
        if pkg_el is None:
            return {}
        return extract_package_geometry(pkg_el)

    def save_orientation(self, pkg_name, tx, ty, tz, rx, ry, rz,
                         drawing_url, model_url):
        """Сохраняет ориентацию, добавляя метаданные в конец description."""
        pkg_el, desc_el = self._packages.get(pkg_name, (None, None))
        if pkg_el is None:
            return

        existing = (desc_el.text or "") if desc_el is not None else ""
        user_content = _strip_metadata(existing)
        new_meta = _build_metadata(tx, ty, tz, rx, ry, rz, drawing_url, model_url)

        if user_content:
            new_text = user_content + "\n" + new_meta
        else:
            new_text = new_meta

        if desc_el is None:
            desc_el = ET.Element("description")
            pkg_el.insert(0, desc_el)
            self._packages[pkg_name] = (pkg_el, desc_el)

        desc_el.text = new_text
        self._save()

    def remove_orientation(self, pkg_name):
        """Удаляет 3D-метаданные из description, сохраняя пользовательский текст."""
        pkg_el, desc_el = self._packages.get(pkg_name, (None, None))
        if pkg_el is None:
            return
        if desc_el is not None:
            existing = desc_el.text or ""
            desc_el.text = _strip_metadata(existing)
        self._save()

    def rename_package(self, old_name: str, new_name: str):
        """Переименовывает футпринт. Поднимает ValueError если новое имя занято."""
        if new_name in self._packages:
            raise ValueError(f"Package '{new_name}' already exists")
        if old_name not in self._packages:
            raise KeyError(f"Package '{old_name}' not found")
        pkg_el, desc_el = self._packages.pop(old_name)
        pkg_el.set("name", new_name)
        self._packages[new_name] = (pkg_el, desc_el)
        self._save()

    def delete_package(self, pkg_name: str):
        """Удаляет футпринт из библиотеки."""
        pkg_el, _ = self._packages.pop(pkg_name, (None, None))
        if pkg_el is None:
            return
        parent_map = {child: parent
                      for parent in self._root.iter()
                      for child in parent}
        parent = parent_map.get(pkg_el)
        if parent is not None:
            parent.remove(pkg_el)
        self._save()

    def duplicate_package(self, src_name: str, new_name: str):
        """Дублирует футпринт под новым именем."""
        if new_name in self._packages:
            raise ValueError(f"Package '{new_name}' already exists")
        if src_name not in self._packages:
            raise KeyError(f"Package '{src_name}' not found")

        src_el, _ = self._packages[src_name]
        new_el = copy.deepcopy(src_el)
        new_el.set("name", new_name)

        # Вставляем сразу после исходного элемента
        parent_map = {child: parent
                      for parent in self._root.iter()
                      for child in parent}
        parent = parent_map.get(src_el)
        if parent is not None:
            idx = list(parent).index(src_el)
            parent.insert(idx + 1, new_el)

        desc_el = new_el.find("description")
        self._packages[new_name] = (new_el, desc_el)
        self._save()
