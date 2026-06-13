# Gorky 17 / Odium 3dgfx Exporter

`gorky17_3dgfx_exporter.py` is a Python 3 command-line exporter for Gorky 17 /
Odium `3dgfx` assets. It converts the original game mesh, animation, clip, and
texture files into formats that are easier to inspect or import into modern art
tools.

You need another tool to extract the `.3df`, `.ani`, `.bfr`, and `.msh` files
from the game's `.dat` archives before using this exporter.

The exporter can write static OBJ files, posed OBJ files, OBJ frame sequences,
converted PNG textures, and baked animated GLB/GLTF files. Animated GLB/GLTF
exports use morph targets / shape keys, not a reconstructed bone rig.

## Requirements

Install Python 3 and these packages:

```powershell
python -m pip install numpy Pillow
```

## Input Files

| File | Contents | When it is needed |
| --- | --- | --- |
| `.msh` | Mesh vertices, UVs, triangles, parts, skin ranges, bone weights, and sometimes texture/ANI references. | Always. |
| `.3df` | Game texture. Supports `p8` palette textures and `yiq` / NCC textures. | Optional if the MSH has resolvable relative texture references, or if untextured output is acceptable. |
| `.bfr` | Animation pose data: per-frame 4x3 bone transforms plus bounding boxes. | Required for `--frame`, OBJ animation sequences, and baked GLB/GLTF. |
| `.ani` | Animation clip table: names, frame ranges, speeds, and sound events. | Required for `--clip`, `--all-clips`, and baked animations. |

`--3df` is an explicit texture override. If it is provided, that texture is used
for every MSH part. If it is not provided, the exporter uses texture references
stored in the MSH and resolves them relative to the MSH file. If the mesh has no
texture references, the export is untextured.

## Quick Start

If the `.msh`, `.bfr`, `.ani`, and referenced `.3df` files are laid out the way
the MSH references expect, start with `--auto`:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\grandma.msh --out .\exports\grandma_walk --clip walk --bake-glb
```

`--auto` sets `--msh`, looks for a same-stem `.bfr`, and finds an `.ani` either
from MSH references or from a same-stem `.ani`. It does not need `--3df` when the
MSH texture references are valid.

If you want to override the texture manually:

```powershell
python -B .\gorky17_3dgfx_exporter.py --msh .\grandma.msh --bfr .\grandma.bfr --ani .\grandma.ani --3df .\babcia.3df --out .\exports\grandma_walk --clip walk --bake-glb
```

## Common Workflows

### Static OBJ

Use this for the mesh in its original pose:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\grandma.msh --out .\exports\grandma_static --static
```

Typical output:

- `grandma_static.obj`
- `grandma.mtl`
- one or more `.png` textures, if textures were resolved
- `summary.json`

### One Posed OBJ Frame

Frame numbers are zero-based BFR frame indices:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\grandma.msh --out .\exports\grandma_frame_10 --frame 10
```

Typical output:

- `grandma_frame_010.obj`
- `grandma.mtl`
- one or more `.png` textures, if textures were resolved
- `summary.json`

### One Animated GLB Clip

Use this for a single animation that imports into Blender as shape keys:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\grandma.msh --out .\exports\grandma_walk --clip walk --bake-glb
```

Typical output:

- `grandma_walk_baked.glb`
- `summary.json`

The texture is embedded in the GLB when one is available. A separate PNG/MTL is
not written in bake-only mode unless you also request OBJ output.

If you do not know the clip names, run with a temporary name. The exporter will
stop and list the available clips:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\grandma.msh --out .\exports\clip_probe --clip not_a_real_clip --bake-glb
```

### All Clips In One GLB

This writes one GLB with one glTF animation per ANI clip:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\grandma.msh --out .\exports\grandma_all --all-clips --bake-glb
```

Typical output:

- `grandma_all_anims_baked.glb`
- `summary.json`

### One GLB Per Clip

For Blender, separate files are often easier to work with:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\grandma.msh --out .\exports\grandma_split --all-clips --bake-glb --split-clip-glbs
```

Typical output:

- `grandma_all_anims_baked.glb`
- `per_clip_baked\grandma_CLIP_baked.glb` files
- `summary.json`

`--split-clip-glbs` only creates split files when more than one clip is selected.

### Combined Timeline GLB

This places selected clips one after another in a single animation track:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\grandma.msh --out .\exports\grandma_timeline --all-clips --bake-glb --combined-timeline
```

Add held-pose gaps between clips with:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\grandma.msh --out .\exports\grandma_timeline_gap --all-clips --bake-glb --combined-timeline --timeline-gap-frames 5
```

### OBJ Animation Sequence

OBJ cannot store animation in one file, so this writes one OBJ per sampled frame:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\grandma.msh --out .\exports\grandma_obj_frames --clip walk
```

Use `--every` to skip frames:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\grandma.msh --out .\exports\grandma_obj_frames_light --clip walk --every 2
```

### GLB And OBJ Frames Together

By default, `--bake-glb` or `--bake-gltf` writes only the baked animated file and
`summary.json`. Add `--obj-sequence` to also write OBJ frames:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\grandma.msh --out .\exports\grandma_both --clip walk --bake-glb --obj-sequence
```

