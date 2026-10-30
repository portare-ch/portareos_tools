#!/usr/bin/env python3
"""Convert disc images to CHD on macOS and Linux using chdman and FFmpeg."""

from __future__ import annotations

import argparse
import errno
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

SUPPORTED = {".cue", ".gdi", ".iso"}
ENCODED_AUDIO = {"MP3", "AIFF", "FLAC", "OGG"}
FILE_LINE = re.compile(r'\s*FILE\s+(?:"([^"]+)"|([^"\s]+))\s+(\S+)\s*', re.IGNORECASE)
TRACK_LINE = re.compile(r"\s*TRACK\s+\d+\s+(\S+)", re.IGNORECASE)


class ConversionError(Exception):
    """An actionable conversion failure, displayed without a traceback."""


@dataclass(frozen=True)
class CueFile:
    line: int
    source: Path
    kind: str


@dataclass(frozen=True)
class Job:
    source: Path
    output: Path


def read_cue(source: Path) -> tuple[list[str], list[CueFile]]:
    data = source.read_bytes()
    try:
        contents = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        contents = data.decode("cp1252", errors="replace")
    lines = contents.splitlines()
    files: list[CueFile] = []
    tracks = 0
    for number, line in enumerate(lines):
        if re.match(r"\s*FILE\b", line, re.IGNORECASE):
            match = FILE_LINE.fullmatch(line)
            if not match:
                raise ConversionError(f"{source.name}:{number + 1}: invalid FILE line")
            name = match[1] or match[2]
            target = source.parent / name
            if not target.is_file():
                # CUE sheets distributed for Windows often use backslashes.
                target = source.parent / name.replace("\\", "/")
            kind = match[3].upper()
            if kind not in {"BINARY", "MOTOROLA", "WAVE"} | ENCODED_AUDIO:
                raise ConversionError(
                    f"{source.name}: unsupported CUE file type: {kind}"
                )
            files.append(CueFile(number, target.resolve(), kind))
        track = TRACK_LINE.match(line)
        if track:
            if not files:
                raise ConversionError(f"{source.name}: TRACK appears before FILE")
            if files[-1].kind in ENCODED_AUDIO and track[1].upper() != "AUDIO":
                raise ConversionError(
                    f"{source.name}: encoded audio must use TRACK AUDIO"
                )
            tracks += 1
    if not files or not tracks:
        raise ConversionError(f"{source.name}: CUE must contain FILE and TRACK entries")
    return lines, files


def plan_jobs(inputs: list[Path], output: Path | None, recursive: bool) -> list[Job]:
    jobs: dict[Path, Job] = {}
    for argument in inputs:
        source = argument.expanduser().resolve()
        if source.is_dir():
            candidates = source.rglob("*") if recursive else source.iterdir()
            images = sorted(
                (
                    p
                    for p in candidates
                    if p.is_file() and p.suffix.lower() in SUPPORTED
                ),
                key=lambda p: str(p).casefold(),
            )
            for image in images:
                destination = (
                    output / image.relative_to(source).with_suffix(".chd")
                    if output
                    else image.with_suffix(".chd")
                )
                jobs.setdefault(image.resolve(), Job(image.resolve(), destination))
        elif source.is_file():
            if source.suffix.lower() not in SUPPORTED:
                raise ConversionError(
                    f"Unsupported input: {source.name}. Select a .cue, .gdi or .iso; "
                    "for BIN or audio tracks, select their CUE sheet."
                )
            destination = (
                output / source.with_suffix(".chd").name
                if output
                else source.with_suffix(".chd")
            )
            jobs.setdefault(source, Job(source, destination))
        else:
            raise ConversionError(f"Input does not exist: {source}")

    # An ISO referenced by a CUE is part of that disc, not a separate game.
    referenced: set[Path] = set()
    for job in jobs.values():
        if job.source.suffix.lower() == ".cue":
            try:
                _, files = read_cue(job.source)
            except (ConversionError, OSError):
                continue  # Report a broken disc when its conversion is attempted.
            referenced.update(entry.source for entry in files)
    planned = [
        Job(job.source, job.output.with_name(job.output.name.replace(" ", "_")))
        for job in jobs.values()
        if job.source not in referenced
    ]
    if not planned:
        raise ConversionError(
            "No .cue, .gdi or .iso images found. Use --recursive to search subfolders."
        )
    destinations: dict[str, Path] = {}
    for job in planned:
        # Also catch collisions on the usual case-insensitive macOS volumes.
        key = str(job.output.resolve()).casefold()
        if key in destinations:
            raise ConversionError(
                f"Output collision: {destinations[key]} and {job.source} both map to {job.output}. "
                "Convert separately or use a common input folder with --recursive."
            )
        destinations[key] = job.source
    return planned


