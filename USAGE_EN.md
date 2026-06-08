# NoABS.Eagle3D — Usage Guide

RU version may be found [here](USAGE_RU.md).

NoABS.Eagle3D turns an Eagle `.brd` into a 3D model — either a **GLB** (lightweight,
textured, for preview in any glTF viewer) or a **STEP** (precise B-rep assembly, for
MCAD: FreeCAD, Fusion 360, SolidWorks, Siemens NX…). Everything runs locally.

You drive it from one ULP — `export3D.ulp` — inside Eagle. GLB and STEP are two
renderings of the same board; pick whichever you need.

---

## One-time setup: `noabs.ini`

Two things live on your workstation, not in the board, so you set them once and forget.
`noabs.ini` sits next to `eagle2gltf` / `eagle2step` and holds the paths to your
component-model libraries:

```ini
# Path to the GLB component model library (used for GLB export)
components_dir=C:/path/to/glb_models

# Path to the STEP component model library (used for STEP export)
step_components_dir=C:/path/to/step_models
```

File names in those folders must match Eagle **footprint** names (case-insensitive),
e.g. footprint `SOIC8` → `SOIC8.glb` / `SOIC8.step`. Components without a matching
model are simply skipped.

---

## Running it from Eagle

Open your board and run the ULP via **File → Execute Script → `export3D.ulp`**, or type
`RUN export3D.ulp` in the command line. It has a few modes:

| Command | What it does |
|---|---|
| `RUN export3D.ulp` | First time (board not configured yet): shows the **settings dialog**, stores the settings, and stops. Run it again to actually generate the **GLB**. |
| `RUN export3D.ulp STEP` | Generate a **STEP** model (board + components) instead of GLB. |
| `RUN export3D.ulp SET` | Re-open the settings dialog to change settings (no generation). |
| `RUN export3D.ulp OPEN` | Open the already-generated GLB in the system viewer. |
| `RUN export3D.ulp LOG` | Open the last generation log (only kept if *Leave artifacts* is on). |

The result (`<board>.glb` or `<board>.step`) appears next to your `.brd`. By default no
other files are left behind.

> **First run only saves settings — it does not generate the model.** Run the script a
> second time (no arguments) to generate. The dialog reminds you of this.

### Why a second click / why it can take a moment

Eagle 9 has a layer-rendering bug in texture export, so for GLB the texture step is run
automatically in a **bundled, headless Eagle 7** (you don't need to install or touch it —
it ships with the tool). A small window may flash; that's normal. STEP needs no textures
and is pure geometry, so it doesn't use Eagle 7 at all.

---

## Settings (the dialog)

