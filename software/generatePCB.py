"""
generatePCB.py
--------------
Генерирует GLB модель платы (без компонентов) из Eagle .brd файла с текстурами.

Текстуры ищутся в <brd_dir>/<brd_stem>_textures/ (результат экспорта Eagle ULP).
Выходной файл сохраняется в <brd_dir>/NoABS_tmp/<brd_stem>.glb

Все размеры — в миллиметрах. Плоскость платы XY, Z направлена вверх (top).

Зависимости:
    pip install pillow earcut
    pip install mapbox-earcut numpy   # рекомендуется: C++ бэкенд, в 50-100x быстрее
"""

import io
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as ET
import math
import re
import sys
import struct
import json
import argparse
import array as _arr
import logging
import shutil
import time as _time
from collections import defaultdict
from pathlib import Path

from PIL import Image
from _earcut import earcut as _earcut


# ══════════════════════════════════════════════
#  Логирование
# ══════════════════════════════════════════════

log = logging.getLogger("generatePCB")


def setup_logging():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


# ══════════════════════════════════════════════
#  Константы
# ══════════════════════════════════════════════

DEFAULT_LAYER     = "20"
DEFAULT_THICKNESS = 1.6
ARC_SEGMENTS        = 64  # arc resolution for board outer outline
CUTOUT_ARC_SEGMENTS = 16  # arc resolution for arcs within milling chains
CIRCLE_CUTOUT_SEGMENTS = 32  # arc resolution for full-circle cutouts on layer 20
HOLE_SEGMENTS       = 24  # arc resolution for circular drill holes


# ══════════════════════════════════════════════
#  Парсинг BRD
# ══════════════════════════════════════════════

def parse_wire(el):
    return {
        "x1": float(el.get("x1")), "y1": float(el.get("y1")),
        "x2": float(el.get("x2")), "y2": float(el.get("y2")),
        "curve": float(el.get("curve", 0)),
    }


def parse_rot(s):
    if not s:
        return 0.0, False
    mirror = s.startswith("M")
    num = s.lstrip("MR")
    return (float(num) if num else 0.0), mirror


def transform_point(lx, ly, angle, mirror, tx, ty):
    if mirror:
        lx = -lx
        angle = -angle
    rad = math.radians(angle)
    return (math.cos(rad) * lx - math.sin(rad) * ly + tx,
            math.sin(rad) * lx + math.cos(rad) * ly + ty)


def parse_hole(el):
    return {
        "x": float(el.get("x")), "y": float(el.get("y")),
        "radius": float(el.get("drill")) / 2.0,
    }


def parse_circle_el(el):
    return {
        "x": float(el.get("x")), "y": float(el.get("y")),
        "radius": float(el.get("radius")),
    }


def transform_wire(wire, angle_deg, mirror, tx, ty):
    x1, y1 = transform_point(wire["x1"], wire["y1"], angle_deg, mirror, tx, ty)
    x2, y2 = transform_point(wire["x2"], wire["y2"], angle_deg, mirror, tx, ty)
    curve = -wire["curve"] if mirror else wire["curve"]
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "curve": curve}


def extract_footprint_outline(root, layers=("20", "46")):
    """
    Извлекает wire и circle на слоях 20/46 из размещённых футпринтов.
    Трансформирует координаты в мировую систему.
    """
    layers = set(layers)
    pkg_index = {}
    for lib in root.iter("library"):
        lib_name = lib.get("name", "")
        for pkg in lib.iter("package"):
            pkg_index[(lib_name, pkg.get("name", ""))] = pkg

    wires, circles = [], []
    for el in root.iter("element"):
        pkg = pkg_index.get((el.get("library", ""), el.get("package", "")))
        if pkg is None:
            continue
        el_x = float(el.get("x", 0))
        el_y = float(el.get("y", 0))
        el_angle, el_mirror = parse_rot(el.get("rot", ""))

        for w in pkg.findall("wire"):
            if w.get("layer") not in layers:
                continue
            wires.append(transform_wire(parse_wire(w), el_angle, el_mirror, el_x, el_y))

        for c in pkg.findall("circle"):
            if c.get("layer") not in layers:
                continue
            raw = parse_circle_el(c)
            cx, cy = transform_point(raw["x"], raw["y"], el_angle, el_mirror, el_x, el_y)
            circles.append({"x": cx, "y": cy, "radius": raw["radius"]})

    return wires, circles


def parse_mm_array(s):
    vals = [None]
    for t in s.strip().split():
        vals.append(float(t.replace("mm", "").replace(",", ".")))
    return vals


