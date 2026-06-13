# Gorky 17 / Odium DAT Extractor and 3D Exporter

This project has two small Python tools for working with files from Gorky 17 /
Odium:

- `gorky17_dat_extractor.py` opens the game's `.dat` archive files and extracts
  the files inside them.
- `gorky17_3dgfx_exporter.py` converts extracted 3D model files into formats
  that are easier to open in tools such as Blender.

The usual workflow is:

1. Extract the game `.dat` files.
2. Pick a model file, usually a `.msh` file.
3. Export it as `.glb` or `.obj`.
4. Open the result in Blender or another 3D program.

## What You Need

Install Python 3 first. Then install the two Python packages.

```powershell
python -m pip install numpy Pillow
```

## Step 1: Extract The Game DAT Files

If Gorky 17 is installed through Steam in the normal location, the game archive
folder is usually:

```text
<STEAM>\steamapps\common\Gorky 17\dat
```

To extract all `.dat` files from that folder and its subfolders:

```powershell
python -B .\gorky17_dat_extractor.py --input-dir "<STEAM>\steamapps\common\Gorky 17\dat" --recursive --out .\out\gorky17_dat
```

The extracted files will be placed under:

```text
out\gorky17_dat
```

The extractor keeps the game's folder layout so files with the same archive
name do not overwrite each other. For example:

```text
01_port\other.dat      -> out\gorky17_dat\01_port\other
common\other.dat       -> out\gorky17_dat\common\other
common\sprite.dat      -> out\gorky17_dat\common\sprite
```

To only see what is inside the archives, without extracting anything:

```powershell
python -B .\gorky17_dat_extractor.py --input-dir "<STEAM>\steamapps\common\Gorky 17\dat" --recursive --list
```

To do a test run that shows what would be extracted:

```powershell
python -B .\gorky17_dat_extractor.py --input-dir "<STEAM>\steamapps\common\Gorky 17\dat" --recursive --dry-run
```

## Step 2: Find A 3D Model

After extraction, search inside `out\gorky17_dat` for `.msh` files. A `.msh`
file is the main 3D model file.

In File Explorer, you can search for:

```text
*.msh
```

You can also list some models in PowerShell:

```powershell
Get-ChildItem .\out\gorky17_dat -Recurse -Filter *.msh | Select-Object -First 20 FullName
```

For many models, the matching `.bfr` and `.ani` files sit next to the `.msh`
file and have the same name. The exporter can usually find them automatically.

## Step 3: Export A Model

The recommended way to export is with `--auto`. Give it a `.msh` file and it
will try to find the matching `.bfr`, `.ani`, and texture files.

Example:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\out\gorky17_dat\common\other\gfx_3d\cpt.msh --out .\out\exports\cpt --all-clips --bake-glb --split-clip-glbs
```

This creates Blender-friendly `.glb` files under:

```text
out\exports\cpt
```

If you only want a still model, use:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\out\gorky17_dat\common\other\gfx_3d\cpt.msh --out .\out\exports\cpt_static --static
```

If you want one named animation clip:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\out\gorky17_dat\common\other\gfx_3d\cpt.msh --out .\out\exports\cpt_walk --clip walk --bake-glb
```

If you do not know the clip names, try a made-up name. The exporter will stop
and print the real clip names:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\out\gorky17_dat\common\other\gfx_3d\cpt.msh --out .\out\exports\clip_probe --clip not_a_real_clip --bake-glb
```

## Opening The Result In Blender

For `.glb/.gltf` files:

1. Open Blender.
2. Choose `File > Import > glTF 2.0`.
3. Select the exported `.glb` file.
4. Look for the imported animation and shape keys.

For `.obj` files:

1. Keep the `.obj`, `.mtl`, and `.png` files together in the same folder.
2. Choose `File > Import > Wavefront (.obj)`.
3. Select the exported `.obj` file.

## Common Export Examples

### Export Every Animation As Separate GLB Files

This is usually the easiest result to use in Blender:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\out\gorky17_dat\common\other\gfx_3d\cpt.msh --out .\out\exports\cpt --all-clips --bake-glb --split-clip-glbs
```

Typical output:

- `cpt_all_anims_baked.glb`
- `per_clip_baked\cpt_CLIPNAME_baked.glb` files
- `summary.json`

### Export All Animations Into One GLB

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\out\gorky17_dat\common\other\gfx_3d\cpt.msh --out .\out\exports\cpt_all --all-clips --bake-glb
```

### Export One Still OBJ

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\out\gorky17_dat\common\other\gfx_3d\cpt.msh --out .\out\exports\cpt_static --static
```

Typical output:

- `cpt_static.obj`
- `cpt.mtl`
- one or more `.png` texture files, if textures were found
- `summary.json`

### Export One Posed OBJ Frame

Frame numbers start at `0`:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\out\gorky17_dat\common\other\gfx_3d\cpt.msh --out .\out\exports\cpt_frame_10 --frame 10
```

### Export OBJ Files For An Animation

OBJ files cannot store animation in one file, so this writes one OBJ per frame:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\out\gorky17_dat\common\other\gfx_3d\cpt.msh --out .\out\exports\cpt_obj_frames --clip walk
```

To write fewer frames and make the output smaller:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\out\gorky17_dat\common\other\gfx_3d\cpt.msh --out .\out\exports\cpt_obj_frames_light --clip walk --every 2
```

### Override The Texture

