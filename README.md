# Gorky 17 / Odium gfx3d Exporter

This folder contains `gorky17_3dgfx_exporter.py`, a Python 3 script for converting
Gorky 17 / Odium character and object assets into files that are easier to use
in modern art tools.

**You'll need another tool to extract the .3df, .ani, .bfr, and .msh files from
the game's .dat archives.**

The script is aimed at extracting models, textures, poses, and baked animations.
Animated exports are baked as shape-key / morph-target animation, which is usually
easy to import into Blender and similar tools.

## What You Can Export

- Static OBJ models.
- One OBJ model at a chosen animation frame.
- OBJ image-sequence style frame folders for a clip.
- Animated GLB files with baked vertex animation.
- Animated embedded GLTF files for debugging or inspection.
- PNG textures converted from the game's `.3df` texture files.

For most cases, the best first choice is:

```powershell
python -B .\gorky17_3dgfx_exporter.py --msh MODEL.msh --bfr MODEL.bfr --ani MODEL.ani --3df TEXTURE.3df --out .\export --clip CLIP_NAME --bake-glb
```

Replace `MODEL.msh`, `MODEL.bfr`, `MODEL.ani`, `TEXTURE.3df`, and `CLIP_NAME`
with your own files and animation name.

## Files Used By The Script

The script works with these original game asset files:

| File | What it contains | Required for |
| --- | --- | --- |
| `.msh` | The mesh: vertices, faces, UVs, parts, and skinning information. | Always required. |
| `.3df` | The texture. The script converts it to PNG. | Needed for textured output. |
| `.bfr` | Animation pose data. | Needed for animated exports or single posed frames. |
| `.ani` | Animation clip names and frame ranges. | Needed when exporting clips. |

## Requirements

You need Python 3 and two Python packages:

- `numpy`
- `Pillow`

## Basic Command Pattern

Most commands follow this shape:

```powershell
python -B .\gorky17_3dgfx_exporter.py --msh PATH_TO_MSH --out OUTPUT_FOLDER [options]
```

Useful habits:

- Put paths with spaces inside quotes, such as `"C:\My Assets\grandma.msh"`.
- Use a different `--out` folder for each export attempt.
- Add `--no-summary` if you do not want the extra `summary.json` report.
- Use `--name my_export_name` if you want cleaner output filenames.

## Export A Static OBJ

Use this when you only want the model in its original mesh pose.

```powershell
python -B .\gorky17_3dgfx_exporter.py --msh .\grandma.msh --3df .\babcia.3df --out .\exports\grandma_static --static
```

Expected output:

- `grandma_static.obj`
- `grandma.mtl`
- `babcia.png`
- `summary.json`

Import the `.obj` into Blender or another 3D tool. Keep the `.mtl` and `.png`
next to the `.obj` so the material can find the texture.

## Export One Posed OBJ Frame

Use this when you want one exact pose from the animation data.

```powershell
python -B .\gorky17_3dgfx_exporter.py --msh .\grandma.msh --bfr .\grandma.bfr --3df .\babcia.3df --out .\exports\grandma_frame_10 --frame 10
```

Expected output:

- `grandma_frame_010.obj`
- `grandma.mtl`
- `babcia.png`
- `summary.json`

Frame numbers start at `0`. If the script says the frame is outside the valid
range, choose a smaller number.

## Export One Animated GLB Clip

Use this when you want an animation that imports into Blender as a baked mesh
animation.

```powershell
python -B .\gorky17_3dgfx_exporter.py --msh .\grandma.msh --bfr .\grandma.bfr --ani .\grandma.ani --3df .\babcia.3df --out .\exports\grandma_clip --clip CLIP_NAME --bake-glb
```

Expected output:

- `grandma_CLIP_NAME_baked.glb`
- `summary.json`

The texture is embedded inside the GLB, so this mode does not need a separate
PNG or MTL file unless you also request OBJ output.

If you do not know the clip names, run the command with any temporary clip name.
The script will stop and print the available clip names.

Example:

```powershell
python -B .\gorky17_3dgfx_exporter.py --msh .\grandma.msh --bfr .\grandma.bfr --ani .\grandma.ani --3df .\babcia.3df --out .\exports\test --clip not_a_real_clip --bake-glb
```