def parse_layer_setup(expr):
    return [int(n) for n in re.findall(r'\d+', expr)]


def parse_color(hex_str):
    val = int(hex_str.strip().lstrip("0x").lstrip("0X"), 16)
    return ((val >> 16) & 0xFF, (val >> 8) & 0xFF, val & 0xFF, (val >> 24) & 0xFF)


_RE_DESCRIPTION_COLORS = re.compile(
    r'\b(substratecolor|coppercolor|silkscreencolor|soldermaskcolor)\s*=\s*(0x[0-9A-Fa-f]+)',
    re.IGNORECASE,
)


def _get_description_colors(root) -> dict:
    """Читает цвета из текста <description> в формате 'name = 0xAARRGGBB, ...'."""
    for desc in root.iter("description"):
        text = desc.text or ""
        colors = {}
        for m in _RE_DESCRIPTION_COLORS.finditer(text):
            try:
                colors[m.group(1).lower()] = parse_color(m.group(2))
            except ValueError:
                pass
        if colors:
            return colors
    return {}


def load_brd(brd_path, layer=DEFAULT_LAYER):
    log.debug("Парсим BRD: %s", brd_path)
    tree = ET.parse(brd_path)
    root = tree.getroot()
    wires, circles, holes = [], [], []

    for section in root.iter("plain"):
        for w in section.findall("wire"):
            if w.get("layer") == layer:
                wires.append(parse_wire(w))
        for c in section.findall("circle"):
            if c.get("layer") == layer:
                circles.append(parse_circle_el(c))
        for h in section.findall("hole"):
            holes.append(parse_hole(h))
    for section in root.iter("signal"):
        for w in section.findall("wire"):
            if w.get("layer") == layer:
                wires.append(parse_wire(w))

    log.debug("BRD: wire=%d, circle=%d, hole=%d (слой %s)", len(wires), len(circles), len(holes), layer)
    return root, wires, circles, holes


def get_component_holes(root):
    """Извлекает отверстия (pad drill + hole) из размещённых футпринтов."""
    pkg_index = {}
    for lib in root.iter("library"):
        lib_name = lib.get("name", "")
        for pkg in lib.iter("package"):
            pkg_name = pkg.get("name", "")
            holes = []
            for pad in pkg.findall("pad"):
                drill = pad.get("drill")
                if drill:
                    holes.append({"x": float(pad.get("x", 0)),
                                  "y": float(pad.get("y", 0)),
                                  "radius": float(drill) / 2.0})
            for hole in pkg.findall("hole"):
                drill = hole.get("drill")
                if drill:
                    holes.append({"x": float(hole.get("x", 0)),
                                  "y": float(hole.get("y", 0)),
                                  "radius": float(drill) / 2.0})
            if holes:
                pkg_index[(lib_name, pkg_name)] = holes

    comp_holes = []
    for el in root.iter("element"):
        holes = pkg_index.get((el.get("library", ""), el.get("package", "")))
        if not holes:
            continue
        el_x, el_y = float(el.get("x", 0)), float(el.get("y", 0))
        el_angle, el_mirror = parse_rot(el.get("rot", ""))
        for hole in holes:
            wx, wy = transform_point(hole["x"], hole["y"], el_angle, el_mirror, el_x, el_y)
            comp_holes.append({"x": wx, "y": wy, "radius": hole["radius"]})

    log.debug("Отверстия компонентов: %d", len(comp_holes))
    return comp_holes


def get_thickness(root):
    params = {}
    for p in root.iter("param"):
        n = p.get("name")
        if n in ("layerSetup", "mtCopper", "mtIsolate"):
            params[n] = p.get("value", "")
    if len(params) < 3:
        return None
    idx = parse_layer_setup(params["layerSetup"])
    if not idx:
        return None
    mc = parse_mm_array(params["mtCopper"])
    mi = parse_mm_array(params["mtIsolate"])
    try:
        return sum(mc[i] for i in idx) + sum(mi[i] for i in idx[:-1])
    except IndexError:
        return None


def get_colors(root) -> dict:
    """
    Читает цвета из BRD. Приоритет: description > mfgpreviewcolor.
    Возвращает dict {name: (R,G,B,A)}.
    """
    mfg_colors = {}
    for el in root.iter("mfgpreviewcolor"):
        name = el.get("name")
        color = el.get("color")
        if name and color:
            try:
                mfg_colors[name] = parse_color(color)
            except ValueError:
                pass
    return {**mfg_colors, **_get_description_colors(root)}


