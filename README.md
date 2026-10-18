# PortareOS tools

A simple command-line converter for preparing CHD disc images for PortareOS.
Runs locally on macOS and Linux with Python 3.10 or newer. No Python packages
or installation of this repository are required.

## Dependencies

macOS, using [Homebrew](https://brew.sh/):

```sh
brew install python rom-tools ffmpeg
```

Debian / Ubuntu:

```sh
sudo apt install python3 mame-tools ffmpeg
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
to the selected folder. Conflicting output names are reported before any
conversion. A batch continues after a failed disc and exits with status 1 if
any disc failed (0 on success, 2 for invalid arguments, 130 for Ctrl+C).

Use `-j 4` to limit compression threads or `--no-verify` to skip verification.
Run `python3 portare_chd.py --help` for all options.

## Checks

```sh
python3 -m unittest discover -s tests -v
```

The tests include round trips using real `chdman` and FFmpeg when installed;
those integration tests are skipped when the relevant tools are unavailable.
No game files are included in this repository.

CHD commands and options follow the [MAME chdman documentation](https://docs.mamedev.org/tools/chdman.html).
