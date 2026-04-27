"""
generatePCB.py
--------------
Генерирует GLB модель платы (без компонентов) из Eagle .brd файла с текстурами.

Текстуры ищутся в <brd_dir>/<brd_stem>_textures/ (результат экспорта Eagle ULP).
Выходной файл сохраняется в <brd_dir>/NoABS_tmp/<brd_stem>.glb

Все размеры — в миллиметрах. Плоскость платы XY, Z направлена вверх (top).

Зависимости:
    pip install pillow earcut
"""

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
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat
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
DEFAULT_COLORS = {
    "substratecolor":  (120, 110,  70, 255),  # #786e46
    "coppercolor":     (192, 192, 192, 255),  # #c0c0c0
    "silkscreencolor": (255, 255, 255, 255),  # #ffffff
    "soldermaskcolor": (  0, 140,  74, 255),  # #008C4A
}
ARC_SEGMENTS      = 64

_TEX_REQUIRED = [
    "outline.png",
    "top_copper.png", "bottom_copper.png",
    "top_mask.png",   "bottom_mask.png",
    "top_silk.png",   "bottom_silk.png",
]


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


# ══════════════════════════════════════════════
#  Обработка текстур (PIL-only, без numpy/scipy)
# ══════════════════════════════════════════════

# Целевой размер изображения при floodfill — PIL floodfill реализован на Python,
# поэтому на больших изображениях его выполняют на уменьшенной копии.
_FILL_MIN_PX = 600

# LUT-таблицы для point() — намного быстрее lambda, т.к. применяются на C уровне
_LUT_LE128 = bytes(255 if i <= 128 else 0 for i in range(256))
_LUT_GT128 = bytes(255 if i > 128 else 0 for i in range(256))
_LUT_EQ128 = bytes(255 if i == 128 else 0 for i in range(256))
_LUT_EQ255 = bytes(255 if i == 255 else 0 for i in range(256))
_LUT_NE128 = bytes(255 if i != 128 else 0 for i in range(256))