def find_tool(name: str, override: str | None) -> str:
    candidate = override or os.environ.get(name.upper()) or name
    executable = shutil.which(os.path.expanduser(candidate))
    if executable:
        return str(Path(executable).resolve())
    raise ConversionError(
        f"Cannot find {name}: {candidate}. On macOS: brew install rom-tools ffmpeg. "
        "On Debian/Ubuntu: sudo apt install mame-tools ffmpeg. "
        f"Or pass --{name} /path/to/{name}."
    )


def run(command: list[str], *, cwd: Path | None = None) -> None:
    try:
        subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL, check=True)
    except subprocess.CalledProcessError as error:
        raise ConversionError(
            f"{Path(command[0]).name} failed (exit {error.returncode}); see output above."
        ) from error


def prepare_cue(source: Path, work: Path, ffmpeg: str | None) -> Path:
    lines, files = read_cue(source)
    for entry in files:
        if not entry.source.is_file():
            raise ConversionError(
                f"Missing track referenced by {source.name}: {entry.source}"
            )

    # Short local symlinks keep paths portable and avoid copying large BIN/ISO files.
    prepared: dict[tuple[Path, str], str] = {}
    for entry in files:
        key = (entry.source, entry.kind)
        if key not in prepared:
            number = len(prepared) + 1
            if entry.kind in ENCODED_AUDIO:
                executable = find_tool("ffmpeg", ffmpeg)
                name = f"audio-{number:03d}.wav"
                print(f"  Preparing audio: {entry.source.name}", flush=True)
                run(
                    [
                        executable,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-nostdin",
                        "-i",
                        str(entry.source),
                        "-map",
                        "0:a:0",
                        "-vn",
                        "-ac",
                        "2",
                        "-af",
                        "aresample=44100,asetnsamples=n=588:p=1",
                        "-ar",
                        "44100",
                        "-c:a",
                        "pcm_s16le",
                        str(work / name),
                    ]
                )
                # 588 stereo samples = one 2352-byte CD sector. The filter pads
                # the last sector with silence so chdman won't truncate samples.
            else:
                name = f"source-{number:03d}{entry.source.suffix}"
                try:
                    (work / name).symlink_to(entry.source)
                except OSError as error:
                    if error.errno not in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS}:
                        raise
                    shutil.copyfile(entry.source, work / name)
            prepared[key] = name
        kind = "WAVE" if entry.kind in ENCODED_AUDIO else entry.kind
        lines[entry.line] = f'FILE "{prepared[key]}" {kind}'
    cue = work / "disc.cue"
    cue.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cue


# Xbox XISO rebuild, kept identical in nova-push and portare_chd.py (the tests
# compare the two copies).
#
# An Xbox image's XDVDFS volume is where hakuX looks for it: at the start of an
# XISO, or after the video partition of a full XGD1, XGD2 or XGD3 disc dump.
# The video partition and the filler in the game partition's unused sectors
# don't compress, so a full dump is rebuilt as an XISO first, as extract-xiso
# -r does: the game partition's files packed one after another
# (https://xboxdevwiki.net/XDVDFS).
XISO_MAGIC = b"MICROSOFT*XBOX*MEDIA"
XISO_OFFSETS = (0, 0x18300000, 0xFD90000, 0x2080000)
XDVDFS_SECTOR = 2048
XDVDFS_DIRECTORY = 0x10
XISO_ROOT_SECTOR = 0x108
XISO_MODULUS = 0x10000
COPY_CHUNK = 4 << 20


def xdvdfs_partition(source: Path) -> int | None:
    """The XDVDFS game partition's offset: 0 in an XISO, None if there is none."""
    with source.open("rb") as image:
        for offset in XISO_OFFSETS:
            image.seek(offset + 32 * XDVDFS_SECTOR)
            descriptor = image.read(XDVDFS_SECTOR)
            if descriptor[:20] == XISO_MAGIC and descriptor[0x7EC:0x800] == XISO_MAGIC:
                return offset
    return None


