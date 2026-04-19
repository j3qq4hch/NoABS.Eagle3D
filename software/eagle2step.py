"""
eagle2step.py
-------------
Генерирует STEP-модель платы из Eagle .brd файла через DRAWEXE.
Толщина вычисляется автоматически из layerSetup/mtCopper/mtIsolate.

Использование:
    python eagle2step.py board.brd
    python eagle2step.py board.brd -o board.step --thickness 1.6
"""

import json
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import math
import re
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step_merge import add_step_color, merge_step_assembly


DEFAULT_LAYER     = "20"
DEFAULT_THICKNESS = 1.6

if getattr(sys, 'frozen', False):
    # COLLECT layout: release_artifacts/tools/eagle2step/eagle2step.exe
    # drawexe_bundle is at:  release_artifacts/drawexe_bundle/
    BUNDLE_DIR = Path(sys.executable).parent.parent.parent / "drawexe_bundle"
else:
    # source layout: software/eagle2step.py, drawexe_bundle/ at repo root
    BUNDLE_DIR = Path(__file__).resolve().parent.parent / "drawexe_bundle"
_DRAWEXE   = BUNDLE_DIR / "bin" / "DRAWEXE.exe"


# ══════════════════════════════════════════════
#  DRAWEXE runtime
# ══════════════════════════════════════════════

def _build_drawexe_env() -> dict:
    env = os.environ.copy()
    env["PATH"] = str(BUNDLE_DIR / "bin") + os.pathsep + env.get("PATH", "")

    def r(name: str) -> str:
        return str(BUNDLE_DIR / "res" / name).replace("\\", "/")

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


