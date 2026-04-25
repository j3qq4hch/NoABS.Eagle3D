"""
addComponents.py
----------------
Добавляет 3D модели компонентов в GLB модель платы (результат generatePCB.py).

Для каждого элемента из BRD ищет GLB файл с именем пакета в папке компонентов.
Позиционирование берётся из координат элемента в BRD и метаданных <!--3d:{...}-->
в поле description соответствующего пакета.

Использование:
    python addComponents.py board.brd board.glb components_dir/
    python addComponents.py board.brd board.glb components_dir/ --output result.glb
"""

import xml.etree.ElementTree as ET
import math
import re
import json
import struct
import sys
import argparse
import logging
from pathlib import Path

# ══════════════════════════════════════════════
#  Логирование
# ══════════════════════════════════════════════

log = logging.getLogger("addComponents")


def setup_logging():
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


# ══════════════════════════════════════════════
#  Парсинг BRD
# ══════════════════════════════════════════════

_RE_3D = re.compile(r'<!--3d:(\{.*?\})-->', re.DOTALL)


def _parse_rot(s):
    """Разбирает строку поворота Eagle: 'R270', 'MR90', '' → (angle_deg, mirror)."""
    if not s:
        return 0.0, False
    mirror = s.startswith("M")
    num = s.lstrip("MR")
    return (float(num) if num else 0.0), mirror


def read_brd(brd_path: Path):
    """
    Читает BRD файл и возвращает:
      placements  — список dict {name, library, package, x, y, angle, mirror}
      orientations — dict {(library, package): {tx,ty,tz,rx,ry,rz}}
      thickness   — толщина платы в мм (float)
    """
    log.debug("Парсим BRD: %s", brd_path)
    tree = ET.parse(brd_path)
    root = tree.getroot()

    # ── Ориентации 3D-моделей из description пакетов ──
    # Ключ: (library_name, package_name) — чтобы разные библиотеки с одинаковым
    # именем пакета не перезаписывали друг друга.
    orientations = {}
    for lib in root.iter("library"):
        lib_name = lib.get("name", "")
        for pkg in lib.iter("package"):
            pkg_name = pkg.get("name", "")
            desc_el = pkg.find("description")
            if desc_el is None or not desc_el.text:
                continue
            m = _RE_3D.search(desc_el.text)
            if not m:
                continue
            try:
                data = json.loads(m.group(1))
                orientations[(lib_name, pkg_name)] = {
                    "tx": float(data.get("tx", 0)),
                    "ty": float(data.get("ty", 0)),
                    "tz": float(data.get("tz", 0)),
                    "rx": float(data.get("rx", 0)),
                    "ry": float(data.get("ry", 0)),
                    "rz": float(data.get("rz", 0)),
                }
            except (ValueError, KeyError) as e:
                log.warning("Не удалось разобрать 3D метаданные %s/%s: %s",
                            lib_name, pkg_name, e)

    log.info("Пакетов с 3D метаданными: %d", len(orientations))

    # ── Размещения элементов ──
    placements = []
    for el in root.iter("element"):
        angle, mirror = _parse_rot(el.get("rot", ""))
        priority = 0
        for attr in el.findall("attribute"):
            if attr.get("name", "").lower() == "priority3d":
                try:
                    priority = int(attr.get("value", "0"))
                except (ValueError, TypeError):
                    pass  # нечисловое значение — игнорируем, оставляем 0
                break
        placements.append({
            "name":     el.get("name", ""),
            "library":  el.get("library", ""),
            "package":  el.get("package", ""),
            "x":        float(el.get("x", 0)),
            "y":        float(el.get("y", 0)),
            "angle":    angle,
            "mirror":   mirror,
            "priority": priority,
        })

    log.info("Элементов в BRD: %d", len(placements))

    # ── Толщина платы из стека слоёв ──
    thickness = _read_thickness(root)
    if thickness is None:
        thickness = 1.6
        log.warning("Толщина из BRD не определена, используем %.1f мм", thickness)
    else:
        log.info("Толщина платы: %.3f мм", thickness)

    return placements, orientations, thickness


