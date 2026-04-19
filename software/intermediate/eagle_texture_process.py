"""
eagle_texture_process.py
------------------------
Обрабатывает текстуры печатной платы, экспортированные из Eagle,
и создаёт texture_top.png и texture_bottom.png.

Ожидаемая структура директорий:
  board.brd
  textures/
    outline.png
    top_copper.png, bottom_copper.png
    top_mask.png,   bottom_mask.png
    top_silk.png,   bottom_silk.png
    pads.png        (опционально)

Зависимости:
    pip install pillow numpy scipy

Использование:
    python eagle_texture_process.py board.brd
    python eagle_texture_process.py board.brd --textures path/to/textures
"""

import xml.etree.ElementTree as ET
import numpy as np
from PIL import Image
from pathlib import Path
from scipy import ndimage
import sys
import argparse


# ══════════════════════════════════════════════
#  Цвета из BRD
# ══════════════════════════════════════════════

def parse_color(hex_str: str) -> tuple[int, int, int, int]:
    """
    Парсит строку цвета Eagle вида "0xAARRGGBB" → (R, G, B, A).
    Формат: MSB→LSB = Alpha, Red, Green, Blue.
    """
    clean = hex_str.strip().lstrip("0x").lstrip("0X")
    val = int(clean, 16)
    a = (val >> 24) & 0xFF
    r = (val >> 16) & 0xFF
    g = (val >>  8) & 0xFF
    b =  val        & 0xFF
    return (r, g, b, a)


def read_colors(brd_path: Path) -> dict:
    """Читает цвета mfgpreviewcolor из BRD файла."""
    tree = ET.parse(brd_path)
    root = tree.getroot()
    colors = {}
    for el in root.iter("mfgpreviewcolor"):
        name  = el.get("name")
        color = el.get("color")
        if name and color:
            colors[name] = parse_color(color)
    return colors


# ══════════════════════════════════════════════
#  Анализ outline.png
# ══════════════════════════════════════════════

def analyze_outline(outline_path: Path, threshold: int = 128):
    """
    Анализирует outline.png и возвращает маски:
      board_mask  — пиксели основного тела платы (заливаем субстратом)
      hole_mask   — пиксели внутренних вырезов (не заливаем)
      full_board  — всё внутри и на границе платы (для обрезки текстур)

    Алгоритм:
      1. BFS от границы изображения — помечает «снаружи»
      2. Оставшиеся чёрные области — внутренние (тело платы и вырезы)
      3. Наибольшая связная внутренняя область = тело платы
    """
    img = Image.open(outline_path).convert("L")
    arr = np.array(img)
    h, w = arr.shape

    is_white = arr > threshold   # линии контура
    is_black = ~is_white

    # Flood-fill снаружи: добавляем 1-пиксельную рамку из черного и метим компонент
    padded = np.pad(is_black, 1, mode="constant", constant_values=True)
    labeled_all, _ = ndimage.label(padded)
    outside_label   = labeled_all[0, 0]       # угол гарантированно снаружи
    labeled_all     = labeled_all[1:-1, 1:-1]  # убираем рамку
    outside_mask    = labeled_all == outside_label

    # Внутренние чёрные области (тело платы и вырезы)
    interior = is_black & ~outside_mask

    if not interior.any():
        empty = np.zeros((h, w), bool)
        return empty, empty, ~outside_mask, h, w

    int_labeled, n_int = ndimage.label(interior)

    if n_int == 1:
        # Один регион — только тело платы, вырезов нет
        board_mask = interior
        hole_mask  = np.zeros((h, w), bool)
    else:
        # Несколько регионов: наибольший = тело платы, остальные = вырезы
        sizes      = [(int_labeled == i).sum() for i in range(1, n_int + 1)]
        main_lbl   = int(np.argmax(sizes)) + 1
        board_mask = int_labeled == main_lbl
        hole_mask  = interior & ~board_mask

    # full_board: всё что не снаружи (включает контурные линии, тело и вырезы)
    full_board = ~outside_mask

    return board_mask, hole_mask, full_board, h, w




# ══════════════════════════════════════════════
#  Bounding box контура
# ══════════════════════════════════════════════

