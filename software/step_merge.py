"""
step_merge.py
-------------
Pure-Python STEP AP214 assembler.

Merges a board STEP with colored component instances by:
  - Parsing all STEP entities as text
  - Renumbering component entities to avoid ID conflicts
  - Adding NEXT_ASSEMBLY_USAGE_OCCURRENCE + ITEM_DEFINED_TRANSFORMATION
    for each instance (the standard OCCT/AP214 assembly structure)

Colors are preserved because STYLED_ITEM entities travel with the
component geometry through renumbering — no BRep extraction involved.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── STEP text parsing ──────────────────────────────────────────────────────────

def _parse_entities(text: str) -> Dict[int, str]:
    """Return {entity_id: definition_string} from a STEP DATA section."""
    try:
        start = text.index('DATA;') + 5
        end   = text.index('ENDSEC;', start)
    except ValueError:
        return {}

    entities: Dict[int, str] = {}
    cur_id: Optional[int] = None
    cur_buf: List[str] = []

    for raw in text[start:end].split('\n'):
        line = raw.strip()
        if not line or line.startswith('/*'):
            continue

        m = re.match(r'^#(\d+)\s*=\s*(.*)', line)
        if m:
            if cur_id is not None:
                defn = ' '.join(cur_buf)
                entities[cur_id] = (defn[:-1] if defn.endswith(';') else defn).strip()
                cur_buf = []
            cur_id = int(m.group(1))
            rest = m.group(2).strip()
            if rest.endswith(';'):
                entities[cur_id] = rest[:-1].strip()
                cur_id = None
            else:
                cur_buf = [rest] if rest else []
        elif cur_id is not None:
            if line.endswith(';'):
                cur_buf.append(line[:-1].strip())
                entities[cur_id] = ' '.join(cur_buf).strip()
                cur_id = None
                cur_buf = []
            else:
                cur_buf.append(line)

    if cur_id is not None and cur_buf:
        defn = ' '.join(cur_buf)
        entities[cur_id] = (defn[:-1] if defn.endswith(';') else defn).strip()

    return entities


def _extract_header(text: str) -> str:
    try:
        return text[:text.index('DATA;')]
    except ValueError:
        return 'ISO-10303-21;\nHEADER;\n...\nENDSEC;\n'


# ── Entity manipulation ────────────────────────────────────────────────────────

def _renumber(entities: Dict[int, str], offset: int) -> Dict[int, str]:
    """Add offset to every entity ID and every #N reference inside definitions."""
    result: Dict[int, str] = {}
    for eid, defn in entities.items():
        new_defn = re.sub(
            r'#(\d+)',
            lambda m: f'#{int(m.group(1)) + offset}',
            defn,
        )
        result[eid + offset] = new_defn
    return result


def _entity_type(defn: str) -> str:
    """Return the entity type name, ignoring spaces before '('."""
    return defn.split('(')[0].strip().upper()


def _find_root(entities: Dict[int, str]) -> Tuple[Optional[int], Optional[int]]:
    """
    Find (product_definition_id, shape_representation_id) from
    SHAPE_DEFINITION_REPRESENTATION. Prefers plain SHAPE_REPRESENTATION;
    falls back to ADVANCED_BREP_SHAPE_REPRESENTATION for files that lack
    an assembly wrapper (handles spaces before parentheses too).
    """
    sdr_candidates = []
    for eid in sorted(entities):
        defn = entities[eid]
        if _entity_type(defn) != 'SHAPE_DEFINITION_REPRESENTATION':
            continue
        refs = [int(x) for x in re.findall(r'#(\d+)', defn)]
        if len(refs) < 2:
            continue
        sdr_candidates.append((refs[0], refs[1]))

    def _pick_best(candidates_by_type):
        """Among SDR candidates whose SR matches the given types, pick the one
        whose SR has the most #N references (= top-level assembly SR)."""
        best_pd, best_sr, best_n = None, None, -1
        for pds_id, sr_id in sdr_candidates:
            if _entity_type(entities.get(sr_id, '')) not in candidates_by_type:
                continue
            pds_defn = entities.get(pds_id, '')
            pd_refs = [int(x) for x in re.findall(r'#(\d+)', pds_defn)]
            if not pd_refs:
                continue
            n = len(re.findall(r'#\d+', entities.get(sr_id, '')))
            if n > best_n:
                best_pd, best_sr, best_n = pd_refs[-1], sr_id, n
        return best_pd, best_sr

    # Pass 1: prefer plain SHAPE_REPRESENTATION (picks top-level for assemblies)
    pd_id, sr_id = _pick_best({'SHAPE_REPRESENTATION'})
    if pd_id is not None:
        return pd_id, sr_id

    # Pass 2: accept ADVANCED_BREP_SHAPE_REPRESENTATION directly
    pd_id, sr_id = _pick_best({'ADVANCED_BREP_SHAPE_REPRESENTATION'})
    if pd_id is not None:
        return pd_id, sr_id

    return None, None