def _read_thickness(root) -> float | None:
    params = {}
    for p in root.iter("param"):
        n = p.get("name")
        if n in ("layerSetup", "mtCopper", "mtIsolate"):
            params[n] = p.get("value", "")
    if len(params) < 3:
        return None

    def parse_setup(expr):
        return [int(x) for x in re.findall(r'\d+', expr)]

    def parse_mm_array(s):
        vals = [None]
        for t in s.strip().split():
            vals.append(float(t.replace("mm", "").replace(",", ".")))
        return vals

    idx = parse_setup(params["layerSetup"])
    if not idx:
        return None
    mc = parse_mm_array(params["mtCopper"])
    mi = parse_mm_array(params["mtIsolate"])
    try:
        return sum(mc[i] for i in idx) + sum(mi[i] for i in idx[:-1])
    except IndexError:
        return None


# ══════════════════════════════════════════════
#  Поиск GLB компонентов
# ══════════════════════════════════════════════

def build_glb_index(components_dir: Path) -> dict:
    """Строит индекс GLB файлов: нижний регистр имени → Path."""
    index = {}
    if not components_dir.exists():
        log.warning("Папка компонентов не найдена: %s", components_dir)
        return index
    for f in components_dir.glob("*.glb"):
        index[f.stem.lower()] = f
    log.info("GLB файлов в папке компонентов: %d", len(index))
    return index


# ══════════════════════════════════════════════
#  Матрица размещения компонента
# ══════════════════════════════════════════════

def _mat4_mul(A, B):
    return [
        [sum(A[i][k] * B[k][j] for k in range(4)) for j in range(4)]
        for i in range(4)
    ]


def _mat4_eye():
    return [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]


def _rot_xyz(rx_deg, ry_deg, rz_deg):
    """Euler XYZ intrinsic (градусы) → 4x4 матрица поворота."""
    rx, ry, rz = map(math.radians, [rx_deg, ry_deg, rz_deg])
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    return [
        [ cy*cz,               -cy*sz,              sy,    0],
        [ sx*sy*cz + cx*sz,  -sx*sy*sz + cx*cz,  -sx*cy,  0],
        [-cx*sy*cz + sx*sz,   cx*sy*sz + sx*cz,   cx*cy,  0],
        [ 0,                   0,                  0,      1],
    ]


def _trans(tx, ty, tz):
    m = _mat4_eye()
    m[0][3], m[1][3], m[2][3] = tx, ty, tz
    return m