def _run_drawexe(tcl: str, verbose: bool = True) -> None:
    if not _DRAWEXE.exists():
        raise RuntimeError(
            f"DRAWEXE не найден: {_DRAWEXE}\n"
            "Запустите build_drawexe_bundle.py для сборки бандла."
        )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".tcl",
                                     delete=False, encoding="utf-8") as f:
        tcl_file = Path(f.name)
        f.write(tcl)
    try:
        result = subprocess.run(
            [str(_DRAWEXE), "-f", str(tcl_file)],
            env=_build_drawexe_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            print(result.stdout, end="")
            raise RuntimeError(f"DRAWEXE завершился с кодом {result.returncode}")
        if verbose and result.stdout:
            print(result.stdout, end="")
    finally:
        tcl_file.unlink(missing_ok=True)


# ══════════════════════════════════════════════
#  Вычисление толщины платы
# ══════════════════════════════════════════════

def parse_mm_array(value_str):
    values = [None]
    for token in value_str.strip().split():
        values.append(float(token.replace("mm", "").replace(",", ".")))
    return values


def parse_layer_setup(expr):
    return [int(n) for n in re.findall(r'\d+', expr)]


def parse_soldermask_color(root):
    """Parse soldermask color from BRD mfgpreviewcolors. Returns (r, g, b) in 0.0-1.0 or None."""
    for el in root.iter("mfgpreviewcolor"):
        if el.get("name") == "soldermaskcolor":
            raw = el.get("color", "")
            try:
                v = int(raw, 16)
                r = ((v >> 16) & 0xFF) / 255.0
                g = ((v >>  8) & 0xFF) / 255.0
                b = ((v      ) & 0xFF) / 255.0
                return (r, g, b)
            except (ValueError, TypeError):
                return None
    return None


def calculate_thickness(root):
    params = {}
    for p in root.iter("param"):
        name = p.get("name")
        if name in ("layerSetup", "mtCopper", "mtIsolate"):
            params[name] = p.get("value", "")

    missing = [k for k in ("layerSetup", "mtCopper", "mtIsolate") if k not in params]
    if missing:
        return None, f"Параметры не найдены: {', '.join(missing)}"

    layer_indices = parse_layer_setup(params["layerSetup"])
    if not layer_indices:
        return None, "layerSetup не содержит номеров слоёв"

    mt_copper  = parse_mm_array(params["mtCopper"])
    mt_isolate = parse_mm_array(params["mtIsolate"])

    try:
        copper_sum = sum(mt_copper[i] for i in layer_indices)
    except IndexError as e:
        return None, f"Индекс вышел за пределы mtCopper: {e}"

    try:
        isolate_sum = sum(mt_isolate[i] for i in layer_indices[:-1])
    except IndexError as e:
        return None, f"Индекс вышел за пределы mtIsolate: {e}"

    thickness = copper_sum + isolate_sum

    detail = (
        f"layerSetup: {params['layerSetup']}\n"
        f"  слои меди: {layer_indices}\n"
        f"  сумма меди:       {copper_sum:.4f} мм  "
        f"({' + '.join(f'{mt_copper[i]:.4f}' for i in layer_indices)})\n"
        f"  сумма диэлектрика:{isolate_sum:.4f} мм  "
        f"({' + '.join(f'{mt_isolate[i]:.4f}' for i in layer_indices[:-1])})\n"
        f"  итого толщина:    {thickness:.4f} мм"
    )

    return thickness, detail


# ══════════════════════════════════════════════
#  Парсинг геометрии BRD
# ══════════════════════════════════════════════

def parse_wire(el):
    return {
        "x1":    float(el.get("x1")),
        "y1":    float(el.get("y1")),
        "x2":    float(el.get("x2")),
        "y2":    float(el.get("y2")),
        "curve": float(el.get("curve", 0)),
    }

def parse_circle(el):
    return {
        "x":      float(el.get("x")),
        "y":      float(el.get("y")),
        "radius": float(el.get("radius")),
    }

def parse_hole(el):
    return {
        "x":      float(el.get("x")),
        "y":      float(el.get("y")),
        "radius": float(el.get("drill")) / 2.0,
    }

def parse_rot(rot_str):
    if not rot_str:
        return 0.0, False
    mirror  = rot_str.startswith("M")
    num_str = rot_str.lstrip("MR")
    angle   = float(num_str) if num_str else 0.0
    return angle, mirror

def transform_point(lx, ly, angle_deg, mirror, tx, ty):
    if mirror:
        lx = -lx
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    return cos_a * lx - sin_a * ly + tx, sin_a * lx + cos_a * ly + ty


def extract_board_elements(brd_path, layer=DEFAULT_LAYER):
    tree = ET.parse(brd_path)
    root = tree.getroot()
    wires, circles, holes = [], [], []

    for section in root.iter("plain"):
        for w in section.findall("wire"):
            if w.get("layer") == layer:
                wires.append(parse_wire(w))
        for c in section.findall("circle"):
            if c.get("layer") == layer:
                circles.append(parse_circle(c))
        for h in section.findall("hole"):
            holes.append(parse_hole(h))

    for section in root.iter("signal"):
        for w in section.findall("wire"):
            if w.get("layer") == layer:
                wires.append(parse_wire(w))

    return root, wires, circles, holes


def transform_wire(wire, angle_deg, mirror, tx, ty):
    def tp(lx, ly):
        return transform_point(lx, ly, angle_deg, mirror, tx, ty)

    x1, y1 = tp(wire["x1"], wire["y1"])
    x2, y2 = tp(wire["x2"], wire["y2"])
    curve = -wire["curve"] if mirror else wire["curve"]
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "curve": curve}


def extract_footprint_outline(root, layers=("20", "46")):
    layers = set(layers)
    pkg_index = {}
    for lib in root.iter("library"):
        lib_name = lib.get("name", "")
        for pkg in lib.iter("package"):
            pkg_index[(lib_name, pkg.get("name", ""))] = pkg

    wires   = []
    circles = []

    for el in root.iter("element"):
        lib_name = el.get("library", "")
        pkg_name = el.get("package", "")
        pkg = pkg_index.get((lib_name, pkg_name))
        if pkg is None:
            continue

        el_x = float(el.get("x", 0))
        el_y = float(el.get("y", 0))
        el_angle, el_mirror = parse_rot(el.get("rot", ""))

        for w in pkg.findall("wire"):
            if w.get("layer") not in layers:
                continue
            wire = parse_wire(w)
            wires.append(transform_wire(wire, el_angle, el_mirror, el_x, el_y))

        for c in pkg.findall("circle"):
            if c.get("layer") not in layers:
                continue
            raw = parse_circle(c)
            cx, cy = transform_point(raw["x"], raw["y"],
                                     el_angle, el_mirror, el_x, el_y)
            circles.append({"x": cx, "y": cy, "radius": raw["radius"]})

    return wires, circles

