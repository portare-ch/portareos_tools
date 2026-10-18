# PortareOS tools

Command-line tools for preparing CHD disc images and transferring games to
PortareOS. Both run locally on macOS and Linux, without installing this
repository or any Python packages. Both require Python 3.10 or newer;
`nova-push` also requires Bash, SSH, and rsync. Keep `nova-push`, `nova_push.py`,
and `portare_chd.py` together.

## Dependencies

macOS, using [Homebrew](https://brew.sh/):

```sh
brew install python rom-tools ffmpeg rsync
```

Debian / Ubuntu:

```sh
sudo apt install python3 mame-tools ffmpeg openssh-client rsync
```

`chdman` is included in `rom-tools` on Homebrew and `mame-tools` on Debian/Ubuntu.
FFmpeg is only needed for CUE sheets referencing encoded audio, such as MP3.
You can also specify executable paths with `--chdman` / `--ffmpeg`, or the
`CHDMAN` / `FFMPEG` environment variables.

## Convert

Run from this repository:

```sh
# A Neo Geo CD game with an ISO and MP3 tracks: select the CUE or its folder.
python3 portare_chd.py "/path/to/Metal Slug"

# Choose an output folder.
python3 portare_chd.py "/path/to/Metal Slug.cue" -o ./converted

# Convert a library, keeping its subfolder structure in the output folder.
python3 portare_chd.py /path/to/discs --recursive -o ./converted

# Multiple discs, including Dreamcast GDI and PS2/PSP ISO images.
python3 portare_chd.py "Game.cue" "Dreamcast.gdi" "PS2.iso"

# A standalone CD ISO without a CUE sheet.
python3 portare_chd.py "CD Game.iso" --iso-type cd
```

Supported inputs are `.cue`, `.gdi`, and `.iso` (including uppercase extensions).
For BIN files or discs with separate audio tracks, select the **CUE sheet**;
it describes the track order and gaps. When scanning folders, ISOs referenced
by a selected CUE sheet are automatically excluded as separate conversions.
ZIP/7z archives must be extracted first.

Standalone ISOs default to `chdman createdvd`, suitable for DVD and PSP images.
Use `--iso-type cd` for standalone CD ISOs; the tool does not guess media type
from file size. CUE and GDI images always use `chdman createcd`.

The converter prepares MP3/AIFF/FLAC/OGG CUE audio as stereo, 44.1 kHz, 16-bit
PCM WAV in a temporary folder, padding the last CD sector with silence where
needed. It preserves the CUE's track indexes and pregap/postgap directives.
MP3 sources retain their existing lossy audio quality; conversion cannot
restore audio or original track timing lost when the disc was ripped.

Each result is checked with `chdman verify` before it is published. Original
files are kept. Existing CHDs are skipped; `--overwrite` replaces them only
after the new conversion succeeds. Temporary files are cleaned up after
success, failure, or Ctrl+C. Allow room in the output folder for decoded audio
and the resulting CHD. On FAT/exFAT volumes, source tracks and the final CHD
are copied where symbolic links or hard links are unavailable.

Without `-o`, CHDs are written beside their source CUE/GDI/ISO. With `-o`,
file inputs go directly into that folder; folder inputs retain paths relative
to the selected folder. Spaces in output filenames are replaced with underscores
(for example, `Metal Slug.cue` becomes `Metal_Slug.chd`). Conflicting output
names, including those caused by replacing spaces, are reported before any
conversion. A batch continues after a failed disc and exits with status 1 if
any disc failed (0 on success, 2 for invalid arguments, 130 for Ctrl+C).

Use `-j 4` to limit compression threads or `--no-verify` to skip verification.
Run `python3 portare_chd.py --help` for all options.

## Transfer to a device

`nova-push` copies files over SSH into an existing `/storage/roms/<system>/`
folder. SSH access must be enabled and `rsync` must also be installed on the
device. Save your device once, then use it from any terminal. There is no
built-in address or user.

```sh
# Remember the SSH destination for future commands.
./nova-push --set-host root@device

./nova-push --list
./nova-push ps2 ./converted/Game.chd
./nova-push snes "Game One.sfc" "Game Two.sfc"

# Disc images are converted to CHD and verified before upload.
./nova-push ps2 "Game.iso"
./nova-push psx "Game.cue"
./nova-push dreamcast "Game.gdi"
./nova-push neogeocd "Metal Slug.cue"

# PS2 CD and DVD ISOs are detected automatically, including in a single batch.
./nova-push ps2 "CD Game.iso" "DVD Game.iso"

# The system can be omitted for extensions such as .sfc, .nes, .gba, and .nds.
./nova-push "Game.sfc"

# Preview a move, then transfer and remove successfully delivered local files.
./nova-push --dry-run --move ps2 "Game.chd"
./nova-push --move ps2 "Game.chd"

# Override the saved destination for one command. SSH config aliases also work.
./nova-push --host my-device psp "Game.iso"
```

The default is saved in `~/.config/nova-push/host` (or
`$XDG_CONFIG_HOME/nova-push/host` when set). Run `--set-host` again to change it.
Host selection uses `--host` first, then the `NOVA` environment variable, then
the saved default. The saved file contains only the SSH destination; keys and
passwords use your normal SSH setup.

Options go before the system and filenames. A dry run still connects to the
device to check the destination. It reports planned CHD conversions without
running them, and previews rsync's work for existing CHDs and cartridge ROMs.
Use an SSH config alias for custom ports, identity files, or IPv6 addresses.

Disc transfers only upload CHDs. ISO, CUE, and GDI inputs are converted into a
temporary folder using `portare_chd.py`, then verified before any files are
uploaded. Existing CHDs are uploaded unchanged. Cartridge systems such as NES,
SNES, Game Boy, GBA, N64, and Mega Drive keep their ROM formats, including
BIN/ROM and ZIP/7z cartridge archives. Other non-CHD formats are rejected;
disc archives must be extracted first, and BIN/audio tracks require their CUE.

Automatic conversion supports `psx` (SwanStation), `ps2` (ARMSX2), `psp`,
`dreamcast` (Flycast), `neogeocd`, `segacd`/`megacd`, `saturn`, `pcenginecd`/
`tg16cd`, `pcfx`, `3do`, and `cdi`. By default (`--iso-type auto`), PS2/ARMSX2
ISOs are probed individually to select `chdman createcd` or `chdman createdvd`.
Detection follows the [PCSX2 media detection heuristic](https://github.com/PCSX2/pcsx2/blob/master/pcsx2/CDVD/CDVDcommon.cpp):
images beyond CD capacity use DVD mode; smaller images use the ISO9660 volume
descriptor's root directory length. A small DVD image can still use DVD mode.
Unrecognized PS2 ISO metadata stops the batch before conversion or upload.
`--iso-type cd` or `--iso-type dvd` can override detection for unusual images.
Other CD systems use `createcd`, and PSP uses `createdvd`. CUE and GDI always
use `createcd`, regardless of the ISO option. Each conversion and dry run shows
the selected command. Converted filenames replace spaces with underscores.
Local CHDs beside the inputs are left untouched; temporary CHDs are removed
after the command finishes, including on failure.

Specify the system for shared formats such as CHD, ISO, CUE, GDI, BIN, and ZIP.
Inferred files must all belong to the same system. For CUE/GDI discs, name
only the descriptor; its companion tracks are read locally during conversion.
An ISO referenced by a selected CUE is excluded as a separate conversion.
Directories, symlinks, and filenames that would collide after conversion are
rejected. Existing remote files may be replaced; other games are kept. File
contents and modification times are copied without preserving local ownership
or permissions.

Interrupted transfers keep resumable data in `.nova-push-partial` on the device.
`--move` uses rsync checksums to compare existing remote content before removing
each delivered source. For converted inputs, `--move` removes the named source
image or descriptor only after the transfer succeeds and rsync has removed
the temporary CHD. Companion tracks are kept. If conversion or transfer fails,
original conversion inputs are kept. A failed batch can leave some files
already transferred and existing CHDs or cartridge ROMs removed locally with
`--move`. Keep source files unchanged during a move.
See the [rsync documentation](https://download.samba.org/pub/rsync/rsync.1) for
transfer and source-removal behavior. Run `./nova-push --help` for usage.

## Checks

```sh
python3 -m unittest discover -s tests -v
```

The tests include round trips using real `chdman` and FFmpeg when installed;
they also exercise `nova-push` with a local SSH substitute and real rsync when
available. Integration tests are skipped when the relevant tools are
unavailable. Tests never connect to a device, and no game files are included.

CHD commands and options follow the [MAME chdman documentation](https://docs.mamedev.org/tools/chdman.html).