def _rot_z(deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return [[c, -s, 0, 0], [s, c, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


def _mirror_xz():
    m = _mat4_eye()
    m[0][0] = -1.0
    m[2][2] = -1.0
    return m


_RX_NEG90 = [
    [1,  0,  0,  0],
    [0,  0,  1,  0],
    [0, -1,  0,  0],
    [0,  0,  0,  1],
]

_MM_TO_M = 0.001


def compute_placement_matrix(placement: dict, orientation: dict,
                              board_thickness: float) -> list:
    """
    Вычисляет column-major матрицу 4x4 (glTF формат) для компонента
    в мировых координатах glTF (метры, Y-up).
    """
    o = orientation
    p = placement

    M_orient = _mat4_mul(
        _trans(o["tx"] * _MM_TO_M, o["ty"] * _MM_TO_M, o["tz"] * _MM_TO_M),
        _rot_xyz(o["rx"], o["ry"], o["rz"])
    )

    if p["mirror"]:
        M_eagle = _mat4_mul(
            _trans(p["x"] * _MM_TO_M, p["y"] * _MM_TO_M, 0.0),
            _mat4_mul(_rot_z(-p["angle"]), _mat4_mul(_mirror_xz(), M_orient))
        )
    else:
        M_eagle = _mat4_mul(
            _trans(p["x"] * _MM_TO_M, p["y"] * _MM_TO_M, board_thickness * _MM_TO_M),
            _mat4_mul(_rot_z(p["angle"]), M_orient)
        )

    M = _mat4_mul(_RX_NEG90, M_eagle)

    # glTF ожидает column-major: column j = [M[0][j], M[1][j], M[2][j], M[3][j]]
    return [M[i][j] for j in range(4) for i in range(4)]


# ══════════════════════════════════════════════
#  GLB парсинг / запись
# ══════════════════════════════════════════════

def parse_glb(data: bytes) -> tuple[dict, bytes]:
    """Разбирает GLB binary → (gltf_dict, bin_bytes)."""
    if len(data) < 20:
        raise ValueError("Файл слишком короткий для GLB")
    magic, version, _ = struct.unpack_from("<III", data, 0)
    if magic != 0x46546C67:
        raise ValueError("Не GLB файл (неверный magic)")
    json_len = struct.unpack_from("<I", data, 12)[0]
    gltf = json.loads(data[20:20 + json_len].decode("utf-8"))
    bin_start = 20 + json_len + 8
    bin_bytes = bytes(data[bin_start:]) if bin_start < len(data) else b""
    return gltf, bin_bytes


def _pad4(data: bytes, fill: bytes = b'\x00') -> bytes:
    r = len(data) % 4
    return data + fill * (4 - r) if r else data


def write_glb(gltf: dict, bin_data: bytes, path: Path):
    """Записывает GLB файл."""
    json_bytes = _pad4(json.dumps(gltf, separators=(",", ":")).encode("utf-8"), b' ')
    bin_bytes  = _pad4(bin_data)

    json_chunk = struct.pack("<II", len(json_bytes), 0x4E4F534A) + json_bytes
    bin_chunk  = struct.pack("<II", len(bin_bytes),  0x004E4942) + bin_bytes
    total_len  = 12 + len(json_chunk) + len(bin_chunk)
    header     = struct.pack("<III", 0x46546C67, 2, total_len)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(header + json_chunk + bin_chunk)

    log.info("GLB записан: %s (%.1f КБ)", path, total_len / 1024)


# ══════════════════════════════════════════════
#  Встраивание компонентов в GLB
# ══════════════════════════════════════════════

def _ensure_list(gltf: dict, key: str) -> list:
    if key not in gltf or gltf[key] is None:
        gltf[key] = []
    return gltf[key]


def embed_components(board_glb_path: Path, placements: list, orientations: dict,
                     glb_index: dict, board_thickness: float, output_path: Path,
                     min_priority: int = 0):
    """
    Встраивает компоненты в GLB платы.
    Компоненты добавляются как дочерние узлы корневого узла платы,
    чтобы наследовать его scale/rotation (мм → метры, ориентация).

    min_priority — минимальный Priority3d для включения компонента в модель.
    """
    if min_priority > 0:
        before = len(placements)
        placements = [p for p in placements if p["priority"] >= min_priority]
        log.info("Фильтр по Priority3d >= %d: %d из %d компонентов",
                 min_priority, len(placements), before)

    log.info("Читаем GLB платы: %s", board_glb_path)
    board_data = board_glb_path.read_bytes()
    gltf, bin_raw = parse_glb(board_data)
    bin_data = bytearray(bin_raw)

    # Гарантируем наличие всех нужных списков
    for key in ("bufferViews", "accessors", "materials", "meshes",
                "nodes", "textures", "images", "samplers"):
        _ensure_list(gltf, key)

    # pkg_geom хранит данные геометрии пакета (добавляется один раз):
    #   mesh_base      — индекс первого mesh в объединённом gltf
    #   comp_nodes     — список узлов из GLB компонента (для клонирования)
    #   comp_scene_roots — индексы корневых узлов сцены компонента
    # None означает ошибку загрузки.
    pkg_geom: dict[str, dict | None] = {}

    new_node_indices: list[int] = []

    missing_glb:    set[str] = set()
    missing_orient: set[str] = set()
    embedded_pkgs:  set[str] = set()
    placed_count = 0

    for p in placements:
        pkg = p["package"]

        # Ищем GLB
        glb_path = glb_index.get(pkg.lower())
        if glb_path is None:
            missing_glb.add(pkg)
            continue

        # ── Геометрия пакета: meshes + materials + textures/images добавляются один раз ──
        if pkg not in pkg_geom:
            log.debug("Встраиваем геометрию пакета: %s (%s)", pkg, glb_path.name)
            try:
                comp_gltf, comp_bin = parse_glb(glb_path.read_bytes())
            except Exception as e:
                log.error("Не удалось прочитать GLB %s: %s", glb_path.name, e)
                pkg_geom[pkg] = None
                continue

            bin_offset = len(bin_data)
            bv_base    = len(gltf["bufferViews"])
            acc_base   = len(gltf["accessors"])
            mat_base   = len(gltf["materials"])
            tex_base   = len(gltf["textures"])
            img_base   = len(gltf["images"])
            mesh_base  = len(gltf["meshes"])

            # Бинарные данные
            bin_data.extend(comp_bin)
            while len(bin_data) % 4:
                bin_data.append(0)

            # BufferViews
            for bv in (comp_gltf.get("bufferViews") or []):
                nb = dict(bv)
                nb["buffer"]     = 0
                nb["byteOffset"] = bin_offset + bv.get("byteOffset", 0)
                gltf["bufferViews"].append(nb)

            # Accessors
            for acc in (comp_gltf.get("accessors") or []):
                na = dict(acc)
                na["bufferView"] = acc["bufferView"] + bv_base
                gltf["accessors"].append(na)

            # Images
            for img in (comp_gltf.get("images") or []):
                ni = dict(img)
                if "bufferView" in ni:
                    ni["bufferView"] = img["bufferView"] + bv_base
                gltf["images"].append(ni)

            # Textures
            for tex in (comp_gltf.get("textures") or []):
                nt = dict(tex)
                if "source" in nt:
                    nt["source"] = tex["source"] + img_base
                gltf["textures"].append(nt)

            # Materials
            for mat in (comp_gltf.get("materials") or []):
                gltf["materials"].append(_remap_material(mat, tex_base))

            # Все meshes компонента (не только первый)
            for mesh in (comp_gltf.get("meshes") or []):
                remapped_prims = []
                for prim in (mesh.get("primitives") or []):
                    rp = dict(prim)
                    rp["attributes"] = {k: v + acc_base for k, v in prim["attributes"].items()}
                    if "indices"  in prim: rp["indices"]  = prim["indices"]  + acc_base
                    if "material" in prim: rp["material"] = prim["material"] + mat_base
                    remapped_prims.append(rp)
                gltf["meshes"].append({"name": mesh.get("name", pkg), "primitives": remapped_prims})

            comp_scene_roots = list((comp_gltf.get("scenes") or [{}])[0].get("nodes") or [])
            pkg_geom[pkg] = {
                "mesh_base":        mesh_base,
                "comp_nodes":       comp_gltf.get("nodes") or [],
                "comp_scene_roots": comp_scene_roots,
            }
            embedded_pkgs.add(pkg)
            log.debug("  meshes=%d, nodes=%d, scene_roots=%s",
                      len(comp_gltf.get("meshes") or []),
                      len(comp_gltf.get("nodes")  or []),
                      comp_scene_roots)

        geom = pkg_geom[pkg]
        if geom is None:
            continue  # ошибка загрузки

        # Ориентация из description — ищем по (library, package), затем по одному package
        orient = orientations.get((p["library"], p["package"])) \
                 or orientations.get(("", p["package"]))
        if orient is None:
            missing_orient.add(p["package"])
            orient = {"tx": 0.0, "ty": 0.0, "tz": 0.0,
                      "rx": 0.0, "ry": 0.0, "rz": 0.0}

        matrix = compute_placement_matrix(p, orient, board_thickness)

        # ── Клонируем иерархию узлов компонента для этого экземпляра ──
        # Узлы компонента копируются (каждый экземпляр — свои узлы),
        # но ссылаются на общие meshes (instancing по mesh index).
        instance_node_base = len(gltf["nodes"])
        mesh_base = geom["mesh_base"]

        for cn in geom["comp_nodes"]:
            new_node: dict = {}
            if "name" in cn:
                new_node["name"] = cn["name"]
            if "mesh" in cn:
                new_node["mesh"] = cn["mesh"] + mesh_base
            if "children" in cn:
                new_node["children"] = [c + instance_node_base for c in cn["children"]]
            # Сохраняем локальную трансформацию узла компонента (если есть)
            for key in ("matrix", "translation", "rotation", "scale"):
                if key in cn:
                    new_node[key] = cn[key]
            gltf["nodes"].append(new_node)

        # Узел-обёртка с матрицей размещения; его дети — корневые узлы компонента
        comp_roots_remapped = [r + instance_node_base for r in geom["comp_scene_roots"]]
        placement_node_idx = len(gltf["nodes"])
        gltf["nodes"].append({
            "name":     p["name"],
            "matrix":   matrix,
            "children": comp_roots_remapped,
        })
        new_node_indices.append(placement_node_idx)
        placed_count += 1

    # Логируем итоги поиска
    if missing_glb:
        log.warning("GLB не найден для пакетов (%d): %s",
                    len(missing_glb), ", ".join(sorted(missing_glb)))
    if missing_orient:
        log.warning("3D метаданные не заданы для пакетов (%d): %s",
                    len(missing_orient), ", ".join(sorted(missing_orient)))
    log.info("Уникальных пакетов встроено: %d", len(embedded_pkgs))
    log.info("Узлов компонентов добавлено: %d", placed_count)

    # Добавляем компоненты напрямую в сцену (не дети board-ноды).
    # Матрица каждого компонента уже включает мм→м и ротацию координатной системы,
    # поэтому они не должны наследовать scale/rotation board-ноды.
    if new_node_indices:
        gltf["scenes"][0]["nodes"].extend(new_node_indices)

    # Обновляем размер буфера
    gltf["buffers"] = [{"byteLength": len(bin_data)}]

    write_glb(gltf, bytes(bin_data), output_path)
    log.info("Выходной файл: %s", output_path.resolve())


def _remap_material(mat: dict, tex_base: int) -> dict:
    """Создаёт копию материала с перемаппленными индексами текстур."""
    nm = dict(mat)
    pbr = nm.get("pbrMetallicRoughness")
    if pbr:
        pbr = dict(pbr)
        for tex_field in ("baseColorTexture", "metallicRoughnessTexture"):
            if tex_field in pbr:
                entry = dict(pbr[tex_field])
                entry["index"] = entry["index"] + tex_base
                pbr[tex_field] = entry
        nm["pbrMetallicRoughness"] = pbr
    for tex_field in ("normalTexture", "occlusionTexture", "emissiveTexture"):
        if tex_field in nm:
            entry = dict(nm[tex_field])
            entry["index"] = entry["index"] + tex_base
            nm[tex_field] = entry
    return nm


# ══════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════

def main():
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Добавляет GLB модели компонентов в модель платы"
    )
    parser.add_argument("brd",        help="Путь к .brd файлу")
    parser.add_argument("board_glb",  help="GLB модель платы (из generatePCB.py)")
    parser.add_argument("components", help="Папка с GLB моделями компонентов")
    parser.add_argument("--output", "-o", default=None,
                        help="Выходной GLB (default: <board_glb_stem>_assembled.glb)")
    args = parser.parse_args()

    brd_path       = Path(args.brd).resolve()
    board_glb_path = Path(args.board_glb).resolve()
    components_dir = Path(args.components).resolve()

    if not brd_path.exists():
        log.error("BRD файл не найден: %s", brd_path); sys.exit(1)
    if not board_glb_path.exists():
        log.error("GLB платы не найден: %s", board_glb_path); sys.exit(1)

    if args.output:
        output_path = Path(args.output).resolve()
    else:
        output_path = board_glb_path.parent / f"{board_glb_path.stem}_assembled.glb"

    log.info("=== addComponents ===")
    log.info("BRD:        %s", brd_path)
    log.info("Board GLB:  %s", board_glb_path)
    log.info("Components: %s", components_dir)
    log.info("Output:     %s", output_path)

    try:
        placements, orientations, thickness = read_brd(brd_path)
        glb_index = build_glb_index(components_dir)
        embed_components(board_glb_path, placements, orientations,
                         glb_index, thickness, output_path)
        print(str(output_path))
    except Exception as e:
        import traceback
        log.error("FATAL: %s\n%s", e, traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
