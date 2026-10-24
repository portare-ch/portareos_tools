# PortareOS tools

Command-line tools for preparing CHD disc images and transferring games to
PortareOS. Both run locally on macOS and Linux and require Python 3.10 or newer,
with no Python packages to install. `nova-push` also requires SSH and rsync.
Both tools are standalone executables; no repository or helper scripts are
needed after installation.

## Quick install

On macOS or Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/portare-ch/portareos_tools/main/install.sh | bash
```

This installs `nova-push` and `portare-chd` into `~/.local/bin`, along with
missing Python, chdman, FFmpeg, SSH and rsync dependencies. On macOS, install
[Homebrew](https://brew.sh/) first and follow its PATH setup instructions. On
Linux, automatic dependency installation supports Debian/Ubuntu (`apt-get`),
Fedora (`dnf`), and Arch (`pacman`); it uses `sudo` when needed. Fedora uses its
[`ffmpeg-free` package](https://packages.fedoraproject.org/pkgs/ffmpeg/ffmpeg-free/).
Other Linux distributions can install dependencies manually and use `--no-deps`.

If `~/.local/bin` is missing from your PATH, the installer prints the line to
add to your shell configuration. Then open a new terminal and run:

```sh
nova-push --set-host root@device
nova-push --help
portare-chd --help
```

From a downloaded or cloned checkout, run `bash install.sh`. To choose another
location or skip dependency checks and installation:

```sh
bash install.sh --prefix "$HOME/tools" --no-deps

# The same options work with the one-command installer.
curl -fsSL https://raw.githubusercontent.com/portare-ch/portareos_tools/main/install.sh | bash -s -- --prefix "$HOME/tools" --no-deps
```

`--prefix` requires an absolute path and installs into that directory's `bin`
subfolder. With `--no-deps`, install Python 3.10+ and the required external tools
yourself. Rerun the installer to update both commands. To uninstall, remove
`nova-push` and `portare-chd` from the installation's `bin` folder; installed
dependencies and your saved device configuration are kept.

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
For `portare-chd`, you can also specify executable paths with `--chdman` /
`--ffmpeg`. Both tools accept the `CHDMAN` / `FFMPEG` environment variables.
RVZ conversion with `nova-push` needs `dolphin-tool` separately (see below);
the installer does not install it.

## Convert

After installation, use `portare-chd`:

```sh
portare-chd "/path/to/Metal Slug.cue" -o ./converted
```

You can also run directly from this repository:

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
any disc failed (0 on success, 2 for command-line syntax errors, 130 for Ctrl+C).

Use `-j 4` to limit compression threads or `--no-verify` to skip verification.
Run `python3 portare_chd.py --help` for all options.

## Transfer to a device

`nova-push` copies files over SSH into an existing `/storage/roms/<system>/`
folder. SSH access must be enabled and `rsync` must also be installed on the
device. Save your device once, then use it from any terminal. There is no
built-in address or user. Use an existing system folder name from `--list`;
names are used literally, without alias mapping.

The examples below run from a checkout. After installation, use `nova-push`
in place of `./nova-push` from any folder.

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
./nova-push neocd "Metal Slug.cue"

# PS2 CD and DVD ISOs are detected automatically, including in a single batch.
./nova-push ps2 "CD Game.iso" "DVD Game.iso"

# The system can be omitted for extensions such as .sfc, .nes, .gba, and .nds.
./nova-push "Game.sfc"

# Preview a move, then transfer and remove successfully delivered local files.
./nova-push --dry-run --move ps2 "Game.chd"
./nova-push --move ps2 "Game.chd"

# Override the saved destination for one command. SSH config aliases also work.
./nova-push --host my-device psp "Game.iso"
./nova-push psp "Game.cso"

# GameCube and Wii images become RVZ (needs dolphin-tool, see below).
./nova-push gamecube "Game.iso"
./nova-push wii "Game.rvz"
```

The default is saved in `~/.config/nova-push/host` (or
`$XDG_CONFIG_HOME/nova-push/host` when set). Run `--set-host` again to change it.
Host selection uses `--host` first, then the `NOVA` environment variable, then
the saved default. The saved file contains only the SSH destination; keys and
passwords use your normal SSH setup.