## Options

| Option | Meaning |
| --- | --- |
| `--msh FILE` | Mesh file. Required unless `--auto` is used. |
| `--auto MSH` | Use the MSH and auto-discover missing same-stem or embedded inputs. |
| `--bfr FILE` | Animation pose file. Required for posed frames and animation exports. |
| `--ani FILE` | Animation clip file. Required for clip exports. |
| `--3df FILE` | Texture override for all parts. Supports `p8` and `yiq` / NCC 3DF files. |
| `--texture-order xrgb` | Palette byte order for `p8` textures. Choices: `xrgb`, `xbgr`, `bgr`, `rgb`. |
| `--out FOLDER` | Output folder. Defaults to `out`. |
| `--name NAME` | Override output basenames. |
| `--no-summary` | Do not write `summary.json`. |
| `--static` | Write a static OBJ. |
| `--frame NUMBER` | Write one posed OBJ at a zero-based BFR frame index. |
| `--clip NAME` | Export one named ANI clip. Case-insensitive. |
| `--all-clips`, `--all-anims` | Export or bake every ANI clip. |
| `--every NUMBER` | Sample every Nth frame for OBJ sequences and baked targets. Defaults to `1`. |
| `--bake-glb` | Write animated binary GLB with baked morph-target animation. |
| `--bake-gltf` | Write animated embedded text GLTF for inspection/debugging. |
| `--fps NUMBER` | Playback FPS for baked animation timing. Defaults to `15`. ANI speed values are ignored for timing. |
| `--obj-sequence` | Also write OBJ frame sequences when baking. |
| `--include-all-bfr-targets` | Include every BFR frame as a morph target, not only selected clip frames. |
| `--combined-timeline` | Create one sequential timeline animation instead of separate animations. |
| `--timeline-gap-frames NUMBER` | Add held frames between clips in combined timeline mode. |
| `--split-clip-glbs` | Also write one baked GLB/GLTF per clip when multiple clips are selected. |
| `--glb-flip-v` | Flip GLB/GLTF V coordinates if a viewer displays textures upside down. |

## Baked Animation Details

The exporter does not reconstruct a skeleton hierarchy. It skins vertices using
the BFR transforms and stores sampled poses as morph target deltas.

- ANI `start_frame` and `end_frame` values are used directly as zero-based BFR
  frame indices.
- The base mesh/rest pose is the first sampled BFR frame, not the original bind
  mesh.
- Morph-weight animation uses `STEP` interpolation.
- `--fps` controls playback timing. ANI speed values are parsed and preserved in
  metadata but are not used for baked timing.
- Long clips can produce large GLB/GLTF files because every sampled pose stores a
  full POSITION morph target.

Use `--every 2` or `--every 3` to reduce target count and file size when exact
per-frame playback is not required.

## Texture Notes

OBJ exports write UVs with V flipped for the OBJ import path. GLB/GLTF exports
keep V unflipped by default because that is the expected path for Blender with
the generated PNG. Use `--glb-flip-v` only if your viewer needs it.

If colors look red/blue swapped, try:

```powershell
--texture-order xbgr
```

The default palette order is `xrgb`, matching the known Gorky 17 palette layout:
unused/alpha byte, red, green, blue.

## Summary File

Unless `--no-summary` is passed, the exporter writes `summary.json`. It records
input paths, mesh counts, parts, material assignments, texture metadata, selected
clips, and baked animation semantics.

The summary is useful for checking:

- whether the expected textures were resolved
- how many vertices, triangles, parts, and weight records were parsed
- which clips were selected
- which frames became morph targets
- which timing convention was used

## Importing Into Blender

For GLB:

1. Choose `File > Import > glTF 2.0`.
2. Select the exported `.glb`.
3. Look for shape keys / morph targets and the imported animation action.

For OBJ:

1. Keep the `.obj`, `.mtl`, and any `.png` textures together.
2. Choose `File > Import > Wavefront (.obj)`.
3. Select the exported `.obj`.

## Troubleshooting

### The texture is missing

If you did not pass `--3df`, the exporter relies on texture references embedded
in the MSH. Make sure the referenced `.3df` files resolve under the MSH
directory, or pass `--3df TEXTURE.3df` to override all parts.

### The texture is upside down in GLB

Try:

```powershell
--glb-flip-v
```

### The colors look swapped

Try:

```powershell
--texture-order xbgr
```

### Blender shows many shape keys

That is expected. Animation is baked as morph targets / shape keys, not as an
editable bone rig.

### A clip frame range is outside the BFR

Use matching `.msh`, `.bfr`, and `.ani` files from the same actor or object. The
exporter validates selected ANI clip ranges against the BFR and aborts instead
of clipping or skipping invalid frames.

### The output GLB is too large

Use a larger `--every` value, export fewer clips, or use `--split-clip-glbs` so
each file only contains one clip's sampled targets.