def read_png_dpi(path: Path):
    """Читает DPI из метаданных PNG файла. Возвращает (dpi_x, dpi_y) или None."""
    try:
        info = Image.open(path).info
        dpi = info.get("dpi")
        if dpi:
            return dpi
    except Exception as e:
        log.warning("Не удалось прочитать DPI из %s: %s", path.name, e)
    return None



# ══════════════════════════════════════════════
#  Цепочки сегментов и полигоны
# ══════════════════════════════════════════════

def _seg_start(seg, flipped):
    return (seg["x2"], seg["y2"]) if flipped else (seg["x1"], seg["y1"])


def _seg_end(seg, flipped):
    return (seg["x1"], seg["y1"]) if flipped else (seg["x2"], seg["y2"])


def _matches(ax, ay, bx, by, tol=1e-3):
    return abs(ax - bx) < tol and abs(ay - by) < tol


def _dedup_wires(wires, tol=1e-3, curve_tol=0.5):
    """Remove duplicate wires — same arc encoded twice (same or opposite direction). O(n)."""
    inv_t  = 1.0 / tol
    inv_ct = 1.0 / curve_tol
    seen   = set()
    kept   = []
    for w in wires:
        x1 = round(w["x1"] * inv_t);  y1 = round(w["y1"] * inv_t)
        x2 = round(w["x2"] * inv_t);  y2 = round(w["y2"] * inv_t)
        cv = round(w["curve"] * inv_ct)
        k_fwd = (x1, y1, x2, y2,  cv)
        k_rev = (x2, y2, x1, y1, -cv)
        if k_fwd in seen or k_rev in seen:
            continue
        seen.add(k_fwd)
        kept.append(w)
    return kept


def chain_segments(wires, tol=1e-3):
    """Assemble wire segments into chains. O(n) via endpoint hash index."""
    wires = _dedup_wires(wires, tol)
    if not wires:
        return []

    inv_t = 1.0 / tol

    def _key(x, y):
        return (round(x * inv_t), round(y * inv_t))

    # Map each endpoint key to list of (wire_idx, flipped):
    #   flipped=False → segment used as-is (start=x1,y1)
    #   flipped=True  → segment reversed   (start=x2,y2)
    ep_idx = defaultdict(list)
    for i, w in enumerate(wires):
        ep_idx[_key(w["x1"], w["y1"])].append((i, False))
        ep_idx[_key(w["x2"], w["y2"])].append((i, True))

    used   = [False] * len(wires)
    chains = []

    for start_i in range(len(wires)):
        if used[start_i]:
            continue
        used[start_i] = True
        chain = [(wires[start_i], False)]

        while True:
            ex, ey = _seg_end(*chain[-1])
            found  = False
            for j, flipped in ep_idx.get(_key(ex, ey), []):
                if used[j]:
                    continue
                sx = wires[j]["x2"] if flipped else wires[j]["x1"]
                sy = wires[j]["y2"] if flipped else wires[j]["y1"]
                if _matches(ex, ey, sx, sy, tol):
                    chain.append((wires[j], flipped))
                    used[j] = True
                    found = True
                    break
            if not found:
                break

        chains.append(chain)

    return chains


def chain_is_closed(chain, tol=1e-3):
    sx, sy = _seg_start(*chain[0])
    ex, ey = _seg_end(*chain[-1])
    return _matches(sx, sy, ex, ey, tol)


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
    closed, open_ = [], []
    for ch in chains:
        (closed if chain_is_closed(ch) else open_).append(ch)
    if not closed:
        return None, [], open_
    closed.sort(key=chain_area, reverse=True)
    return closed[0], closed[1:], open_