def board_bbox(full_board: np.ndarray):
    """
    Вычисляет tight bounding box по маске full_board.
    Возвращает (row_min, row_max, col_min, col_max) — включительно.
    """
    rows = np.any(full_board, axis=1)
    cols = np.any(full_board, axis=0)
    row_min, row_max = np.where(rows)[0][[0, -1]]
    col_min, col_max = np.where(cols)[0][[0, -1]]
    return int(row_min), int(row_max), int(col_min), int(col_max)


def crop_rgba(arr: np.ndarray,
              row_min: int, row_max: int,
              col_min: int, col_max: int) -> np.ndarray:
    """Обрезает RGBA массив по bounding box."""
    return arr[row_min:row_max+1, col_min:col_max+1]

# ══════════════════════════════════════════════
#  Вспомогательные функции
# ══════════════════════════════════════════════

def load_gray(path: Path, target_h: int, target_w: int) -> np.ndarray:
    """Загружает PNG в grayscale, при необходимости ресайзит."""
    img = Image.open(path).convert("L")
    if img.size != (target_w, target_h):
        img = img.resize((target_w, target_h), Image.NEAREST)
    return np.array(img)


def make_rgba_layer(mask: np.ndarray, color: tuple[int,int,int,int],
                    h: int, w: int, alpha_override: int = None) -> np.ndarray:
    """Создаёт RGBA-массив: color там, где mask=True."""
    r, g, b, a = color
    if alpha_override is not None:
        a = alpha_override
    layer = np.zeros((h, w, 4), dtype=np.uint8)
    layer[mask, 0] = r
    layer[mask, 1] = g
    layer[mask, 2] = b
    layer[mask, 3] = a
    return layer


def to_pil(arr: np.ndarray) -> Image.Image:
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


def alpha_composite(base: np.ndarray, overlay: np.ndarray) -> np.ndarray:
    """Накладывает overlay поверх base с учётом альфа-канала."""
    base_img    = to_pil(base)
    overlay_img = to_pil(overlay)
    result      = Image.alpha_composite(base_img, overlay_img)
    return np.array(result)


# ══════════════════════════════════════════════
#  Обработка слоёв
# ══════════════════════════════════════════════

def extract_drill_holes(pads_arr: np.ndarray) -> np.ndarray:
    """
    Шаг 1: извлекает внутренние чёрные области из pads.png.
    pads.png содержит белые кольца с чёрными отверстиями внутри.
    Возвращает маску отверстий (True = дрилл-отверстие).
    """
    is_black = pads_arr <= 128

    # Flood-fill снаружи — внешний чёрный фон
    padded = np.pad(is_black, 1, mode="constant", constant_values=True)
    labeled, _ = ndimage.label(padded)
    outside_label = labeled[0, 0]
    labeled = labeled[1:-1, 1:-1]
    outside_mask = labeled == outside_label

    # Внутренние чёрные области = отверстия
    return is_black & ~outside_mask


def make_board_mask(board_mask: np.ndarray, hole_mask: np.ndarray,
                    drill_holes: np.ndarray) -> np.ndarray:
    """
    Шаг 2: строит итоговую маску платы.
    board_mask (тело платы) = True, вырезы и дрилл-отверстия = False.
    Используется как белая маска для обрезки всех остальных слоёв.
    """
    result = board_mask.copy()
    if hole_mask is not None:
        result &= ~hole_mask
    if drill_holes is not None:
        result &= ~drill_holes
    return result


def apply_mask(img_arr: np.ndarray, board_white: np.ndarray) -> np.ndarray:
    """
    Шаг 3: оставляет в img_arr только пиксели, где board_white=True.
    Возвращает grayscale-массив с нулями снаружи маски.
    """
    result = img_arr.copy()
    result[~board_white] = 0
    return result


def make_substrate(board_white: np.ndarray,
                   color: tuple, h: int, w: int) -> np.ndarray:
    """Outline-слой: substratecolor там где маска, прозрачно снаружи и в отверстиях."""
    return make_rgba_layer(board_white, color, h, w, alpha_override=color[3])


def make_copper(img_arr: np.ndarray, color: tuple, h: int, w: int) -> np.ndarray:
    """Шаги 4–5: белые пиксели → coppercolor. Маска уже применена."""
    return make_rgba_layer(img_arr > 128, color, h, w, alpha_override=255)