def _find_components_pil(mask_img: Image.Image):
    """
    Находит связные компоненты 255-пикселей в PIL L-изображении.
    Возвращает список (count, mask_image) отсортированный по убыванию count.
    """
    w, h = mask_img.size
    work = mask_img.copy()
    components = []
    lut_128 = bytes(255 if i == 128 else 0 for i in range(256))

    while True:
        raw = work.tobytes()
        idx = raw.find(b'\xff')
        if idx == -1:
            break
        seed = (idx % w, idx // w)

        filled = work.copy()
        ImageDraw.floodfill(filled, seed, 128)

        comp = filled.point(lut_128)
        count = int(ImageStat.Stat(comp).sum[0]) // 255
        work = ImageChops.subtract(work, comp)

        components.append((count, comp))

    components.sort(key=lambda t: t[0], reverse=True)
    return components


def _tex_analyze_outline(outline_path: Path, threshold: int = 128):
    """
    Анализирует outline.png, возвращает PIL L-маски (0/255):
      board_mask  — основной interior платы
      hole_mask   — interior вырезов внутри платы
      full_board  — всё что не снаружи (для bbox)
      h, w        — размеры изображения

    Floodfill выполняется на уменьшенной копии (до _FILL_MIN_PX px),
    маски увеличиваются обратно через NEAREST — даёт 10-50x ускорение
    на высоких DPI без заметного влияния на качество.
    """
    img = Image.open(outline_path).convert("L")
    w, h = img.size

    scale = max(1, max(w, h) // _FILL_MIN_PX)
    sw, sh = (max(w // scale, 1), max(h // scale, 1)) if scale > 1 else (w, h)

    lut_black = bytes(255 if i <= threshold else 0 for i in range(256))
    is_black_full = img.point(lut_black)
    if scale > 1:
        # Eagle exports dark background + bright outline, so is_black has 255=background, 0=outline.
        # PIL NEAREST resize can miss a 1-px outline entirely (sample points skip it).
        # MinFilter at full resolution thickens the outline (expands 0-pixels) before downscale,
        # guaranteeing every NEAREST block contains at least one outline pixel.
        # scale//2+1 passes make the outline wide enough to survive the scale-factor block size.
        dilated = is_black_full
        for _ in range(scale // 2 + 1):
            dilated = dilated.filter(ImageFilter.MinFilter(3))
        is_black = dilated.resize((sw, sh), Image.NEAREST)
        # Ensure exterior background (255) at image edges connects to padded border (also 255),
        # even if MinFilter expansion reached the image boundary.
        ImageDraw.Draw(is_black).rectangle([(0, 0), (sw - 1, sh - 1)], outline=255, width=1)
    else:
        is_black = is_black_full

    padded = Image.new("L", (sw + 2, sh + 2), 255)
    padded.paste(is_black, (1, 1))
    ImageDraw.floodfill(padded, (0, 0), 128)
    inner = padded.crop((1, 1, sw + 1, sh + 1))

    interior   = inner.point(_LUT_EQ255)
    full_board = inner.point(_LUT_NE128)

    empty = Image.new("L", (sw, sh), 0)

    if interior.getbbox() is None:
        board_mask, hole_mask = empty, empty
    else:
        components = _find_components_pil(interior)
        if not components:
            board_mask, hole_mask = empty, empty
        elif len(components) == 1:
            board_mask = components[0][1]
            hole_mask  = empty
        else:
            board_mask = components[0][1]
            hole_mask  = empty.copy()
            for _, comp in components[1:]:
                hole_mask = ImageChops.add(hole_mask, comp)

    if scale > 1:
        board_mask = board_mask.resize((w, h), Image.NEAREST)
        hole_mask  = hole_mask.resize((w, h), Image.NEAREST)
        full_board = full_board.resize((w, h), Image.NEAREST)

    return board_mask, hole_mask, full_board, h, w


def _tex_board_bbox(full_board: Image.Image):
    """Возвращает (r0, r1, c0, c1) bounding box ненулевых пикселей."""
    bbox = full_board.getbbox()
    if bbox is None:
        w, h = full_board.size
        return 0, h - 1, 0, w - 1
    left, upper, right, lower = bbox
    return upper, lower - 1, left, right - 1


def _tex_load_gray(path: Path, h: int, w: int) -> Image.Image:
    """Загружает PNG как оттенки серого PIL L-изображение нужного размера."""
    img = Image.open(path).convert("L")
    if img.size != (w, h):
        img = img.resize((w, h), Image.NEAREST)
    return img


def _tex_make_rgba(mask: Image.Image, color, alpha=None) -> Image.Image:
    """Создаёт RGBA PIL-изображение: color там где mask=255, прозрачно где 0."""
    r, g, b, a = color
    if alpha is not None:
        a = alpha
    w, h = mask.size
    result  = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    colored = Image.new("RGBA", (w, h), (r, g, b, a))
    result.paste(colored, mask=mask)
    return result


def _tex_composite(base: Image.Image, overlay: Image.Image) -> Image.Image:
    return Image.alpha_composite(base, overlay)


def _tex_extract_drill_holes(pads_gray: Image.Image) -> Image.Image:
    """Возвращает PIL L-маску drill holes (чёрные области не связанные с границей)."""
    w, h = pads_gray.size

    scale = max(1, max(w, h) // _FILL_MIN_PX)
    sw, sh = (max(w // scale, 1), max(h // scale, 1)) if scale > 1 else (w, h)

    is_black_full = pads_gray.point(_LUT_LE128)
    if scale > 1:
        dilated = is_black_full
        for _ in range(scale // 2 + 1):
            dilated = dilated.filter(ImageFilter.MinFilter(3))
        is_black = dilated.resize((sw, sh), Image.NEAREST)
        ImageDraw.Draw(is_black).rectangle([(0, 0), (sw - 1, sh - 1)], outline=255, width=1)
    else:
        is_black = is_black_full

    padded = Image.new("L", (sw + 2, sh + 2), 255)
    padded.paste(is_black, (1, 1))
    ImageDraw.floodfill(padded, (0, 0), 128)
    inner = padded.crop((1, 1, sw + 1, sh + 1))
    result = inner.point(_LUT_EQ255)

    if scale > 1:
        result = result.resize((w, h), Image.NEAREST)
    return result


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


def process_textures(brd_path: Path, tex_dir: Path, output_dir: Path,
                     colors_override: dict = None) -> bool:
    """
    Обрабатывает текстуры Eagle -> texture_top.png + texture_bottom.png.
    Сохраняет результат в output_dir.
    Возвращает True при успехе.

    colors_override — полностью разрешённый dict цветов (substratecolor, coppercolor,
    silkscreencolor, soldermaskcolor) в виде (R,G,B,A) кортежей. Если передан,
    используется вместо чтения из BRD; иначе цвета читаются из BRD с DEFAULT_COLORS как fallback.
    """
    log.info("Обрабатываем текстуры из: %s", tex_dir)

    if colors_override is not None:
        colors = colors_override
    else:
        root = ET.parse(brd_path).getroot()
        colors = {**DEFAULT_COLORS, **get_colors(root)}

    substrate_color  = colors.get("substratecolor",  DEFAULT_COLORS["substratecolor"])
    copper_color     = colors.get("coppercolor",      DEFAULT_COLORS["coppercolor"])
    silkscreen_color = colors.get("silkscreencolor",  DEFAULT_COLORS["silkscreencolor"])
    soldermask_color = colors.get("soldermaskcolor",  DEFAULT_COLORS["soldermaskcolor"])

    log.debug("Цвета: substrate=%s, copper=%s, soldermask=%s",
              substrate_color[:3], copper_color[:3], soldermask_color[:3])

    missing = [f for f in _TEX_REQUIRED if not (tex_dir / f).exists()]
    if missing:
        log.warning("Отсутствуют файлы текстур: %s", ", ".join(missing))
        return False

    log.debug("Все обязательные текстуры найдены в %s", tex_dir)

    dpi = None
    for fname in _TEX_REQUIRED:
        dpi = read_png_dpi(tex_dir / fname)
        if dpi:
            log.info("DPI текстур: %.1f x %.1f (из %s)", dpi[0], dpi[1], fname)
            break
    if dpi is None:
        log.warning("DPI не найден ни в одном файле текстур")

    board_mask, hole_mask, full_board, H, W = _tex_analyze_outline(tex_dir / "outline.png")
    log.debug("Размер текстуры: %d x %d пикселей", W, H)

    drill_holes = None
    pads_path = tex_dir / "pads.png"
    if pads_path.exists():
        drill_holes = _tex_extract_drill_holes(_tex_load_gray(pads_path, H, W))
        log.debug("pads.png найден, извлекаем drill holes")
    else:
        log.debug("pads.png не найден, drill holes пропускаем")

    # board_white = board_mask без отверстий
    board_white = board_mask.copy()
    board_white = ImageChops.multiply(board_white, ImageOps.invert(hole_mask))
    if drill_holes is not None:
        board_white = ImageChops.multiply(board_white, ImageOps.invert(drill_holes))

    r0, r1, c0, c1 = _tex_board_bbox(board_white)
    crop_w, crop_h = c1 - c0 + 1, r1 - r0 + 1
    log.info("Bounding box платы на текстуре: %d x %d пикселей", crop_w, crop_h)

    if dpi:
        phys_w_mm = crop_w / dpi[0] * 25.4
        phys_h_mm = crop_h / dpi[1] * 25.4
        log.info("Физический размер текстуры: %.2f x %.2f мм", phys_w_mm, phys_h_mm)

    def load_m(fname):
        return ImageChops.multiply(_tex_load_gray(tex_dir / fname, H, W), board_white)

    def load_r(fname):
        return _tex_load_gray(tex_dir / fname, H, W)

    top_cu  = load_m("top_copper.png")
    bot_cu  = load_m("bottom_copper.png")
    top_si  = load_m("top_silk.png")
    bot_si  = load_m("bottom_silk.png")
    top_msk = load_r("top_mask.png")
    bot_msk = load_r("bottom_mask.png")

    def thresh(img):
        return img.point(_LUT_GT128)

    top_cu_t  = thresh(top_cu)
    bot_cu_t  = thresh(bot_cu)
    top_si_t  = thresh(top_si)
    bot_si_t  = thresh(bot_si)
    top_msk_t = thresh(top_msk)
    bot_msk_t = thresh(bot_msk)

    top_cu_l  = _tex_make_rgba(top_cu_t, copper_color, alpha=255)
    bot_cu_l  = _tex_make_rgba(bot_cu_t, copper_color, alpha=255)

    top_si_mask = ImageChops.multiply(top_si_t, ImageOps.invert(top_msk_t))
    bot_si_mask = ImageChops.multiply(bot_si_t, ImageOps.invert(bot_msk_t))
    top_si_l    = _tex_make_rgba(top_si_mask, silkscreen_color, alpha=255)
    bot_si_l    = _tex_make_rgba(bot_si_mask, silkscreen_color, alpha=255)

    top_msk_mask = ImageChops.multiply(ImageOps.invert(top_msk_t), board_white)
    bot_msk_mask = ImageChops.multiply(ImageOps.invert(bot_msk_t), board_white)
    top_msk_l    = _tex_make_rgba(top_msk_mask, soldermask_color, alpha=soldermask_color[3])
    bot_msk_l    = _tex_make_rgba(bot_msk_mask, soldermask_color, alpha=soldermask_color[3])

    outline_l = _tex_make_rgba(board_white, substrate_color, alpha=substrate_color[3])

    def compose(layers):
        result = layers[0].copy()
        for layer in layers[1:]:
            result = _tex_composite(result, layer)
        return result

    tex_top = compose([outline_l, top_cu_l, top_msk_l, top_si_l])
    tex_bot = compose([outline_l, bot_cu_l, bot_msk_l, bot_si_l])

    tex_top = tex_top.crop((c0, r0, c1 + 1, r1 + 1))
    tex_bot = tex_bot.crop((c0, r0, c1 + 1, r1 + 1))

    kw = {"dpi": dpi} if dpi else {}
    output_dir.mkdir(parents=True, exist_ok=True)
    tex_top.save(str(output_dir / "texture_top.png"),    **kw)
    tex_bot.save(str(output_dir / "texture_bottom.png"), **kw)
    log.info("Текстуры сохранены: texture_top.png + texture_bottom.png")
    return True


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
    """Remove duplicate wires — same arc encoded twice (same or opposite direction)."""
    kept = []
    for a in wires:
        is_dup = False
        for b in kept:
            same_dir = (_matches(a["x1"], a["y1"], b["x1"], b["y1"], tol) and
                        _matches(a["x2"], a["y2"], b["x2"], b["y2"], tol) and
                        abs(a["curve"] - b["curve"]) < curve_tol)
            rev_dir  = (_matches(a["x1"], a["y1"], b["x2"], b["y2"], tol) and
                        _matches(a["x2"], a["y2"], b["x1"], b["y1"], tol) and
                        abs(a["curve"] + b["curve"]) < curve_tol)
            if same_dir or rev_dir:
                is_dup = True
                break
        if not is_dup:
            kept.append(a)
    return kept


def chain_segments(wires, tol=1e-3):
    wires = _dedup_wires(wires, tol)
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
                if _matches(ex, ey, s["x1"], s["y1"], tol):
                    chain.append((s, False)); remaining.remove(i); found = True; break
                if _matches(ex, ey, s["x2"], s["y2"], tol):
                    chain.append((s, True));  remaining.remove(i); found = True; break
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

    flat         = [coord for pt in all_pts for coord in pt]
    hole_starts  = ring_ends[:-1] if len(ring_ends) > 1 else None
    indices      = _earcut(flat, hole_starts, 2)
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
    cutout_polys = [chain_to_polygon(ch) for ch in cutout_chains]
    cutout_polys += [circle_to_polygon(c["x"], c["y"], c["radius"]) for c in layer_circles]

    hole_polys = []
    for h in board_holes + comp_holes:
        hole_polys.append(circle_to_polygon(h["x"], h["y"], h["radius"], n=24))

    all_cutouts = cutout_polys + hole_polys

    outer_pts   = ensure_ccw(outer_pts)
    all_cutouts = [ensure_cw(list(h)) for h in all_cutouts]

    all_x = [p[0] for p in outer_pts]
    all_y = [p[1] for p in outer_pts]
    xmin, xmax = min(all_x), max(all_x)
    ymin, ymax = min(all_y), max(all_y)
    dx = xmax - xmin or 1.0
    dy = ymax - ymin or 1.0

    def uv_top(x, y):
        return (x - xmin) / dx, 1.0 - (y - ymin) / dy

    def uv_bottom(x, y):
        return (x - xmin) / dx, 1.0 - (y - ymin) / dy

    face_pts, face_idx = triangulate_face(outer_pts, all_cutouts)
    n_face = len(face_pts)

    # Верхняя грань
    top_verts = _arr.array('f', [c for p in face_pts for c in (p[0], p[1], thickness)])
    top_norms = _arr.array('f', [c for _ in face_pts for c in (0.0, 0.0, 1.0)])
    top_uvs   = _arr.array('f', [c for p in face_pts for c in uv_top(p[0], p[1])])
    top_idx   = _arr.array('I', face_idx)

    # Нижняя грань (нормаль -Z, обратный порядок треугольников)
    bot_verts = _arr.array('f', [c for p in face_pts for c in (p[0], p[1], 0.0)])
    bot_norms = _arr.array('f', [c for _ in face_pts for c in (0.0, 0.0, -1.0)])
    bot_uvs   = _arr.array('f', [c for p in face_pts for c in uv_bottom(p[0], p[1])])
    bot_idx   = _arr.array('I')
    for i in range(0, len(face_idx), 3):
        bot_idx.extend([face_idx[i], face_idx[i + 2], face_idx[i + 1]])

    # Боковые грани
    def build_side_ring(ring_pts, facing_out):
        n = len(ring_pts)
        ring_len = sum(
            math.hypot(ring_pts[(i + 1) % n][0] - ring_pts[i][0],
                       ring_pts[(i + 1) % n][1] - ring_pts[i][1])
            for i in range(n)
        ) or 1.0

        verts   = _arr.array('f')
        norms   = _arr.array('f')
        uvs     = _arr.array('f')
        indices = _arr.array('I')
        vc      = 0
        cum     = 0.0

        for i in range(n):
            x0, y0 = ring_pts[i]
            x1, y1 = ring_pts[(i + 1) % n]
            ex, ey = x1 - x0, y1 - y0
            L = math.hypot(ex, ey) or 1.0
            nx, ny = ey / L, -ex / L
            if not facing_out:
                nx, ny = -nx, -ny

            seg_len = math.hypot(ex, ey)
            u0 = cum / ring_len
            u1 = (cum + seg_len) / ring_len
            cum += seg_len

            verts.extend([x0, y0, 0.0,       x0, y0, thickness,
                          x1, y1, thickness,  x1, y1, 0.0])
            norms.extend([nx, ny, 0.0,  nx, ny, 0.0,
                          nx, ny, 0.0,  nx, ny, 0.0])
            uvs.extend([u0, 0.0,  u0, 1.0,  u1, 1.0,  u1, 0.0])
            indices.extend([vc, vc + 1, vc + 2, vc, vc + 2, vc + 3])
            vc += 4

        return verts, norms, uvs, indices, vc

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
        side_idx.extend(v + vert_base for v in si)
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


def build_glb(primitives_data, tex_top_path: Path, tex_bot_path: Path,
              side_color_rgba, output_path: Path):
    """
    Записывает GLB (плата без компонентов) в output_path.
    primitives_data: [(verts, norms, uvs, idx), ...] x 3 (top, bot, side)
    verts/norms/uvs — array.array('f') плоские, idx — array.array('I')
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

    def add_image(path: Path, name: str) -> int:
        with open(path, "rb") as f:
            png = f.read()
        bv_img  = append_bin(png)
        img_idx = len(images)
        images.append({"bufferView": bv_img, "mimeType": "image/png", "name": name})
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

    if tex_top_path.exists():
        log.info("Текстура top: %s", tex_top_path.name)
        ti = add_image(tex_top_path, "texture_top")
        materials.append(make_tex_material("mat_top", ti))
    else:
        log.warning("texture_top.png не найдена, используем цвет подложки")
        materials.append(make_color_material("mat_top", side_color_rgba))

    if tex_bot_path.exists():
        log.info("Текстура bottom: %s", tex_bot_path.name)
        bi = add_image(tex_bot_path, "texture_bottom")
        materials.append(make_tex_material("mat_bottom", bi))
    else:
        log.warning("texture_bottom.png не найдена, используем цвет подложки")
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

    tex_top = output_dir / "texture_top.png"
    tex_bot = output_dir / "texture_bottom.png"

    if tex_top.exists() and tex_bot.exists():
        log.info("Текстуры уже есть в NoABS_tmp, пропускаем")
    elif tex_dir.exists():
        pre_top = tex_dir / "top_texture.png"
        pre_bot = tex_dir / "bottom_texture.png"
        if pre_top.exists() and pre_bot.exists():
            shutil.copy2(pre_top, tex_top)
            shutil.copy2(pre_bot, tex_bot)
            log.info("Текстуры скопированы из Eagle экспорта")
        else:
            log.warning("Текстуры Eagle не найдены в %s", tex_dir)
    else:
        log.warning("Директория текстур не найдена: %s", tex_dir)

    if tex_top.exists():
        dpi = read_png_dpi(tex_top)
        if dpi:
            log.info("DPI выходной текстуры top: %.1f x %.1f", dpi[0], dpi[1])
    if not tex_top.exists():
        log.warning("texture_top.png отсутствует")
    if not tex_bot.exists():
        log.warning("texture_bottom.png отсутствует")

    log.info("Собираем GLB...")
    build_glb(prim_data, tex_top, tex_bot, side_color, output_path)

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