## Export All Animations Into One GLB

Use this when you want every clip from the `.ani` file in one GLB.

```powershell
python -B .\gorky17_3dgfx_exporter.py --msh .\grandma.msh --bfr .\grandma.bfr --ani .\grandma.ani --3df .\babcia.3df --out .\exports\grandma_all --all-clips --bake-glb
```

Expected output:

- `grandma_all_anims_baked.glb`
- `summary.json`

Some tools show multiple animations in one GLB clearly. Others can make them
hard to select. For Blender, the next option may be easier.

## Export One GLB Per Clip

This is often the most convenient option for Blender users.

```powershell
python -B .\gorky17_3dgfx_exporter.py --msh .\grandma.msh --bfr .\grandma.bfr --ani .\grandma.ani --3df .\babcia.3df --out .\exports\grandma_split --all-clips --bake-glb --split-clip-glbs
```

Expected output:

- `grandma_all_anims_baked.glb`
- A `per_clip_baked` folder containing one GLB per animation clip.
- `summary.json`

## Export All Clips As One Long Timeline

Use this when you want every clip placed one after another in a single animation
track.

```powershell
python -B .\gorky17_3dgfx_exporter.py --msh .\grandma.msh --bfr .\grandma.bfr --ani .\grandma.ani --3df .\babcia.3df --out .\exports\grandma_timeline --all-clips --bake-glb --combined-timeline
```

You can add a short held pose between clips:

```powershell
python -B .\gorky17_3dgfx_exporter.py --msh .\grandma.msh --bfr .\grandma.bfr --ani .\grandma.ani --3df .\babcia.3df --out .\exports\grandma_timeline_gap --all-clips --bake-glb --combined-timeline --timeline-gap-frames 5
```

## Export OBJ Animation Frames

OBJ cannot store animation in one file. Instead, the script can write one OBJ
file per animation frame.

```powershell
python -B .\gorky17_3dgfx_exporter.py --msh .\grandma.msh --bfr .\grandma.bfr --ani .\grandma.ani --3df .\babcia.3df --out .\exports\grandma_obj_sequence --clip CLIP_NAME
```

Expected output:

- A folder named like `grandma_clip_CLIP_NAME`.
- Many OBJ files inside that folder.
- A copied `.mtl` file and PNG texture.

You can skip frames to make a lighter sequence:

```powershell
python -B .\gorky17_3dgfx_exporter.py --msh .\grandma.msh --bfr .\grandma.bfr --ani .\grandma.ani --3df .\babcia.3df --out .\exports\grandma_obj_sequence_light --clip CLIP_NAME --every 2
```

`--every 2` exports every second frame. `--every 3` exports every third frame.

## Export GLB And OBJ Frames Together

Normally, if you request `--bake-glb`, the script only writes the baked model
and the summary report. Add `--obj-sequence` if you also want OBJ frame files.

```powershell
python -B .\gorky17_3dgfx_exporter.py --msh .\grandma.msh --bfr .\grandma.bfr --ani .\grandma.ani --3df .\babcia.3df --out .\exports\grandma_both --clip CLIP_NAME --bake-glb --obj-sequence
```

## Common Options

| Option | Meaning |
| --- | --- |
| `--msh FILE` | Mesh file. Required. |
| `--3df FILE` | Texture file. Required. |
| `--bfr FILE` | Animation pose file. Required for animated or posed exports. |
| `--ani FILE` | Animation clip file. Required for clip exports. |
| `--out FOLDER` | Folder where exported files are written. |
| `--name NAME` | Custom base name for exported files. |
| `--static` | Write a static OBJ. |
| `--frame NUMBER` | Write one posed OBJ frame. |
| `--clip NAME` | Export one named animation clip. |
| `--all-clips` | Export every clip in the ANI file. Same as `--all-anims`. |
| `--bake-glb` | Write an animated `.glb` file. Best general option. |
| `--bake-gltf` | Write an embedded `.gltf` file. Useful for inspection. |
| `--obj-sequence` | Also write OBJ frames when baking animation. |
| `--every NUMBER` | Export/sample every Nth frame. Use `1` for all frames. |
| `--fps NUMBER` | Playback speed for baked GLB/GLTF. Default is `15`. |
| `--combined-timeline` | Put selected clips into one long animation timeline. |
| `--timeline-gap-frames NUMBER` | Add held frames between clips in combined timeline mode. |
| `--split-clip-glbs` | Also make one baked GLB/GLTF per clip when using `--all-clips`. |
| `--glb-flip-v` | Flip GLB texture coordinates if a viewer shows the texture upside down. |
| `--texture-order xbgr` | Try this if the texture appears red/blue swapped. |
| `--no-summary` | Do not write `summary.json`. |