def make_silk(silk_arr: np.ndarray, mask_arr: np.ndarray,
              color: tuple, h: int, w: int) -> np.ndarray:
    """
    Шаги 6–7: удаляем перекрытия с mask_arr (белое в маске = вырез),
    белые пиксели → silkscreencolor. Маска платы уже применена.
    """
    is_silk      = silk_arr > 128
    is_mask_open = mask_arr > 128
    return make_rgba_layer(is_silk & ~is_mask_open, color, h, w, alpha_override=255)


def make_soldermask(img_arr: np.ndarray, board_white: np.ndarray,
                    color: tuple, h: int, w: int) -> np.ndarray:
    """
    Шаги 8–9:
    1. Всё кроме белых областей → soldermaskcolor
    2. Белые области → прозрачные
    3. Наложить board_white: оставить только то, что внутри платы
    """
    is_opening = img_arr > 128          # белое = вырез → прозрачный
    filled     = ~is_opening            # непрозрачная паяльная маска
    clipped    = filled & board_white   # обрезаем по контуру платы
    return make_rgba_layer(clipped, color, h, w, alpha_override=color[3])


# ══════════════════════════════════════════════
#  Основной поток
# ══════════════════════════════════════════════

REQUIRED_FILES = [
    "outline.png",
    "top_copper.png", "bottom_copper.png",
    "top_mask.png",   "bottom_mask.png",
    "top_silk.png",   "bottom_silk.png",
]

OPTIONAL_FILES = ["pads.png"]


