"""
eagle_board_outline.py
----------------------
Читает Eagle .brd файл, извлекает контур платы (layer 20 - Dimension)
и отрисовывает его в виде PNG-картинки.

Структура контура:
  - ровно 1 внешний контур (наибольшая площадь)
  - 0..N внутренних вырезов (не вложенных, не пересекающихся)

Зависимости:
    pip install matplotlib numpy

Использование:
    python eagle_board_outline.py your_board.brd
    python eagle_board_outline.py your_board.brd -o preview.png --dpi 150
"""

import xml.etree.ElementTree as ET
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import PathPatch
from matplotlib.path import Path as MplPath
import argparse
import math
import sys
from pathlib import Path


DEFAULT_DIMENSION_LAYER = "20"


# ──────────────────────────────────────────────
#  Парсинг геометрии
# ──────────────────────────────────────────────

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
        "radius": float(el.get("drill")) / 2.0,  # drill — диаметр
    }


def extract_dimension_elements(brd_path, layer=DEFAULT_DIMENSION_LAYER):
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

    return wires, circles, holes


# ──────────────────────────────────────────────
#  Отверстия выводных компонентов
# ──────────────────────────────────────────────

def parse_rot(rot_str):
    """
    Разбирает строку поворота Eagle вида "", "R90", "MR180", "M0" и т.п.
    Возвращает (angle_deg: float, mirror: bool).
    """
    if not rot_str:
        return 0.0, False
    mirror = rot_str.startswith("M")
    num_str = rot_str.lstrip("MR")
    angle = float(num_str) if num_str else 0.0
    return angle, mirror


def transform_point(lx, ly, angle_deg, mirror, tx, ty):
    """
    Переводит локальную точку (lx, ly) корпуса в мировые координаты.
    Порядок: сначала зеркало по X, потом поворот, потом перенос.
    """
    if mirror:
        lx = -lx
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    wx = cos_a * lx - sin_a * ly + tx
    wy = sin_a * lx + cos_a * ly + ty
    return wx, wy


def build_package_index(root):
    """
    Строит словарь (lib_name, pkg_name) -> список падов.
    Каждый пад: {"x", "y", "radius", "rot_str"}.
    Учитываются только <pad> (сквозные), <smd> пропускаются.
    """
    index = {}
    for lib in root.iter("library"):
        lib_name = lib.get("name", "")
        for pkg in lib.iter("package"):
            pkg_name = pkg.get("name", "")
            pads = []
            for pad in pkg.findall("pad"):
                drill = pad.get("drill")
                if drill is None:
                    continue
                pads.append({
                    "x":       float(pad.get("x", 0)),
                    "y":       float(pad.get("y", 0)),
                    "radius":  float(drill) / 2.0,
                    "rot_str": pad.get("rot", ""),
                })
            if pads:
                index[(lib_name, pkg_name)] = pads
    return index


def extract_component_holes(root):
    """
    Возвращает список отверстий выводных компонентов в мировых координатах.
    Каждое отверстие: {"x", "y", "radius"}.
    """
    pkg_index = build_package_index(root)
    comp_holes = []

    for el in root.iter("element"):
        lib_name = el.get("library", "")
        pkg_name = el.get("package", "")
        pads = pkg_index.get((lib_name, pkg_name))
        if not pads:
            continue

        el_x = float(el.get("x", 0))
        el_y = float(el.get("y", 0))
        el_angle, el_mirror = parse_rot(el.get("rot", ""))

        for pad in pads:
            # pad["rot_str"] описывает ориентацию формы пада (овал и т.п.),
            # но НЕ влияет на положение центра отверстия в системе корпуса.
            # Позицию трансформируем только поворотом и зеркалом элемента.
            wx, wy = transform_point(
                pad["x"], pad["y"],
                el_angle, el_mirror,
                el_x, el_y
            )
            comp_holes.append({"x": wx, "y": wy, "radius": pad["radius"]})

    return comp_holes


# ──────────────────────────────────────────────
#  Дуга Eagle → полилиния
# ──────────────────────────────────────────────

def arc_to_polyline(x1, y1, x2, y2, curve_deg, n=64):
    dx, dy = x2 - x1, y2 - y1
    chord  = math.hypot(dx, dy)
    if chord < 1e-12:
        return [x1, x2], [y1, y2]

    alpha  = math.radians(abs(curve_deg) / 2)
    r      = chord / (2 * math.sin(alpha))
    mx, my = (x1 + x2) / 2, (y1 + y2) / 2
    d      = math.sqrt(max(r**2 - (chord / 2)**2, 0))
    px, py = -dy / chord, dx / chord
    sign   = 1 if curve_deg > 0 else -1
    cx     = mx + sign * d * px
    cy     = my + sign * d * py

    a1 = math.degrees(math.atan2(y1 - cy, x1 - cx))
    a2 = math.degrees(math.atan2(y2 - cy, x2 - cx))
    if curve_deg > 0:
        if a2 < a1:
            a2 += 360
    else:
        if a1 < a2:
            a1 += 360
        a1, a2 = a2, a1

    angles = np.linspace(math.radians(a1), math.radians(a2), n)
    return (cx + r * np.cos(angles)).tolist(), (cy + r * np.sin(angles)).tolist()