def chain_to_polygon(chain, n=ARC_SEGMENTS):
    pts = []
    for seg, flipped in chain:
        if flipped:
            x1, y1, x2, y2 = seg["x2"], seg["y2"], seg["x1"], seg["y1"]
            curve = -seg["curve"]
        else:
            x1, y1, x2, y2 = seg["x1"], seg["y1"], seg["x2"], seg["y2"]
            curve = seg["curve"]

        if not pts:
            pts.append((x1, y1))

        if curve == 0:
            pts.append((x2, y2))
        else:
            dx, dy = x2 - x1, y2 - y1
            chord = math.hypot(dx, dy)
            if chord < 1e-12:
                pts.append((x2, y2))
                continue
            alpha = math.radians(abs(curve) / 2)
            r = chord / (2 * math.sin(alpha))
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            d = math.sqrt(max(r ** 2 - (chord / 2) ** 2, 0))
            px, py = -dy / chord, dx / chord
            sign = 1 if curve > 0 else -1
            if abs(curve) > 180:
                sign = -sign
            cx = mx + sign * d * px
            cy = my + sign * d * py
            a1 = math.degrees(math.atan2(y1 - cy, x1 - cx))
            a2 = math.degrees(math.atan2(y2 - cy, x2 - cx))
            if curve > 0:
                if a2 < a1: a2 += 360
            else:
                if a2 > a1: a2 -= 360
            r1_rad = math.radians(a1)
            r2_rad = math.radians(a2)
            angles = [r1_rad + (r2_rad - r1_rad) * i / (n - 1) for i in range(1, n)]
            for ang in angles:
                pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))

    if len(pts) > 1 and _matches(pts[0][0], pts[0][1], pts[-1][0], pts[-1][1]):
        pts.pop()
    return pts


def circle_to_polygon(cx, cy, r, n=ARC_SEGMENTS):
    step = 2 * math.pi / n
    return [(cx + r * math.cos(step * i), cy + r * math.sin(step * i)) for i in range(n)]


# ══════════════════════════════════════════════
#  Триангуляция
# ══════════════════════════════════════════════

def triangulate_face(outer_pts, holes_pts):
    """
    Триангулирует грань с вырезами через earcut.
    Возвращает (all_pts, indices).
    """
    all_pts   = list(outer_pts)
    ring_ends = [len(outer_pts)]
    for h in holes_pts:
        all_pts.extend(h)
        ring_ends.append(len(all_pts))

    flat        = [coord for pt in all_pts for coord in pt]
    hole_starts = ring_ends[:-1] if len(ring_ends) > 1 else None
    indices     = _earcut(flat, hole_starts, 2)
    return all_pts, list(indices)


# ══════════════════════════════════════════════
#  Построение 3D меша (array.array, без numpy)
# ══════════════════════════════════════════════

def ensure_ccw(pts):
    n = len(pts)
    a = sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1]
            for i in range(n))
    if a < 0:
        pts.reverse()
    return pts


def ensure_cw(pts):
    n = len(pts)
    a = sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1]
            for i in range(n))
    if a > 0:
        pts.reverse()
    return pts


