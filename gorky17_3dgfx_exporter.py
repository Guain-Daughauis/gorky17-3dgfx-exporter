#!/usr/bin/env python3
"""
Gorky 17 / Odium gfx3d exporter.

Handles the companion set:
  .msh  mesh: vertices, UVs, faces, skin ranges, bone weights
  .bfr  animation: 4x3 float32 bone matrices + one AABB per frame
  .ani  clip table: animation names, frame ranges, sound triggers
  .3df  3dfx texture: p8 palette or yiq/NCC texture with mipmaps

What this can export:
  - static OBJ
  - one skinned-frame OBJ
  - OBJ sequences for one clip or all clips
  - animated GLB/GLTF with baked vertex/morph-target animation

Examples:
  python gorky17_3dgfx_exporter.py --msh actor.msh --3df actor.3df --out out --static
  python gorky17_3dgfx_exporter.py --msh actor.msh --bfr actor.bfr --ani actor.ani --3df actor.3df --out out --clip walk
  python gorky17_3dgfx_exporter.py --msh actor.msh --bfr actor.bfr --ani actor.ani --3df actor.3df --out out --clip walk --bake-glb
  python gorky17_3dgfx_exporter.py --msh actor.msh --bfr actor.bfr --ani actor.ani --3df actor.3df --out out --all-anims --bake-glb

Notes:
  OBJ cannot store animation. OBJ animation has to be exported as a sequence.
  GLB animation here is baked as morph targets / shape keys, one target per sampled frame.
  For Blender, --combined-timeline or --split-clip-glbs can be easier than a multi-action GLB.
  This does not reconstruct a skeleton hierarchy. It bakes final skinned vertex positions.
  Frame-based exports validate requested frames against the BFR range used by
  --frame. If any selected ANI clip is outside that range, export aborts
  instead of skipping or clipping frames.
  Baked GLB/GLTF uses ANI frame numbers directly as zero-based BFR indices,
  uses STEP morph-weight animation, uses --fps for timing, and intentionally
  ignores ANI speed values.

  The BFR transform is row-vector style:
    p_out = p @ matrix[:3, :] + matrix[3, :]

  This script supports p8/XRGB palettes and yiq/NCC 3DF textures. For p8, this game's palette is stored as XRGB/ARGB bytes:
    byte 0 = unused/alpha, byte 1 = red, byte 2 = green, byte 3 = blue.
  Use --texture-order xbgr only if red/blue look swapped.
"""

from __future__ import annotations
from pathlib import Path, PurePosixPath, PureWindowsPath
import argparse, base64, io, json, math, re, shutil, struct
import numpy as np
from PIL import Image

# -----------------------------
# Parsing helpers
# -----------------------------

def require_bytes(data: bytes, offset: int, size: int, context: str):
    if offset < 0 or size < 0:
        raise ValueError(f"{context}: invalid byte range offset={offset} size={size}")
    end=offset+size
    if end > len(data):
        available=max(0, len(data)-offset)
        raise ValueError(f"Truncated {context}: need {size} bytes at offset {offset}, found {available}")
    return end


def unpack_from_checked(fmt: str, data: bytes, offset: int, context: str):
    require_bytes(data, offset, struct.calcsize(fmt), context)
    return struct.unpack_from(fmt, data, offset)


def read_input_bytes(path, context: str):
    try:
        return Path(path).read_bytes()
    except FileNotFoundError as exc:
        raise ValueError(f"{context} file not found: {path}") from exc
    except OSError as exc:
        raise ValueError(f"Could not read {context} file {path}: {exc.strerror or exc}") from exc


def is_power_of_two(value: int):
    return value > 0 and (value & (value - 1)) == 0


def array_bounds(values):
    arr=np.asarray(values)
    if arr.size == 0:
        return None
    return {'min': arr.min(axis=0).tolist(), 'max': arr.max(axis=0).tolist()}


def scan_msh_blocks(data: bytes):
    """Locate the common Gorky 17 MSH block chain.

    Observed variants:
      - skinned actors: type 4 vertices, type 5 weight ranges,
        type 6 triangles, type 7 weight records
      - static/simple parts: same type 4/5/6 chain, but type 7 can be
        present with count=0 and zero payload

    Earlier versions rejected count=0 for block type 7, which broke meshes
    such as grandmashand.msh.
    """
    chains=find_msh_block_chains(data)
    if chains:
        return chains[0]

    raise ValueError("Could not locate MSH block chain: expected block types 4/5/6/7; type 7 may be empty")


def read_msh_block_stream(data: bytes):
    blocks=[]
    off=0
    while off < len(data):
        require_bytes(data, off, 8, "MSH block stream header")
        size,block_type=unpack_from_checked('<II', data, off, "MSH block stream header")
        if size < 8:
            raise ValueError(f"Invalid MSH block stream at offset {off}: block size {size} is smaller than 8")
        require_bytes(data, off, size, f"MSH block stream block type {block_type} at offset {off}")
        if size >= 12:
            count=unpack_from_checked('<I', data, off+8, f"MSH block type {block_type} count")[0]
            payload=off+12
        else:
            count=0
            payload=off+8
        blocks.append({'offset': off, 'size': size, 'type': block_type, 'count': count, 'payload': payload, 'stream_index': len(blocks)})
        off += size
    return blocks


def msh_block_chains_from_stream(blocks):
    chains=[]
    i=0
    while i < len(blocks):
        if blocks[i]['type'] != 4:
            i += 1
            continue
        if i + 3 >= len(blocks) or [block['type'] for block in blocks[i:i+4]] != [4,5,6,7]:
            raise ValueError(
                f"Invalid MSH mesh block chain at offset {blocks[i]['offset']}: "
                "expected consecutive block types 4/5/6/7"
            )
        chains.append(blocks[i:i+4])
        i += 4
    return chains


def find_msh_block_chains(data: bytes):
    return msh_block_chains_from_stream(read_msh_block_stream(data))


def block_ascii_payload(data: bytes, block):
    if not block:
        return None
    require_bytes(data, block['payload'], block['offset']+block['size']-block['payload'], f"MSH block type {block['type']} ASCII payload")
    payload=data[block['payload']:block['offset']+block['size']]
    try:
        return payload.decode('ascii')
    except UnicodeDecodeError:
        return payload.decode('ascii', errors='replace')


def validate_msh_block(data: bytes, block, expected_type: int, item_size: int, allow_empty=False):
    if block['type'] != expected_type:
        raise ValueError(f"Expected MSH block type {expected_type}, got {block['type']} at offset {block['offset']}")
    if block['size'] < 12:
        raise ValueError(f"Invalid MSH block type {expected_type} at offset {block['offset']}: size {block['size']} is smaller than header")
    if block['count'] == 0 and not allow_empty:
        raise ValueError(f"Invalid MSH block type {expected_type} at offset {block['offset']}: count must be non-zero")
    payload_size=block['size']-12
    expected_payload=block['count']*item_size
    if payload_size != expected_payload:
        raise ValueError(
            f"Invalid MSH block type {expected_type} at offset {block['offset']}: "
            f"payload is {payload_size} bytes, expected {expected_payload}"
        )
    require_bytes(data, block['payload'], expected_payload, f"MSH block type {expected_type} payload")


def validate_msh_skin_range(start: int, end: int, weight_count: int, part_index: int, range_index: int):
    if start > end or end > weight_count:
        raise ValueError(
            f"Invalid MSH skin range {range_index} in part {part_index}: "
            f"{start}..{end} is outside local weight record range 0..{weight_count} "
            "(end-exclusive)"
        )


def validate_msh_triangle_indices(indices, vertex_count: int, part_index: int, triangle_index: int):
    for index in indices:
        if index >= vertex_count:
            raise ValueError(
                f"Invalid MSH triangle {triangle_index} in part {part_index}: "
                f"vertex index {index} is outside local vertex range 0..{vertex_count-1}"
            )


def validate_skin_ranges(mesh, context='MSH skinning'):
    vertices=mesh.get('vertices', [])
    ranges=mesh.get('ranges', [])
    weights=mesh.get('weights', [])
    if not ranges:
        if weights:
            raise ValueError(f"{context}: found {len(weights)} weight records but no skin ranges")
        return
    if len(ranges) != len(vertices):
        raise ValueError(f"{context}: expected {len(vertices)} skin ranges, found {len(ranges)}")

    weight_count=len(weights)
    for vertex_index,item in enumerate(ranges):
        try:
            start,end=item
        except (TypeError, ValueError):
            raise ValueError(f"{context}: invalid skin range for vertex {vertex_index}: {item!r}") from None
        start=int(start)
        end=int(end)
        if start < 0 or start > end or end > weight_count:
            raise ValueError(
                f"{context}: skin range for vertex {vertex_index} {start}..{end} "
                f"is outside weight record range 0..{weight_count} (end-exclusive)"
            )


def bfr_bone_range_text(bfr):
    count=int(bfr.get('bone_count', 0)) if bfr else 0
    if count <= 0:
        return '<empty>'
    return f"0..{count-1}"


def validate_skin_bone_ids(mesh, bfr, context='MSH skinning'):
    if bfr is None:
        return
    bone_count=int(bfr.get('bone_count', 0))
    valid_range=bfr_bone_range_text(bfr)
    weights=mesh.get('weights', [])
    for vertex_index,(start,end) in enumerate(mesh.get('ranges', [])):
        for weight_index in range(int(start), int(end)):
            bone, _weight_percent=weights[weight_index]
            bone=int(bone)
            if bone < 0 or bone >= bone_count:
                raise ValueError(
                    f"{context}: vertex {vertex_index} weight record {weight_index} "
                    f"references BFR bone {bone}, outside valid BFR bone range {valid_range}"
                )


def validate_faces_for_vertex_count(faces, vertex_count: int, context='mesh'):
    arr=np.asarray(faces)
    if arr.size == 0:
        return
    if np.any(arr < 0):
        raise ValueError(f"{context}: face indices must be non-negative")
    max_index=int(arr.max())
    if max_index >= vertex_count:
        raise ValueError(f"{context}: face index {max_index} is outside vertex range 0..{vertex_count-1}")


def msh_part_metadata(data: bytes, block_stream, chain):
    chain_index=chain[0]['stream_index']
    name_block=None
    texture_block=None
    for block in reversed(block_stream[:chain_index]):
        if block['type'] in (4, 5, 6, 7):
            break
        if texture_block is None and block['type'] == 9:
            texture_block=block
            continue
        if name_block is None and block['type'] == 3:
            name_block=block
        if name_block and texture_block:
            break
    return {
        'name': block_ascii_payload(data, name_block) if name_block and name_block['type'] == 3 else None,
        'texture': block_ascii_payload(data, texture_block) if texture_block and texture_block['type'] == 9 else None,
    }