# ──────────────────────────────────────────────
#  Сборка сегментов в полилинии
# ──────────────────────────────────────────────

def segments_to_polylines(wires, tol=1e-3):
    segs = []
    for w in wires:
        if w["curve"] == 0:
            segs.append(([w["x1"], w["x2"]], [w["y1"], w["y2"]]))
        else:
            xs, ys = arc_to_polyline(w["x1"], w["y1"], w["x2"], w["y2"], w["curve"])
            segs.append((xs, ys))

    polylines = []
    used = [False] * len(segs)

    def matches(ax, ay, bx, by):
        return abs(ax - bx) < tol and abs(ay - by) < tol

    for start in range(len(segs)):
        if used[start]:
            continue
        used[start] = True
        px = list(segs[start][0])
        py = list(segs[start][1])

        while True:
            tx, ty = px[-1], py[-1]
            found = False
            for i, (sx, sy) in enumerate(segs):
                if used[i]:
                    continue
                if matches(tx, ty, sx[0], sy[0]):
                    px.extend(sx[1:]); py.extend(sy[1:])
                    used[i] = True; found = True; break
                elif matches(tx, ty, sx[-1], sy[-1]):
                    px.extend(list(reversed(sx[:-1]))); py.extend(list(reversed(sy[:-1])))
                    used[i] = True; found = True; break
            if not found:
                break

        polylines.append((px, py))

    return polylines


# ──────────────────────────────────────────────
#  Классификация: внешний контур vs вырезы
# ──────────────────────────────────────────────

def polygon_area(xs, ys):
    """Площадь полигона по формуле Гаусса."""
    n = len(xs)
    a = 0.0
    for i in range(n):
        j = (i + 1) % n
        a += xs[i] * ys[j] - xs[j] * ys[i]
    return abs(a) / 2.0


def split_contours(polylines, tol=1e-3):
    """
    Возвращает (outer, cutouts, open_segments).
      outer       — (xs, ys) — один внешний замкнутый контур
      cutouts     — список (xs, ys) — замкнутые внутренние вырезы
      open_segs   — список (xs, ys) — незамкнутые отрезки (размерные линии и т.п.)
    """
    closed = []
    open_segs = []

    for xs, ys in polylines:
        is_closed = (len(xs) >= 3
                     and abs(xs[0] - xs[-1]) < tol
                     and abs(ys[0] - ys[-1]) < tol)
        if is_closed:
            closed.append((xs, ys))
        else:
            open_segs.append((xs, ys))

    if not closed:
        return None, [], open_segs

    # Внешний контур — с наибольшей площадью
    closed.sort(key=lambda c: polygon_area(c[0], c[1]), reverse=True)
    outer   = closed[0]
    cutouts = closed[1:]

    return outer, cutouts, open_segs


# ──────────────────────────────────────────────
#  Отрисовка
# ──────────────────────────────────────────────

BG_COLOR    = "#0f0f1a"
BOARD_COLOR = "#00e676"
FILL_COLOR  = "#0d2b1a"


