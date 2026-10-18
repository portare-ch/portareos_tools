"""Internal conversion and transfer implementation for the nova-push CLI."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import portare_chd as chd

CARTRIDGE_SYSTEMS = {
    "nes",
    "famicom",
    "snes",
    "sfc",
    "gb",
    "gbc",
    "gba",
    "n64",
    "nds",
    "3ds",
    "megadrive",
    "genesis",
    "sega32x",
    "mastersystem",
    "gamegear",
    "sg1000",
    "sg-1000",
    "pcengine",
    "tg16",
    "supergrafx",
    "virtualboy",
    "ngp",
    "ngpc",
    "wonderswan",
    "wonderswancolor",
    "atari2600",
    "atari5200",
    "atari7800",
    "atarilynx",
    "colecovision",
    "intellivision",
    "odyssey2",
    "vectrex",
    "supervision",
    "pokemini",
    "neogeo",
}
CARTRIDGE_FORMATS = {
    ".nes",
    ".sfc",
    ".smc",
    ".gb",
    ".gbc",
    ".gba",
    ".z64",
    ".n64",
    ".v64",
    ".nds",
    ".3ds",
    ".md",
    ".gen",
    ".32x",
    ".sms",
    ".gg",
    ".sg",
    ".pce",
    ".vb",
    ".ngp",
    ".ngc",
    ".ws",
    ".wsc",
    ".a26",
    ".a52",
    ".a78",
    ".lnx",
    ".col",
    ".int",
    ".vec",
    ".sv",
    ".min",
    ".neo",
    ".bin",
    ".rom",
    ".zip",
    ".7z",
}
CD_SYSTEMS = {
    "psx",
    "ps1",
    "playstation",
    "swanstation",
    "dreamcast",
    "flycast",
    "neogeocd",
    "segacd",
    "megacd",
    "saturn",
    "pcenginecd",
    "tg16cd",
    "pcfx",
    "3do",
    "cdi",
}
PS2_SYSTEMS = {"ps2", "armsx2"}
DVD_SYSTEMS = PS2_SYSTEMS | {"psp"}


@dataclass(frozen=True)
class Transfer:
    source: Path
    upload: Path
    conversion: chd.Job | None = None


def detect_ps2_iso_type(source: Path) -> str:
    """Apply PCSX2's CD/DVD heuristic to a cooked, 2048-byte-sector ISO."""
    sectors, remainder = divmod(source.stat().st_size, 2048)
    with source.open("rb") as image:
        image.seek(16 * 2048)
        descriptor = image.read(2048)
    if remainder or len(descriptor) != 2048 or descriptor[:7] != b"\x01CD001\x01":
        raise chd.ConversionError(
            f"Cannot detect PS2 CD/DVD media type: {source.name}; "
            "expected an ISO9660 image with 2048-byte sectors. "
            "Select the CUE for raw CD tracks, or use --iso-type cd or --iso-type dvd "
            "for an image with nonstandard metadata."
        )

    # PCSX2/CDVD/CDVDcommon.cpp::FindDiskType: images beyond this sector
    # count are DVDs. Smaller images use the root directory length in the PVD.
    # https://github.com/PCSX2/pcsx2/blob/master/pcsx2/CDVD/CDVDcommon.cpp
    if sectors > 452849:
        return "dvd"
    root_size = int.from_bytes(descriptor[166:170], "little")
    if not root_size or root_size != int.from_bytes(descriptor[170:174], "big"):
        raise chd.ConversionError(
            f"Cannot detect PS2 CD/DVD media type: {source.name}; "
            "invalid ISO9660 root directory length. "
            "Use --iso-type cd or --iso-type dvd to override."
        )
    return "cd" if descriptor[166:168] == descriptor[171:173] else "dvd"


def media_type(source: Path, system: str, iso_type: str) -> str:
    if source.suffix.lower() != ".iso":
        return "cd"  # CUE/GDI always describe CDs.
    if iso_type != "auto":
        return iso_type
    if system.casefold() in PS2_SYSTEMS:
        return detect_ps2_iso_type(source)
    return "cd" if system.casefold() in CD_SYSTEMS else "dvd"


