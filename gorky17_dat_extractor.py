#!/usr/bin/env python3
"""
Extract Gorky 17 / Odium DAT archives.

The DAT files used by this game have a compact table at the start:

  - 0x00: uint32 file entry count
  - 0x04: 28 bytes reserved
  - 0x20: repeated 128-byte file entries

Each file entry stores a uint32 payload offset, uint32 payload size, uint64
Windows FILETIME timestamp, and a null-terminated archive path.

Examples:

  python -B .\\gorky17_dat_extractor.py
  python -B .\\gorky17_dat_extractor.py --list
  python -B .\\gorky17_dat_extractor.py .\\in\\sprite.dat --out .\\out\\sprite
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import struct
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


HEADER_SIZE = 0x20
ENTRY_SIZE = 0x80
ENTRY_NAME_SIZE = ENTRY_SIZE - 16
FILETIME_TO_UNIX_SECONDS = 11_644_473_600
COPY_CHUNK_SIZE = 1024 * 1024


class DatArchiveError(ValueError):
    pass


@dataclass(frozen=True)
class DatEntry:
    index: int
    offset: int
    size: int
    filetime: int
    name: str

    @property
    def end_offset(self) -> int:
        return self.offset + self.size

    @property
    def is_empty_name(self) -> bool:
        return self.name == ""


@dataclass
class ExtractStats:
    extracted: int = 0
    skipped_empty: int = 0
    skipped_existing: int = 0


def read_c_string(data: bytes, encoding: str) -> str:
    raw = data.split(b"\0", 1)[0]
    return raw.decode(encoding, errors="replace")


def parse_dat_archive(path: Path, encoding: str = "cp1250") -> list[DatEntry]:
    size = path.stat().st_size
    if size < HEADER_SIZE:
        raise DatArchiveError(f"{path} is too small to be a DAT archive")

    with path.open("rb") as fh:
        header = fh.read(HEADER_SIZE)
        (entry_count,) = struct.unpack_from("<I", header, 0)

        table_size = HEADER_SIZE + entry_count * ENTRY_SIZE
        if table_size > size:
            raise DatArchiveError(
                f"{path} has an invalid table: {entry_count} entries exceed file size"
            )

        entries: list[DatEntry] = []
        for index in range(entry_count):
            raw_entry = fh.read(ENTRY_SIZE)
            if len(raw_entry) != ENTRY_SIZE:
                raise DatArchiveError(f"{path} ended while reading entry {index}")

            offset, payload_size, filetime = struct.unpack_from("<IIQ", raw_entry, 0)
            name = read_c_string(raw_entry[16 : 16 + ENTRY_NAME_SIZE], encoding)

            entry = DatEntry(
                index=index,
                offset=offset,
                size=payload_size,
                filetime=filetime,
                name=name,
            )
            validate_entry(path, size, table_size, entry)
            entries.append(entry)

    return entries


def validate_entry(path: Path, archive_size: int, table_size: int, entry: DatEntry) -> None:
    if entry.offset > archive_size:
        raise DatArchiveError(
            f"{path} entry {entry.index} starts outside the archive: {entry.offset}"
        )
    if entry.end_offset > archive_size:
        raise DatArchiveError(
            f"{path} entry {entry.index} extends outside the archive: "
            f"{entry.end_offset} > {archive_size}"
        )
    if not entry.is_empty_name and entry.offset < table_size:
        raise DatArchiveError(
            f"{path} entry {entry.index} points into the archive table: {entry.offset}"
        )


def filetime_to_unix_timestamp(filetime: int) -> float | None:
    if filetime <= 0:
        return None
    return filetime / 10_000_000 - FILETIME_TO_UNIX_SECONDS


def format_filetime(filetime: int) -> str:
    timestamp = filetime_to_unix_timestamp(filetime)
    if timestamp is None:
        return "-"
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%SZ"
        )
    except (OSError, OverflowError, ValueError):
        return "-"


def safe_archive_member_path(name: str) -> Path:
    if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
        raise DatArchiveError(f"absolute archive path is not allowed: {name!r}")

    parts: list[str] = []
    for part in re.split(r"[\\/]+", name):
        if part in ("", "."):
            continue
        if part == ".." or ":" in part:
            raise DatArchiveError(f"unsafe archive path is not allowed: {name!r}")
        parts.append(part)

    if not parts:
        raise DatArchiveError("empty archive path")
    return Path(*parts)


def unique_member_path(relative_path: Path, seen: dict[str, int]) -> Path:
    key = str(relative_path).casefold()
    count = seen.get(key, 0)
    seen[key] = count + 1

    if count == 0:
        return relative_path

    suffix = f"__{count + 1}"
    parent = relative_path.parent
    stem = relative_path.stem
    ext = relative_path.suffix
    return parent / f"{stem}{suffix}{ext}"


def copy_entry_payload(archive_path: Path, entry: DatEntry, output_path: Path) -> None:
    remaining = entry.size
    with archive_path.open("rb") as src, output_path.open("wb") as dst:
        src.seek(entry.offset)
        while remaining:
            chunk = src.read(min(COPY_CHUNK_SIZE, remaining))
            if not chunk:
                raise DatArchiveError(
                    f"{archive_path} ended while extracting entry {entry.index}"
                )
            dst.write(chunk)
            remaining -= len(chunk)


def preserve_entry_time(output_path: Path, filetime: int) -> None:
    timestamp = filetime_to_unix_timestamp(filetime)
    if timestamp is None:
        return
    try:
        os.utime(output_path, (timestamp, timestamp))
    except (OSError, OverflowError, ValueError):
        pass


def list_archive(archive_path: Path, entries: list[DatEntry]) -> None:
    print(f"{archive_path}: {len(entries)} entries")
    print("  idx  offset      size        timestamp             name")
    for entry in entries:
        name = entry.name or "<empty>"
        print(
            f"  {entry.index:3d}  0x{entry.offset:08x}  {entry.size:10d}  "
            f"{format_filetime(entry.filetime):20s}  {name}"
        )


def extract_archive(
    archive_path: Path,
    entries: list[DatEntry],
    output_dir: Path,
    overwrite: bool,
    preserve_times: bool,
    dry_run: bool,
) -> ExtractStats:
    stats = ExtractStats()
    seen: dict[str, int] = {}

    for entry in entries:
        if entry.is_empty_name:
            stats.skipped_empty += 1
            continue

        relative_path = unique_member_path(safe_archive_member_path(entry.name), seen)
        output_path = output_dir / relative_path

        if output_path.exists() and not overwrite:
            stats.skipped_existing += 1
            continue

        if dry_run:
            print(f"{archive_path}: {entry.name} -> {output_path}")
            stats.extracted += 1
            continue

        output_path.parent.mkdir(parents=True, exist_ok=True)
        copy_entry_payload(archive_path, entry, output_path)
        if preserve_times:
            preserve_entry_time(output_path, entry.filetime)
        stats.extracted += 1

    return stats


def discover_archives(input_dir: Path, recursive: bool) -> list[Path]:
    pattern = "**/*.dat" if recursive else "*.dat"
    return sorted(path for path in input_dir.glob(pattern) if path.is_file())


def resolve_archives(args: argparse.Namespace) -> list[Path]:
    if args.archives:
        archives = [Path(path) for path in args.archives]
    else:
        archives = discover_archives(args.input_dir, args.recursive)

    missing = [str(path) for path in archives if not path.is_file()]
    if missing:
        raise DatArchiveError("archive not found: " + ", ".join(missing))
    if not archives:
        raise DatArchiveError(f"no .dat archives found in {args.input_dir}")
    return archives


def common_archive_base(archives: list[Path], input_dir: Path | None) -> Path | None:
    if input_dir is not None:
        return input_dir.resolve()
    if len(archives) == 1:
        return archives[0].resolve().parent

    try:
        parent_paths = [str(path.resolve().parent) for path in archives]
        return Path(os.path.commonpath(parent_paths))
    except ValueError:
        return None


def archive_output_subdir(archive_path: Path, base_dir: Path | None) -> Path:
    if base_dir is not None:
        try:
            return archive_path.resolve().relative_to(base_dir).with_suffix("")
        except ValueError:
            pass
    return Path(archive_path.stem)


def output_dir_for_archive(
    output_root: Path,
    archive_path: Path,
    archive_count: int,
    no_archive_dir: bool,
    base_dir: Path | None,
) -> Path:
    if no_archive_dir:
        if archive_count != 1:
            raise DatArchiveError("--no-archive-dir can only be used with one archive")
        return output_root
    return output_root / archive_output_subdir(archive_path, base_dir)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract Gorky 17 / Odium DAT archives."
    )
    parser.add_argument(
        "archives",
        nargs="*",
        help="DAT archives to extract. Defaults to every .dat file in --input-dir.",
    )
    parser.add_argument(
        "--input-dir",
        "--in",
        dest="input_dir",
        type=Path,
        default=Path("in"),
        help="folder searched for .dat archives when no archive path is given",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="search --input-dir recursively when no archive path is given",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("out"),
        help=(
            "output root folder; archive relative paths are preserved without "
            "the .dat suffix"
        ),
    )
    parser.add_argument(
        "--no-archive-dir",
        action="store_true",
        help="extract directly into --out; only allowed with one archive",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list archive contents without extracting",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be extracted without writing files",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="overwrite existing output files",
    )
    parser.add_argument(
        "--encoding",
        default="cp1250",
        help="filename encoding used by the archive table; default: cp1250",
    )
    parser.add_argument(
        "--no-times",
        dest="preserve_times",
        action="store_false",
        help="do not copy archive timestamps to extracted files",
    )
    parser.set_defaults(preserve_times=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        archives = resolve_archives(args)
        archive_count = len(archives)
        base_dir = common_archive_base(
            archives, args.input_dir if not args.archives else None
        )
        total = ExtractStats()

        for archive_path in archives:
            entries = parse_dat_archive(archive_path, args.encoding)
            if args.list:
                list_archive(archive_path, entries)
                continue

            output_dir = output_dir_for_archive(
                args.out,
                archive_path,
                archive_count,
                args.no_archive_dir,
                base_dir,
            )
            stats = extract_archive(
                archive_path=archive_path,
                entries=entries,
                output_dir=output_dir,
                overwrite=args.overwrite,
                preserve_times=args.preserve_times,
                dry_run=args.dry_run,
            )
            total.extracted += stats.extracted
            total.skipped_empty += stats.skipped_empty
            total.skipped_existing += stats.skipped_existing
            action = "Would extract" if args.dry_run else "Extracted"
            print(
                f"{action} {stats.extracted} files from {archive_path} -> {output_dir} "
                f"({stats.skipped_empty} empty entries skipped, "
                f"{stats.skipped_existing} existing files skipped)"
            )

        if not args.list and archive_count > 1:
            action = "Would extract" if args.dry_run else "Extracted"
            print(
                f"{action} {total.extracted} files total "
                f"({total.skipped_empty} empty entries skipped, "
                f"{total.skipped_existing} existing files skipped)"
            )
        return 0
    except (DatArchiveError, OSError, UnicodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