def build_board_mesh(outer_chain, cutout_chains, layer_circles,
                     board_holes, comp_holes, thickness):
    """
    Строит вершины/нормали/UV/индексы для трёх примитивов:
      0 - верхняя грань (Z = thickness, нормаль +Z) - material_top
      1 - нижняя грань (Z = 0, нормаль -Z)          - material_bottom
      2 - боковые грани                              - material_side

    Каждый примитив: (verts_f32, norms_f32, uvs_f32, idx_u32)
    verts_f32 — array.array('f') плоский: [x0,y0,z0, x1,y1,z1, ...]
    uvs_f32   — array.array('f') плоский: [u0,v0, u1,v1, ...]
    idx_u32   — array.array('I') плоский: [i0,i1,i2, ...]
    """
    outer_pts    = chain_to_polygon(outer_chain)
    # Milling cutouts from chains — winding unknown, need ensure_cw
    cutout_polys  = [chain_to_polygon(ch, n=CUTOUT_ARC_SEGMENTS) for ch in cutout_chains]
    # Full circles on board outline are cutouts — use CIRCLE_CUTOUT_SEGMENTS (not CUTOUT_ARC_SEGMENTS
    # which is intended for short arcs within chains; a full circle with 6 pts = hexagon)
    circle_cutouts = [circle_to_polygon(c["x"], c["y"], c["radius"], n=CIRCLE_CUTOUT_SEGMENTS)
                      for c in layer_circles]
    # Drill/component holes — circle_to_polygon always produces CCW, no area check needed
    hole_polys    = [circle_to_polygon(h["x"], h["y"], h["radius"], n=HOLE_SEGMENTS)
                     for h in board_holes + comp_holes]

    outer_pts = ensure_ccw(outer_pts)
    # Chain-based cutouts: winding unknown → compute area and conditionally reverse
    cw_cutouts = [ensure_cw(list(h)) for h in cutout_polys]
    # Circle-based cutouts: always CCW → reverse directly, no area computation
    cw_circles = [h[::-1] for h in circle_cutouts]
    cw_holes   = [h[::-1] for h in hole_polys]
    all_cutouts = cw_cutouts + cw_circles + cw_holes

    all_x = [p[0] for p in outer_pts]
    all_y = [p[1] for p in outer_pts]
    xmin, xmax = min(all_x), max(all_x)
    ymin, ymax = min(all_y), max(all_y)
    dx   = xmax - xmin or 1.0
    dy   = ymax - ymin or 1.0
    inv_dx = 1.0 / dx
    inv_dy = 1.0 / dy

    _t0 = _time.monotonic()
    log.debug("Earcut: контур=%d вершин, вырезов=%d, точек в вырезах=%d",
              len(outer_pts), len(all_cutouts), sum(len(h) for h in all_cutouts))
    face_pts, face_idx = triangulate_face(outer_pts, all_cutouts)
    log.debug("Earcut завершён за %.1f мс", (_time.monotonic() - _t0) * 1000)
    n_face = len(face_pts)

    # Single pass over face_pts → top+bot verts and shared UV (uv_top == uv_bottom)
    _t1 = _time.monotonic()
    top_v = []; bot_v = []; uv_d = []
    for px, py in face_pts:
        top_v.extend((px, py, thickness))
        bot_v.extend((px, py, 0.0))
        uv_d.extend(((px - xmin) * inv_dx, 1.0 - (py - ymin) * inv_dy))

    top_verts = _arr.array('f', top_v)
    bot_verts = _arr.array('f', bot_v)
    shared_uvs = _arr.array('f', uv_d)   # top and bottom UVs are identical
    top_uvs = bot_uvs = shared_uvs

    # Constant normals: list multiplication is O(n) at C level, no Python loop
    top_norms = _arr.array('f', [0.0, 0.0,  1.0] * n_face)
    bot_norms = _arr.array('f', [0.0, 0.0, -1.0] * n_face)

    top_idx = _arr.array('I', face_idx)

    # Bottom face: reverse winding per triangle — single list comprehension, no per-tri extend
    fi      = face_idx
    bot_idx = _arr.array('I', [fi[i + j] for i in range(0, len(fi), 3) for j in (0, 2, 1)])
    log.debug("Грани top/bot построены за %.1f мс", (_time.monotonic() - _t1) * 1000)

    # Боковые грани
    def build_side_ring(ring_pts, facing_out):
        n = len(ring_pts)

        # Precompute next-point list to avoid repeated modulo indexing
        next_pts = ring_pts[1:] + [ring_pts[0]]

        # Single pass: edge vectors and lengths (was: two passes with duplicate hypot)
        ex_arr   = [next_pts[i][0] - ring_pts[i][0] for i in range(n)]
        ey_arr   = [next_pts[i][1] - ring_pts[i][1] for i in range(n)]
        seg_lens = [math.hypot(ex_arr[i], ey_arr[i]) for i in range(n)]
        ring_len = sum(seg_lens) or 1.0

        # Cumulative arc lengths for UV u-coordinates
        cum = 0.0
        cum_arr = []
        for sl in seg_lens:
            cum_arr.append(cum)
            cum += sl

        # Accumulate into plain Python lists; create arrays once at the end
        # (array.extend per iteration is ~3x slower than a single array() call)
        verts_d = []
        norms_d = []
        uvs_d   = []
        idx_d   = []

        for i in range(n):
            x0, y0 = ring_pts[i]
            x1, y1 = next_pts[i]
            L = seg_lens[i] or 1.0
            nx, ny = ey_arr[i] / L, -ex_arr[i] / L
            if not facing_out:
                nx, ny = -nx, -ny

            u0 = cum_arr[i] / ring_len
            u1 = (cum_arr[i] + seg_lens[i]) / ring_len
            vc = i * 4

            verts_d += [x0, y0, 0.0,       x0, y0, thickness,
                        x1, y1, thickness,  x1, y1, 0.0]
            norms_d += [nx, ny, 0.0,  nx, ny, 0.0,
                        nx, ny, 0.0,  nx, ny, 0.0]
            uvs_d   += [u0, 0.0,  u0, 1.0,  u1, 1.0,  u1, 0.0]
            idx_d   += [vc, vc + 1, vc + 2, vc, vc + 2, vc + 3]

        return (_arr.array('f', verts_d), _arr.array('f', norms_d),
                _arr.array('f', uvs_d),   _arr.array('I', idx_d),   n * 4)

    all_rings = [(outer_pts, True)] + [(cut, False) for cut in all_cutouts]
    ring_data = [build_side_ring(pts, out) for pts, out in all_rings]

    side_verts = _arr.array('f')
    side_norms = _arr.array('f')
    side_uvs   = _arr.array('f')
    side_idx   = _arr.array('I')
    vert_base  = 0

    for sv, sn, su, si, vc in ring_data:
        side_verts.extend(sv)
        side_norms.extend(sn)
        side_uvs.extend(su)
        side_idx.extend([v + vert_base for v in si])
        vert_base += vc

    return (
        (top_verts,  top_norms,  top_uvs,  top_idx),
        (bot_verts,  bot_norms,  bot_uvs,  bot_idx),
        (side_verts, side_norms, side_uvs, side_idx),
    )