Usually `--auto` is enough. If the texture is missing or you want to force a
specific `.3df` texture, add `--3df`. Replace `PATH_TO_TEXTURE.3df` with the
texture file you want to use:

```powershell
python -B .\gorky17_3dgfx_exporter.py --auto .\out\gorky17_dat\common\other\gfx_3d\cpt.msh --3df PATH_TO_TEXTURE.3df --out .\out\exports\cpt_textured --all-clips --bake-glb
```

### Old Manual Input Style

The old style still works if you want to give every file yourself:

```powershell
python -B .\gorky17_3dgfx_exporter.py --msh .\out\gorky17_dat\common\other\gfx_3d\cpt.msh --bfr .\out\gorky17_dat\common\other\gfx_3d\cpt.bfr --ani .\out\gorky17_dat\common\other\gfx_3d\cpt.ani --3df PATH_TO_TEXTURE.3df --out .\out\exports\cpt_manual --clip walk --bake-glb
```

## File Types In Plain Language

| File type | What it is |
| --- | --- |
| `.dat` | A game archive. Think of it as a zip file. |
| `.msh` | A 3D model shape. This is the file you normally give to `--auto`. |
| `.bfr` | Movement data used for animated poses. |
| `.ani` | Animation names and frame ranges, such as walk or attack. |
| `.3df` | A game texture file. The exporter can turn it into PNG. |
| `.glb/.gltf` | A modern 3D file that Blender can import. Best choice for animated exports. |
| `.obj` | A simple 3D model file. Good for still models or frame-by-frame output. |
| `summary.json` | A small report written by the exporter. Useful when checking what happened. |

## DAT Extractor Options

| Option | Meaning |
| --- | --- |
| `archives` | Optional archive paths. If omitted, the script searches `--input-dir`. |
| `--input-dir FOLDER`, `--in FOLDER` | Folder to search for `.dat` files. Defaults to `in`. |
| `--recursive` | Also search subfolders. Use this for the full game `dat` folder. |
| `--out FOLDER` | Where extracted files are written. Defaults to `out`. |
| `--no-archive-dir` | Extract directly into `--out`. Only allowed with one archive. |
| `--list` | Show archive contents without extracting. |
| `--dry-run` | Show what would be extracted without writing files. |
| `--overwrite` | Replace existing extracted files. |
| `--encoding TEXT` | Filename text encoding. Default is `cp1250`. |
| `--no-times` | Do not copy the original archive timestamps to extracted files. |

## 3D Exporter Options

| Option | Meaning |
| --- | --- |
| `--auto MSH` | Recommended. Use this with a `.msh` file and let the exporter find the related files. |
| `--msh FILE` | Manual model input. Needed only when not using `--auto`. |
| `--bfr FILE` | Manual movement data input. Needed for posed or animated exports. |
| `--ani FILE` | Manual animation list input. Needed for clip exports. |
| `--3df FILE` | Optional texture override for all model parts. |
| `--texture-order xrgb` | Palette byte order for `p8` textures. Try `xbgr` if colors look swapped. |
| `--out FOLDER` | Output folder. Defaults to `out`. |
| `--name NAME` | Override output file names. |
| `--no-summary` | Do not write `summary.json`. |
| `--static` | Write one still OBJ file. |
| `--frame NUMBER` | Write one posed OBJ at a frame number. Frame numbers start at `0`. |
| `--clip NAME` | Export one named animation clip. |
| `--all-clips`, `--all-anims` | Export every animation clip in the `.ani` file. |
| `--every NUMBER` | Use every Nth frame. Larger numbers make smaller files. |
| `--bake-glb` | Write an animated `.glb` file. This is the usual Blender choice. |
| `--bake-gltf` | Write an animated text `.gltf` file. Mostly useful for inspection. |
| `--fps NUMBER` | Playback speed for baked animation timing. Default is `15`. |
| `--obj-sequence` | Also write OBJ frame sequences when baking animation. |
| `--include-all-bfr-targets` | Advanced. Include every BFR frame as a morph target. |
| `--combined-timeline` | Put selected clips one after another in one animation track. |
| `--timeline-gap-frames NUMBER` | Add held frames between clips in combined timeline mode. |
| `--split-clip-glbs` | Also write one GLB/GLTF per clip when exporting multiple clips. |
| `--glb-flip-v` | Flip texture coordinates for GLB/GLTF if textures appear upside down. |

## Notes About Animated GLB Files

The exporter does not rebuild the game's original editable bone rig. Instead, it
saves animation as shape keys, also called morph targets. This is normal for
this tool.

- The first exported frame becomes the base pose.
- Animation timing is controlled by `--fps`.
- Large animations can create large `.glb` files.
- Use `--every 2` or `--every 3` to make smaller files if exact frame-by-frame
  playback is not important.

## Troubleshooting

### The extractor finds no files

Check that the path after `--input-dir` points to the game's `dat` folder. If
you are extracting the full game folder, include `--recursive`.

### The exported model has no texture

The exporter normally uses texture names stored inside the `.msh` file. If that
does not work, pass a texture manually with `--3df`.

### The texture is upside down in GLB

Try adding:

```powershell
--glb-flip-v
```

### The colors look swapped

Try adding:

```powershell
--texture-order xbgr
```

### Blender shows many shape keys

That is expected. The animation is stored as shape keys / morph targets, not as
an editable bone rig.

### The output GLB is too large

Use fewer clips, use `--split-clip-glbs`, or add `--every 2` or `--every 3`.