def extract_component_holes(root):
    pkg_index = {}
    for lib in root.iter("library"):
        lib_name = lib.get("name", "")
        for pkg in lib.iter("package"):
            pkg_name = pkg.get("name", "")
            holes = []

            for pad in pkg.findall("pad"):
                drill = pad.get("drill")
                if drill is None:
                    continue
                holes.append({
                    "x":      float(pad.get("x", 0)),
                    "y":      float(pad.get("y", 0)),
                    "radius": float(drill) / 2.0,
                })

            for hole in pkg.findall("hole"):
                drill = hole.get("drill")
                if drill is None:
                    continue
                holes.append({
                    "x":      float(hole.get("x", 0)),
                    "y":      float(hole.get("y", 0)),
                    "radius": float(drill) / 2.0,
                })

            if holes:
                pkg_index[(lib_name, pkg_name)] = holes

    comp_holes = []
    for el in root.iter("element"):
        holes = pkg_index.get((el.get("library", ""), el.get("package", "")))
        if not holes:
            continue
        el_x = float(el.get("x", 0))
        el_y = float(el.get("y", 0))
        el_angle, el_mirror = parse_rot(el.get("rot", ""))

        for hole in holes:
            wx, wy = transform_point(
                hole["x"], hole["y"], el_angle, el_mirror, el_x, el_y
            )
            comp_holes.append({"x": wx, "y": wy, "radius": hole["radius"]})

    return comp_holes


# ══════════════════════════════════════════════
#  Цепочки сегментов
# ══════════════════════════════════════════════

def _seg_start(seg, flipped):
    return (seg["x2"], seg["y2"]) if flipped else (seg["x1"], seg["y1"])

def _seg_end(seg, flipped):
    return (seg["x1"], seg["y1"]) if flipped else (seg["x2"], seg["y2"])

def _close(ax, ay, bx, by, tol):
    return abs(ax - bx) < tol and abs(ay - by) < tol


def chain_segments(wires, tol=1e-3):
    remaining = list(range(len(wires)))
    chains = []

    while remaining:
        idx = remaining.pop(0)
        chain = [(wires[idx], False)]

        while True:
            ex, ey = _seg_end(*chain[-1])
            found = False
            for i in remaining:
                s = wires[i]
                if _close(ex, ey, s["x1"], s["y1"], tol):
                    chain.append((s, False)); remaining.remove(i); found = True; break
                if _close(ex, ey, s["x2"], s["y2"], tol):
                    chain.append((s, True));  remaining.remove(i); found = True; break
            if not found:
                break

        chains.append(chain)

    return chains


def chain_is_closed(chain, tol=1e-3):
    sx, sy = _seg_start(*chain[0])
    ex, ey = _seg_end(*chain[-1])
    return _close(sx, sy, ex, ey, tol)


def chain_area(chain):
    pts = [_seg_start(*item) for item in chain]
    n = len(pts)
    a = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def classify_chains(chains):
    closed, open_chains = [], []
    for ch in chains:
        (closed if chain_is_closed(ch) else open_chains).append(ch)
    if not closed:
        return None, [], open_chains
    closed.sort(key=chain_area, reverse=True)
    return closed[0], closed[1:], open_chains


def chain_to_polyline(chain, arc_n=64):
    xs, ys = [], []
    for seg, flipped in chain:
        if flipped:
            x1, y1, x2, y2 = seg["x2"], seg["y2"], seg["x1"], seg["y1"]
            curve = -seg["curve"]
        else:
            x1, y1, x2, y2 = seg["x1"], seg["y1"], seg["x2"], seg["y2"]
            curve = seg["curve"]

        if not xs:
            xs.append(x1); ys.append(y1)

        if curve == 0:
            xs.append(x2); ys.append(y2)
        else:
            dx, dy = x2 - x1, y2 - y1
            chord = math.hypot(dx, dy)
            if chord < 1e-12:
                xs.append(x2); ys.append(y2)
                continue
            alpha = math.radians(abs(curve) / 2)
            r = chord / (2 * math.sin(alpha))
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            d = math.sqrt(max(r**2 - (chord / 2)**2, 0))
            px, py = -dy / chord, dx / chord
            sign = 1 if curve > 0 else -1
            cx = mx + sign * d * px
            cy = my + sign * d * py
            a1 = math.degrees(math.atan2(y1 - cy, x1 - cx))
            a2 = math.degrees(math.atan2(y2 - cy, x2 - cx))
            if curve > 0:
                if a2 < a1: a2 += 360
            else:
                if a2 > a1: a2 -= 360
            r1, r2 = math.radians(a1), math.radians(a2)
            angles = [r1 + (r2 - r1) * i / (arc_n - 1) for i in range(arc_n)]
            xs.extend(cx + r * math.cos(a) for a in angles)
            ys.extend(cy + r * math.sin(a) for a in angles)

    return xs, ys