def process(brd_path: Path, tex_dir: Path, output_dir: Path):
    # ── Цвета из BRD ──
    print(f"Читаем цвета из {brd_path.name}...")
    colors = read_colors(brd_path)
    substrate_color  = colors.get("substratecolor",  (120, 110, 70,  255))
    copper_color     = colors.get("coppercolor",      (255, 191,  0,  255))
    silkscreen_color = colors.get("silkscreencolor",  (255, 255, 255, 255))
    soldermask_color = colors.get("soldermaskcolor",  (  0, 128,  0,  200))
    print(f"  substrate:  RGBA{substrate_color}")
    print(f"  copper:     RGBA{copper_color}")
    print(f"  silkscreen: RGBA{silkscreen_color}")
    print(f"  soldermask: RGBA{soldermask_color}")

    # ── Проверка файлов ──
    missing = [f for f in REQUIRED_FILES if not (tex_dir / f).exists()]
    if missing:
        print(f"⚠  Отсутствуют файлы: {', '.join(missing)}")
        sys.exit(1)

    # ── Анализ outline.png ──
    print("Анализируем outline.png...")
    board_mask, hole_mask, full_board, H, W = analyze_outline(tex_dir / "outline.png")
    print(f"  Размер: {W}×{H} px, тело: {board_mask.sum()} px, вырезов: {hole_mask.sum()} px")

    # ── Шаг 1: pads.png → маска дрилл-отверстий ──
    drill_holes = None
    pads_path = tex_dir / "pads.png"
    if pads_path.exists():
        print("Шаг 1: извлекаем дрилл-отверстия из pads.png...")
        pads_arr    = load_gray(pads_path, H, W)
        drill_holes = extract_drill_holes(pads_arr)
        print(f"  Дрилл-отверстий: {drill_holes.sum()} пикселей")
    else:
        print("Шаг 1: pads.png не найден — пропускаем")

    # ── Шаг 2: итоговая маска платы (белая область) ──
    # board_mask (тело) минус hole_mask (вырезы) минус drill_holes (отверстия)
    print("Шаг 2: строим маску платы...")
    board_white = make_board_mask(board_mask, hole_mask, drill_holes)

    # Bounding box для кропа
    r0, r1, c0, c1 = board_bbox(board_white)
    print(f"  Bounding box: ({c0},{r0})–({c1},{r1}), размер {c1-c0+1}×{r1-r0+1} px")

    # ── Шаг 3: применяем маску ко всем файлам КРОМЕ mask-слоёв ──
    print("Шаг 3: применяем маску к copper и silk слоям...")
    def load_masked(fname):
        return apply_mask(load_gray(tex_dir / fname, H, W), board_white)
    def load_raw(fname):
        return load_gray(tex_dir / fname, H, W)

    top_copper_raw = load_masked("top_copper.png")
    bot_copper_raw = load_masked("bottom_copper.png")
    top_silk_raw   = load_masked("top_silk.png")
    bot_silk_raw   = load_masked("bottom_silk.png")
    # mask-файлы загружаем без маски — она применяется в шагах 8–9
    top_mask_raw   = load_raw("top_mask.png")
    bot_mask_raw   = load_raw("bottom_mask.png")

    # ── Шаги 4–5: медь ──
    print("Шаги 4–5: медные слои...")
    top_copper_layer = make_copper(top_copper_raw, copper_color, H, W)
    bot_copper_layer = make_copper(bot_copper_raw, copper_color, H, W)

    # ── Шаги 6–7: шелкография (маскируется паяльной маской) ──
    print("Шаги 6–7: шелкография...")
    top_silk_layer = make_silk(top_silk_raw, top_mask_raw, silkscreen_color, H, W)
    bot_silk_layer = make_silk(bot_silk_raw, bot_mask_raw, silkscreen_color, H, W)

    # ── Шаги 8–9: паяльная маска ──
    # Порядок: заливка soldermaskcolor → вырезка белых → наложение board_white
    print("Шаги 8–9: паяльные маски...")
    top_mask_layer = make_soldermask(top_mask_raw, board_white, soldermask_color, H, W)
    bot_mask_layer = make_soldermask(bot_mask_raw, board_white, soldermask_color, H, W)

    # ── Шаги 10–11: подготовка outline-слоя + компоновка ──
    # Outline: не белые области → прозрачные, белые → substratecolor
    print("Шаги 10–11: подготовка outline-слоя и компоновка...")
    outline_layer = make_substrate(board_white, substrate_color, H, W)
    tex_top = outline_layer.copy()
    for layer in (top_copper_layer, top_mask_layer, top_silk_layer):
        tex_top = alpha_composite(tex_top, layer)

    tex_bot = outline_layer.copy()
    for layer in (bot_copper_layer, bot_mask_layer, bot_silk_layer):
        tex_bot = alpha_composite(tex_bot, layer)

    # ── Обрезка по bounding box ──
    tex_top = crop_rgba(tex_top, r0, r1, c0, c1)
    tex_bot = crop_rgba(tex_bot, r0, r1, c0, c1)

    # ── Сохранение ──
    source_dpi = None
    for fname in REQUIRED_FILES:
        try:
            info = Image.open(tex_dir / fname).info
            source_dpi = info.get("dpi")
            if source_dpi:
                print(f"  DPI: {source_dpi[0]:.0f} (из {fname})")
                break
        except Exception:
            continue

    save_kwargs = {"dpi": source_dpi} if source_dpi else {}
    out_top = output_dir / "texture_top.png"
    out_bot = output_dir / "texture_bottom.png"
    to_pil(tex_top).save(str(out_top), **save_kwargs)
    to_pil(tex_bot).save(str(out_bot), **save_kwargs)
    print(f"✓  {out_top}")
    print(f"✓  {out_bot}")


# ══════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Eagle BRD текстуры → texture_top.png + texture_bottom.png"
    )
    parser.add_argument("brd", help="Путь к .brd файлу")
    parser.add_argument(
        "--textures", "-t", default=None,
        help="Директория с текстурами (default: <brd_dir>/textures)"
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="Директория для результата (default: директория brd файла)"
    )
    args = parser.parse_args()

    brd_path = Path(args.brd).resolve()

    if not brd_path.exists():
        print(f"Файл не найден: {brd_path}")
        sys.exit(1)

    if brd_path.is_dir():
        print(f"Ожидается путь к .brd файлу, а не директория: {brd_path}")
        sys.exit(1)

    tex_dir    = Path(args.textures).resolve() if args.textures else brd_path.parent / f"{brd_path.stem}_textures"
    output_dir = Path(args.output).resolve()   if args.output   else tex_dir

    if not tex_dir.exists():
        print(f"Директория текстур не найдена: {tex_dir}")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)
    process(brd_path, tex_dir, output_dir)


if __name__ == "__main__":
    main()