def draw_outline(wires, circles, holes, comp_holes, output_path, dpi=150, title="Board outline"):
    if not wires and not circles:
        print("⚠  Элементы layer 20 не найдены. Проверьте файл.")
        sys.exit(1)

    polylines = segments_to_polylines(wires)
    outer, cutouts, open_segs = split_contours(polylines)

    fig, ax = plt.subplots(figsize=(10, 8))
    ax.set_aspect("equal")
    ax.set_facecolor(BG_COLOR)
    fig.patch.set_facecolor(BG_COLOR)

    # ── Внешний контур: заливка + обводка ──
    if outer:
        ax.fill(outer[0], outer[1], color=FILL_COLOR, zorder=1)
        ax.plot(outer[0], outer[1], color=BOARD_COLOR, linewidth=1.8, zorder=3)

    # ── Вырезы: перекрашиваем фоновым цветом + обводка ──
    for xs, ys in cutouts:
        ax.fill(xs, ys, color=BG_COLOR, zorder=2)           # "вырезаем" заливку
        ax.plot(xs, ys, color=BOARD_COLOR, linewidth=1.8, zorder=3)

    # ── Незамкнутые сегменты (размерные линии и т.п.) ──
    for xs, ys in open_segs:
        ax.plot(xs, ys, color=BOARD_COLOR, linewidth=1.0,
                linestyle="--", zorder=3, alpha=0.5)

    # ── Окружности (круглые вырезы на layer 20) ──
    for c in circles:
        patch = plt.Circle(
            (c["x"], c["y"]), c["radius"],
            facecolor=BG_COLOR, edgecolor=BOARD_COLOR,
            linewidth=1.5, zorder=4
        )
        ax.add_patch(patch)

    # ── Отверстия (<hole>) ──
    for h in holes:
        patch = plt.Circle(
            (h["x"], h["y"]), h["radius"],
            facecolor=BG_COLOR, edgecolor=BOARD_COLOR,
            linewidth=1.2, linestyle="--", zorder=4
        )
        ax.add_patch(patch)

    # ── Отверстия выводных компонентов ──
    for h in comp_holes:
        patch = plt.Circle(
            (h["x"], h["y"]), h["radius"],
            facecolor=BG_COLOR, edgecolor="#ff9800",
            linewidth=1.0, zorder=4
        )
        ax.add_patch(patch)

    # ── Габариты и диагностика ──
    all_xs = (outer[0] if outer else []) + [c["x"] for c in circles]
    all_ys = (outer[1] if outer else []) + [c["y"] for c in circles]
    if all_xs and all_ys:
        xmin, xmax = min(all_xs), max(all_xs)
        ymin, ymax = min(all_ys), max(all_ys)
        w_mm = xmax - xmin
        h_mm = ymax - ymin
        margin = max(w_mm, h_mm) * 0.08
        ax.set_xlim(xmin - margin, xmax + margin)
        ax.set_ylim(ymin - margin, ymax + margin)

        lines = [f"W: {w_mm:.2f} mm   H: {h_mm:.2f} mm"]
        if cutouts:
            lines.append(f"Вырезов: {len(cutouts)}")
        if holes:
            lines.append(f"Отверстий: {len(holes)}")
        if comp_holes:
            lines.append(f"Отверстий компонентов: {len(comp_holes)}")
        if open_segs:
            lines.append(f"Незамкнутых сегментов: {len(open_segs)}")
        ax.text(0.01, 0.01, "\n".join(lines), transform=ax.transAxes,
                color="#aaaaaa", fontsize=8, va="bottom", linespacing=1.5)

    ax.set_title(title, color="#cccccc", pad=10)
    ax.tick_params(colors="#555555")
    for spine in ax.spines.values():
        spine.set_edgecolor("#333333")
    ax.set_xlabel("X (mm)", color="#777777", fontsize=8)
    ax.set_ylabel("Y (mm)", color="#777777", fontsize=8)
    ax.grid(True, color="#222244", linewidth=0.4, linestyle="--")

    plt.tight_layout()
    plt.savefig(output_path, dpi=dpi, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close()
    print(f"✓  Сохранено: {output_path}")
    if cutouts:
        print(f"   Найдено вырезов: {len(cutouts)}")
    if holes:
        print(f"   Найдено отверстий (<hole>): {len(holes)}")
    if comp_holes:
        print(f"   Найдено отверстий компонентов: {len(comp_holes)}")
    if open_segs:
        print(f"   Незамкнутых сегментов: {len(open_segs)} (пунктир)")


# ──────────────────────────────────────────────
#  CLI
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Отрисовка контура PCB из Eagle .brd файла"
    )
    parser.add_argument("brd", help="Путь к .brd файлу")
    parser.add_argument("--output", "-o", default=None,
                        help="PNG для сохранения (default: <имя>_outline.png)")
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--layer", default=DEFAULT_DIMENSION_LAYER,
                        help=f"Номер слоя контура (default: {DEFAULT_DIMENSION_LAYER})")
    args = parser.parse_args()

    brd_path = Path(args.brd)
    if not brd_path.exists():
        print(f"Файл не найден: {brd_path}")
        sys.exit(1)

    output = args.output or (brd_path.stem + "_outline.png")

    print(f"Читаем: {brd_path}")
    wires, circles, holes = extract_dimension_elements(brd_path, layer=args.layer)
    print(f"  wire: {len(wires)}, circle: {len(circles)}, hole: {len(holes)}")

    tree = ET.parse(brd_path)
    comp_holes = extract_component_holes(tree.getroot())
    print(f"  отверстий компонентов: {len(comp_holes)}")

    draw_outline(wires, circles, holes, comp_holes, output, dpi=args.dpi, title=brd_path.name)


if __name__ == "__main__":
    main()