# ── STEP geometry formatting ───────────────────────────────────────────────────

def _fmt_pt(v) -> str:
    return f'({v[0]:.6f},{v[1]:.6f},{v[2]:.6f})'


def _fmt_dir(v) -> str:
    return f'({v[0]:.10f},{v[1]:.10f},{v[2]:.10f})'


# ── Color injection ───────────────────────────────────────────────────────────

def add_step_color(step_path: Path, r: float, g: float, b: float) -> None:
    """
    Inject COLOUR_RGB + STYLED_ITEM entities into a STEP file in-place.
    Attaches the color to every MANIFOLD_SOLID_BREP found in the file.
    """
    text = step_path.read_text(encoding='utf-8', errors='replace')
    entities = _parse_entities(text)
    if not entities:
        return

    solid_ids = [eid for eid, defn in entities.items()
                 if defn.startswith('MANIFOLD_SOLID_BREP(')]
    if not solid_ids:
        return

    max_id = max(entities)
    n = max_id + 1

    entities[n]   = f"COLOUR_RGB('',{r:.6f},{g:.6f},{b:.6f})"
    entities[n+1] = f"FILL_AREA_STYLE_COLOUR('',#{n})"
    entities[n+2] = f"FILL_AREA_STYLE('', (#{n+1}))"
    entities[n+3] = f"SURFACE_STYLE_FILL_AREA(#{n+2})"
    entities[n+4] = f"SURFACE_SIDE_STYLE('', (#{n+3}))"
    entities[n+5] = f"SURFACE_STYLE_USAGE(.BOTH.,#{n+4})"
    entities[n+6] = f"PRESENTATION_STYLE_ASSIGNMENT((#{n+5}))"

    next_id = n + 7
    for solid_id in solid_ids:
        entities[next_id] = f"STYLED_ITEM('color',(#{n+6}),#{solid_id})"
        next_id += 1

    header = _extract_header(text)
    lines = [header.rstrip(), 'DATA;']
    for eid in sorted(entities):
        lines.append(f'#{eid} = {entities[eid]};')
    lines.append('ENDSEC;')
    lines.append('END-ISO-10303-21;')
    step_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


# ── Main assembler ─────────────────────────────────────────────────────────────