# ══════════════════════════════════════════════
#  Генерация TCL для DRAWEXE
# ══════════════════════════════════════════════

def _arc_center_radius(x1, y1, x2, y2, curve_deg):
    """Центр и радиус дуги из двух точек и угла кривизны Eagle."""
    dx, dy = x2 - x1, y2 - y1
    chord  = math.hypot(dx, dy)
    alpha  = math.radians(abs(curve_deg) / 2)
    r      = chord / (2 * math.sin(alpha))
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    d      = math.sqrt(max(r**2 - (chord / 2)**2, 0))
    px, py = -dy / chord, dx / chord
    sign   = 1 if curve_deg > 0 else -1
    return mx + sign * d * px, my + sign * d * py, r


def _chain_to_tcl(chain, tag: str, lines: list) -> str:
    """
    Генерирует TCL-команды для wire из цепочки сегментов.
    Возвращает имя переменной wire.

    Вершины РАЗДЕЛЯЮТСЯ между соседними рёбрами — иначе wire в DRAW
    не сшивается (несмотря на совпадение координат).

    Для CCW-дуг (curve > 0): circle с нормалью (0,0,1).
    Для CW-дуг  (curve < 0): circle с нормалью (0,0,-1).
    """
    n = len(chain)

    # Создаём ровно N общих вершин (по одной на каждый стык сегментов)
    for i, (seg, flipped) in enumerate(chain):
        if flipped:
            x, y = seg["x2"], seg["y2"]
        else:
            x, y = seg["x1"], seg["y1"]
        lines.append(f"vertex v_{tag}_{i} {x:.6f} {y:.6f} 0")

    edge_names = []
    for i, (seg, flipped) in enumerate(chain):
        eid = f"e_{tag}_{i}"
        vi_s = f"v_{tag}_{i}"
        vi_e = f"v_{tag}_{(i + 1) % n}"

        if flipped:
            x1, y1, x2, y2 = seg["x2"], seg["y2"], seg["x1"], seg["y1"]
            curve = -seg["curve"]
        else:
            x1, y1, x2, y2 = seg["x1"], seg["y1"], seg["x2"], seg["y2"]
            curve = seg["curve"]

        if curve == 0:
            lines.append(f"edge {eid} {vi_s} {vi_e}")
        else:
            cx, cy, r = _arc_center_radius(x1, y1, x2, y2, curve)
            if curve > 0:
                # CCW дуга от p1 к p2 — прямой порядок
                u1 = math.atan2(y1 - cy, x1 - cx)
                u2 = math.atan2(y2 - cy, x2 - cx)
                if u2 <= u1:
                    u2 += 2 * math.pi
            else:
                # CW дуга: строим CCW-дугу от p2 к p1 — wire развернёт автоматически
                u1 = math.atan2(y2 - cy, x2 - cx)
                u2 = math.atan2(y1 - cy, x1 - cx)
                if u2 <= u1:
                    u2 += 2 * math.pi
            lines.append(f"circle {eid}_c {cx:.6f} {cy:.6f} 0  0 0 1  {r:.6f}")
            lines.append(f"mkedge {eid} {eid}_c {u1:.10f} {u2:.10f}")

        edge_names.append(eid)

    wire_name = f"w_{tag}"
    lines.append(f"wire {wire_name} {' '.join(edge_names)}")
    return wire_name


def _circle_wire_tcl(x, y, r, tag: str, lines: list) -> str:
    """Генерирует TCL для полного окружности-wire. Возвращает имя переменной."""
    lines.append(f"circle c_{tag} {x:.6f} {y:.6f} 0  0 0 1  {r:.6f}")
    lines.append(f"mkedge e_{tag} c_{tag}")
    wire_name = f"w_{tag}"
    lines.append(f"wire {wire_name} e_{tag}")
    return wire_name