@dataclass(eq=False)
class XdvdfsEntry:
    """A file or directory of a dump's XDVDFS tree, and its place in the XISO."""

    name: bytes
    attributes: int
    sector: int
    size: int
    children: list[XdvdfsEntry] | None = None  # None for files
    target: int = 0  # start sector in the XISO
    # Directories: the table as (entry, offset, left offset, right offset).
    nodes: list[tuple[XdvdfsEntry, int, int, int]] = field(default_factory=list)
    table_size: int = 0


def xdvdfs_sectors(size: int) -> int:
    return -(-size // XDVDFS_SECTOR)


def read_xdvdfs(
    image: BinaryIO, source: Path, partition: int, sector: int, size: int
) -> list[XdvdfsEntry]:
    """The root directory's entries, and recursively its subdirectories'. Each
    table is a binary tree: u16 left and right child offsets in dwords, u32
    start sector, u32 size, u8 attributes, u8 name length, name."""
    end = os.fstat(image.fileno()).st_size
    tables: set[int] = set()  # each directory's table is read once

    def damaged(detail: str) -> ConversionError:
        return ConversionError(
            f"{source.name}: damaged Xbox file system ({detail}); check the dump"
        )

    def directory(sector: int, size: int, path: str, depth: int) -> list[XdvdfsEntry]:
        if not size:
            return []
        start = partition + sector * XDVDFS_SECTOR
        if depth > 32 or sector in tables or size > 64 << 20 or start + size > end:
            raise damaged(f"directory {path}")
        tables.add(sector)
        image.seek(start)
        table = image.read(size)
        entries = []
        pending, seen = [0], set()
        while pending:
            offset = pending.pop()
            if offset in seen or offset + 14 > size:
                raise damaged(f"directory {path}")
            seen.add(offset)
            left, right, first, length, attributes, name_length = struct.unpack_from(
                "<HHIIBB", table, offset
            )
            if left == 0xFFFF:
                continue  # 0xFF padding: an empty directory's table
            name = table[offset + 14 : offset + 14 + name_length]
            if len(name) != name_length:
                raise damaged(f"directory {path}")
            entry = XdvdfsEntry(name, attributes, first, length)
            display = path + name.decode("ascii", "replace")
            if attributes & XDVDFS_DIRECTORY:
                entry.children = directory(first, length, display + "/", depth + 1)
            elif partition + first * XDVDFS_SECTOR + length > end:
                raise damaged(f"{display} extends past the end of the image")
            entries.append(entry)
            pending.extend(4 * child for child in (right, left) if child)
        return entries

    return directory(sector, size, "/", 0)


def xiso_order(entry: XdvdfsEntry) -> tuple[int, ...]:
    """extract-xiso's name order: ASCII upper-cased, compared as signed C chars."""
    return tuple(
        (c - 32 if 97 <= c <= 122 else c) - (256 if c > 127 else 0) for c in entry.name
    )


def plan_xiso(directory: XdvdfsEntry) -> None:
    """Lay out a directory's table, then its subdirectories': a balanced binary
    tree of the sorted names, in prefix order, each entry 4-byte aligned and
    never across a sector."""
    ordered = sorted(directory.children or [], key=xiso_order)

    def prefix(low: int, high: int):
        if low < high:
            middle = (low + high - 1) // 2
            yield low, middle, high
            yield from prefix(low, middle)
            yield from prefix(middle + 1, high)

    spans = list(prefix(0, len(ordered)))
    offsets: dict[int, int] = {}
    size = 0
    for _, middle, _ in spans:
        length = (14 + len(ordered[middle].name) + 3) & ~3
        if size // XDVDFS_SECTOR != (size + length - 1) // XDVDFS_SECTOR:
            size = xdvdfs_sectors(size) * XDVDFS_SECTOR
        offsets[middle] = size
        size += length
    directory.nodes = [
        (
            ordered[middle],
            offsets[middle],
            offsets[(low + middle - 1) // 2] if low < middle else 0,
            offsets[(middle + high) // 2] if middle + 1 < high else 0,
        )
        for low, middle, high in spans
    ]
    directory.table_size = size or XDVDFS_SECTOR  # an empty table is a padding sector
    for entry in ordered:
        if entry.children is not None:
            plan_xiso(entry)


def place_xiso(directory: XdvdfsEntry, sector: int) -> int:
    """Assign sectors: the directory's table, its files, then its subdirectories.
    Returns the next free sector."""
    directory.target = sector
    sector += xdvdfs_sectors(directory.table_size)
    for entry, *_ in directory.nodes:
        if entry.children is None:
            entry.target = sector
            sector += xdvdfs_sectors(entry.size)
    for entry, *_ in directory.nodes:
        if entry.children is not None:
            sector = place_xiso(entry, sector)
    return sector


def write_xiso_directory(
    image: BinaryIO,
    output: BinaryIO,
    source: Path,
    partition: int,
    directory: XdvdfsEntry,
) -> None:
    """Write a directory's table, its files, then its subdirectories, padding
    each to a sector with 0xFF."""
    table = bytearray(b"\xff" * (xdvdfs_sectors(directory.table_size) * XDVDFS_SECTOR))
    for entry, offset, left, right in directory.nodes:
        size = (
            entry.size
            if entry.children is None
            else xdvdfs_sectors(entry.table_size) * XDVDFS_SECTOR
        )
        struct.pack_into(
            f"<HHIIBB{len(entry.name)}s",
            table,
            offset,
            left // 4,
            right // 4,
            entry.target,
            size,
            entry.attributes,
            len(entry.name),
            entry.name,
        )
    output.write(table)
    for entry, *_ in directory.nodes:
        if entry.children is not None:
            continue
        image.seek(partition + entry.sector * XDVDFS_SECTOR)
        remaining = entry.size
        while remaining:
            chunk = image.read(min(remaining, COPY_CHUNK))
            if not chunk:
                raise ConversionError(f"{source.name}: the image ended early")
            output.write(chunk)
            remaining -= len(chunk)
        output.write(b"\xff" * (-entry.size % XDVDFS_SECTOR))
    for entry, *_ in directory.nodes:
        if entry.children is not None:
            write_xiso_directory(image, output, source, partition, entry)


def rebuild_xiso(source: Path, target: Path, partition: int) -> Path:
    """Rewrite a full dump's game partition as an XISO, laid out as extract-xiso
    -r does: the volume descriptor at sector 32, the root table at sector 0x108,
    each table followed by its directory's files and then its subdirectories;
    zeros pad the image to 64 KiB. File contents are copied unchanged."""
    with source.open("rb") as image:
        image.seek(partition + 32 * XDVDFS_SECTOR)
        volume = image.read(XDVDFS_SECTOR)
        root_sector, root_size = struct.unpack_from("<II", volume, 20)
        root = XdvdfsEntry(
            b"",
            XDVDFS_DIRECTORY,
            root_sector,
            root_size,
            read_xdvdfs(image, source, partition, root_sector, root_size),
        )
        plan_xiso(root)
        place_xiso(root, XISO_ROOT_SECTOR)
        descriptor = bytearray(XDVDFS_SECTOR)
        descriptor[:20] = descriptor[0x7EC:] = XISO_MAGIC
        struct.pack_into(
            "<II8s",
            descriptor,
            20,
            root.target,
            xdvdfs_sectors(root.table_size) * XDVDFS_SECTOR,
            volume[28:36],  # the disc's timestamp
        )
        with target.open("wb") as output:
            output.write(bytes(32 * XDVDFS_SECTOR))
            output.write(descriptor)
            output.write(bytes((XISO_ROOT_SECTOR - 33) * XDVDFS_SECTOR))
            write_xiso_directory(image, output, source, partition, root)
            output.write(bytes(-output.tell() % XISO_MODULUS))
    return target


# End of the Xbox XISO rebuild.


def publish(staged: Path, output: Path, overwrite: bool) -> None:
    if overwrite:
        os.replace(staged, output)
        return
    try:
        # Unlike rename(), link() atomically refuses an existing destination.
        os.link(staged, output)
    except OSError as error:
        if error.errno not in {
            errno.EPERM,
            errno.EOPNOTSUPP,
            errno.ENOSYS,
            errno.EXDEV,
        }:
            raise
        # FAT/exFAT output volumes cannot hard-link; exclusive creation still
        # protects existing files, and interrupted copies remove their output.
        with output.open("xb") as destination:
            try:
                with staged.open("rb") as source:
                    shutil.copyfileobj(source, destination)
                destination.flush()
            except BaseException:
                output.unlink()
                raise


def convert(job: Job, args: argparse.Namespace, chdman: str) -> bool:
    if os.path.lexists(job.output) and not args.overwrite:
        print(f"Skipped existing: {job.output}", flush=True)
        return False
    job.output.parent.mkdir(parents=True, exist_ok=True)
    print(f"Converting: {job.source}\n        to: {job.output}", flush=True)
    with tempfile.TemporaryDirectory(
        prefix=".portare-chd-", dir=job.output.parent
    ) as temporary:
        work = Path(temporary)
        command = (
            "createdvd"
            if job.source.suffix.lower() == ".iso" and args.iso_type == "dvd"
            else "createcd"
        )
        # XISOs convert directly; full Xbox disc dumps are rebuilt as XISOs first.
        partition = xdvdfs_partition(job.source) if command == "createdvd" else 0
        if job.source.suffix.lower() == ".cue":
            image = prepare_cue(job.source, work, args.ffmpeg)
        elif partition:
            print(
                "  Full Xbox disc dump: rebuilding the game partition as an XISO…",
                flush=True,
            )
            image = rebuild_xiso(job.source, work / "disc.iso", partition)
            print(
                f"  XISO: {image.stat().st_size / (1024 * 1024):.1f} MiB", flush=True
            )
        else:
            image = job.source
        staged = work / "output.chd"
        options = [chdman, command, "-i", str(image), "-o", str(staged)]
        # Use established codecs supported by handheld emulators' libchdr.
        codecs = "lzma,zlib,huff,flac" if command == "createdvd" else "cdlz,cdzl,cdfl"
        options.extend(["-c", codecs])
        if args.jobs:
            options.extend(["-np", str(args.jobs)])
        run(options, cwd=work)
        if not args.no_verify:
            print("  Verifying CHD…", flush=True)
            run([chdman, "verify", "-i", str(staged)])
        publish(staged, job.output, args.overwrite)
    print(
        f"Created: {job.output} ({job.output.stat().st_size / (1024 * 1024):.1f} MiB)",
        flush=True,
    )
    return True


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(
        description="PortareOS CHD converter for macOS and Linux. Source files are kept.",
        epilog="Select the CUE for multi-track games (including ISO + MP3). "
        "Standalone ISOs default to DVD/PSP format; full Xbox disc dumps are "
        "rebuilt as XISOs (game partition only) first.",
    )
    cli.add_argument(
        "inputs", type=Path, nargs="+", help="CUE, GDI, ISO files or folders"
    )
    cli.add_argument(
        "-o",
        "--output",
        type=Path,
        help="output folder (default: beside each source image)",
    )
    cli.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="search subfolders; preserve their structure under --output",
    )
    cli.add_argument(
        "--iso-type",
        choices=("dvd", "cd"),
        default="dvd",
        help="standalone ISO media type (default: dvd; also for PSP)",
    )
    cli.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing CHDs only after successful conversion and verification",
    )
    cli.add_argument(
        "--no-verify", action="store_true", help="skip the final chdman integrity check"
    )
    cli.add_argument(
        "-j", "--jobs", type=positive_int, help="maximum compression threads per disc"
    )
    cli.add_argument("--chdman", help="path to chdman (or set CHDMAN)")
    cli.add_argument(
        "--ffmpeg", help="path to FFmpeg for encoded audio (or set FFMPEG)"
    )
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        output = args.output.expanduser().resolve() if args.output else None
        jobs = plan_jobs(args.inputs, output, args.recursive)
        chdman = find_tool("chdman", args.chdman)
        created = skipped = failed = 0
        for job in jobs:
            try:
                if convert(job, args, chdman):
                    created += 1
                else:
                    skipped += 1
            except (ConversionError, OSError) as error:
                print(
                    f"Error converting {job.source.name}: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                failed += 1
        print(
            f"Done: {created} created, {skipped} skipped, {failed} failed.", flush=True
        )
        return 1 if failed else 0
    except (ConversionError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Temporary conversion files removed.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