def merge_step_assembly(
    board_step: Path,
    instances: List[Tuple[Path, list]],
    out_path: Path,
) -> None:
    """
    Write a STEP AP214 assembly to out_path.

    Parameters
    ----------
    board_step : Path
        STEP file of the board (generated by eagle2step.py).
    instances : list of (step_file, M)
        step_file — STEP file of the component (with colors).
        M        — 4×4 placement matrix as list-of-lists (board coordinate system).
    out_path : Path
        Output assembly STEP file.
    """
    board_text     = board_step.read_text(encoding='utf-8', errors='replace')
    board_entities = _parse_entities(board_text)
    board_header   = _extract_header(board_text)

    asm_pd_id, asm_sr_id = _find_root(board_entities)
    if asm_pd_id is None:
        raise RuntimeError(
            f"Не найден корневой PRODUCT_DEFINITION в {board_step}"
        )

    all_entities: Dict[int, str] = dict(board_entities)
    max_id = max(all_entities)

    for inst_idx, (step_file, M) in enumerate(instances):
        comp_text     = step_file.read_text(encoding='utf-8', errors='replace')
        comp_entities = _parse_entities(comp_text)

        comp_pd_id, comp_sr_id = _find_root(comp_entities)
        if comp_pd_id is None:
            print(f"  !  Пропуск {step_file.name}: корень не найден")
            continue

        offset = max_id
        all_entities.update(_renumber(comp_entities, offset))
        max_id = max(all_entities)

        comp_pd_g = comp_pd_id + offset
        comp_sr_g = comp_sr_id + offset

        # Decompose placement matrix
        t     = [M[i][3] for i in range(3)]
        z_dir = [M[i][2] for i in range(3)]
        x_dir = [M[i][0] for i in range(3)]

        n = max_id + 1

        # Source placement (identity — component at its own origin)
        all_entities[n]   = f"CARTESIAN_POINT('',{_fmt_pt((0., 0., 0.))})"
        all_entities[n+1] = f"DIRECTION('',{_fmt_dir((0., 0., 1.))})"
        all_entities[n+2] = f"DIRECTION('',{_fmt_dir((1., 0., 0.))})"
        all_entities[n+3] = f"AXIS2_PLACEMENT_3D('',#{n},#{n+1},#{n+2})"

        # Target placement
        all_entities[n+4] = f"CARTESIAN_POINT('',{_fmt_pt(t)})"
        all_entities[n+5] = f"DIRECTION('',{_fmt_dir(z_dir)})"
        all_entities[n+6] = f"DIRECTION('',{_fmt_dir(x_dir)})"
        all_entities[n+7] = f"AXIS2_PLACEMENT_3D('inst{inst_idx}',#{n+4},#{n+5},#{n+6})"

        # ITEM_DEFINED_TRANSFORMATION: from component origin to target position
        all_entities[n+8] = (
            f"ITEM_DEFINED_TRANSFORMATION('','',#{n+3},#{n+7})"
        )

        # Complex REPRESENTATION_RELATIONSHIP with transformation
        # Mirrors the structure OCCT generates for its own assembly linkage:
        # #533 = ( REPRESENTATION_RELATIONSHIP('','',#32,#10)
        #          REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION(#534)
        #          SHAPE_REPRESENTATION_RELATIONSHIP() );
        all_entities[n+9] = (
            f"( REPRESENTATION_RELATIONSHIP('','',#{comp_sr_g},#{asm_sr_id})"
            f" REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION(#{n+8})"
            f" SHAPE_REPRESENTATION_RELATIONSHIP() )"
        )

        # NEXT_ASSEMBLY_USAGE_OCCURRENCE: parent=board assembly, child=component
        all_entities[n+10] = (
            f"NEXT_ASSEMBLY_USAGE_OCCURRENCE('{inst_idx+1}','{inst_idx+1}','',"
            f"#{asm_pd_id},#{comp_pd_g},$)"
        )

        # PRODUCT_DEFINITION_SHAPE for the occurrence (mirrors OCCT output)
        all_entities[n+11] = (
            f"PRODUCT_DEFINITION_SHAPE('Placement','Placement of an item',#{n+10})"
        )

        # CONTEXT_DEPENDENT_SHAPE_REPRESENTATION links the two
        all_entities[n+12] = (
            f"CONTEXT_DEPENDENT_SHAPE_REPRESENTATION(#{n+9},#{n+11})"
        )

        max_id = n + 12

    # Write output STEP file
    lines = [board_header.rstrip(), 'DATA;']
    for eid in sorted(all_entities):
        lines.append(f'#{eid} = {all_entities[eid]};')
    lines.append('ENDSEC;')
    lines.append('END-ISO-10303-21;')
    out_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f"OK  Сборка: {out_path}  ({len(all_entities)} сущностей)")