## Choosing The Right Export

| Goal | Recommended command style |
| --- | --- |
| View or edit a still model | Use `--static`. |
| Get a single pose | Use `--frame NUMBER`. |
| Bring an animation into Blender | Use `--clip NAME --bake-glb`. |
| Bring many animations into Blender | Use `--all-clips --bake-glb --split-clip-glbs`. |
| Inspect each frame separately | Use OBJ sequence export. |
| Make one long preview of every clip | Use `--all-clips --bake-glb --combined-timeline`. |

## Importing Into Blender

For GLB:

1. Open Blender.
2. Choose `File > Import > glTF 2.0`.
3. Select the exported `.glb`.
4. The model should import with its texture embedded.

For OBJ:

1. Keep the `.obj`, `.mtl`, and `.png` files together.
2. Choose `File > Import > Wavefront (.obj)`.
3. Select the exported `.obj`.

If the texture looks upside down in a GLB viewer, export again with
`--glb-flip-v`.

If the colors look wrong, especially if red and blue appear swapped, export
again with:

```powershell
--texture-order xbgr
```

## About Baked Animation

The script does not build a bone rig. Instead, it stores each sampled pose as a
morph target. This means:

- The animation can look correct without reconstructing the original skeleton.
- The exported GLB can become large for long clips.
- Editing individual bones is not possible because there are no exported bones.
- Shape keys / morph targets are the important animation data.

Use `--every 2` or `--every 3` if the file is too large and you can accept a
less detailed animation.

## The Summary File

By default, the script writes `summary.json`. This file is mostly a report. It
can help you see:

- Which input files were used.
- How many vertices and triangles were found.
- Which clips were selected.
- Which textures and materials were used.
- How the baked animation was timed.

You can skip it with:

```powershell
--no-summary
```

## Troubleshooting

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

That is expected. The animation is baked as morph targets / shape keys, not as
an editable bone rig.

### Nothing is written because of a frame range error

The selected animation clip asks for frames that are not present in the BFR
file. Use a matching `.msh`, `.bfr`, and `.ani` set from the same character or
object.

## Advanced Notes

`--include-all-bfr-targets` is mainly for special workflows. It includes every
BFR frame as a morph target, even frames outside the selected clip.

`--bake-gltf` writes a text-based embedded GLTF instead of a binary GLB. It is
larger and less convenient for normal use, but it can be helpful when someone
needs to inspect the file contents.

## Quick Examples

Static OBJ:

```powershell
python -B .\gorky17_3dgfx_exporter.py --msh .\grandma.msh --3df .\babcia.3df --out .\exports\grandma_static --static
```

Animated GLB:

```powershell
python -B .\gorky17_3dgfx_exporter.py --msh .\grandma.msh --bfr .\grandma.bfr --ani .\grandma.ani --3df .\babcia.3df --out .\exports\grandma_anim --clip CLIP_NAME --bake-glb
```

All clips, one GLB per clip:

```powershell
python -B .\gorky17_3dgfx_exporter.py --msh .\grandma.msh --bfr .\grandma.bfr --ani .\grandma.ani --3df .\babcia.3df --out .\exports\grandma_split --all-clips --bake-glb --split-clip-glbs
```

OBJ frame sequence:

```powershell
python -B .\gorky17_3dgfx_exporter.py --msh .\grandma.msh --bfr .\grandma.bfr --ani .\grandma.ani --3df .\babcia.3df --out .\exports\grandma_obj_frames --clip CLIP_NAME
```

Basic Example

```powershell
py -3 gorky17_3dgfx_exporter.py --3df .\babcia.3df --ani .\grandma.ani --bfr .\grandma.bfr --msh .\grandma.msh --out out --bake-gltf --all-anims --combined-timeline --fps 23.976 --name grandma
```