def ascii_strings(data: bytes, min_len=4):
    out=[]; start=None; chars=[]
    for i,b in enumerate(data):
        if 32 <= b < 127:
            if start is None: start=i
            chars.append(chr(b))
        else:
            if start is not None and len(chars) >= min_len:
                out.append({'offset': start, 'text': ''.join(chars)})
            start=None; chars=[]
    if start is not None and len(chars) >= min_len:
        out.append({'offset': start, 'text': ''.join(chars)})
    return out

def parse_msh(path):
    data=read_input_bytes(path, "MSH")
    if len(data) < 12:
        raise ValueError(f"MSH file is too small: {len(data)} bytes")
    block_stream=read_msh_block_stream(data)
    chains=msh_block_chains_from_stream(block_stream)
    if not chains:
        raise ValueError("Could not locate MSH block chain: expected block types 4/5/6/7; type 7 may be empty")

    verts=[]; uvs=[]; vertex_ids=[]; ranges=[]; faces=[]; weights=[]; parts=[]; all_blocks=[]
    for part_index, blocks in enumerate(chains):
        b4,b5,b6,b7=blocks
        validate_msh_block(data, b4, 4, 22)
        validate_msh_block(data, b5, 5, 8)
        validate_msh_block(data, b6, 6, 6)
        validate_msh_block(data, b7, 7, 8, allow_empty=True)
        meta=msh_part_metadata(data, block_stream, blocks)
        vertex_start=len(verts)
        triangle_start=len(faces)
        weight_start=len(weights)

        off=b4['payload']
        for i in range(b4['count']):
            x,y,z,u,v,vid=unpack_from_checked('<fffffH', data, off+i*22, f"MSH vertex {i} in part {part_index}")
            verts.append((x,y,z)); uvs.append((u,v)); vertex_ids.append(vid)

        off=b5['payload']
        for i in range(b5['count']):
            a,b=unpack_from_checked('<II', data, off+i*8, f"MSH skin range {i} in part {part_index}")
            validate_msh_skin_range(a, b, int(b7['count']), part_index, i)
            ranges.append((a+weight_start, b+weight_start))

        off=b6['payload']
        for i in range(b6['count']):
            a,b,c=unpack_from_checked('<HHH', data, off+i*6, f"MSH triangle {i} in part {part_index}")
            validate_msh_triangle_indices((a,b,c), int(b4['count']), part_index, i)
            faces.append((a+vertex_start, b+vertex_start, c+vertex_start))

        off=b7['payload']
        for i in range(b7['count']):
            bone,weight=unpack_from_checked('<If', data, off+i*8, f"MSH weight {i} in part {part_index}")
            weights.append((bone, weight))

        parts.append({
            'index': part_index,
            'name': meta['name'],
            'texture': meta['texture'],
            'vertex_start': vertex_start,
            'vertex_count': int(b4['count']),
            'triangle_start': triangle_start,
            'triangle_count': int(b6['count']),
            'weight_start': weight_start,
            'weight_count': int(b7['count']),
            'blocks': blocks,
        })
        all_blocks.extend(blocks)

    return {
        'vertices': np.array(verts, dtype=np.float32),
        'uvs': np.array(uvs, dtype=np.float32),
        'vertex_ids': vertex_ids,
        'ranges': ranges,
        'faces': np.array(faces, dtype=np.uint32),
        'weights': weights,
        'strings': ascii_strings(data[:chains[0][0]['offset']], min_len=4),
        'blocks': all_blocks,
        'parts': parts,
        'file_size': len(data),
    }

def parse_bfr(path):
    data=read_input_bytes(path, "BFR")
    frame_count=unpack_from_checked('<I', data, 0, "BFR frame count")[0]
    if frame_count == 0:
        raise ValueError("Invalid BFR frame count: 0")

    bbox_bytes_per_frame=6*4
    matrix_bytes_per_bone=12*4
    payload_bytes=len(data)-4
    if payload_bytes < frame_count*bbox_bytes_per_frame:
        raise ValueError(
            f"Truncated BFR data: frame count {frame_count} needs at least "
            f"{frame_count*bbox_bytes_per_frame} payload bytes, found {payload_bytes}"
        )
    if payload_bytes % frame_count != 0:
        raise ValueError(
            f"Invalid BFR file size: {payload_bytes} payload bytes is not divisible by "
            f"frame count {frame_count}"
        )

    per_frame_bytes=payload_bytes//frame_count
    transform_bytes_per_frame=per_frame_bytes-bbox_bytes_per_frame
    if transform_bytes_per_frame < 0 or transform_bytes_per_frame % matrix_bytes_per_bone != 0:
        raise ValueError(
            f"Cannot infer BFR bone count from file size: per-frame payload is "
            f"{per_frame_bytes} bytes"
        )
    bone_count = transform_bytes_per_frame // matrix_bytes_per_bone
    frame_stride = bone_count*matrix_bytes_per_bone
    transform_bytes=frame_count*frame_stride
    bbox_offset = 4 + transform_bytes
    expected_size=bbox_offset + frame_count*bbox_bytes_per_frame
    if expected_size != len(data):
        raise ValueError(f"Invalid BFR file size: expected {expected_size} bytes, found {len(data)}")
    require_bytes(data, 4, transform_bytes, "BFR transform matrix data")
    require_bytes(data, bbox_offset, frame_count*bbox_bytes_per_frame, "BFR bounding boxes")

    transforms = np.frombuffer(data, dtype='<f4', count=frame_count*bone_count*12, offset=4).reshape(frame_count, bone_count, 4, 3).copy()
    bboxes = np.frombuffer(data, dtype='<f4', count=frame_count*6, offset=bbox_offset).reshape(frame_count, 6).copy()
    return {'frame_count': frame_count, 'bone_count': bone_count, 'transforms': transforms, 'bboxes': bboxes, 'bbox_offset': bbox_offset, 'file_size': len(data)}

def parse_ani(path):
    data=read_input_bytes(path, "ANI")
    off=0
    clip_count=unpack_from_checked('<I', data, off, "ANI clip count")[0]; off+=4
    min_clip_bytes=4 + 8 + 4 + 4
    if clip_count > (len(data)-off)//min_clip_bytes:
        raise ValueError(f"Invalid ANI clip count {clip_count}: file is too small for that many clips")

    clips=[]
    for clip_index in range(clip_count):
        remaining_clips=clip_count-clip_index-1
        name_len=unpack_from_checked('<I', data, off, f"ANI clip {clip_index} name length")[0]; off+=4
        max_name_len=len(data)-off-(16 + remaining_clips*min_clip_bytes)
        if name_len > max_name_len:
            raise ValueError(
                f"Invalid ANI clip {clip_index} name length {name_len}: "
                f"only {max(0, max_name_len)} bytes available"
            )
        require_bytes(data, off, name_len, f"ANI clip {clip_index} name")
        name=data[off:off+name_len].decode('ascii', errors='replace'); off+=name_len
        start,end=unpack_from_checked('<II', data, off, f"ANI clip {clip_index} frame range"); off+=8
        if end < start:
            raise ValueError(f"Invalid ANI clip {name!r} frame range: {start}..{end}")
        speed=unpack_from_checked('<f', data, off, f"ANI clip {clip_index} speed")[0]; off+=4
        if not math.isfinite(speed):
            raise ValueError(f"Invalid ANI clip {name!r} speed: {speed!r}")
        event_count=unpack_from_checked('<I', data, off, f"ANI clip {clip_index} event count")[0]; off+=4
        min_event_bytes=4 + 4 + 1
        max_event_count=(len(data)-off-(remaining_clips*min_clip_bytes))//min_event_bytes
        if event_count > max_event_count:
            raise ValueError(
                f"Invalid ANI clip {name!r} event count {event_count}: "
                f"only {max(0, max_event_count)} events can fit"
            )
        events=[]
        for event_index in range(event_count):
            remaining_events=event_count-event_index-1
            sound_len=unpack_from_checked('<I', data, off, f"ANI clip {name!r} event {event_index} sound length")[0]; off+=4
            max_sound_len=len(data)-off-(5 + remaining_events*min_event_bytes + remaining_clips*min_clip_bytes)
            if sound_len > max_sound_len:
                raise ValueError(
                    f"Invalid ANI clip {name!r} event {event_index} sound length {sound_len}: "
                    f"only {max(0, max_sound_len)} bytes available"
                )
            require_bytes(data, off, sound_len, f"ANI clip {name!r} event {event_index} sound")
            sound=data[off:off+sound_len].decode('ascii', errors='replace'); off+=sound_len
            frame=unpack_from_checked('<I', data, off, f"ANI clip {name!r} event {event_index} frame")[0]; off+=4
            require_bytes(data, off, 1, f"ANI clip {name!r} event {event_index} volume")
            volume=data[off]; off+=1
            events.append({'sound': sound, 'frame': frame, 'volume': volume})
        clips.append({'name': name, 'start_frame': start, 'end_frame': end, 'frame_count': end-start+1, 'speed': speed, 'events': events})
    if off != len(data):
        raise ValueError(f"ANI trailing data: {len(data)-off} extra bytes at offset {off}")
    validate_ani_event_frames(clips)
    return {'clip_count': clip_count, 'clips': clips, 'bytes_read': off, 'file_size': len(data)}


def parse_3df_header(data: bytes):
    newlines=[]
    for i,b in enumerate(data[:512]):
        if b == 10:
            newlines.append(i)
            if len(newlines) == 4:
                break
    if len(newlines) < 4:
        raise ValueError("Could not read 3DF four-line header")

    header_len = newlines[-1] + 1
    try:
        lines=data[:header_len].decode('ascii').splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("3DF header is not ASCII") from exc
    if len(lines) != 4:
        raise ValueError(f"Expected 3DF four-line header, got {len(lines)} lines")
    if lines[0].strip() != '3df v1.1':
        raise ValueError(f"Unexpected 3DF signature: {lines[0]!r}")
    fmt=lines[1].strip().lower()
    if fmt not in ('p8', 'yiq'):
        raise NotImplementedError(f"Unsupported 3DF format: {fmt!r}")

    m=re.fullmatch(r'lod range:\s*(\d+)\s+(\d+)', lines[2].strip(), re.I)
    if not m:
        raise ValueError("Could not parse 3DF lod range")
    min_lod,max_lod=int(m.group(1)),int(m.group(2))
    if not (is_power_of_two(min_lod) and is_power_of_two(max_lod) and min_lod <= max_lod):
        raise ValueError(f"Invalid 3DF lod range: {min_lod} {max_lod}")

    m=re.fullmatch(r'aspect ratio:\s*(\d+)\s+(\d+)', lines[3].strip(), re.I)
    if not m:
        raise ValueError("Could not parse 3DF aspect ratio")
    ar_w,ar_h=int(m.group(1)),int(m.group(2))
    if not (is_power_of_two(ar_w) and is_power_of_two(ar_h)):
        raise ValueError(f"Invalid 3DF aspect ratio: {ar_w} {ar_h}")
    if ar_w != 1 and ar_h != 1:
        raise ValueError(f"Invalid 3DF aspect ratio: expected one side to be 1, got {ar_w} {ar_h}")

    return {
        'header_len': header_len,
        'lines': lines,
        'format': fmt,
        'min_lod': min_lod,
        'max_lod': max_lod,
        'aspect_ratio': (ar_w, ar_h),
    }