def make_board_step(outer_chain, cutout_chains, layer_circles,
                    board_holes, comp_holes, thickness, out_path: Path,
                    color=None, debug: bool = False) -> None:
    """
    debug=False: запускает DRAWEXE напрямую, без сохранения TCL/BAT.
    debug=True:  сохраняет TCL + BAT рядом с out_path, DRAWEXE не запускает.
    """
    lines = ["pload OCAFKERNEL XDE MODELING"]
    t = thickness

    # Основное тело платы
    outer_wire = _chain_to_tcl(outer_chain, "outer", lines)
    lines.append(f"plane board_pln 0 0 0  0 0 1")
    lines.append(f"mkface board_face board_pln {outer_wire}")
    lines.append(f"prism board board_face 0 0 {t:.6f}")

    # Вырезы из chain-контуров (layer 20/46) — bcut
    for i, ch in enumerate(cutout_chains):
        w = _chain_to_tcl(ch, f"cut{i}", lines)
        lines.append(f"mkface cut{i}_f board_pln {w}")
        lines.append(f"prism cut{i}_s cut{i}_f 0 0 {t:.6f}")
        lines.append(f"bcut board board cut{i}_s")

    # Вырезы из окружностей layer 20 — pcylinder + bcut
    for i, c in enumerate(layer_circles):
        lines.append(f"pcylinder lc{i}_s {c['radius']:.6f} {t + 2:.6f}")
        lines.append(f"ttranslate lc{i}_s {c['x']:.6f} {c['y']:.6f} -1")
        lines.append(f"bcut board board lc{i}_s")

    # Отверстия — pcylinder + bcut
    all_holes = board_holes + comp_holes
    if all_holes:
        print(f"  Отверстий платы: {len(board_holes)}, компонентов: {len(comp_holes)}")
    for i, h in enumerate(all_holes):
        lines.append(f"pcylinder hole{i}_s {h['radius']:.6f} {t + 2:.6f}")
        lines.append(f"ttranslate hole{i}_s {h['x']:.6f} {h['y']:.6f} -1")
        lines.append(f"bcut board board hole{i}_s")

    out_step = str(out_path.resolve()).replace("\\", "/")
    lines.append(f'NewDocument D XmlXCAF')
    lines.append(f'XAddShape D board')
    lines.append(f'WriteStep D "{out_step}"')
    lines.append("exit")

    tcl = "\n".join(lines) + "\n"

    if debug:
        tcl_path = out_path.with_suffix(".tcl")
        bat_path = out_path.with_suffix(".bat")
        tcl_path.write_text(tcl, encoding="utf-8")

        env = _build_drawexe_env()
        bat_lines = ["@echo off"]
        csf_keys = [k for k in env if k.startswith("CSF_") or k in
                    ("DRAWHOME", "DRAWDEFAULT", "MMGT_CLEAR", "CSF_LANGUAGE")]
        for k in sorted(csf_keys):
            bat_lines.append(f'set "{k}={env[k]}"')
        bat_lines.append(f'set "PATH={env["PATH"]}"')
        bat_lines.append(f'"{_DRAWEXE}" -f "{tcl_path}"')
        bat_lines.append("pause")
        bat_path.write_text("\r\n".join(bat_lines), encoding="utf-8")

        print(f"  TCL сохранён: {tcl_path}")
        print(f"  Запустите:    {bat_path}")
    else:
        _run_drawexe(tcl, verbose=False)
        if not out_path.exists():
            raise RuntimeError(f"DRAWEXE не создал файл: {out_path}")
        if color is not None:
            add_step_color(out_path, *color)
        print(f"OK  STEP: {out_path}")


# ══════════════════════════════════════════════
# ══════════════════════════════════════════════
#  Компоненты: плейсмент STEP
# ══════════════════════════════════════════════

_DEFAULT_ORIENT = {"tx": 0.0, "ty": 0.0, "tz": 0.0, "rx": 0.0, "ry": 0.0, "rz": 0.0}


def _mat4_mul(A, B):
    return [
        [sum(A[i][k] * B[k][j] for k in range(4)) for j in range(4)]
        for i in range(4)
    ]


def _mat4_eye():
    return [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]


_RX_NEG90 = [
    [1,  0,  0, 0],
    [0,  0,  1, 0],
    [0, -1,  0, 0],
    [0,  0,  0, 1],
]


