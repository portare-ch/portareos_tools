import contextlib
import errno
import io
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import portare_chd as chd
from xbox_images import XBOX_FILES, read_xdvdfs, write_xiso, xdvdfs_data

REPOSITORY = Path(__file__).resolve().parents[1]


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def file(self, name, data=b""):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def cue(self, name="Game.cue", image="Game.iso"):
        return self.file(
            name,
            f'FILE "{image}" BINARY\n  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n'.encode(),
        )

    def invoke(self, *arguments):
        with (
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            status = chd.main([str(argument) for argument in arguments])
        return status, output.getvalue(), errors.getvalue()


class PlanningTests(WorkspaceTest):
    def test_folder_uses_cue_instead_of_its_iso_and_deduplicates_inputs(self):
        self.file("Game Disc.iso")
        cue = self.cue("Game Disc.cue", "Game Disc.iso")
        jobs = chd.plan_jobs([self.root, cue], None, False)
        self.assertEqual(jobs, [chd.Job(cue, self.root / "Game_Disc.chd")])

    def test_recursive_output_preserves_subfolders(self):
        first = self.file("discs/a folder/Game Name.ISO")
        second = self.file("discs/b/Game  Name.iso")
        output = self.root / "converted"
        self.assertEqual(
            chd.plan_jobs([self.root / "discs"], output, True),
            [
                chd.Job(first, output / "a folder/Game_Name.chd"),
                chd.Job(second, output / "b/Game__Name.chd"),
            ],
        )

    def test_flat_output_collision_is_reported_before_conversion(self):
        first = self.file("a/Game Disc.iso")
        second = self.file("b/game_disc.iso")
        with self.assertRaisesRegex(chd.ConversionError, "Output collision"):
            chd.plan_jobs([first, second], self.root / "out", False)

    def test_folder_without_images_has_helpful_error(self):
        self.file("nested/Game.iso")
        with self.assertRaisesRegex(chd.ConversionError, "--recursive"):
            chd.plan_jobs([self.root], None, False)

    def test_bin_requires_cue(self):
        image = self.file("track.bin")
        with self.assertRaisesRegex(chd.ConversionError, "CUE sheet"):
            chd.plan_jobs([image], None, False)

    def test_missing_input(self):
        with self.assertRaisesRegex(chd.ConversionError, "does not exist"):
            chd.plan_jobs([self.root / "missing"], None, False)


class CueTests(WorkspaceTest):
    def test_windows_paths_and_bom(self):
        track = self.file("tracks/data.bin")
        cue = self.file(
            "Game.cue",
            '\ufeffFILE "tracks\\data.bin" BINARY\r\nTRACK 01 MODE1/2352\r\nINDEX 01 00:00:00\r\n'.encode(),
        )
        _, entries = chd.read_cue(cue)
        self.assertEqual(entries[0].source, track)

    def test_cp1252_filename(self):
        track = self.file("Café.iso")
        cue = self.file(
            "Game.cue",
            'FILE "Café.iso" BINARY\nTRACK 01 MODE1/2048\nINDEX 01 00:00:00\n'.encode(
                "cp1252"
            ),
        )
        _, entries = chd.read_cue(cue)
        self.assertEqual(entries[0].source, track)

    def test_staging_preserves_indexes_gaps_and_shared_files(self):
        image = self.file("Game.iso", b"original data")
        cue = self.file(
            "Game.cue",
            b'FILE "Game.iso" BINARY\n'
            b"  TRACK 01 MODE1/2048\n    INDEX 01 00:00:00\n    POSTGAP 00:02:00\n"
            b'FILE "Game.iso" BINARY\n'
            b"  TRACK 02 AUDIO\n    PREGAP 00:02:00\n    INDEX 00 00:00:00\n    INDEX 01 00:02:00\n",
        )
        work = self.root / "work"
        work.mkdir()
        with patch.object(
            chd, "find_tool", side_effect=AssertionError("FFmpeg not needed")
        ):
            staged = chd.prepare_cue(cue, work, None)
        self.assertEqual(
            staged.read_text(),
            cue.read_text().replace(
                'FILE "Game.iso" BINARY', 'FILE "source-001.iso" BINARY'
            ),
        )
        self.assertEqual((work / "source-001.iso").resolve(), image)
        self.assertEqual(image.read_bytes(), b"original data")

    def test_staging_on_volume_without_symlinks_copies_source(self):
        image = self.file("Game.iso", b"original data")
        cue = self.cue()
        work = self.root / "work"
        work.mkdir()
        with patch.object(
            Path, "symlink_to", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")
        ):
            chd.prepare_cue(cue, work, None)
        self.assertFalse((work / "source-001.iso").is_symlink())
        self.assertEqual((work / "source-001.iso").read_bytes(), image.read_bytes())

    def test_missing_tracks_are_detected_before_audio_decoding(self):
        self.file("audio.mp3")
        cue = self.file(
            "Game.cue",
            b'FILE "audio.mp3" MP3\nTRACK 01 AUDIO\nINDEX 01 00:00:00\n'
            b'FILE "missing.bin" BINARY\nTRACK 02 MODE1/2352\nINDEX 01 00:00:00\n',
        )
        with (
            patch.object(chd, "run", side_effect=AssertionError("must validate first")),
            self.assertRaisesRegex(chd.ConversionError, "Missing track"),
        ):
            chd.prepare_cue(cue, self.root, None)

    def test_bad_cues_are_rejected(self):
        for contents in (
            b'FILE "unterminated BINARY\nTRACK 01 AUDIO\n',
            b"TRACK 01 AUDIO\n",
            b'FILE "audio.mp3" MP3\nTRACK 01 MODE1/2048\n',
            b'FILE "audio.dat" UNKNOWN\nTRACK 01 AUDIO\n',
            b"REM no tracks\n",
        ):
            with self.subTest(contents=contents):
                cue = self.file("Game.cue", contents)
                with self.assertRaises(chd.ConversionError):
                    chd.read_cue(cue)


class FailureTests(WorkspaceTest):
    def test_existing_output_is_skipped(self):
        image = self.file("Game.iso")
        output = self.file("Game.chd", b"existing")
        args = chd.parser().parse_args([str(image)])
        with (
            patch.object(chd, "run", side_effect=AssertionError("must skip")),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertFalse(chd.convert(chd.Job(image, output), args, "chdman"))
        self.assertEqual(output.read_bytes(), b"existing")

    def test_failed_verification_keeps_existing_output_and_cleans_staging(self):
        image = self.file("Game.iso")
        output = self.file("Game.chd", b"existing")
        args = chd.parser().parse_args([str(image), "--overwrite"])

        def fake_run(command, **kwargs):
            if command[1] == "verify":
                raise chd.ConversionError("verification failed")
            Path(command[command.index("-o") + 1]).write_bytes(b"new but invalid")

        with (
            patch.object(chd, "run", side_effect=fake_run),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(chd.ConversionError, "verification failed"),
        ):
            chd.convert(chd.Job(image, output), args, "chdman")
        self.assertEqual(output.read_bytes(), b"existing")
        self.assertEqual(list(self.root.glob(".portare-chd-*")), [])

    def test_interrupt_removes_temporary_output(self):
        image = self.file("Game.iso")

        def interrupt(command, **kwargs):
            Path(command[command.index("-o") + 1]).write_bytes(b"partial")
            raise KeyboardInterrupt

        with (
            patch.object(chd, "find_tool", return_value="chdman"),
            patch.object(chd, "run", side_effect=interrupt),
        ):
            status, _, _ = self.invoke(image)
        self.assertEqual(status, 130)
        self.assertFalse((self.root / "Game.chd").exists())
        self.assertEqual(list(self.root.glob(".portare-chd-*")), [])

    def test_batch_continues_and_reports_failure(self):
        first = self.file("bad.iso")
        second = self.file("good.iso")
        with (
            patch.object(chd, "find_tool", return_value="chdman"),
            patch.object(
                chd, "convert", side_effect=[chd.ConversionError("bad disc"), True]
            ) as convert,
        ):
            status, output, errors = self.invoke(first, second)
        self.assertEqual(status, 1)
        self.assertEqual(convert.call_count, 2)
        self.assertIn("1 created, 0 skipped, 1 failed", output)
        self.assertIn("bad disc", errors)

    def test_missing_dependency_has_install_instructions(self):
        with (
            patch.object(chd.shutil, "which", return_value=None),
            self.assertRaisesRegex(chd.ConversionError, "brew install"),
        ):
            chd.find_tool("chdman", "/missing/chdman")

    def test_publish_refuses_racing_existing_file(self):
        staged = self.file("staged", b"new")
        output = self.file("Game.chd", b"existing")
        with self.assertRaises(FileExistsError):
            chd.publish(staged, output, False)
        self.assertEqual(output.read_bytes(), b"existing")

    def test_exfat_fallback_and_exclusive_creation(self):
        staged = self.file("staged", b"new")
        output = self.root / "Game.chd"
        with patch.object(
            chd.os, "link", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")
        ):
            chd.publish(staged, output, False)
            with self.assertRaises(FileExistsError):
                chd.publish(staged, output, False)
        self.assertEqual(output.read_bytes(), b"new")

    def test_failed_exfat_copy_is_removed(self):
        staged = self.file("staged", b"new")
        output = self.root / "Game.chd"
        with (
            patch.object(
                chd.os, "link", side_effect=OSError(errno.EOPNOTSUPP, "unsupported")
            ),
            patch.object(chd.shutil, "copyfileobj", side_effect=OSError("disk full")),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            chd.publish(staged, output, False)
        self.assertFalse(output.exists())


class XboxTests(WorkspaceTest):
    def convert(self, image, *options):
        """Convert with chdman stubbed: the commands it got, the images it
        read (none for the source itself) and the console output."""
        output = self.root / f"out-{len(list(self.root.iterdir()))}" / "Game.chd"
        args = chd.parser().parse_args([str(image), *options])
        commands, images = [], []

        def fake_run(command, **kwargs):
            commands.append(command)
            if command[1] != "verify":
                source = Path(command[command.index("-i") + 1])
                images.append(None if source == image else source.read_bytes())
                Path(command[command.index("-o") + 1]).write_bytes(b"converted CHD")

        with (
            patch.object(chd, "run", side_effect=fake_run),
            contextlib.redirect_stdout(io.StringIO()) as console,
        ):
            self.assertTrue(chd.convert(chd.Job(image, output), args, "chdman"))
        self.assertEqual(output.read_bytes(), b"converted CHD")
        self.assertEqual(list(output.parent.glob(".portare-chd-*")), [])
        return commands, images, console.getvalue()

    def test_full_dumps_are_rebuilt_as_xisos_in_the_staging_folder(self):
        games = self.root / "games"
        games.mkdir()
        for offset in (0x18300000, 0xFD90000, 0x2080000):
            with self.subTest(offset=hex(offset)):
                source = write_xiso(games / "Game (USA).iso", offset)
                before = source.stat()
                commands, images, console = self.convert(source)
                self.assertIn("Full Xbox disc dump: rebuilding", console)
                self.assertEqual(
                    [command[1] for command in commands], ["createdvd", "verify"]
                )
                rebuilt = Path(commands[0][commands[0].index("-i") + 1])
                self.assertTrue(rebuilt.parent.name.startswith(".portare-chd-"))
                self.assertFalse(rebuilt.parent.exists())
                self.assertEqual(read_xdvdfs(images[0], self), XBOX_FILES)
                self.assertEqual(len(images[0]) % 0x10000, 0)
                self.assertNotIn(b"video partition", images[0])
                self.assertNotIn(b"sector filler", images[0])
                self.assertEqual(list(games.iterdir()), [source])
                self.assertEqual(
                    (source.stat().st_size, source.stat().st_mtime_ns),
                    (before.st_size, before.st_mtime_ns),
                )

    def test_xisos_other_dvds_and_cd_conversions_use_the_source(self):
        for name, data, options, command in (
            ("XISO.iso", None, (), "createdvd"),
            ("PS2.iso", bytes(range(256)) * 256, (), "createdvd"),
            ("Dump as CD.iso", 0x18300000, ("--iso-type", "cd"), "createcd"),
        ):
            with self.subTest(name=name):
                image = self.root / name
                if isinstance(data, bytes):
                    image.write_bytes(data)
                else:
                    write_xiso(image, data or 0)
                commands, images, console = self.convert(image, *options)
                self.assertEqual(commands[0][1], command)
                self.assertEqual(images, [None])
                self.assertNotIn("Full Xbox disc dump", console)

    def test_damaged_dump_stops_before_chdman_and_cleans_staging(self):
        data = bytearray(xdvdfs_data(XBOX_FILES))
        root = struct.unpack_from("<I", data, 32 * 2048 + 20)[0] * 2048
        struct.pack_into("<I", data, data.index(b"default.xbe", root) - 10, 0x7FFFFF)
        source = self.root / "Damaged.iso"
        with source.open("wb") as image:
            image.seek(0x18300000)
            image.write(data)
        output = self.root / "out" / "Damaged.chd"
        args = chd.parser().parse_args([str(source)])
        with (
            patch.object(chd, "run", side_effect=AssertionError("must not convert")),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(
                chd.ConversionError,
                r"Damaged\.iso: damaged Xbox file system "
                r"\(/default\.xbe extends past the end of the image\)",
            ),
        ):
            chd.convert(chd.Job(source, output), args, "chdman")
        self.assertEqual(list(output.parent.iterdir()), [])

    def test_xiso_rebuild_is_identical_in_both_tools(self):
        def block(path):
            text = (REPOSITORY / path).read_text()
            start = text.index("# Xbox XISO rebuild, kept identical")
            return text[start : text.index("# End of the Xbox XISO rebuild.", start)]

        self.assertEqual(block("portare_chd.py"), block("nova-push"))


@unittest.skipUnless(shutil.which("chdman"), "chdman not installed")
class IntegrationTests(WorkspaceTest):
    def command(self, *arguments):
        result = subprocess.run(
            [str(argument) for argument in arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def convert_cli(self, image, *options):
        self.command(
            sys.executable,
            Path(chd.__file__),
            image,
            "-o",
            self.root / "out",
            "-j",
            "2",
            *options,
        )
        return self.root / "out" / image.with_suffix(".chd").name.replace(" ", "_")

    def test_dvd_iso_round_trip(self):
        data = bytes(range(256)) * 256
        image = self.file("PS2 Game.iso", data)
        result = self.convert_cli(image)
        recovered = self.root / "recovered.iso"
        self.command("chdman", "extractdvd", "-i", result, "-o", recovered)
        self.assertEqual(recovered.read_bytes(), data)

    def test_full_xbox_dump_round_trip(self):
        image = write_xiso(self.root / "Xbox Game (USA).iso", 0x18300000)
        result = self.convert_cli(image)
        recovered = self.root / "recovered.iso"
        self.command("chdman", "extractdvd", "-i", result, "-o", recovered)
        self.assertEqual(read_xdvdfs(recovered.read_bytes(), self), XBOX_FILES)

    def test_standalone_cd_iso_round_trip(self):
        data = bytes(range(256)) * 256
        image = self.file("CD Game.iso", data)
        result = self.convert_cli(image, "--iso-type", "cd")
        recovered = self.root / "recovered.cue"
        self.command("chdman", "extractcd", "-i", result, "-o", recovered)
        _, files = chd.read_cue(recovered)
        self.assertEqual(files[0].source.read_bytes(), data)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg not installed")
    def test_iso_mp3_round_trip_preserves_data_audio_and_gaps(self):
        data = bytes(range(256)) * 256
        self.file("Game.iso", data)
        wav = self.root / "original.wav"
        with wave.open(str(wav), "wb") as stream:
            stream.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
            stream.writeframes(
                b"".join(struct.pack("<h", i % 1000 - 500) for i in range(1001))
            )
        audio = self.root / "audio.mp3"
        self.command("ffmpeg", "-v", "error", "-i", wav, audio)
        cue = self.file(
            "Mixed disc.cue",
            b'FILE "Game.iso" BINARY\nTRACK 01 MODE1/2048\nINDEX 01 00:00:00\nPOSTGAP 00:02:00\n'
            b'FILE "audio.mp3" MP3\nTRACK 02 AUDIO\nPREGAP 00:02:00\nINDEX 01 00:00:00\n',
        )
        work = self.root / "prepared"
        work.mkdir()
        chd.prepare_cue(cue, work, None)
        with wave.open(str(work / "audio-002.wav"), "rb") as stream:
            self.assertEqual(
                (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()),
                (2, 2, 44100),
            )
            self.assertEqual(
                stream.getnframes(), 2352
            )  # 2002 samples padded to 4 sectors.
            pcm = stream.readframes(stream.getnframes())
        result = self.convert_cli(cue)
        recovered = self.root / "recovered.cue"
        self.command("chdman", "extractcd", "-i", result, "-o", recovered, "-sb")
        _, files = chd.read_cue(recovered)
        self.assertEqual(len(files), 2)
        self.assertEqual(files[0].source.read_bytes(), data)
        # chdman swaps CHD's big-endian CD audio back to little-endian CUE/BIN.
        self.assertEqual(files[1].source.read_bytes(), pcm)
        info = self.command("chdman", "info", "-i", result, "-v")
        self.assertIn("POSTGAP:150", info)
        self.assertIn("PREGAP:150", info)

    def test_dreamcast_gdi(self):
        self.file("track01.bin", b"\x01" * 2048 * 20)
        self.file("track02.raw", b"\x00" * 2352 * 8)
        self.file("track03.bin", b"\x03" * 2048 * 20)
        gdi = self.file(
            "Dreamcast.gdi",
            b"3\n1 0 4 2048 track01.bin 0\n2 150 0 2352 track02.raw 0\n3 45000 4 2048 track03.bin 0\n",
        )
        result = self.convert_cli(gdi)
        info = self.command("chdman", "info", "-i", result, "-v")
        self.assertIn("TRACK:3", info)


if __name__ == "__main__":
    unittest.main()