def dimensions_for_3df_lod(lod: int, ar_w: int, ar_h: int):
    if ar_w >= ar_h:
        return lod, max(1, lod*ar_h//ar_w)
    return max(1, lod*ar_w//ar_h), lod


def make_3df_mip_chain(min_lod: int, max_lod: int, ar_w: int, ar_h: int, pixel_offset: int):
    mips=[]
    offset=pixel_offset
    lod=max_lod
    while True:
        w,h=dimensions_for_3df_lod(lod, ar_w, ar_h)
        byte_count=w*h
        mips.append({'width':w, 'height':h, 'offset':offset, 'bytes':byte_count})
        offset += byte_count
        if lod == min_lod:
            break
        lod //= 2
    return mips


def validate_3df_pixels(data: bytes, mips, context: str):
    if not mips:
        raise ValueError(f"No 3DF mip levels for {context}")
    expected_end=mips[-1]['offset'] + mips[-1]['bytes']
    if len(data) < expected_end:
        pixel_offset=mips[0]['offset']
        expected=expected_end-pixel_offset
        found=max(0, len(data)-pixel_offset)
        raise ValueError(f"Truncated 3DF {context} texture pixels: need {expected} bytes, found {found}")
    if len(data) > expected_end:
        raise ValueError(f"3DF {context} trailing data: {len(data)-expected_end} extra bytes at offset {expected_end}")


def parse_3df_p8(path, channel_order='xrgb'):
    """Parse supported Gorky 17 3DF texture variants.

    Supported:
      - p8: 256-entry palette + 8-bit indices
      - yiq: 3dfx NCC/YIQ table + 8-bit YIQ texels

    The function name is kept for compatibility with older script calls.
    """
    data=read_input_bytes(path, "3DF")
    header=parse_3df_header(data)
    header_len=header['header_len']
    lines=header['lines']
    fmt=header['format']
    min_lod=header['min_lod']
    max_lod=header['max_lod']
    ar_w,ar_h=header['aspect_ratio']
    width,height=dimensions_for_3df_lod(max_lod, ar_w, ar_h)

    if fmt == 'p8':
        require_bytes(data, header_len, 1024, "3DF p8 palette")
        palette=np.frombuffer(data[header_len:header_len+1024], dtype=np.uint8).reshape(256,4).copy()
        pixel_offset=header_len+1024
        mips=make_3df_mip_chain(min_lod, max_lod, ar_w, ar_h, pixel_offset)
        validate_3df_pixels(data, mips, 'p8')
        top_mip=mips[0]
        idx=np.frombuffer(data[top_mip['offset']:top_mip['offset']+top_mip['bytes']], dtype=np.uint8).reshape(height,width).copy()
        rgba=np.zeros((height,width,4), dtype=np.uint8)

        if channel_order in ('xrgb', 'argb'):
            rgba[:,:,:3] = palette[idx][:,:,1:4]
        elif channel_order in ('xbgr', 'abgr'):
            rgba[:,:,:3] = palette[idx][:,:, [3,2,1]]
        elif channel_order == 'rgb':
            rgba[:,:,:3] = palette[idx,:3]
        elif channel_order == 'bgr':
            rgba[:,:,:3] = palette[idx,:3][:,:,::-1]
        else:
            raise ValueError("--texture-order must be xrgb, xbgr, rgb, or bgr")
        rgba[:,:,3] = 255
        image=Image.fromarray(rgba, 'RGBA')
        return {
            'header_lines': lines,
            'format': fmt,
            'width': width,
            'height': height,
            'min_lod': min_lod,
            'max_lod': max_lod,
            'aspect_ratio': [ar_w, ar_h],
            'palette_entries': 256,
            'palette_first_byte_minmax': [int(palette[:,0].min()), int(palette[:,0].max())],
            'mips': mips,
            'image': image,
            'file_size': len(data),
            'channel_order': channel_order,
        }

    if fmt == 'yiq':
        # 3dfx NCC table:
        #   16 big-endian signed 16-bit Y values
        #   4x3 big-endian signed 16-bit I RGB deltas
        #   4x3 big-endian signed 16-bit Q RGB deltas
        # Then 8-bit texels: YYYYIIQQ.
        table_len=80
        require_bytes(data, header_len, table_len, "3DF YIQ/NCC table")
        table=np.frombuffer(data[header_len:header_len+table_len], dtype='>i2').astype(np.int32)
        if table.size != 40:
            raise ValueError("Invalid YIQ/NCC table length")
        y_values=table[:16]
        i_rgb=table[16:28].reshape(4,3)
        q_rgb=table[28:40].reshape(4,3)
        pixel_offset=header_len+table_len
        mips=make_3df_mip_chain(min_lod, max_lod, ar_w, ar_h, pixel_offset)
        validate_3df_pixels(data, mips, 'YIQ/NCC')
        top_mip=mips[0]
        idx=np.frombuffer(data[top_mip['offset']:top_mip['offset']+top_mip['bytes']], dtype=np.uint8).reshape(height,width).copy()

        y_idx=(idx >> 4) & 0x0F
        i_idx=(idx >> 2) & 0x03
        q_idx=idx & 0x03
        rgb=y_values[y_idx][...,None] + i_rgb[i_idx] + q_rgb[q_idx]
        rgb=np.clip(rgb,0,255).astype(np.uint8)

        rgba=np.zeros((height,width,4), dtype=np.uint8)
        rgba[:,:,:3]=rgb
        rgba[:,:,3]=255
        image=Image.fromarray(rgba, 'RGBA')

        return {
            'header_lines': lines,
            'format': fmt,
            'width': width,
            'height': height,
            'min_lod': min_lod,
            'max_lod': max_lod,
            'aspect_ratio': [ar_w, ar_h],
            'ncc_table': {
                'y_values': y_values.tolist(),
                'i_rgb': i_rgb.tolist(),
                'q_rgb': q_rgb.tolist(),
            },
            'mips': mips,
            'image': image,
            'file_size': len(data),
            'channel_order': 'ncc_yiq_YYYYIIQQ',
        }


# -----------------------------
# Skinning / export helpers
# -----------------------------

def bfr_frame_range_text(bfr):
    count=int(bfr.get('frame_count', 0)) if bfr else 0
    if count <= 0:
        return '<empty>'
    return f"0..{count-1}"


def validate_bfr_frame_index(bfr, frame_index, subject='frame'):
    if bfr is None:
        raise ValueError(f"{subject} requires BFR data")
    count=int(bfr.get('frame_count', 0))
    if count <= 0:
        raise ValueError(f"{subject} cannot be exported because the BFR has no frames")
    if frame_index < 0 or frame_index >= count:
        raise ValueError(
            f"{subject} {frame_index} is outside valid BFR frame range {bfr_frame_range_text(bfr)}"
        )


def validate_clip_frame_ranges(clips, bfr):
    if not clips:
        return
    if bfr is None:
        raise ValueError("clip export requires BFR data")
    valid_range=bfr_frame_range_text(bfr)
    invalid=[]
    for clip in clips:
        start=int(clip['start_frame'])
        end=int(clip['end_frame'])
        if start < 0 or end < start or end >= int(bfr.get('frame_count', 0)):
            invalid.append(f"{clip['name']!r} {start}..{end}")
    if invalid:
        raise ValueError(
            "Selected ANI clip frame range is outside valid BFR frame range "
            f"{valid_range}: " + '; '.join(invalid) +
            ". Invalid clips abort the export."
        )
    validate_ani_event_frames(clips, bfr, "Selected ANI event metadata")


def validate_ani_event_frames(clips, bfr=None, context='ANI event metadata'):
    if not clips:
        return
    clip_invalid=[]
    bfr_invalid=[]
    bfr_count=int(bfr.get('frame_count', 0)) if bfr is not None else None
    for clip in clips:
        name=clip.get('name', '<unnamed>')
        start=int(clip['start_frame'])
        end=int(clip['end_frame'])
        for event_index,event in enumerate(clip.get('events', [])):
            try:
                frame=int(event['frame'])
            except (KeyError, TypeError, ValueError):
                raise ValueError(
                    f"{context}: clip {name!r} event {event_index} has invalid frame "
                    f"{event.get('frame') if isinstance(event, dict) else event!r}"
                ) from None
            if frame < start or frame > end:
                clip_invalid.append(
                    f"{name!r} event {event_index} frame {frame} outside clip frame range {start}..{end}"
                )
            elif bfr_count is not None and (frame < 0 or frame >= bfr_count):
                bfr_invalid.append(
                    f"{name!r} event {event_index} frame {frame} outside BFR frame range {bfr_frame_range_text(bfr)}"
                )
    if clip_invalid:
        raise ValueError(f"{context}: event frame is outside owning clip range: " + '; '.join(clip_invalid))
    if bfr_invalid:
        raise ValueError(f"{context}: event frame is outside BFR frame range: " + '; '.join(bfr_invalid))


def validate_baked_fps(fps):
    try:
        value=float(fps)
    except (TypeError, ValueError):
        raise ValueError("--fps must be a finite number greater than zero") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError("--fps must be a finite number greater than zero")
    return value


def validate_frame_step(every):
    try:
        value=int(every)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("--every must be an integer greater than or equal to 1") from None
    if value != every or value < 1:
        raise ValueError("--every must be an integer greater than or equal to 1")
    return value


def validate_timeline_gap_frames(timeline_gap_frames):
    try:
        value=int(timeline_gap_frames)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("--timeline-gap-frames must be an integer greater than or equal to 0") from None
    if value != timeline_gap_frames or value < 0:
        raise ValueError("--timeline-gap-frames must be an integer greater than or equal to 0")
    return value


def baked_animation_semantics(fps, every):
    step=validate_frame_step(every)
    return {
        'frame_numbering_convention': 'zero_based_bfr_indices',
        'frame_numbering_detail': 'ANI start_frame/end_frame values are used directly as BFR frame indices; no one-based conversion is applied.',
        'timing_formula': 'per-clip time_seconds = (source_bfr_frame - first_sampled_bfr_frame) / fps; combined timeline sampled frames use clip_base_time + (source_bfr_frame - first_sampled_bfr_frame) / fps, with clip_base_time advanced by source clip duration and repeated gap samples.',
        'fps': float(fps),
        'every': int(step),
        'ani_speed': 'ignored_for_baked_timing',
        'ani_speed_detail': 'ANI speed is parsed and preserved in ANI metadata, but baked GLB/GLTF playback timing is controlled only by --fps.',
        'interpolation': 'STEP',
        'rest_pose': 'first_sampled_bfr_frame',
        'rest_pose_detail': 'The base mesh and zero morph weights are the first sampled BFR frame, not the bind/static mesh.',
    }


def skin_vertices(mesh, bfr, frame_index):
    verts=mesh['vertices']
    if bfr is not None:
        validate_bfr_frame_index(bfr, frame_index, 'skinning frame')
    validate_skin_ranges(mesh)
    validate_skin_bone_ids(mesh, bfr)
    # Static/unskinned mesh parts use an empty type-7 weight block and often
    # a BFR containing only one AABB. In that case, the bind vertices are the
    # final vertices.
    if bfr is None or bfr.get('bone_count', 0) == 0 or len(mesh.get('weights', [])) == 0:
        return verts.copy()

    out=np.zeros_like(verts)
    mats=bfr['transforms'][frame_index]
    for i,p in enumerate(verts):
        a,b=mesh['ranges'][i]
        if a == b:
            out[i]=p
            continue
        acc=np.zeros(3, dtype=np.float32)
        total=0.0
        for wi in range(a,b):
            bone, weight_percent = mesh['weights'][wi]
            bone=int(bone)
            w=weight_percent/100.0
            m=mats[bone]
            acc += w * (p @ m[:3, :] + m[3, :])
            total += w
        if total:
            if abs(total-1.0) > 1e-6:
                acc /= total
            out[i]=acc
        else:
            out[i]=p
    return out


def sanitize_name(value, fallback='asset'):
    safe=re.sub(r'[^A-Za-z0-9._-]+', '_', str(value or '')).strip('._-')
    return safe or fallback


def path_stem_name(path, fallback='asset'):
    return sanitize_name(Path(path).stem if path else '', fallback)


def derive_export_names(msh_path, texture_path=None, output_name=None):
    mesh_name=sanitize_name(output_name, 'mesh') if output_name else path_stem_name(msh_path, 'mesh')
    texture_name=(mesh_name if output_name else path_stem_name(texture_path, f'{mesh_name}_texture')) if texture_path else None
    material_name=texture_name or f'{mesh_name}_material'
    return {
        'mesh_name': mesh_name,
        'object_name': mesh_name,
        'node_name': mesh_name,
        'material_name': material_name,
        'texture_name': texture_name,
        'texture_png': f'{texture_name}.png' if texture_name else None,
        'mtl_name': f'{mesh_name}.mtl',
        'static_obj': f'{mesh_name}_static.obj',
    }


def make_material_record(name, texture_png=None, image=None, source_texture=None, texture_name=None):
    resolved_texture_name=texture_name or (Path(texture_png).stem if texture_png else name)
    return {
        'name': sanitize_name(name, 'material'),
        'texture_png': texture_png,
        'image': image,
        'source_texture': str(source_texture) if source_texture else None,
        'texture_name': sanitize_name(resolved_texture_name, 'texture'),
    }


def unique_material_name(base_name, used_names, fallback='material'):
    base=sanitize_name(base_name, fallback)
    if base not in used_names:
        return base
    suffix=2
    while f"{base}_{suffix}" in used_names:
        suffix += 1
    return f"{base}_{suffix}"


def validate_material_plan(materials, part_material_names, context='material plan'):
    names=[]
    for index,material in enumerate(materials):
        name=material.get('name')
        if not name:
            raise ValueError(f"{context}: material {index} has no name")
        if name in names:
            raise ValueError(f"{context}: duplicate material name {name!r}")
        names.append(name)
    known=set(names)
    missing=sorted({name for name in part_material_names.values() if name not in known})
    if missing:
        raise ValueError(
            f"{context}: part material assignment references missing material record(s): "
            + ', '.join(repr(name) for name in missing)
        )


def unique_part_textures(mesh):
    seen=set()
    textures=[]
    for part in mesh.get('parts', []):
        texture=(part.get('texture') or '').strip()
        if texture and texture not in seen:
            seen.add(texture)
            textures.append(texture)
    return textures


def resolve_msh_texture_path(msh_dir, texture):
    texture=str(texture).strip()
    windows_ref=PureWindowsPath(texture)
    posix_ref=PurePosixPath(texture)
    if windows_ref.anchor or posix_ref.anchor:
        raise ValueError(
            f"MSH texture reference {texture!r} must be relative to the MSH directory; "
            "absolute paths are not allowed."
        )
    if '..' in windows_ref.parts or '..' in posix_ref.parts:
        raise ValueError(
            f"MSH texture reference {texture!r} cannot contain parent directory components."
        )

    base=Path(msh_dir).resolve()
    candidate=(Path(msh_dir)/texture).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        raise ValueError(
            f"MSH texture reference {texture!r} resolves outside the MSH directory: {candidate}"
        ) from None
    return candidate


def resolve_export_materials(mesh, msh_path, cli_texture, texture_order, names):
    """Resolve OBJ/GLB materials and texture images.

    Policy:
      - --3df is an explicit override and applies to every MSH part.
      - without --3df, texture names embedded in the MSH are resolved next to
        the MSH file; missing referenced textures are errors.
      - meshes without texture references export with one untextured material.
    """
    part_material_names={}
    tex_infos=[]

    if cli_texture:
        tex=parse_3df_p8(cli_texture, texture_order)
        texture_png=names['texture_png']
        material=make_material_record(
            names['material_name'],
            texture_png=texture_png,
            image=tex['image'],
            source_texture=cli_texture,
            texture_name=names['texture_name'] or names['material_name'],
        )
        for part in mesh.get('parts', []):
            part_material_names[part['index']]=material['name']
        tex_infos.append({k:v for k,v in tex.items() if k != 'image'})
        validate_material_plan([material], part_material_names, 'CLI texture material resolution')
        return {
            'mode': 'cli_override_all_parts',
            'materials': [material],
            'part_material_names': part_material_names,
            'texture_infos': tex_infos,
        }

    textures=unique_part_textures(mesh)
    if textures:
        materials=[]
        texture_to_material={}
        used_names=set()
        msh_dir=Path(msh_path).parent
        for texture in textures:
            tex_path=resolve_msh_texture_path(msh_dir, texture)
            if not tex_path.exists():
                raise ValueError(
                    f"MSH references texture {texture!r}, but it was not found next to the MSH: {tex_path}. "
                    "Provide --3df to override all MSH texture references."
                )
            tex=parse_3df_p8(tex_path, texture_order)
            texture_stem=path_stem_name(tex_path, 'texture')
            material_name=unique_material_name(texture_stem, used_names, 'material')
            used_names.add(material_name)
            texture_png=f"{material_name}.png"
            material=make_material_record(
                material_name,
                texture_png=texture_png,
                image=tex['image'],
                source_texture=tex_path,
                texture_name=material_name,
            )
            texture_to_material[texture]=material['name']
            materials.append(material)
            tex_infos.append({k:v for k,v in tex.items() if k != 'image'})

        untextured_material=None
        has_untextured_parts=any(not (part.get('texture') or '').strip() for part in mesh.get('parts', []))
        if has_untextured_parts:
            used_names={material['name'] for material in materials}
            fallback_name=sanitize_name(names['material_name'], 'material')
            if fallback_name in used_names:
                fallback_name=unique_material_name(f"{fallback_name}_untextured", used_names)
            untextured_material=make_material_record(fallback_name)
            materials.append(untextured_material)

        for part in mesh.get('parts', []):
            texture=(part.get('texture') or '').strip()
            if texture:
                part_material_names[part['index']]=texture_to_material[texture]
            else:
                part_material_names[part['index']]=untextured_material['name']
        validate_material_plan(materials, part_material_names, 'MSH material resolution')
        return {
            'mode': 'msh_references',
            'materials': materials,
            'part_material_names': part_material_names,
            'texture_infos': tex_infos,
        }

    material=make_material_record(names['material_name'])
    for part in mesh.get('parts', []):
        part_material_names[part['index']]=material['name']
    validate_material_plan([material], part_material_names, 'untextured material resolution')
    return {
        'mode': 'untextured',
        'materials': [material],
        'part_material_names': part_material_names,
        'texture_infos': [],
    }


def write_material_textures(out_dir, materials):
    for material in materials:
        image=material.get('image')
        texture_png=material.get('texture_png')
        if image is not None and texture_png:
            image.save(out_dir/texture_png)


def part_face_ranges(parts, face_count):
    if not parts or len(parts) <= 1:
        return []
    ranges=[]
    covered=[]
    for part in sorted(parts, key=lambda p: int(p['triangle_start'])):
        start=int(part['triangle_start'])
        count=int(part['triangle_count'])
        end=start+count
        if start < 0 or count < 0 or end > face_count:
            raise ValueError(
                f"MSH part {part.get('index')} face range {start}..{end-1} is outside face range 0..{face_count-1}"
            )
        ranges.append((part, start, end))
        covered.extend(range(start,end))
    if sorted(covered) != list(range(face_count)):
        raise ValueError("MSH part face ranges do not cover every face exactly once")
    return ranges


def write_obj(path, vertices, faces, uvs, mtl_name, material_name, object_name='mesh', parts=None, part_material_names=None):
    path=Path(path)
    validate_faces_for_vertex_count(faces, len(vertices), 'OBJ export')
    with path.open('w', encoding='utf-8') as f:
        f.write("# Gorky 17/Odium gfx3d export\n")
        if mtl_name:
            f.write(f"mtllib {mtl_name}\n")
        f.write(f"o {object_name}\n")
        for x,y,z in vertices:
            f.write(f"v {float(x):.6f} {float(y):.6f} {float(z):.6f}\n")
        for u,v in uvs:
            f.write(f"vt {float(u):.6f} {float(1.0-v):.6f}\n")
        ranges=part_face_ranges(parts or [], len(faces))
        if ranges:
            for part,start,end in ranges:
                part_name=sanitize_name(part.get('name') or f"part_{part.get('index', start)}", f"part_{start}")
                f.write(f"g {object_name}_{part_name}\n")
                f.write(f"usemtl {(part_material_names or {}).get(part['index'], material_name)}\n")
                for a,b,c in faces[start:end]:
                    f.write(f"f {a+1}/{a+1} {b+1}/{b+1} {c+1}/{c+1}\n")
        else:
            f.write(f"usemtl {material_name}\n")
            for a,b,c in faces:
                f.write(f"f {a+1}/{a+1} {b+1}/{b+1} {c+1}/{c+1}\n")

def write_mtl(out_dir, mtl_name, materials):
    if isinstance(materials, dict) or isinstance(materials, str):
        raise TypeError("write_mtl expects a list of material records")
    lines=[]
    for index,material in enumerate(materials):
        if index:
            lines.append("")
        lines.extend([
            f"newmtl {material['name']}",
            "Ka 1.000000 1.000000 1.000000",
            "Kd 1.000000 1.000000 1.000000",
            "Ks 0.000000 0.000000 0.000000",
            "d 1.000000",
            "illum 1",
        ])
        if material.get('texture_png'):
            lines.append(f"map_Kd {material['texture_png']}")
    (out_dir/mtl_name).write_text('\n'.join(lines) + '\n', encoding='utf-8')

def select_clips(ani, clip_name=None, all_clips=False):
    if clip_name and all_clips:
        raise ValueError("--clip and --all-clips are mutually exclusive")
    if all_clips:
        if ani is None:
            raise ValueError("--all-clips requires --ani")
        if not ani['clips']:
            raise ValueError("--all-clips requested, but the ANI contains no clips")
        return ani['clips']
    if clip_name and ani is None:
        raise ValueError("--clip requires --ani")
    if ani is None:
        return []
    if clip_name:
        candidates=[c for c in ani['clips'] if c['name'].lower()==clip_name.lower()]
        if not candidates:
            raise ValueError("clip not found. Available: " + ', '.join(c['name'] for c in ani['clips']))
        return [candidates[0]]
    return []

def remove_stale_obj_sequence_frames(seq, mesh_name, clip_name):
    prefix=f"{mesh_name}_{clip_name}_"
    frame_name=re.compile(rf"^{re.escape(prefix)}\d{{3,}}\.obj$")
    for path in seq.iterdir():
        if path.is_file() and frame_name.fullmatch(path.name):
            path.unlink()

def export_obj_clip_sequence(out_dir, mesh, bfr, clip, names, every=1, part_material_names=None, materials=None, material_name=None):
    step=validate_frame_step(every)
    clip_name=sanitize_name(clip['name'], 'clip')
    seq=out_dir/f"{names['mesh_name']}_clip_{clip_name}"
    seq.mkdir(exist_ok=True)
    remove_stale_obj_sequence_frames(seq, names['mesh_name'], clip_name)
    shutil.copy(out_dir/names['mtl_name'], seq/names['mtl_name'])
    for material in materials or []:
        texture_png=material.get('texture_png')
        if texture_png and (out_dir/texture_png).exists():
            shutil.copy(out_dir/texture_png, seq/texture_png)
    for frame in range(clip['start_frame'], clip['end_frame']+1, step):
        write_obj(
            seq/f"{names['mesh_name']}_{clip_name}_{frame:03d}.obj",
            skin_vertices(mesh,bfr,frame),
            mesh['faces'],
            mesh['uvs'],
            names['mtl_name'],
            material_name or names['material_name'],
            names['object_name'],
            parts=mesh.get('parts'),
            part_material_names=part_material_names,
        )

# -----------------------------
# Minimal GLB writer
# -----------------------------

_COMPONENT_FLOAT = 5126
_COMPONENT_UINT16 = 5123
_COMPONENT_UINT32 = 5125
_ARRAY_BUFFER = 34962
_ELEMENT_ARRAY_BUFFER = 34963
_COMPONENT_BYTE_SIZES = {
    _COMPONENT_FLOAT: 4,
    _COMPONENT_UINT16: 2,
    _COMPONENT_UINT32: 4,
}
_ACCESSOR_TYPE_SIZES = {
    'SCALAR': 1,
    'VEC2': 2,
    'VEC3': 3,
    'VEC4': 4,
    'MAT2': 4,
    'MAT3': 9,
    'MAT4': 16,
}

def _pad4(b: bytes, pad_byte=b'\x00') -> bytes:
    return b + pad_byte * ((4 - len(b) % 4) % 4)


def index_component_type_for_faces(faces):
    indices=np.asarray(faces)
    if indices.size == 0:
        raise ValueError("Baked GLB/GLTF requires at least one face index")
    if np.any(indices < 0):
        raise ValueError("Baked GLB/GLTF face indices must be non-negative")
    max_index=int(indices.max())
    if max_index <= 65535:
        return _COMPONENT_UINT16
    if max_index <= 0xFFFFFFFF:
        return _COMPONENT_UINT32
    raise ValueError(f"Baked GLB/GLTF face index {max_index} exceeds UNSIGNED_INT range")


def index_component_name(component_type):
    if component_type == _COMPONENT_UINT16:
        return 'UNSIGNED_SHORT'
    if component_type == _COMPONENT_UINT32:
        return 'UNSIGNED_INT'
    return str(component_type)


def validate_baked_mesh_geometry(mesh):
    vertices=np.asarray(mesh.get('vertices'))
    uvs=np.asarray(mesh.get('uvs'))
    faces=np.asarray(mesh.get('faces'))
    if vertices.ndim != 2 or vertices.shape[1] != 3:
        raise ValueError("Baked GLB/GLTF requires vertices shaped Nx3")
    if uvs.ndim != 2 or uvs.shape[1] != 2:
        raise ValueError("Baked GLB/GLTF requires UVs shaped Nx2")
    if len(vertices) != len(uvs):
        raise ValueError(f"Baked GLB/GLTF vertex/UV count mismatch: {len(vertices)} vs {len(uvs)}")
    if faces.ndim != 2 or faces.shape[1] != 3:
        raise ValueError("Baked GLB/GLTF requires triangular faces shaped Nx3")
    if faces.size == 0:
        raise ValueError("Baked GLB/GLTF requires at least one triangle")
    if np.any(faces < 0):
        raise ValueError("Baked GLB/GLTF face indices must be non-negative")
    max_index=int(faces.max())
    if max_index >= len(vertices):
        raise ValueError(
            f"Baked GLB/GLTF face index {max_index} is outside vertex range 0..{len(vertices)-1}"
        )


def accessor_numpy_dtype(component_type):
    if component_type == _COMPONENT_FLOAT:
        return np.dtype('<f4')
    if component_type == _COMPONENT_UINT16:
        return np.dtype('<u2')
    if component_type == _COMPONENT_UINT32:
        return np.dtype('<u4')
    raise ValueError(f"Unsupported glTF accessor component type: {component_type}")


def accessor_component_count(gltf_type):
    if gltf_type not in _ACCESSOR_TYPE_SIZES:
        raise ValueError(f"Unsupported glTF accessor type: {gltf_type!r}")
    return _ACCESSOR_TYPE_SIZES[gltf_type]


def read_accessor_array(gltf, bin_data: bytes, accessor_index: int):
    accessors=gltf.get('accessors', [])
    if accessor_index < 0 or accessor_index >= len(accessors):
        raise ValueError(f"Invalid glTF accessor index: {accessor_index}")
    acc=accessors[accessor_index]
    views=gltf.get('bufferViews', [])
    view_index=acc.get('bufferView')
    if view_index is None or view_index < 0 or view_index >= len(views):
        raise ValueError(f"Accessor {accessor_index} references invalid bufferView {view_index}")
    view=views[view_index]
    if 'byteStride' in view:
        raise ValueError(f"Accessor {accessor_index} uses unsupported byteStride validation path")
    dtype=accessor_numpy_dtype(acc['componentType'])
    component_count=accessor_component_count(acc['type'])
    count=int(acc['count'])
    start=int(view.get('byteOffset', 0)) + int(acc.get('byteOffset', 0))
    item_count=count*component_count
    arr=np.frombuffer(bin_data, dtype=dtype, count=item_count, offset=start)
    if component_count == 1:
        return arr.reshape(count)
    return arr.reshape(count, component_count)


def validate_gltf_document(gltf, bin_data: bytes):
    buffers=gltf.get('buffers')
    if not isinstance(buffers, list) or len(buffers) != 1:
        raise ValueError("glTF must contain exactly one buffer")
    declared_buffer_len=int(buffers[0].get('byteLength', -1))
    if declared_buffer_len < 0:
        raise ValueError("glTF buffer byteLength is missing or invalid")
    if declared_buffer_len > len(bin_data):
        raise ValueError(
            f"glTF buffer byteLength {declared_buffer_len} exceeds binary payload {len(bin_data)}"
        )

    views=gltf.get('bufferViews', [])
    for i,view in enumerate(views):
        if view.get('buffer', 0) != 0:
            raise ValueError(f"bufferView {i} references unsupported buffer {view.get('buffer')}")
        offset=int(view.get('byteOffset', 0))
        length=int(view.get('byteLength', -1))
        if offset < 0 or length < 0:
            raise ValueError(f"bufferView {i} has invalid byte range")
        if offset % 4 != 0:
            raise ValueError(f"bufferView {i} byteOffset {offset} is not 4-byte aligned")
        if offset + length > declared_buffer_len:
            raise ValueError(f"bufferView {i} exceeds declared buffer byteLength")
        if 'target' in view and view['target'] not in (_ARRAY_BUFFER, _ELEMENT_ARRAY_BUFFER):
            raise ValueError(f"bufferView {i} has unsupported target {view['target']}")

    accessors=gltf.get('accessors', [])
    for i,acc in enumerate(accessors):
        view_index=acc.get('bufferView')
        if view_index is None or view_index < 0 or view_index >= len(views):
            raise ValueError(f"Accessor {i} references invalid bufferView {view_index}")
        component_type=acc.get('componentType')
        if component_type not in _COMPONENT_BYTE_SIZES:
            raise ValueError(f"Accessor {i} has unsupported componentType {component_type}")
        gltf_type=acc.get('type')
        component_count=accessor_component_count(gltf_type)
        count=int(acc.get('count', -1))
        if count < 0:
            raise ValueError(f"Accessor {i} has invalid count {count}")
        byte_offset=int(acc.get('byteOffset', 0))
        component_size=_COMPONENT_BYTE_SIZES[component_type]
        if byte_offset < 0 or byte_offset % component_size != 0:
            raise ValueError(f"Accessor {i} has invalid byteOffset {byte_offset}")
        view=views[view_index]
        element_size=component_count*component_size
        if 'byteStride' in view:
            stride=int(view['byteStride'])
            needed=0 if count == 0 else byte_offset + stride*(count-1) + element_size
        else:
            needed=byte_offset + count*element_size
        if needed > int(view['byteLength']):
            raise ValueError(f"Accessor {i} exceeds bufferView {view_index}")

        if count and ('min' in acc or 'max' in acc):
            arr=read_accessor_array(gltf, bin_data, i)
            vals=arr.reshape(count, component_count)
            if 'min' in acc:
                expected_min=np.asarray(acc['min'])
                actual_min=vals.min(axis=0)
                if component_type == _COMPONENT_FLOAT:
                    ok=np.allclose(actual_min, expected_min, rtol=0, atol=1e-6)
                else:
                    ok=np.array_equal(actual_min.astype(np.uint64), expected_min.astype(np.uint64))
                if not ok:
                    raise ValueError(f"Accessor {i} min metadata does not match binary data")
            if 'max' in acc:
                expected_max=np.asarray(acc['max'])
                actual_max=vals.max(axis=0)
                if component_type == _COMPONENT_FLOAT:
                    ok=np.allclose(actual_max, expected_max, rtol=0, atol=1e-6)
                else:
                    ok=np.array_equal(actual_max.astype(np.uint64), expected_max.astype(np.uint64))
                if not ok:
                    raise ValueError(f"Accessor {i} max metadata does not match binary data")

    for mesh_index,mesh in enumerate(gltf.get('meshes', [])):
        for prim_index,primitive in enumerate(mesh.get('primitives', [])):
            if primitive.get('mode', 4) != 4:
                raise ValueError(f"Mesh {mesh_index} primitive {prim_index} is not TRIANGLES")
            attrs=primitive.get('attributes', {})
            pos_index=attrs.get('POSITION')
            if pos_index is None:
                raise ValueError(f"Mesh {mesh_index} primitive {prim_index} is missing POSITION")
            pos_acc=accessors[pos_index]
            if pos_acc['componentType'] != _COMPONENT_FLOAT or pos_acc['type'] != 'VEC3':
                raise ValueError(f"Mesh {mesh_index} primitive {prim_index} POSITION accessor is invalid")
            vertex_count=int(pos_acc['count'])
            if 'TEXCOORD_0' in attrs:
                uv_acc=accessors[attrs['TEXCOORD_0']]
                if uv_acc['componentType'] != _COMPONENT_FLOAT or uv_acc['type'] != 'VEC2':
                    raise ValueError(f"Mesh {mesh_index} primitive {prim_index} TEXCOORD_0 accessor is invalid")
                if int(uv_acc['count']) != vertex_count:
                    raise ValueError(f"Mesh {mesh_index} primitive {prim_index} UV count does not match POSITION")
            index_acc=accessors[primitive['indices']]
            if index_acc['type'] != 'SCALAR' or index_acc['componentType'] not in (_COMPONENT_UINT16, _COMPONENT_UINT32):
                raise ValueError(f"Mesh {mesh_index} primitive {prim_index} index accessor is invalid")
            index_view=views[index_acc['bufferView']]
            if index_view.get('target') != _ELEMENT_ARRAY_BUFFER:
                raise ValueError(f"Mesh {mesh_index} primitive {prim_index} index bufferView target is invalid")
            if index_acc.get('max') and int(index_acc['max'][0]) >= vertex_count:
                raise ValueError(f"Mesh {mesh_index} primitive {prim_index} index max exceeds vertex count")
            for target in primitive.get('targets', []):
                if 'POSITION' in target:
                    target_acc=accessors[target['POSITION']]
                    if target_acc['componentType'] != _COMPONENT_FLOAT or target_acc['type'] != 'VEC3':
                        raise ValueError(f"Mesh {mesh_index} primitive {prim_index} target POSITION accessor is invalid")
                    if int(target_acc['count']) != vertex_count:
                        raise ValueError(f"Mesh {mesh_index} primitive {prim_index} target count mismatch")

    return True

class GltfBuilder:
    def __init__(self):
        self.bin = bytearray()
        self.bufferViews=[]
        self.accessors=[]
        self.images=[]
        self.samplers=[]
        self.textures=[]
        self.materials=[]
        self.meshes=[]
        self.nodes=[]
        self.scenes=[]
        self.animations=[]
        self.extensionsUsed=[]
        self.extras=None

    def add_blob(self, blob: bytes, target=None, byte_stride=None):
        # bufferView byte offsets must be 4-byte aligned.
        pad=(4 - (len(self.bin)%4))%4
        if pad:
            self.bin.extend(b'\x00'*pad)
        off=len(self.bin)
        self.bin.extend(blob)
        # pad payload for following views, but bufferView length excludes padding.
        self.bin.extend(b'\x00'*((4 - (len(blob)%4))%4))
        view={'buffer':0,'byteOffset':off,'byteLength':len(blob)}
        if target is not None:
            view['target']=target
        if byte_stride is not None:
            view['byteStride']=byte_stride
        self.bufferViews.append(view)
        return len(self.bufferViews)-1

    def add_accessor(self, array, gltf_type, component_type=_COMPONENT_FLOAT, target=None, include_minmax=True):
        arr=np.asarray(array)
        # Ensure glTF little-endian binary.
        if component_type == _COMPONENT_FLOAT:
            arr=arr.astype('<f4', copy=False)
        elif component_type in (_COMPONENT_UINT16, _COMPONENT_UINT32):
            if arr.size:
                if np.any(arr < 0):
                    raise ValueError("glTF unsigned integer accessor cannot contain negative values")
                max_allowed=65535 if component_type == _COMPONENT_UINT16 else 0xFFFFFFFF
                max_value=int(arr.max())
                if max_value > max_allowed:
                    raise ValueError(
                        f"glTF accessor value {max_value} exceeds {index_component_name(component_type)} range"
                    )
            arr=arr.astype('<u2' if component_type == _COMPONENT_UINT16 else '<u4', copy=False)
        else:
            raise ValueError(f"Unsupported component type: {component_type}")
        blob=arr.tobytes(order='C')
        view_idx=self.add_blob(blob, target=target)
        if gltf_type == 'SCALAR':
            count=int(arr.size)
        else:
            count=int(arr.shape[0])
        acc={'bufferView':view_idx,'byteOffset':0,'componentType':component_type,'count':count,'type':gltf_type}
        if include_minmax and arr.size and component_type == _COMPONENT_FLOAT:
            if gltf_type == 'SCALAR':
                flat=arr.reshape(-1)
                acc['min']=[float(np.min(flat))]
                acc['max']=[float(np.max(flat))]
            else:
                vals=arr.reshape(count, -1)
                acc['min']=[float(x) for x in vals.min(axis=0)]
                acc['max']=[float(x) for x in vals.max(axis=0)]
        elif include_minmax and arr.size and component_type in (_COMPONENT_UINT16, _COMPONENT_UINT32):
            flat=arr.reshape(-1)
            acc['min']=[int(flat.min())]
            acc['max']=[int(flat.max())]
        self.accessors.append(acc)
        return len(self.accessors)-1

    def add_png_image(self, image: Image.Image, name='texture'):
        buf=io.BytesIO()
        image.save(buf, format='PNG')
        png=buf.getvalue()
        view_idx=self.add_blob(png)
        self.images.append({'name':name,'mimeType':'image/png','bufferView':view_idx})
        img_idx=len(self.images)-1
        self.samplers.append({'magFilter':9729,'minFilter':9987,'wrapS':10497,'wrapT':10497})
        sampler_idx=len(self.samplers)-1
        self.textures.append({'sampler':sampler_idx,'source':img_idx})
        return len(self.textures)-1

    def to_json(self):
        gltf={
            'asset': {'version':'2.0', 'generator':'gorky17_3dgfx_exporter.py'},
            'buffers': [{'byteLength': len(self.bin)}],
            'bufferViews': self.bufferViews,
            'accessors': self.accessors,
            'meshes': self.meshes,
            'nodes': self.nodes,
            'scenes': self.scenes,
            'scene': 0,
        }
        if self.images: gltf['images']=self.images
        if self.samplers: gltf['samplers']=self.samplers
        if self.textures: gltf['textures']=self.textures
        if self.materials: gltf['materials']=self.materials
        if self.animations: gltf['animations']=self.animations
        if self.extensionsUsed: gltf['extensionsUsed']=sorted(set(self.extensionsUsed))
        if self.extras is not None: gltf['extras']=self.extras
        return gltf

    def write_glb(self, path):
        gltf=self.to_json()
        validate_gltf_document(gltf, bytes(self.bin))
        gltf_json=json.dumps(gltf, separators=(',',':')).encode('utf-8')
        json_chunk=_pad4(gltf_json, b' ')
        bin_chunk=_pad4(bytes(self.bin), b'\x00')
        total_len=12 + 8 + len(json_chunk) + 8 + len(bin_chunk)
        with Path(path).open('wb') as f:
            f.write(struct.pack('<4sII', b'glTF', 2, total_len))
            f.write(struct.pack('<I4s', len(json_chunk), b'JSON'))
            f.write(json_chunk)
            f.write(struct.pack('<I4s', len(bin_chunk), b'BIN\x00'))
            f.write(bin_chunk)

    def write_gltf(self, path):
        # Embedded .gltf with base64 buffer, useful for debugging.
        gltf=self.to_json()
        validate_gltf_document(gltf, bytes(self.bin))
        encoded=base64.b64encode(bytes(self.bin)).decode('ascii')
        gltf['buffers']=[{'uri':'data:application/octet-stream;base64,'+encoded, 'byteLength':len(self.bin)}]
        Path(path).write_text(json.dumps(gltf, indent=2), encoding='utf-8')



def make_baked_gltf(
    mesh,
    bfr,
    clips,
    out_path,
    texture_image=None,
    materials=None,
    part_material_names=None,
    fps=15.0,
    every=1,
    fmt='glb',
    include_all_targets=False,
    combined_timeline=False,
    timeline_gap_frames=0,
    glb_flip_v=False,
    mesh_name='mesh',
    material_name='material',
    texture_name='texture',
):
    """Write an animated GLB/GLTF using baked morph-target vertex animation.

    Modes:
      - default: one glTF animation per ANI clip
      - combined_timeline=True: one Blender-obvious animation named ALL_CLIPS_TIMELINE
        containing all selected clips sequentially. Clip time ranges are stored in extras.
    """
    if bfr is None:
        raise ValueError("Baked GLB/GLTF requires --bfr")
    if not clips:
        raise ValueError("Baked GLB/GLTF requires --clip NAME or --all-anims with --ani")
    fps=validate_baked_fps(fps)
    validate_baked_mesh_geometry(mesh)
    validate_clip_frame_ranges(clips, bfr)
    step=validate_frame_step(every)
    timeline_gap_frames=validate_timeline_gap_frames(timeline_gap_frames)
    semantics=baked_animation_semantics(fps, step)

    # Sampled clip frames drive animation timing and define the documented rest
    # pose. Target frames may include extra BFR frames for advanced workflows.
    sampled_clip_frames=[]
    for clip in clips:
        sampled_clip_frames.extend(range(clip['start_frame'], clip['end_frame']+1, step))
    sampled_clip_frames=sorted(set(f for f in sampled_clip_frames if 0 <= f < bfr['frame_count']))
    if not sampled_clip_frames:
        raise ValueError("No valid BFR frames selected for baked animation")

    if include_all_targets:
        target_frames=list(range(bfr['frame_count']))
    else:
        target_frames=sampled_clip_frames
    frame_to_target={frame:i for i,frame in enumerate(target_frames)}

    # Base mesh is the first sampled clip frame, even when extra BFR targets are
    # included. This keeps zero morph weights consistent with restPose metadata.
    base_frame=sampled_clip_frames[0]
    base=skin_vertices(mesh,bfr,base_frame).astype(np.float32)
    uvs=mesh['uvs'].astype(np.float32).copy()

    # Important:
    # OBJ export keeps v -> 1-v because Blender's OBJ import path expects it for this asset.
    # glTF/GLB uses the PNG with its normal top-left row order in Blender, so do NOT flip
    # by default. Use --glb-flip-v only for viewers that need the opposite convention.
    if glb_flip_v:
        uvs[:,1]=1.0-uvs[:,1]

    index_component_type=index_component_type_for_faces(mesh['faces'])
    index_dtype='<u2' if index_component_type == _COMPONENT_UINT16 else '<u4'
    indices=mesh['faces'].astype(index_dtype).reshape(-1)

    gb=GltfBuilder()
    gb.extras={'bakedAnimationSemantics': semantics}
    pos_acc=gb.add_accessor(base, 'VEC3', _COMPONENT_FLOAT, target=_ARRAY_BUFFER)
    uv_acc=gb.add_accessor(uvs, 'VEC2', _COMPONENT_FLOAT, target=_ARRAY_BUFFER)
    ranges=part_face_ranges(mesh.get('parts', []), len(mesh['faces']))
    idx_acc=None
    if not ranges:
        idx_acc=gb.add_accessor(indices, 'SCALAR', index_component_type, target=_ELEMENT_ARRAY_BUFFER)

    target_accessors=[]
    for frame in target_frames:
        delta=(skin_vertices(mesh,bfr,frame).astype(np.float32) - base).astype(np.float32)
        target_accessors.append(gb.add_accessor(delta, 'VEC3', _COMPONENT_FLOAT, target=_ARRAY_BUFFER))

    if materials is None:
        if texture_image is not None:
            materials=[make_material_record(
                material_name,
                image=texture_image,
                texture_name=texture_name,
            )]
        else:
            materials=[make_material_record(material_name)]
    if not materials:
        raise ValueError("Baked GLB/GLTF requires at least one material")
    material_index_by_name={}
    for material in materials:
        if material['name'] in material_index_by_name:
            raise ValueError(f"Baked GLB/GLTF duplicate material name {material['name']!r}")
        if material.get('image') is not None:
            tex_idx=gb.add_png_image(material['image'], name=material.get('texture_name') or material['name'])
            gb.extensionsUsed.append('KHR_materials_unlit')
            gb.materials.append({
                'name':material['name'],
                'doubleSided': True,
                'pbrMetallicRoughness': {
                    'baseColorTexture': {'index': tex_idx},
                    'metallicFactor': 0.0,
                    'roughnessFactor': 1.0,
                },
                'extensions': {'KHR_materials_unlit': {}}
            })
        else:
            gb.materials.append({
                'name':material['name'],
                'doubleSided': True,
                'pbrMetallicRoughness': {'baseColorFactor':[0.8,0.8,0.8,1.0], 'metallicFactor':0.0, 'roughnessFactor':1.0},
            })
        material_index_by_name[material['name']]=len(gb.materials)-1

    def make_primitive(face_indices, material_idx, extras=None):
        part_component_type=index_component_type_for_faces(face_indices)
        part_dtype='<u2' if part_component_type == _COMPONENT_UINT16 else '<u4'
        part_indices=np.asarray(face_indices).astype(part_dtype).reshape(-1)
        part_idx_acc=gb.add_accessor(part_indices, 'SCALAR', part_component_type, target=_ELEMENT_ARRAY_BUFFER)
        primitive={
            'attributes': {'POSITION': pos_acc, 'TEXCOORD_0': uv_acc},
            'indices': part_idx_acc,
            'mode': 4,
            'material': material_idx,
            'targets': [{'POSITION': acc} for acc in target_accessors],
        }
        if extras:
            primitive['extras']=extras
        return primitive

    primitives=[]
    if ranges:
        fallback_material=materials[0]['name']
        for part,start,end in ranges:
            material_for_part=(part_material_names or {}).get(part['index'], fallback_material)
            if material_for_part not in material_index_by_name:
                raise ValueError(
                    f"Baked GLB/GLTF part {part.get('index')} references unknown material "
                    f"{material_for_part!r}"
                )
            primitives.append(make_primitive(
                mesh['faces'][start:end],
                material_index_by_name[material_for_part],
                extras={
                    'partIndex': int(part['index']),
                    'partName': part.get('name'),
                    'sourceTexture': part.get('texture'),
                    'triangleStart': int(start),
                    'triangleCount': int(end-start),
                    'materialName': material_for_part,
                },
            ))
    else:
        primitives.append({
            'attributes': {'POSITION': pos_acc, 'TEXCOORD_0': uv_acc},
            'indices': idx_acc,
            'mode': 4,
            'material': 0,
            'targets': [{'POSITION': acc} for acc in target_accessors],
        })

    gb.meshes.append({
        'name':f'{mesh_name}_baked_vertex_animation',
        'primitives':primitives,
        'weights':[0.0]*len(target_frames),
        'extras': {
            'bakedFrameNumbers': target_frames,
            'baseFrame': int(base_frame),
            'restPose': semantics['rest_pose'],
            'frameNumberingConvention': semantics['frame_numbering_convention'],
            'primitiveSplit': 'msh_parts' if ranges else 'single_primitive',
        }
    })
    gb.nodes.append({'name':mesh_name, 'mesh':0, 'weights':[0.0]*len(target_frames)})
    gb.scenes.append({'name':f'{mesh_name}_scene', 'nodes':[0]})

    if combined_timeline:
        timeline_frames=[]
        timeline_times=[]
        clip_ranges=[]
        cursor_time=0.0
        gap=timeline_gap_frames
        sampled_clips=[]
        for clip in clips:
            sampled=[f for f in range(clip['start_frame'], clip['end_frame']+1, step) if f in frame_to_target]
            if sampled:
                sampled_clips.append((clip, sampled))
        for clip_index,(clip,sampled) in enumerate(sampled_clips):
            start_sample=len(timeline_frames)
            start_time=cursor_time
            first_sampled=sampled[0]
            for frame in sampled:
                timeline_frames.append(frame)
                timeline_times.append(start_time + ((frame - first_sampled) / float(fps)))
            end_sample=len(timeline_frames)-1
            end_time=timeline_times[-1]
            clip_ranges.append({
                'name': clip['name'],
                'sampleStart': int(start_sample),
                'sampleEnd': int(end_sample),
                'timeStart': float(start_time),
                'timeEnd': float(end_time),
                'sourceStartFrame': int(clip['start_frame']),
                'sourceEndFrame': int(clip['end_frame']),
                'sampledFrames': [int(x) for x in sampled],
                'sourceSpeed': float(clip.get('speed', 0.0)),
                'speedUsage': semantics['ani_speed'],
            })
            cursor_time=start_time + ((int(clip['end_frame']) - int(clip['start_frame']) + 1) / float(fps))
            if gap and clip_index < len(sampled_clips)-1:
                # Hold the last pose briefly between clips.
                for gap_index in range(gap):
                    timeline_frames.append(sampled[-1])
                    timeline_times.append(cursor_time + (gap_index / float(fps)))
                cursor_time += gap / float(fps)

        if not timeline_frames:
            raise ValueError("No frames available for combined timeline")
        times=np.asarray(timeline_times, dtype=np.float32)
        weights=np.zeros((len(timeline_frames), len(target_frames)), dtype=np.float32)
        for row, frame in enumerate(timeline_frames):
            weights[row, frame_to_target[frame]]=1.0
        time_acc=gb.add_accessor(times, 'SCALAR', _COMPONENT_FLOAT, include_minmax=True)
        weight_acc=gb.add_accessor(weights.reshape(-1), 'SCALAR', _COMPONENT_FLOAT, include_minmax=False)
        gb.animations.append({
            'name': 'ALL_CLIPS_TIMELINE' if len(clips) > 1 else clips[0]['name'],
            'samplers': [{'input': time_acc, 'output': weight_acc, 'interpolation': 'STEP'}],
            'channels': [{'sampler':0, 'target': {'node':0, 'path':'weights'}}],
            'extras': {
                'mode': 'combined_timeline',
                'clipRanges': clip_ranges,
                'fps': fps,
                'every': step,
                'targetFrameCount': len(target_frames),
                'timingFormula': semantics['timing_formula'],
                'frameNumberingConvention': semantics['frame_numbering_convention'],
                'speedUsage': semantics['ani_speed'],
                'restPose': semantics['rest_pose'],
            }
        })
    else:
        for clip in clips:
            sampled=[f for f in range(clip['start_frame'], clip['end_frame']+1, step) if f in frame_to_target]
            if not sampled:
                continue
            times=np.array([(f - sampled[0]) / fps for f in sampled], dtype=np.float32)
            weights=np.zeros((len(sampled), len(target_frames)), dtype=np.float32)
            for row, frame in enumerate(sampled):
                weights[row, frame_to_target[frame]]=1.0
            time_acc=gb.add_accessor(times, 'SCALAR', _COMPONENT_FLOAT, include_minmax=True)
            weight_acc=gb.add_accessor(weights.reshape(-1), 'SCALAR', _COMPONENT_FLOAT, include_minmax=False)
            gb.animations.append({
                'name': clip['name'],
                'samplers': [{'input': time_acc, 'output': weight_acc, 'interpolation': 'STEP'}],
                'channels': [{'sampler':0, 'target': {'node':0, 'path':'weights'}}],
                'extras': {
                    'sourceStartFrame': int(clip['start_frame']),
                    'sourceEndFrame': int(clip['end_frame']),
                    'sampledFrames': [int(x) for x in sampled],
                    'fps': fps,
                    'every': step,
                    'sourceSpeed': float(clip.get('speed', 0.0)),
                    'timingFormula': semantics['timing_formula'],
                    'frameNumberingConvention': semantics['frame_numbering_convention'],
                    'speedUsage': semantics['ani_speed'],
                    'restPose': semantics['rest_pose'],
                }
            })

    out_path=Path(out_path)
    if fmt == 'glb':
        gb.write_glb(out_path)
    elif fmt == 'gltf':
        gb.write_gltf(out_path)
    else:
        raise ValueError("fmt must be glb or gltf")
    return {
        'path': str(out_path),
        'target_count': len(target_frames),
        'target_frames': target_frames,
        'clip_names': [c['name'] for c in clips],
        'fps': fps,
        'every': step,
        'format': fmt,
        'combined_timeline': bool(combined_timeline),
        'glb_flip_v': bool(glb_flip_v),
        'index_component_type': index_component_name(index_component_type),
        'base_frame': int(base_frame),
        'animation_semantics': semantics,
    }

# -----------------------------
# Main CLI
# -----------------------------

def main():
    ap=argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument('--msh', required=True, help='MSH mesh file')
    ap.add_argument('--bfr')
    ap.add_argument('--ani')
    ap.add_argument('--3df', dest='texture', metavar='3DF', help='3DF texture file; supports p8 and yiq')
    ap.add_argument('--texture-order', choices=['xrgb','xbgr','bgr','rgb'], default='xrgb')
    ap.add_argument('--out', default='out')
    ap.add_argument('--name', help='override output basenames; defaults to the .msh stem for model files and the .3df stem for texture files')
    ap.add_argument('--no-summary', action='store_true', help='do not write summary.json')
    ap.add_argument('--static', action='store_true')
    ap.add_argument('--frame', type=int)
    clip_group=ap.add_mutually_exclusive_group()
    clip_group.add_argument('--clip', help='clip name from ANI, e.g. walk')
    clip_group.add_argument('--all-clips', '--all-anims', dest='all_clips', action='store_true', help='export/use every clip listed in the ANI file')
    ap.add_argument('--every', type=int, default=1, help='frame step when exporting OBJ sequences or baked GLB targets')
    ap.add_argument('--bake-glb', action='store_true', help='write one animated .glb with baked vertex/morph-target animation')
    ap.add_argument('--bake-gltf', action='store_true', help='write one animated embedded .gltf with baked vertex/morph-target animation')
    ap.add_argument('--fps', type=float, default=15.0, help='playback fps for baked GLB/GLTF animation timing')
    ap.add_argument('--obj-sequence', action='store_true', help='when --clip/--all-clips is set, also write OBJ frame sequence(s). If omitted with --bake-glb/--bake-gltf, only the baked model is written.')
    ap.add_argument('--include-all-bfr-targets', action='store_true', help='advanced: include every BFR frame as a morph target, not only selected clip frames')
    ap.add_argument('--combined-timeline', action='store_true', help='for --all-clips/--clip baked GLB, create one sequential timeline animation instead of separate glTF animations')
    ap.add_argument('--timeline-gap-frames', type=int, default=0, help='pose-hold gap inserted between clips when --combined-timeline is used')
    ap.add_argument('--split-clip-glbs', action='store_true', help='with --all-clips and --bake-glb/--bake-gltf, also export one baked file per clip; most reliable in Blender')
    ap.add_argument('--glb-flip-v', action='store_true', help='flip V texture coordinates only for GLB/GLTF. Default is OFF because Blender showed the texture flipped with the previous GLB exporter')
    args=ap.parse_args()
    args.fps=validate_baked_fps(args.fps)
    args.every=validate_frame_step(args.every)
    args.timeline_gap_frames=validate_timeline_gap_frames(args.timeline_gap_frames)

    names=derive_export_names(args.msh, args.texture, args.name)
    mesh=parse_msh(args.msh)
    bfr=parse_bfr(args.bfr) if args.bfr else None
    ani=parse_ani(args.ani) if args.ani else None
    selected_clips=select_clips(ani, args.clip, args.all_clips)

    # Static export unless user is explicitly only baking animation.
    only_baking = (args.bake_glb or args.bake_gltf) and not args.obj_sequence and not args.static and args.frame is None
    needs_skinning_validation=False

    if args.frame is not None:
        if bfr is None:
            raise SystemExit("--frame requires --bfr")
        validate_bfr_frame_index(bfr, args.frame, '--frame')
        needs_skinning_validation=True

    if selected_clips and (args.obj_sequence or not (args.bake_glb or args.bake_gltf)):
        if bfr is None or ani is None:
            raise SystemExit("--clip/--all-clips requires --bfr and --ani")
        validate_clip_frame_ranges(selected_clips, bfr)
        needs_skinning_validation=True

    if args.bake_glb or args.bake_gltf:
        if bfr is None or ani is None:
            raise SystemExit("--bake-glb/--bake-gltf requires --bfr and --ani plus --clip or --all-anims")
        if not selected_clips:
            raise SystemExit("--bake-glb/--bake-gltf requires --clip NAME or --all-anims")
        validate_clip_frame_ranges(selected_clips, bfr)
        validate_baked_mesh_geometry(mesh)
        needs_skinning_validation=True

    if needs_skinning_validation:
        validate_skin_ranges(mesh)
        validate_skin_bone_ids(mesh, bfr)

    material_plan=resolve_export_materials(mesh, args.msh, args.texture, args.texture_order, names)
    materials=material_plan['materials']
    part_material_names=material_plan['part_material_names']
    primary_texture_image=materials[0].get('image') if materials else None
    primary_material_name=materials[0]['name'] if materials else names['material_name']

    out_dir=Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not only_baking:
        write_material_textures(out_dir, materials)
        write_mtl(out_dir, names['mtl_name'], materials)

    if args.static or (args.frame is None and not selected_clips and not only_baking):
        write_obj(
            out_dir/names['static_obj'],
            mesh['vertices'],
            mesh['faces'],
            mesh['uvs'],
            names['mtl_name'],
            primary_material_name,
            names['object_name'],
            parts=mesh.get('parts'),
            part_material_names=part_material_names,
        )

    if args.frame is not None:
        if bfr is None:
            raise SystemExit("--frame requires --bfr")
        validate_bfr_frame_index(bfr, args.frame, '--frame')
        write_obj(
            out_dir/f"{names['mesh_name']}_frame_{args.frame:03d}.obj",
            skin_vertices(mesh,bfr,args.frame),
            mesh['faces'],
            mesh['uvs'],
            names['mtl_name'],
            primary_material_name,
            names['object_name'],
            parts=mesh.get('parts'),
            part_material_names=part_material_names,
        )

    if selected_clips and (args.obj_sequence or not (args.bake_glb or args.bake_gltf)):
        if bfr is None or ani is None:
            raise SystemExit("--clip/--all-clips requires --bfr and --ani")
        validate_clip_frame_ranges(selected_clips, bfr)
        for clip in selected_clips:
            export_obj_clip_sequence(
                out_dir, mesh, bfr, clip, names, every=args.every,
                part_material_names=part_material_names,
                materials=materials,
                material_name=primary_material_name,
            )

    baked=[]
    if args.bake_glb or args.bake_gltf:
        if bfr is None or ani is None:
            raise SystemExit("--bake-glb/--bake-gltf requires --bfr and --ani plus --clip or --all-anims")
        if not selected_clips:
            raise SystemExit("--bake-glb/--bake-gltf requires --clip NAME or --all-anims")
        validate_clip_frame_ranges(selected_clips, bfr)

        label=sanitize_name('all_anims' if args.all_clips else selected_clips[0]['name'], 'anim')

        # Main baked file: either multi-action glTF animations or one combined Blender-friendly timeline.
        if args.bake_glb:
            out_name = f"{names['mesh_name']}_{label}_timeline.glb" if args.combined_timeline else f"{names['mesh_name']}_{label}_baked.glb"
            baked.append(make_baked_gltf(
                mesh,bfr,selected_clips,out_dir/out_name,
                texture_image=primary_texture_image,materials=materials,part_material_names=part_material_names,
                fps=args.fps,every=args.every,fmt='glb',
                include_all_targets=args.include_all_bfr_targets,
                combined_timeline=args.combined_timeline,
                timeline_gap_frames=args.timeline_gap_frames,
                glb_flip_v=args.glb_flip_v,
                mesh_name=names['mesh_name'],
                material_name=names['material_name'],
                texture_name=names['texture_name'] or names['material_name'],
            ))
        if args.bake_gltf:
            out_name = f"{names['mesh_name']}_{label}_timeline.gltf" if args.combined_timeline else f"{names['mesh_name']}_{label}_baked.gltf"
            baked.append(make_baked_gltf(
                mesh,bfr,selected_clips,out_dir/out_name,
                texture_image=primary_texture_image,materials=materials,part_material_names=part_material_names,
                fps=args.fps,every=args.every,fmt='gltf',
                include_all_targets=args.include_all_bfr_targets,
                combined_timeline=args.combined_timeline,
                timeline_gap_frames=args.timeline_gap_frames,
                glb_flip_v=args.glb_flip_v,
                mesh_name=names['mesh_name'],
                material_name=names['material_name'],
                texture_name=names['texture_name'] or names['material_name'],
            ))

        # Optional split files: a separate GLB/GLTF for each clip. This avoids Blender UI ambiguity
        # around multiple morph-weight actions sharing one mesh.
        if args.split_clip_glbs and len(selected_clips) > 1:
            split_dir=out_dir/'per_clip_baked'
            split_dir.mkdir(exist_ok=True)
            for clip in selected_clips:
                clip_name=sanitize_name(clip['name'], 'clip')
                if args.bake_glb:
                    baked.append(make_baked_gltf(
                        mesh,bfr,[clip],split_dir/f"{names['mesh_name']}_{clip_name}_baked.glb",
                        texture_image=primary_texture_image,materials=materials,part_material_names=part_material_names,
                        fps=args.fps,every=args.every,fmt='glb',
                        include_all_targets=False,
                        combined_timeline=False,
                        glb_flip_v=args.glb_flip_v,
                        mesh_name=names['mesh_name'],
                        material_name=names['material_name'],
                        texture_name=names['texture_name'] or names['material_name'],
                    ))
                if args.bake_gltf:
                    baked.append(make_baked_gltf(
                        mesh,bfr,[clip],split_dir/f"{names['mesh_name']}_{clip_name}_baked.gltf",
                        texture_image=primary_texture_image,materials=materials,part_material_names=part_material_names,
                        fps=args.fps,every=args.every,fmt='gltf',
                        include_all_targets=False,
                        combined_timeline=False,
                        glb_flip_v=args.glb_flip_v,
                        mesh_name=names['mesh_name'],
                        material_name=names['material_name'],
                        texture_name=names['texture_name'] or names['material_name'],
                    ))

    summary={
        'inputs': {
            'msh': str(Path(args.msh)),
            'bfr': str(Path(args.bfr)) if args.bfr else None,
            'ani': str(Path(args.ani)) if args.ani else None,
            '3df': str(Path(args.texture)) if args.texture else None,
        },
        'export_names': {k:v for k,v in names.items() if v is not None},
        'msh': {
            'file_size': mesh['file_size'],
            'vertex_count': int(len(mesh['vertices'])),
            'triangle_count': int(len(mesh['faces'])),
            'weight_record_count': int(len(mesh['weights'])),
            'bone_ids': sorted(set(int(b) for b,w in mesh['weights'])),
            'part_count': int(len(mesh['parts'])),
            'parts': mesh['parts'],
            'strings': mesh['strings'],
            'blocks': mesh['blocks'],
            'bounds': array_bounds(mesh['vertices']),
            'uv_bounds': array_bounds(mesh['uvs']),
        }
    }
    if bfr:
        summary['bfr']={
            'file_size': bfr['file_size'],
            'frame_count': int(bfr['frame_count']),
            'bone_count': int(bfr['bone_count']),
            'bbox_offset': int(bfr['bbox_offset']),
            'matrix_format': '4x3 float32; row-vector transform p_out = p @ matrix[:3,:] + matrix[3,:]',
        }
    if ani:
        summary['ani']=ani
    summary['materials']={
        'mode': material_plan['mode'],
        'entries': [
            {k:v for k,v in material.items() if k != 'image' and v is not None}
            for material in materials
        ],
        'part_material_names': {str(k): v for k,v in part_material_names.items()},
    }
    if material_plan['texture_infos']:
        if len(material_plan['texture_infos']) == 1:
            summary['texture_3df']=material_plan['texture_infos'][0]
        summary['texture_3df_list']=material_plan['texture_infos']
    if selected_clips:
        summary['selected_clips']=[c['name'] for c in selected_clips]
    if baked:
        summary['baked_animation_semantics']=baked[0]['animation_semantics']
        summary['baked_animation_exports']=baked

    if not args.no_summary:
        (out_dir/'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(f"Wrote {out_dir}")
    if baked:
        for item in baked:
            print(f"Baked {item['format'].upper()}: {item['path']} ({item['target_count']} morph targets)")

if __name__ == '__main__':
    try:
        main()
    except (ValueError, NotImplementedError) as exc:
        raise SystemExit(str(exc)) from None