def _trans(tx, ty, tz):
    m = _mat4_eye()
    m[0][3] = tx; m[1][3] = ty; m[2][3] = tz
    return m


def _rot_z(deg):
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    m = _mat4_eye()
    m[0][0] = c;  m[0][1] = -s
    m[1][0] = s;  m[1][1] =  c
    return m


def _rot_xyz(rx_deg, ry_deg, rz_deg):
    rx, ry, rz = map(math.radians, [rx_deg, ry_deg, rz_deg])
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    return [
        [ cy*cz,             -cy*sz,            sy,    0],
        [ sx*sy*cz + cx*sz,  -sx*sy*sz + cx*cz, -sx*cy, 0],
        [-cx*sy*cz + sx*sz,   cx*sy*sz + sx*cz,  cx*cy, 0],
        [ 0,                   0,                 0,     1],
    ]


def _mirror_xz():
    m = _mat4_eye()
    m[0][0] = -1.0
    m[2][2] = -1.0
    return m


def read_brd_placements(brd_path):
    tree = ET.parse(brd_path)
    root = tree.getroot()

    orientations = {}
    for lib in root.iter("library"):
        lib_name = lib.get("name", "")
        for pkg in lib.iter("package"):
            pkg_name = pkg.get("name", "")
            desc = pkg.findtext("description") or ""
            m = re.search(r'<!--3d:(\{[^>]+\})-->', desc)
            if m:
                try:
                    data = json.loads(m.group(1))
                    orientations[(lib_name, pkg_name)] = data
                except json.JSONDecodeError:
                    pass

    placements = []
    for el in root.iter("element"):
        lib_name = el.get("library", "")
        pkg_name = el.get("package", "")
        angle, mirror = parse_rot(el.get("rot", ""))
        placements.append({
            "name":    el.get("name", ""),
            "library": lib_name,
            "package": pkg_name,
            "x":       float(el.get("x", 0)),
            "y":       float(el.get("y", 0)),
            "angle":   angle,
            "mirror":  mirror,
        })

    return placements, orientations


def build_step_index(step_dir: Path) -> dict:
    index = {}
    for ext in ("*.step", "*.stp", "*.STEP", "*.STP"):
        for f in step_dir.glob(ext):
            index[f.stem.lower()] = f
    return index


def compute_step_placement(p: dict, o: dict, thickness: float):
    M_orient = _mat4_mul(_trans(o["tx"], o["ty"], o["tz"]),
               _mat4_mul(_rot_xyz(o["rx"], o["ry"], o["rz"]), _RX_NEG90))

    if p["mirror"]:
        M = _mat4_mul(_trans(p["x"], p["y"], 0.0),
            _mat4_mul(_rot_z(p["angle"]),
            _mat4_mul(_mirror_xz(), M_orient)))
    else:
        M = _mat4_mul(_trans(p["x"], p["y"], thickness),
            _mat4_mul(_rot_z(p["angle"]), M_orient))

    return M


def make_assembly_step(
    brd_path: Path,
    board_step: Path,
    step_dir: Path,
    thickness: float,
    out_path: Path,
    debug: bool = False,
) -> None:
    placements, orientations = read_brd_placements(brd_path)
    step_index = build_step_index(step_dir)

    instances = []
    placed = 0
    skipped = 0

    missing_packages = set()
    for p in placements:
        key = (p["library"], p["package"])
        step_file = step_index.get(p["package"].lower())
        if step_file is None:
            skipped += 1
            missing_packages.add(p["package"])
            continue

        o = orientations.get(key, _DEFAULT_ORIENT)
        M = compute_step_placement(p, o, thickness)

        if debug:
            t = [M[i][3] for i in range(3)]
            print(
                f"  {p['name']:12s} {p['package']:20s}"
                f"  pos=({t[0]:.2f},{t[1]:.2f},{t[2]:.2f})"
                f"  rot={p['angle']}°  mirror={p['mirror']}"
                f"  -> {step_file.name}"
            )

        instances.append((step_file, M))
        placed += 1

    merge_step_assembly(board_step, instances, out_path)
    print(f"  Размещено: {placed}, пропущено (нет STEP): {skipped}")
    if missing_packages:
        print(f"  Нет STEP для пакетов: {', '.join(sorted(missing_packages))}")