def plan_transfers(system: str, sources: list[Path], output: Path) -> list[Transfer]:
    system = system.casefold()
    images = []
    passthrough = []
    for source in sources:
        extension = source.suffix.lower()
        if extension == ".chd" or (
            system in CARTRIDGE_SYSTEMS and extension in CARTRIDGE_FORMATS
        ):
            passthrough.append(Transfer(source, source))
        elif extension in chd.SUPPORTED and system in CD_SYSTEMS | DVD_SYSTEMS:
            images.append(source)
        else:
            raise chd.ConversionError(
                f"Non-CHD transfers are only allowed for cartridge ROMs: {source.name}. "
                "For supported disc systems, supply a CHD or an ISO/CUE/GDI to convert. "
                "Select the CUE sheet for BIN/audio tracks; extract disc archives first."
            )

    jobs = chd.plan_jobs(images, output, False) if images else []
    planned = {job.source: Transfer(job.source, job.output, job) for job in jobs}
    planned.update({item.source.resolve(): item for item in passthrough})
    # plan_jobs excludes ISOs referenced by a selected CUE sheet.
    transfers = [
        planned[source.resolve()] for source in sources if source.resolve() in planned
    ]
    names = set()
    for item in transfers:
        name = item.upload.name.casefold()
        if name in names:
            raise chd.ConversionError(
                f"duplicate destination filename: {item.upload.name}"
            )
        names.add(name)
    return transfers


def push(
    host: str,
    system: str,
    transfers: list[Transfer],
    *,
    move: bool,
    dry_run: bool,
    iso_type: str = "auto",
) -> int:
    # Probe the entire batch before converting or uploading any of it.
    conversions = [
        (item, media_type(item.source, system, iso_type))
        for item in transfers
        if item.conversion
    ]
    chdman = chd.find_tool("chdman", None) if conversions and not dry_run else None
    destination = f"/storage/roms/{system}/"
    if subprocess.run(["ssh", host, f"test -d {destination}"], check=False).returncode:
        print(
            f"nova-push: cannot access {host}:{destination}; "
            "check SSH and use --list to see system folders",
            file=sys.stderr,
        )
        return 1

    for item, selected_type in conversions:
        if dry_run:
            print(
                f"nova-push: would convert and verify {item.source} "
                f"-> {host}:{destination}{item.upload.name} (chdman create{selected_type})",
                flush=True,
            )
        else:
            print(
                f"nova-push: {item.source.name}: using chdman create{selected_type}",
                flush=True,
            )
            args = chd.parser().parse_args(
                [str(item.source), "--iso-type", selected_type]
            )
            chd.convert(item.conversion, args, chdman)

    # A dry run previews future CHDs above; rsync needs actual source files.
    uploads = [item for item in transfers if not (dry_run and item.conversion)]
    if uploads:
        options = ["-th", "--progress", "--partial-dir=.nova-push-partial", "--rsh=ssh"]
        if move:
            options.extend(["--checksum", "--remove-source-files"])
        if dry_run:
            options.append("--dry-run")
        status = subprocess.run(
            [
                "rsync",
                *options,
                "--",
                *(str(item.upload) for item in uploads),
                f"{host}:{destination}",
            ],
            check=False,
        ).returncode
        if status:
            return status if status > 0 else 128 - status

    if move and not dry_run:
        for item, _ in conversions:
            # rsync removes a staged CHD only after confirming its delivery.
            # A failed batch retains every original conversion input.
            if not item.upload.exists():
                item.source.unlink()

    action = (
        "dry run; no files copied or removed"
        if dry_run
        else "moved"
        if move
        else "copied"
    )
    print(f"nova-push: {len(transfers)} file(s) -> {host}:{destination} ({action})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("system")
    parser.add_argument("move", choices=("true", "false"))
    parser.add_argument("dry_run", choices=("true", "false"))
    parser.add_argument("--iso-type", choices=("auto", "cd", "dvd"), default="auto")
    parser.add_argument("sources", type=Path, nargs="+")
    args = parser.parse_args(argv)
    try:
        with tempfile.TemporaryDirectory(prefix="nova-push-") as temporary:
            try:
                transfers = plan_transfers(args.system, args.sources, Path(temporary))
            except chd.ConversionError as error:
                print(f"nova-push: {error}", file=sys.stderr)
                return 2
            return push(
                args.host,
                args.system,
                transfers,
                move=args.move == "true",
                dry_run=args.dry_run == "true",
                iso_type=args.iso_type,
            )
    except (chd.ConversionError, OSError) as error:
        print(f"nova-push: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nnova-push: interrupted; temporary CHDs removed.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