# ══════════════════════════════════════════════
#  Сборка GLB
# ══════════════════════════════════════════════

def _pad4(data: bytes) -> bytes:
    r = len(data) % 4
    return data + b'\x00' * (4 - r) if r else data


def _pad4_json(data: bytes) -> bytes:
    r = len(data) % 4
    return data + b' ' * (4 - r) if r else data


def build_glb(primitives_data, tex_top, tex_bot,
              side_color_rgba, output_path: Path):
    """
    Записывает GLB (плата без компонентов) в output_path.
    primitives_data: [(verts, norms, uvs, idx), ...] x 3 (top, bot, side)
    verts/norms/uvs — array.array('f') плоские, idx — array.array('I')
    tex_top / tex_bot — Path, PIL Image, или None.
    """
    bin_data     = bytearray()
    buffer_views = []
    accessors    = []

    def append_bin(data: bytes, target=None) -> int:
        offset = len(bin_data)
        bin_data.extend(data)
        while len(bin_data) % 4:
            bin_data.append(0)
        bv = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
        if target:
            bv["target"] = target
        bv_idx = len(buffer_views)
        buffer_views.append(bv)
        return bv_idx

    def add_accessor(bv_idx, count, comp_type, gltf_type,
                     min_vals=None, max_vals=None) -> int:
        acc = {"bufferView": bv_idx, "byteOffset": 0,
               "componentType": comp_type, "count": count, "type": gltf_type}
        if min_vals: acc["min"] = [float(v) for v in min_vals]
        if max_vals: acc["max"] = [float(v) for v in max_vals]
        acc_idx = len(accessors)
        accessors.append(acc)
        return acc_idx

    ARRAY_BUF = 34962
    ELEM_BUF  = 34963
    FLOAT32   = 5126
    UINT32    = 5125

    images   = []
    samplers = [{"magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071}]
    textures = []

    def _encode(source) -> bytes:
        if isinstance(source, Path):
            with open(source, "rb") as f:
                return f.read()
        img = source if source.mode in ("RGB", "L") else source.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        return buf.getvalue()

    def _has_tex(t):
        if t is None:
            return False
        return t.exists() if isinstance(t, Path) else True

    # Encode PIL images to PNG bytes in parallel (zlib releases GIL → threads work)
    pil_textures = [(k, v) for k, v in (("top", tex_top), ("bot", tex_bot))
                    if v is not None and not isinstance(v, Path)]
    if len(pil_textures) == 2:
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_top = ex.submit(_encode, tex_top)
            f_bot = ex.submit(_encode, tex_bot)
            tex_top = f_top.result()
            tex_bot = f_bot.result()
    else:
        if tex_top is not None and not isinstance(tex_top, Path):
            tex_top = _encode(tex_top)
        if tex_bot is not None and not isinstance(tex_bot, Path):
            tex_bot = _encode(tex_bot)

    def add_image(source, name: str) -> int:
        data    = source if isinstance(source, bytes) else _encode(source)
        mime    = "image/jpeg" if data[:2] == b'\xff\xd8' else "image/png"
        bv_img  = append_bin(data)
        img_idx = len(images)
        images.append({"bufferView": bv_img, "mimeType": mime, "name": name})
        tex_idx = len(textures)
        textures.append({"source": img_idx, "sampler": 0})
        return tex_idx

    materials = []

    def make_tex_material(name, tex_idx, double_sided=False):
        return {"name": name,
                "pbrMetallicRoughness": {
                    "baseColorTexture": {"index": tex_idx},
                    "metallicFactor": 0.0, "roughnessFactor": 0.8},
                "doubleSided": double_sided}

    def make_color_material(name, rgba, double_sided=False):
        r, g, b, a = rgba
        return {"name": name,
                "pbrMetallicRoughness": {
                    "baseColorFactor": [r / 255, g / 255, b / 255, a / 255],
                    "metallicFactor": 0.0, "roughnessFactor": 0.8},
                "doubleSided": double_sided}

    def _tex_label(t):
        if isinstance(t, Path):  return t.name
        if isinstance(t, bytes): return f"{len(t)} bytes"
        return str(t.size)

    if _has_tex(tex_top):
        log.info("Текстура top: %s", _tex_label(tex_top))
        ti = add_image(tex_top, "texture_top")
        materials.append(make_tex_material("mat_top", ti))
    else:
        log.warning("Текстура top отсутствует, используем цвет подложки")
        materials.append(make_color_material("mat_top", side_color_rgba))

    if _has_tex(tex_bot):
        log.info("Текстура bottom: %s", _tex_label(tex_bot))
        bi = add_image(tex_bot, "texture_bottom")
        materials.append(make_tex_material("mat_bottom", bi))
    else:
        log.warning("Текстура bottom отсутствует, используем цвет подложки")
        materials.append(make_color_material("mat_bottom", side_color_rgba))

    materials.append(make_color_material("mat_side", side_color_rgba, double_sided=True))
    mat_indices = [0, 1, 2]

    primitives = []
    for (verts, norms, uvs, idx), mat_idx in zip(primitives_data, mat_indices):
        if len(idx) == 0:
            log.warning("Примитив %d пуст, пропускаем", mat_idx)
            continue

        n_verts = len(verts) // 3

        pos_bv  = append_bin(verts.tobytes(), ARRAY_BUF)
        xs = verts[0::3]; ys = verts[1::3]; zs = verts[2::3]
        pos_acc = add_accessor(pos_bv, n_verts, FLOAT32, "VEC3",
                               [min(xs), min(ys), min(zs)],
                               [max(xs), max(ys), max(zs)])

        nrm_bv  = append_bin(norms.tobytes(), ARRAY_BUF)
        nrm_acc = add_accessor(nrm_bv, n_verts, FLOAT32, "VEC3")

        uv_bv   = append_bin(uvs.tobytes(), ARRAY_BUF)
        uv_acc  = add_accessor(uv_bv, n_verts, FLOAT32, "VEC2")

        idx_bv  = append_bin(idx.tobytes(), ELEM_BUF)
        idx_acc = add_accessor(idx_bv, len(idx), UINT32, "SCALAR")

        primitives.append({
            "attributes": {"POSITION": pos_acc, "NORMAL": nrm_acc, "TEXCOORD_0": uv_acc},
            "indices": idx_acc,
            "material": mat_idx,
            "mode": 4,
        })

    gltf_json = {
        "asset": {"version": "2.0", "generator": "generatePCB.py"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes":  [{"mesh": 0, "scale": [0.001, 0.001, 0.001],
                    "rotation": [-0.7071067811865476, 0.0, 0.0, 0.7071067811865476]}],
        "meshes": [{"name": "board", "primitives": primitives}],
        "materials":   materials,
        "textures":    textures,
        "images":      images,
        "samplers":    samplers,
        "accessors":   accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(bin_data)}],
    }
    if not textures: del gltf_json["textures"]
    if not images:   del gltf_json["images"]

    json_bytes = _pad4_json(json.dumps(gltf_json, separators=(",", ":")).encode("utf-8"))
    bin_bytes  = _pad4(bytes(bin_data))

    json_chunk = struct.pack("<II", len(json_bytes), 0x4E4F534A) + json_bytes
    bin_chunk  = struct.pack("<II", len(bin_bytes),  0x004E4942) + bin_bytes
    total_len  = 12 + len(json_chunk) + len(bin_chunk)
    header     = struct.pack("<III", 0x46546C67, 2, total_len)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(header + json_chunk + bin_chunk)

    log.info("GLB записан: %.1f КБ", total_len / 1024)