# ══════════════════════════════════════════════
#  Основной поток
# ══════════════════════════════════════════════

def process(brd_path, step_path, thickness_override, layer,
            step_dir=None, assembly_path=None, debug=False):
    print(f"Читаем: {brd_path}")
    root, wires, circles, holes = extract_board_elements(brd_path, layer)
    print(f"  wire: {len(wires)}, circle: {len(circles)}, hole: {len(holes)}")

    comp_holes = extract_component_holes(root)
    print(f"  отверстий компонентов: {len(comp_holes)}")

    fp_wires, fp_circles = extract_footprint_outline(root, layers=("20", "46"))
    if fp_wires or fp_circles:
        print(f"  из футпринтов: wire: {len(fp_wires)}, circle: {len(fp_circles)}")
        wires   += fp_wires
        circles += fp_circles
    else:
        print(f"  из футпринтов: вырезов не найдено")

    if thickness_override is not None:
        thickness = thickness_override
        print(f"  толщина: {thickness} мм (задана вручную)")
    else:
        thickness, detail = calculate_thickness(root)
        if thickness is None:
            print(f"  !  Не удалось вычислить толщину: {detail}")
            print(f"  !  Используется значение по умолчанию: {DEFAULT_THICKNESS} мм")
            thickness = DEFAULT_THICKNESS
        else:
            print(f"  толщина вычислена из стека слоёв:")
            for line in detail.splitlines():
                print(f"    {line}")

    chains = chain_segments(wires)
    outer_chain, cutout_chains, open_chains = classify_chains(chains)

    if outer_chain is None:
        print("!  Внешний контур не найден.")
        sys.exit(1)

    print(f"  вырезов в контуре: {len(cutout_chains)}")
    if open_chains:
        total = sum(len(ch) for ch in open_chains)
        print(f"  !  {total} сегментов незамкнуты — пропускаются")

    soldermask_color = parse_soldermask_color(root)
    if soldermask_color:
        r, g, b = soldermask_color
        print(f"  цвет маски: RGB({r:.3f}, {g:.3f}, {b:.3f})")
    else:
        print(f"  цвет маски: не найден, плата будет серой")

    print(f"Строим 3D модель платы через DRAWEXE...")
    make_board_step(
        outer_chain, cutout_chains, circles,
        holes, comp_holes, thickness, step_path, color=soldermask_color, debug=debug
    )

    if step_dir is not None:
        if assembly_path is None:
            assembly_path = brd_path.with_name(brd_path.stem + "_assembly.step")
        print(f"Добавляем компоненты из {step_dir}...")
        make_assembly_step(brd_path, step_path, step_dir, thickness, assembly_path, debug=debug)


# ══════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Eagle .brd -> STEP + PNG превью платы (через DRAWEXE)"
    )
    parser.add_argument("brd", help="Путь к .brd файлу")
    parser.add_argument("--output", "-o", default=None,
                        help="Выходной .step файл (default: <имя>.step)")
    parser.add_argument("--thickness", "-t", type=float, default=None,
                        help="Толщина платы в мм (default: вычисляется из BRD)")
    parser.add_argument("--layer", default=DEFAULT_LAYER,
                        help=f"Слой контура (default: {DEFAULT_LAYER})")
    parser.add_argument("--step-dir", "-c", default=None,
                        help="Папка со STEP-файлами компонентов -> строит сборку")
    parser.add_argument("--assembly-output", "-a", default=None,
                        help="Выходной файл сборки (default: <имя>_assembly.step)")
    parser.add_argument("--debug", action="store_true",
                        help="Сохранить TCL/BAT вместо прямого запуска DRAWEXE")
    args = parser.parse_args()

    brd_path = Path(args.brd)
    if not brd_path.exists():
        print(f"Файл не найден: {brd_path}")
        sys.exit(1)

    step_path = Path(args.output) if args.output else brd_path.with_suffix(".step")
    step_dir  = Path(args.step_dir).resolve() if args.step_dir else None
    assembly_path = Path(args.assembly_output) if args.assembly_output else None

    if step_dir is not None and not step_dir.is_dir():
        print(f"Папка компонентов не найдена: {step_dir}")
        sys.exit(1)

    process(brd_path, step_path, args.thickness, args.layer,
            step_dir, assembly_path, debug=args.debug)


if __name__ == "__main__":
    main()
