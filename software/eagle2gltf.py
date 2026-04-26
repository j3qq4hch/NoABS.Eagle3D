"""
eagle2gltf1.py
--------------
Генерирует GLB модель платы с текстурами и компонентами из Eagle .brd файла.
Обёртка над generatePCB.py + addComponents.py с оригинальным API eagle2gltf.py.

Использование:
    python eagle2gltf1.py board.brd
    python eagle2gltf1.py board.brd -o board.glb --thickness 1.6
    python eagle2gltf1.py board.brd -c path/to/components/
    python eagle2gltf1.py board.brd --log eagle2gltf.log
"""

import sys
import shutil
import argparse
import logging
import time
from pathlib import Path

# ── Импортируем building blocks из generatePCB и addComponents ──
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from generatePCB import (
    load_brd,
    get_component_holes,
    get_colors,
    get_thickness,
    parse_color,
    extract_footprint_outline,
    chain_segments,
    classify_chains,
    build_board_mesh,
    process_textures,
    build_glb,
    DEFAULT_LAYER,
    DEFAULT_THICKNESS,
    DEFAULT_COLORS,
)
from addComponents import (
    read_brd as read_brd_components,
    build_glb_index,
    embed_components,
)

log = logging.getLogger("eagle2gltf1")


# ══════════════════════════════════════════════
#  Основной поток (API идентичен оригинальному eagle2gltf.py)
# ══════════════════════════════════════════════

