"""
makeOutline.py
--------------
Генерирует PNG-изображение контура платы из Eagle .brd файлов.
Полезен для диагностики некорректно определённого контура.

Цветовое кодирование:
  светло-зелёный  — площадь платы (внешний контур)
  белый           — вырезы (cutouts) внутри платы
  красный         — незамкнутые цепочки (проблемные сегменты)
  синий контур    — circles на слое контура
  синий кружок    — монтажные отверстия BRD (<hole>)
  светло-голубой  — отверстия компонентов (pad drill)

Использование:
    python makeOutline.py <directory>
    python makeOutline.py <directory> --layer 20 --dpi 15
"""

import sys
import argparse
import logging
from pathlib import Path

from PIL import Image, ImageDraw

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from generatePCB import (
    load_brd,
    get_component_holes,
    extract_footprint_outline,
    chain_segments,
    classify_chains,
    chain_to_polygon,
    DEFAULT_LAYER,
)

log = logging.getLogger("makeOutline")

MARGIN_MM = 3.0


def _bounds(outer_poly, cutout_polys, open_polys, circles, holes, comp_holes):
    pts = list(outer_poly)
    for poly in cutout_polys + open_polys:
        pts.extend(poly)
    for c in circles:
        r = c["radius"]
        pts += [(c["x"] - r, c["y"] - r), (c["x"] + r, c["y"] + r)]
    for h in holes + comp_holes:
        r = h["radius"]
        pts += [(h["x"] - r, h["y"] - r), (h["x"] + r, h["y"] + r)]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _make_transform(min_x, min_y, img_h, px_per_mm, margin_px):
    def to_px(x, y):
        px = int((x - min_x) * px_per_mm) + margin_px
        py = img_h - (int((y - min_y) * px_per_mm) + margin_px) - 1
        return px, py
    return to_px


def _poly_px(pts, to_px):
    return [to_px(x, y) for x, y in pts]


def make_outline_image(brd_path: Path, layer: str = DEFAULT_LAYER,
                       px_per_mm: float = 10.0) -> Image.Image:
    root, wires, circles, holes = load_brd(brd_path, layer)
    comp_holes = get_component_holes(root)

    fp_wires, fp_circles = extract_footprint_outline(root, layers=("20", "46"))
    wires   += fp_wires
    circles += fp_circles

    chains = chain_segments(wires)
    outer_chain, cutout_chains, open_chains = classify_chains(chains)

    if outer_chain is None:
        log.warning("%s: внешний контур не найден", brd_path.name)
        img = Image.new("RGB", (400, 80), (255, 255, 255))
        ImageDraw.Draw(img).text((10, 30), "Контур не найден", fill=(200, 0, 0))
        return img

    outer_poly   = chain_to_polygon(outer_chain)
    cutout_polys = [chain_to_polygon(ch) for ch in cutout_chains]
    open_polys   = [chain_to_polygon(ch) for ch in open_chains]

    min_x, min_y, max_x, max_y = _bounds(
        outer_poly, cutout_polys, open_polys, circles, holes, comp_holes
    )
    min_x -= MARGIN_MM
    min_y -= MARGIN_MM

    margin_px = int(MARGIN_MM * px_per_mm)
    w_px = int((max_x - min_x + MARGIN_MM) * px_per_mm) + margin_px
    h_px = int((max_y - min_y + MARGIN_MM) * px_per_mm) + margin_px

    img  = Image.new("RGB", (w_px, h_px), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    to_px = _make_transform(min_x, min_y, h_px, px_per_mm, margin_px)

    # Внешний контур — зелёная заливка
    op = _poly_px(outer_poly, to_px)
    draw.polygon(op, fill=(200, 230, 200))
    draw.polygon(op, outline=(0, 120, 0), width=2)

    # Вырезы — белая заливка
    for poly in cutout_polys:
        pp = _poly_px(poly, to_px)
        draw.polygon(pp, fill=(255, 255, 255))
        draw.polygon(pp, outline=(0, 120, 0), width=2)

    # Незамкнутые цепочки — красным (диагностика)
    for poly in open_polys:
        pp = _poly_px(poly, to_px)
        if len(pp) >= 2:
            draw.line(pp, fill=(220, 0, 0), width=2)
        for pt in pp:
            r = 3
            draw.ellipse([pt[0] - r, pt[1] - r, pt[0] + r, pt[1] + r],
                         fill=(220, 0, 0))

    # Circles слоя контура — синий контур
    line_w = max(1, int(px_per_mm * 0.2))
    for c in circles:
        r_px = int(c["radius"] * px_per_mm)
        cx, cy = to_px(c["x"], c["y"])
        draw.ellipse([cx - r_px, cy - r_px, cx + r_px, cy + r_px],
                     outline=(0, 0, 180), width=line_w)

    # Монтажные отверстия BRD — тёмно-синие
    for h in holes:
        r_px = max(2, int(h["radius"] * px_per_mm))
        cx, cy = to_px(h["x"], h["y"])
        draw.ellipse([cx - r_px, cy - r_px, cx + r_px, cy + r_px],
                     fill=(80, 80, 200))

    # Отверстия компонентов — светло-голубые
    for h in comp_holes:
        r_px = max(1, int(h["radius"] * px_per_mm))
        cx, cy = to_px(h["x"], h["y"])
        draw.ellipse([cx - r_px, cy - r_px, cx + r_px, cy + r_px],
                     fill=(180, 210, 240))

    log.info(
        "%s: %dx%d px | outer=%d pts | cutouts=%d | open=%d | holes=%d | comp_holes=%d",
        brd_path.name, w_px, h_px,
        len(outer_poly), len(cutout_chains), len(open_chains),
        len(holes), len(comp_holes),
    )
    if open_chains:
        log.warning("%s: %d незамкнутых цепочек (красные линии на картинке)",
                    brd_path.name, len(open_chains))

    return img


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(
        description="Генерирует PNG контура платы для каждого .brd в директории"
    )
    parser.add_argument("directory", help="Директория с .brd файлами")
    parser.add_argument("--layer", default=DEFAULT_LAYER,
                        help=f"Слой контура (default: {DEFAULT_LAYER})")
    parser.add_argument("--dpi", type=float, default=10.0,
                        help="Пикселей на мм (default: 10)")
    args = parser.parse_args()

    brd_dir = Path(args.directory).resolve()
    if not brd_dir.is_dir():
        log.error("Директория не найдена: %s", brd_dir)
        sys.exit(1)

    brd_files = sorted(brd_dir.glob("*.brd"))
    if not brd_files:
        log.warning("BRD файлы не найдены в %s", brd_dir)
        sys.exit(0)

    log.info("Найдено BRD файлов: %d в %s", len(brd_files), brd_dir)

    errors = 0
    for brd_path in brd_files:
        png_path = brd_path.with_suffix(".png")
        try:
            img = make_outline_image(brd_path, args.layer, args.dpi)
            img.save(str(png_path))
            log.info("  сохранено: %s", png_path.name)
        except Exception as e:
            import traceback
            log.error("  %s: %s\n%s", brd_path.name, e, traceback.format_exc())
            errors += 1

    if errors:
        log.error("Завершено с ошибками: %d из %d файлов", errors, len(brd_files))
        sys.exit(1)


if __name__ == "__main__":
    main()