Settings are stored as **global attributes inside the board** (`NOABS_*`). They are
human-readable (you can see "solder mask = Blue" right in the board's attributes) and
travel with the `.brd` file.

| Setting | Meaning |
|---|---|
| **Solder mask** | Board color: Green / Blue / Red / Black / Purple / White |
| **Pad finish** | ENIG (gold) or HASL (tin) |
| **Silkscreen** | White or Black |
| **Texture DPI** | Texture resolution for GLB (clamped 72–2400; 400–600 is a good balance) |
| **Min Priority3d** | Component filter — see below |
| **Photorealistic vias** | Render via holes as dark dots in the texture |
| **Merge components by material** | GLB only: merge un-keyed components by material so big boards stay light in web viewers (trades away per-component structure). Ignored for STEP. |
| **Leave 3D model generation artifacts** | Keep intermediate textures / logs in `NoABS_tmp` for debugging (off by default — no clutter) |

The STEP board takes its color from the **Solder mask** setting too.

---

## Choosing which components appear: `priority3d`

`priority3d` is a per-part attribute you can set in Eagle (Edit → Attributes on the part,
or via the library). It interacts with the **Min Priority3d** setting:

- **Not set / `0`** — normal: the component is included.
- **`Min Priority3d` = N (N > 0)** — include **only** components with `priority3d ≥ N`.
  Use this to strip a heavy board down to the mechanically important parts (connectors,
  big modules) — set those to a high `priority3d`, raise the threshold, and everything
  else drops out. Great for fast, light MCAD models.
- **`priority3d` = `-1`** (any negative) — **never** put this part in 3D, regardless of
  threshold. Use this for the opposite case: "I want everything *except* these few."

> If a part is mysteriously missing from the model, check its `priority3d` attribute —
> a forgotten `-1` is the usual reason. The attribute lives on the part itself, so that's
> where to look.

---

## Component placement metadata

For a model to sit correctly on its footprint (right height, rotation, offset), the
footprint's `description` must contain metadata in the `<!--3d:{...}-->` format
(X/Y/Z offset and rotation). Without it the model is placed at the footprint origin with
no correction, which is wrong for most packages.

This metadata is created with **glue**, the GUI tool in this package: it lets you visually
dial in the placement and writes it into the footprint description. The link travels with
the footprint when you copy it between libraries.

---

## Command-line tools (advanced / standalone)

The ULP calls these for you, but you can run them directly. Both read their component
library path from `noabs.ini` unless you pass `-c`.

### eagle2gltf — GLB

```
eagle2gltf.exe board.brd
eagle2gltf.exe board.brd -o out.glb
eagle2gltf.exe board.brd -c path\to\glb_models\ --min-priority 2 --merge
```

| Argument | Description |
|---|---|
| `board.brd` | Eagle board file (required) |
| `-o`, `--output` | Output `.glb` (default: next to BRD) |
| `-t`, `--thickness` | Board thickness in mm (default: from BRD) |
| `-c`, `--components` | GLB component folder (overrides `noabs.ini`) |
| `--min-priority` | Minimum `priority3d` to include (default 0) |
| `--merge` | Merge un-keyed components by material |
| `--textures` | PNG/BMP texture folder (default: auto) |
| `--leave-artifacts` | Keep intermediate textures/GLB |
| `--open` | Open the result when done |
| `--log` | Log file path |

### eagle2step — STEP

```
eagle2step.exe board.brd
eagle2step.exe board.brd -o out.step
eagle2step.exe board.brd -c path\to\step_models\ --min-priority 2
```

| Argument | Description |
|---|---|
| `board.brd` | Eagle board file (required) |
| `-o`, `--output` | Output `.step` (default: `<board>.step`) |
| `-t`, `--thickness` | Board thickness in mm (default: from BRD) |
| `-c`, `--step-dir` | STEP component folder (overrides `noabs.ini`) |
| `-a`, `--assembly-output` | Assembly output path (default: same as `-o`) |
| `--min-priority` | Minimum `priority3d` to include (default 0) |
| `--leave-artifacts` | Keep the intermediate board-only STEP |
| `--debug` | Save TCL/BAT instead of running DRAWEXE |

The result is a **single** `<board>.step` = board + components, a proper assembly tree
with named, colored parts. Need just the bare board for MCAD? Set `--min-priority` high
enough to drop every component.

---

## Troubleshooting

**Board appears as a solid color (no textures, GLB)**
Textures weren't exported from Eagle. Run through `export3D.ulp` (not `eagle2gltf` by hand),
or point `--textures` at the texture folder.

**A component is missing**
1. Is there a model file whose name matches the footprint (in `components_dir` /
   `step_components_dir`)?
2. Does the part have `priority3d = -1`, or is `Min Priority3d` set above its priority?

**Eagle 9 board won't open in the bundled Eagle 7 (managed libraries)**
Boards that use Eagle 9 *managed* libraries can fail to open in the bundled Eagle 7
(duplicate library names). Unlink the managed libraries in Eagle 9 and re-save.

**STEP file is large / slow to open**
That's the nature of a precise B-rep assembly with many detailed parts — normal in any
MCAD. Use `Min Priority3d` (or `priority3d = -1`) to include only the parts you actually
need.