def process(brd_path, output_path, thickness_override, layer,
            glb_dir=None, tex_dir_override=None, colors_override=None,
            min_priority=0):
    """
    Полный pipeline: BRD → GLB платы с текстурами и (опционально) компонентами.

    Аргументы:
        brd_path          — путь к .brd файлу
        output_path       — путь к выходному .glb
        thickness_override — толщина платы в мм (None = из BRD)
        layer             — слой контура платы (default: "20")
        glb_dir           — директория с GLB моделями компонентов (None = без компонентов)
        tex_dir_override  — директория с PNG текстурами (None = авто)
        colors_override   — dict цветов из CLI (substratecolor и др.) в виде (R,G,B,A);
                            перекрывает значения из BRD, DEFAULT_COLORS — fallback
    """
    brd_path    = Path(brd_path)
    output_path = Path(output_path)

    t0 = time.perf_counter()
    def ms():
        return int((time.perf_counter() - t0) * 1000)

    # Промежуточные файлы — в NoABS_tmp рядом с BRD
    work_dir       = brd_path.parent / "NoABS_tmp"
    board_glb_path = work_dir / f"{brd_path.stem}.glb"

    log.info("=== eagle2gltf1 ===")
    log.info("BRD:    %s", brd_path)
    log.info("Output: %s", output_path)

    # ── Шаг 1: геометрия платы ──
    root, wires, circles, holes = load_brd(brd_path, layer)
    comp_holes = get_component_holes(root)

    # Приоритет цветов: CLI > BRD > DEFAULT_COLORS
    brd_colors   = get_colors(root)
    final_colors = {**DEFAULT_COLORS, **brd_colors, **(colors_override or {})}
    side_color   = final_colors["substratecolor"]
    log.info("Цвет торца: RGBA%s", side_color)

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
        log.debug("Контуры из футпринтов: wire=%d, circle=%d",
                  len(fp_wires), len(fp_circles))
        wires   += fp_wires
        circles += fp_circles

    log.info("Контур: wire=%d, circle=%d, hole=%d, pad-hole=%d",
             len(wires), len(circles), len(holes), len(comp_holes))

    chains = chain_segments(wires)
    outer_chain, cutout_chains, open_chains = classify_chains(chains)
    if outer_chain is None:
        log.error("Внешний контур не найден - проверьте слой %s в BRD", layer)
        sys.exit(1)
    log.info("Контур: %d вырезов, %d незамкнутых цепочек",
             len(cutout_chains), len(open_chains))
    if open_chains:
        log.warning("Незамкнутые цепочки: %d шт.", len(open_chains))

    log.info("Триангулируем...")
    prim_data = build_board_mesh(
        outer_chain, cutout_chains, circles, holes, comp_holes, thickness
    )
    top_d, bot_d, side_d = prim_data
    log.info("Меш: top=%d/%d треуг, bot=%d/%d треуг, side=%d/%d треуг",
             len(top_d[0]) // 3,  len(top_d[3])  // 3,
             len(bot_d[0]) // 3,  len(bot_d[3])  // 3,
             len(side_d[0]) // 3, len(side_d[3]) // 3)
    log.info("[+%dms] контур + меш платы", ms())

    # ── Шаг 2: текстуры ──
    # По умолчанию ищем в <brd_dir>/<stem>_textures/ (оригинальное поведение eagle2gltf)
    # и в NoABS_tmp/<stem>_textures/ (поведение generatePCB)
    if tex_dir_override:
        tex_dir = Path(tex_dir_override)
    else:
        tex_dir = brd_path.parent / f"{brd_path.stem}_textures"
        if not tex_dir.exists():
            tex_dir = work_dir / f"{brd_path.stem}_textures"

    log.info("Ищем текстуры в: %s", tex_dir)

    tex_top = work_dir / "texture_top.png"
    tex_bot = work_dir / "texture_bottom.png"

    t_tex = time.perf_counter()
    if tex_dir.exists():
        ok = process_textures(brd_path, tex_dir, work_dir, colors_override=final_colors)
        if not ok:
            log.warning("Обработка текстур не удалась - GLB будет без текстур")
    else:
        log.warning("Директория текстур не найдена: %s - GLB без текстур", tex_dir)

    if not tex_top.exists():
        log.warning("texture_top.png отсутствует")
    if not tex_bot.exists():
        log.warning("texture_bottom.png отсутствует")
    log.info("[+%dms] текстуры (%dms)", ms(), int((time.perf_counter() - t_tex) * 1000))

    # ── Шаг 3: GLB платы (промежуточный) ──
    t_glb = time.perf_counter()
    log.info("Собираем GLB платы...")
    build_glb(prim_data, tex_top, tex_bot, side_color, board_glb_path)
    log.info("[+%dms] GLB платы (%dms)", ms(), int((time.perf_counter() - t_glb) * 1000))

    # ── Шаг 4: компоненты (если указаны) ──
    if glb_dir is not None:
        t_comp = time.perf_counter()
        log.info("Добавляем компоненты из: %s", glb_dir)
        placements, orientations, _ = read_brd_components(brd_path)
        glb_index = build_glb_index(Path(glb_dir))
        embed_components(
            board_glb_path, placements, orientations,
            glb_index, thickness, output_path,
            min_priority=min_priority,
        )
        log.info("[+%dms] компоненты (%dms)", ms(), int((time.perf_counter() - t_comp) * 1000))
    else:
        # Без компонентов — копируем промежуточный GLB в output_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if board_glb_path.resolve() != output_path.resolve():
            shutil.copy2(board_glb_path, output_path)
            log.debug("Скопирован GLB: %s -> %s", board_glb_path, output_path)

    log.info("[+%dms] ИТОГО", ms())
    log.info("Готово: %s", output_path.resolve())
    print(str(output_path.resolve()))


# ══════════════════════════════════════════════
#  CLI (идентичен оригинальному eagle2gltf.py)
# ══════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Eagle .brd -> GLB с текстурами")
    parser.add_argument("brd",
                        help="Путь к .brd файлу")
    parser.add_argument("--output", "-o", default=None,
                        help="Выходной .glb (default: <имя>.glb рядом с BRD)")
    parser.add_argument("--thickness", "-t", type=float, default=None,
                        help="Толщина платы в мм (default: вычисляется из BRD)")
    parser.add_argument("--layer", default=DEFAULT_LAYER,
                        help=f"Слой контура (default: {DEFAULT_LAYER})")
    parser.add_argument("--components", "-c", default=None,
                        help="Директория с GLB-файлами компонентов")
    parser.add_argument("--min-priority", type=int, default=0,
                        help="Минимальный Priority3d компонента для включения в модель (default: 0 — все)")
    parser.add_argument("--textures", default=None,
                        help="Директория с PNG текстурами (default: <brd_stem>_textures/)")
    parser.add_argument("--substrate-color", default=None, metavar="0xAARRGGBB",
                        help="Цвет торца/подложки платы (default: 0xFF786E46)")
    parser.add_argument("--copper-color", default=None, metavar="0xAARRGGBB",
                        help="Цвет меди (default: 0xFFC0C0C0)")
    parser.add_argument("--silkscreen-color", default=None, metavar="0xAARRGGBB",
                        help="Цвет шелкографии (default: 0xFFFFFFFF)")
    parser.add_argument("--soldermask-color", default=None, metavar="0xAARRGGBB",
                        help="Цвет паяльной маски (default: 0xFF008C4A)")
    parser.add_argument("--log", default=None,
                        help="Путь к лог-файлу (default: только stdout)")
    args = parser.parse_args()

    brd_path = Path(args.brd).resolve()

    # Настройка логирования
    handlers = [logging.StreamHandler(sys.stdout)]
    if args.log:
        log_path = Path(args.log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(str(log_path), encoding="utf-8", mode="w"))

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )

    if args.log:
        logging.info("eagle2gltf1 started")
        logging.info("args: %s", vars(args))

    if not brd_path.exists():
        log.error("Файл не найден: %s", brd_path)
        sys.exit(1)

    output           = Path(args.output).resolve() if args.output else brd_path.with_suffix(".glb")
    glb_dir          = Path(args.components).resolve() if args.components else None
    tex_dir_override = Path(args.textures).resolve() if args.textures else None

    cli_color_args = {
        "substratecolor":  args.substrate_color,
        "coppercolor":     args.copper_color,
        "silkscreencolor": args.silkscreen_color,
        "soldermaskcolor": args.soldermask_color,
    }
    colors_override = {
        name: parse_color(val)
        for name, val in cli_color_args.items()
        if val is not None
    }

    try:
        process(brd_path, output, args.thickness, args.layer, glb_dir, tex_dir_override,
                colors_override=colors_override or None,
                min_priority=args.min_priority)
    except Exception as e:
        import traceback
        log.error("FATAL ERROR: %s\n%s", e, traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