Options go before the system and filenames. A dry run still connects to the
device to check the destination. It reports planned CHD and RVZ conversions
without running them, and previews rsync's work for files uploaded unchanged.
Use an SSH config alias for custom ports, identity files, or IPv6 addresses.

Disc transfers use CHD except for the Dolphin systems described below. ISO,
CUE, and GDI inputs are converted into a temporary folder using the built-in
converter, then verified before any files are uploaded. Existing CHDs are
uploaded unchanged. Cartridge systems such as NES, SNES, Game Boy, GBA, N64,
and Mega Drive keep their ROM formats, including
BIN/ROM and ZIP/7z cartridge archives. CSO images (v1, as most PSP and PS2
CSOs are) are decompressed and converted like an ISO for the DVD systems:
`ps2` and `psp`/`pspminis`. Xbox games are the exception: hakuX reads XISO
images, not CHD yet, so an Xbox `.iso` is checked for its XDVDFS volume
(an extracted XISO or a full disc dump) and uploaded unchanged, and Xbox
CHDs and CSOs are rejected. Other non-CHD formats are rejected; disc
archives must be extracted first, and BIN/audio tracks require their CUE.

Automatic CHD conversion recognizes `psx` (SwanStation), `ps2` (ARMSX2),
`psp`/`pspminis`, `dreamcast` (Flycast), `neocd`, `segacd`/`megacd`, `saturn`, `pcenginecd`/
`tg16cd`, `pcfx`, `3do` and `cdi`. By default (`--iso-type auto`),
PS2/ARMSX2 ISOs and CSOs are probed individually to select `chdman createcd` or `chdman createdvd`.
Detection follows the [PCSX2 media detection heuristic](https://github.com/PCSX2/pcsx2/blob/master/pcsx2/CDVD/CDVDcommon.cpp):
images beyond CD capacity use DVD mode; smaller images use the ISO9660 volume
descriptor's root directory length. A small DVD image can still use DVD mode.
Unrecognized PS2 ISO metadata stops the batch before conversion or upload.
`--iso-type cd` or `--iso-type dvd` can override detection for unusual images.
Other CD systems use `createcd`, and PSP uses `createdvd`. CUE and GDI
always use `createcd`, regardless of the ISO option. Each conversion and dry
run shows the selected command. Converted filenames replace spaces with underscores.

GameCube, Triforce, Wii and WiiWare games are RVZ instead, Dolphin's lossless
format: it regenerates the discs' pseudo-random padding and stores Wii data
decrypted, neither of which CHD can compress. RVZ images and `.dol` homebrew
are copied unchanged, as are `.wad` channels for `wii`/`wiiware`. ISO, GCM,
GCZ, WBFS, CISO and WIA images are converted with `dolphin-tool convert`
(Zstandard level 5, 128 KiB blocks, Dolphin's suggested settings) and verified
by comparing the SHA-1 of the disc the RVZ decodes to with the source's before
anything is uploaded. `dolphin-tool` comes with Debian/Ubuntu's `dolphin-emu`
package; Dolphin's macOS app does not include it, so on a Mac convert in
Dolphin (right-click the game, Convert File..., RVZ) and push the `.rvz`. Set
`DOLPHIN_TOOL` to use another copy.
Local CHDs and RVZs beside the inputs are left untouched; temporary images are
removed after the command finishes, including on failure.

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
each delivered source. For CHD conversions, `--move` removes the named source
image or descriptor only after the transfer succeeds and rsync has removed
the temporary CHD. Companion tracks are kept. Sources converted to RVZ are
kept even with `--move`. If conversion or transfer fails, original conversion
inputs are kept. A failed batch can leave some files already transferred and
files uploaded unchanged removed locally with `--move`. Keep source files
unchanged during a move.
See the [rsync documentation](https://download.samba.org/pub/rsync/rsync.1) for
transfer and source-removal behavior. Run `./nova-push --help` for usage.

## Checks

```sh
python3 -m unittest discover -s tests -v
```

The tests include round trips using real `chdman`, FFmpeg and `dolphin-tool` when installed;
they also exercise `nova-push` with a local SSH substitute and real rsync when
available. Integration tests are skipped when the relevant tools are
unavailable. Tests never connect to a device, and no game files are included.

CHD commands and options follow the [MAME chdman documentation](https://docs.mamedev.org/tools/chdman.html).