# ══════════════════════════════════════════════
#  Основной поток
# ══════════════════════════════════════════════

def process(brd_path: Path, thickness_override=None, layer=DEFAULT_LAYER,
            tex_dir_override=None):
    """
    Полный pipeline: BRD -> GLB платы с текстурами.
    Результат сохраняется в <brd_dir>/NoABS_tmp/<brd_stem>.glb
    """
    log.info("=== generatePCB ===")
    log.info("BRD: %s", brd_path)

    output_dir  = brd_path.parent / "NoABS_tmp"
    output_path = output_dir / f"{brd_path.stem}.glb"

    root, wires, circles, holes = load_brd(brd_path, layer)
    comp_holes = get_component_holes(root)
    colors     = get_colors(root)
    side_color = colors.get("substratecolor", (120, 110, 70, 255))
    log.info("Цвет торца (substrate): RGBA%s", side_color)

    if thickness_override is not None:
        thickness = thickness_override
        log.info("Толщина: %.3f мм (задана вручную)", thickness)
    else:
        thickness = get_thickness(root)
        if thickness is None:
            thickness = DEFAULT_THICKNESS
            log.info("Толщина: %.3f мм (по умолчанию)", thickness)
        else:
            log.info("Толщина: %.3f мм (из стека слоёв BRD)", thickness)

    fp_wires, fp_circles = extract_footprint_outline(root, layers=("20", "46"))
    if fp_wires or fp_circles:
        log.debug("Дополнительные контуры из футпринтов: wire=%d, circle=%d",
                  len(fp_wires), len(fp_circles))
        wires   += fp_wires
        circles += fp_circles

    log.info("Итого контур: wire=%d, circle=%d, hole=%d, pad-hole=%d",
             len(wires), len(circles), len(holes), len(comp_holes))

    chains = chain_segments(wires)
    outer_chain, cutout_chains, open_chains = classify_chains(chains)
    if outer_chain is None:
        log.error("Внешний контур не найден - проверьте слой %s в BRD", layer)
        sys.exit(1)
    log.info("Внешний контур найден, вырезов: %d, незамкнутых цепочек: %d",
             len(cutout_chains), len(open_chains))
    if open_chains:
        log.warning("Обнаружены незамкнутые цепочки: %d шт.", len(open_chains))

    log.info("Триангулируем...")
    prim_data = build_board_mesh(
        outer_chain, cutout_chains, circles,
        holes, comp_holes, thickness
    )
    top_d, bot_d, side_d = prim_data
    log.info("Меш: top=%d вершин/%d треуг, bot=%d вершин/%d треуг, side=%d вершин/%d треуг",
             len(top_d[0]) // 3,  len(top_d[3])  // 3,
             len(bot_d[0]) // 3,  len(bot_d[3])  // 3,
             len(side_d[0]) // 3, len(side_d[3]) // 3)

    tex_dir = tex_dir_override if tex_dir_override \
        else output_dir / f"{brd_path.stem}_textures"

    log.info("Ищем текстуры в: %s", tex_dir)
    if not tex_dir.exists():
        log.warning("Директория текстур не найдена: %s - GLB будет без текстур", tex_dir)

    img_top = img_bot = None
    if tex_dir.exists():
        pre_top = tex_dir / "top_texture.bmp"
        pre_bot = tex_dir / "bottom_texture.bmp"
        if pre_top.exists() and pre_bot.exists():
            img_top = Image.open(pre_top).copy()
            img_bot = Image.open(pre_bot).copy()
            pre_top.unlink(missing_ok=True)
            pre_bot.unlink(missing_ok=True)
            log.info("Текстуры загружены, BMP удалены")
        else:
            log.warning("Текстуры Eagle не найдены в %s", tex_dir)
    else:
        log.warning("Директория текстур не найдена: %s", tex_dir)

    if img_top is None:
        log.warning("texture_top отсутствует")
    if img_bot is None:
        log.warning("texture_bottom отсутствует")

    log.info("Собираем GLB...")
    build_glb(prim_data, img_top, img_bot, side_color, output_path)

    log.info("Выходной файл: %s", output_path.resolve())
    return output_path.resolve()


# ══════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════

def main():
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Eagle .brd -> GLB модель платы с текстурами (без компонентов)"
    )
    parser.add_argument("brd",
                        help="Путь к .brd файлу")
    parser.add_argument("--thickness", "-t", type=float, default=None,
                        help="Толщина платы в мм (default: вычисляется из BRD)")
    parser.add_argument("--layer", default=DEFAULT_LAYER,
                        help=f"Слой контура платы (default: {DEFAULT_LAYER})")
    parser.add_argument("--textures", default=None,
                        help="Директория с PNG текстурами (default: <brd_stem>_textures/)")
    args = parser.parse_args()

    brd_path = Path(args.brd).resolve()
    if not brd_path.exists():
        log.error("Файл не найден: %s", brd_path)
        sys.exit(1)

    tex_dir_override = Path(args.textures).resolve() if args.textures else None

    try:
        out = process(brd_path, args.thickness, args.layer, tex_dir_override)
        print(str(out))
    except Exception as e:
        import traceback
        log.error("FATAL: %s\n%s", e, traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
