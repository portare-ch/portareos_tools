import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "nova-push"
RSYNC = shutil.which("rsync")
CHDMAN = shutil.which("chdman")


def ps2_iso_data(root_size=300):
    """Small cooked ISO fixture with a primary volume descriptor for probing."""
    data = bytearray(bytes(range(256)) * 256)
    descriptor = bytearray(2048)
    descriptor[:7] = b"\x01CD001\x01"
    descriptor[80:84] = (32).to_bytes(4, "little")
    descriptor[84:88] = (32).to_bytes(4, "big")
    descriptor[128:130] = (2048).to_bytes(2, "little")
    descriptor[130:132] = (2048).to_bytes(2, "big")
    descriptor[156] = 34
    descriptor[166:170] = root_size.to_bytes(4, "little")
    descriptor[170:174] = root_size.to_bytes(4, "big")
    descriptor[181] = 2
    data[16 * 2048 : 17 * 2048] = descriptor
    return bytes(data)


# Stand-ins record argv without contacting a device. Integration tests let the
# SSH stand-in run a real rsync receiver inside the temporary device folder.
STAND_IN = r"""
import json
import os
import sys
from pathlib import Path

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["NOVA_TEST_LOG"], "a") as log:
    log.write(json.dumps([tool, *args]) + "\n")
status = int(os.environ.get("NOVA_TEST_" + tool.upper() + "_STATUS", "0"))
if status:
    sys.exit(status)
if tool == "chdman":
    if args[0] == "verify":
        sys.exit(int(os.environ.get("NOVA_TEST_VERIFY_STATUS", "0")))
    Path(args[args.index("-o") + 1]).write_bytes(b"converted CHD")
    sys.exit(0)
real_rsync = os.environ.get("NOVA_TEST_RSYNC")
if not real_rsync:
    sys.exit(0)
if tool == "rsync":
    os.execv(real_rsync, [real_rsync, *args])

device = Path(os.environ["NOVA_TEST_DEVICE"])
if args[-1].startswith("test -d "):
    folder = args[-1].removeprefix("test -d /storage/roms/")
    sys.exit(0 if (device / folder).is_dir() else 1)
if args[-1] == "ls -1 /storage/roms":
    print("\n".join(sorted(entry.name for entry in device.iterdir())))
    sys.exit(0)

command = args[args.index("rsync") + 1:]
command[-1] = str(device) + command[-1].removeprefix("/storage/roms")
os.execv(real_rsync, [real_rsync, *command])
"""


class TransferTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # Exercise an isolated, executable copy with no repository helpers.
        self.script = self.bin / "nova-push"
        shutil.copy2(SCRIPT, self.script)
        self.log = self.root / "calls.jsonl"
        self.config_home = self.root / "config"
        self.config_file = self.config_home / "nova-push" / "host"
        self.device = self.root / "device"
        for system in ("psx", "ps2", "psp", "dreamcast", "neogeocd", "snes"):
            (self.device / system).mkdir(parents=True)
        for tool in ("ssh", "rsync", "chdman"):
            executable = self.bin / tool
            executable.write_text(f"#!{sys.executable}\n{STAND_IN}")
            executable.chmod(0o755)
        self.environment = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "NOVA": "root@test-device",
            "XDG_CONFIG_HOME": str(self.config_home),
            "NOVA_TEST_LOG": str(self.log),
            "NOVA_TEST_DEVICE": str(self.device),
            "NOVA_TEST_RSYNC": "",
            "NOVA_TEST_SSH_STATUS": "0",
            "NOVA_TEST_RSYNC_STATUS": "0",
            "NOVA_TEST_CHDMAN_STATUS": "0",
            "NOVA_TEST_VERIFY_STATUS": "0",
            "CHDMAN": str(self.bin / "chdman"),
        }

    def file(self, name, content=None):
        source = self.root / name
        source.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            content = (
                ps2_iso_data() if source.suffix.lower() == ".iso" else b"game data"
            )
        source.write_bytes(content)
        return source

    def invoke(self, *arguments, **environment):
        return subprocess.run(
            [str(self.script), *map(str, arguments)],
            cwd=self.root,
            env={**self.environment, **environment},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def assert_invalid(self, *arguments, **environment):
        result = self.invoke(*arguments, **environment)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.calls(), [], result.stdout)
        return result


class NovaPushTests(TransferTestCase):
    def test_help_and_no_arguments_do_not_need_a_host(self):
        result = self.invoke("--help", NOVA="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)
        self.assertIn("--dry-run", result.stdout)
        self.assert_invalid(NOVA="")

    def test_host_is_required_and_host_option_overrides_environment(self):
        source = self.file("game.chd")
        result = self.assert_invalid("ps2", source, NOVA="")
        self.assertIn("--set-host", result.stderr)
        result = self.invoke("--host", "other-device", "ps2", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[0][1], "other-device")
        self.assertEqual(self.calls()[1][-1], "other-device:/storage/roms/ps2/")

    def test_saved_host_is_used_by_later_commands_without_an_environment_override(self):
        result = self.invoke("--set-host", "root@saved-device")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.config_file.read_text(), "root@saved-device\n")
        source = self.file("game.chd")
        result = self.invoke("ps2", source, NOVA="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[0][1], "root@saved-device")
        self.assertEqual(self.calls()[-1][-1], "root@saved-device:/storage/roms/ps2/")

    def test_saved_host_can_be_changed_and_overridden(self):
        for host in ("first-device", "second-device"):
            result = self.invoke("--set-host", host)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.config_file.read_text(), "second-device\n")
        self.assertEqual(self.calls(), [])
        for arguments, environment, expected in (
            (("--list",), {"NOVA": ""}, "second-device"),
            (("--list",), {"NOVA": "env-device"}, "env-device"),
            (
                ("--host", "flag-device", "--list"),
                {"NOVA": "env-device"},
                "flag-device",
            ),
        ):
            with self.subTest(host=expected):
                result = self.invoke(*arguments, **environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.calls()[-1][1], expected)

    def test_saved_host_falls_back_to_home_when_xdg_config_home_is_unset(self):
        isolated_home = self.root / "home"
        environment = {"HOME": str(isolated_home), "XDG_CONFIG_HOME": "", "NOVA": ""}
        result = self.invoke("--set-host", "home-device", **environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = isolated_home / ".config" / "nova-push" / "host"
        self.assertEqual(config.read_text(), "home-device\n")
        result = self.invoke("--list", **environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1][1], "home-device")

    def test_invalid_saved_hosts_are_data_and_cannot_run_commands(self):
        self.config_file.parent.mkdir(parents=True)
        self.config_file.write_text("$(touch marker)\n")
        self.assert_invalid("--list", NOVA="")
        self.assertFalse((self.root / "marker").exists())
        result = self.invoke("--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1][1], "root@test-device")

    def test_invalid_save_commands_keep_the_previous_host_and_never_connect(self):
        self.config_file.parent.mkdir(parents=True)
        self.config_file.write_text("saved-device\n")
        for arguments in (
            ("--set-host",),
            ("--set-host", ""),
            ("--set-host", "host;touch marker"),
            ("--set-host", "other-device", "ps2"),
            ("--set-host", "other-device", "--list"),
            ("--set-host", "other-device", "--dry-run"),
            ("--set-host", "other-device", "--move"),
            ("--set-host", "other-device", "--iso-type", "cd"),
            ("--set-host", "other-device", "--host", "another-device"),
        ):
            with self.subTest(arguments=arguments):
                self.assert_invalid(*arguments)
                self.assertEqual(self.config_file.read_text(), "saved-device\n")

    def test_invalid_hosts_systems_and_arguments_never_connect(self):
        source = self.file("game.chd")
        for host in ("-option", "host:/other", "host;touch marker", "user@host name"):
            with self.subTest(host=host):
                self.assert_invalid("ps2", source, NOVA=host)
        for system in ("../ps2", "ps2;touch marker", "/tmp", "ps2/subfolder"):
            with self.subTest(system=system):
                self.assert_invalid(system, source)
        for args in (("--unknown",), ("--host",), ("ps2",), ("ps2", "missing")):
            with self.subTest(args=args):
                self.assert_invalid(*args)
        self.assert_invalid("ps2", self.device)
        link = self.root / "link.chd"
        link.symlink_to(source)
        self.assert_invalid("ps2", link)

    def test_ambiguous_and_mixed_extensions_require_a_system(self):
        for extension in ("chd", "iso", "bin", "zip", "pbp", "cso", "rvz", "gcz"):
            with self.subTest(extension=extension):
                self.assert_invalid(self.file(f"game.{extension}"))
        self.assert_invalid(self.file("game.sfc"), self.file("game.gba"))
        self.assert_invalid(self.file("game.sfc"), self.file("game.chd"))

    def test_inference_accepts_matching_extensions_and_uppercase(self):
        first = self.file("Game One.SFC")
        second = self.file("Game Two.smc")
        result = self.invoke(first, second)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[-1][-1], "root@test-device:/storage/roms/snes/")
        self.assertFalse(any(call[0] == "chdman" for call in self.calls()))

    def test_disc_formats_that_cannot_be_converted_are_rejected_before_connecting(self):
        for extension in ("bin", "zip", "7z", "pbp", "cso", "rvz", "gcz", "m3u", "sfc"):
            with self.subTest(extension=extension):
                result = self.assert_invalid("psx", self.file(f"game.{extension}"))
                self.assertIn("Non-CHD transfers", result.stderr)
        self.assert_invalid("gamecube", self.file("game.iso"))
        self.assert_invalid("snes", self.file("disc.iso"))

    def test_cartridge_roms_and_archives_are_transferred_unchanged(self):
        for system, name in (
            ("snes", "game.sfc"),
            ("nes", "game.nes"),
            ("megadrive", "game.bin"),
            ("gba", "game.zip"),
        ):
            with self.subTest(system=system):
                source = self.file(name)
                result = self.invoke(system, source)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(str(source), self.calls()[-1])
                self.assertEqual(
                    self.calls()[-1][-1], f"root@test-device:/storage/roms/{system}/"
                )
        self.assertFalse(any(call[0] == "chdman" for call in self.calls()))

    def test_iso_conversion_uses_system_media_type_and_verifies_before_rsync(self):
        for system, command in (
            ("psx", "createcd"),
            ("swanstation", "createcd"),
            ("ps2", "createdvd"),
            ("armsx2", "createdvd"),
            ("dreamcast", "createcd"),
            ("flycast", "createcd"),
            ("neogeocd", "createcd"),
            ("psp", "createdvd"),
            ("saturn", "createcd"),
            ("segacd", "createcd"),
            ("pcenginecd", "createcd"),
        ):
            with self.subTest(system=system):
                self.log.unlink(missing_ok=True)
                source = self.file("Game 'one':two.ISO")
                result = self.invoke(system, source)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = self.calls()
                self.assertEqual(
                    [call[0] for call in calls], ["ssh", "chdman", "chdman", "rsync"]
                )
                self.assertEqual(calls[1][1], command)
                self.assertEqual(calls[2][1], "verify")
                uploads = calls[-1][calls[-1].index("--") + 1 : -1]
                self.assertEqual(
                    [Path(path).name for path in uploads], ["Game_'one':two.chd"]
                )
                self.assertTrue(source.exists())
                self.assertTrue(all(not Path(path).exists() for path in uploads))

    def test_cue_and_gdi_convert_to_cd_and_do_not_upload_companion_tracks(self):
        image = self.file("Game.iso")
        cue = self.file(
            "Game.cue",
            b'FILE "Game.iso" BINARY\nTRACK 01 MODE1/2048\nINDEX 01 00:00:00\n',
        )
        gdi = self.file("Dreamcast.gdi")
        for options in (("dreamcast",), ("--iso-type", "dvd", "ps2")):
            with self.subTest(options=options):
                self.log.unlink(missing_ok=True)
                result = self.invoke(*options, cue, image, gdi)
                self.assertEqual(result.returncode, 0, result.stderr)
                conversions = [
                    call for call in self.calls() if call[:2] == ["chdman", "createcd"]
                ]
                self.assertEqual(len(conversions), 2)
                args = self.calls()[-1]
                uploads = args[args.index("--") + 1 : -1]
                self.assertEqual(
                    [Path(path).name for path in uploads], ["Game.chd", "Dreamcast.chd"]
                )
        self.assertTrue(cue.exists())
        self.assertTrue(image.exists())

    def test_iso_type_override_selects_chdman_command_and_matching_codecs(self):
        source = self.file("PS2 Game.iso")
        for media_type, command, codecs in (
            ("cd", "createcd", "cdlz,cdzl,cdfl"),
            ("CD", "createcd", "cdlz,cdzl,cdfl"),
            ("dvd", "createdvd", "lzma,zlib,huff,flac"),
            ("auto", "createdvd", "lzma,zlib,huff,flac"),
        ):
            with self.subTest(media_type=media_type):
                self.log.unlink(missing_ok=True)
                result = self.invoke("--iso-type", media_type, "ps2", source)
                self.assertEqual(result.returncode, 0, result.stderr)
                conversion = next(call for call in self.calls() if call[0] == "chdman")
                self.assertEqual(conversion[1], command)
                self.assertEqual(conversion[conversion.index("-c") + 1], codecs)

    def test_ps2_auto_detects_cd_and_small_dvd_images_in_the_same_batch(self):
        cd = self.file("CD.iso", ps2_iso_data(root_size=2048))
        other_cd = self.file("Other CD.ISO", ps2_iso_data(root_size=4096))
        dvd = self.file("DVD.iso", ps2_iso_data(root_size=300))
        self.assertEqual(cd.stat().st_size, dvd.stat().st_size)
        for system in ("ps2", "armsx2"):
            with self.subTest(system=system):
                self.log.unlink(missing_ok=True)
                result = self.invoke(system, cd, dvd, other_cd)
                self.assertEqual(result.returncode, 0, result.stderr)
                commands = [
                    call[1]
                    for call in self.calls()
                    if call[0] == "chdman" and call[1] != "verify"
                ]
                self.assertEqual(commands, ["createcd", "createdvd", "createcd"])
                self.assertIn("CD.iso: using chdman createcd", result.stdout)
                self.assertIn("DVD.iso: using chdman createdvd", result.stdout)

    def test_ps2_dvd_above_cd_capacity_is_detected_without_conversion(self):
        source = self.file("Large.iso", ps2_iso_data(root_size=2048))
        with source.open("r+b") as image:
            image.truncate(452850 * 2048)
        result = self.invoke("--dry-run", "ps2", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("chdman createdvd", result.stdout)
        self.assertEqual([call[0] for call in self.calls()], ["ssh"])

    def test_ps2_unrecognized_metadata_stops_the_whole_batch_before_connecting(self):
        valid = self.file("valid.iso")
        invalid = self.file("invalid.iso", b"not an ISO")
        result = self.invoke("--move", "ps2", valid, invalid)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Cannot detect PS2 CD/DVD", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertTrue(valid.exists())
        self.assertTrue(invalid.exists())

    def test_ps2_invalid_root_directory_metadata_requires_explicit_override(self):
        for root_size in (0, 2048):
            with self.subTest(root_size=root_size):
                data = bytearray(ps2_iso_data(root_size=root_size))
                data[16 * 2048 + 170 : 16 * 2048 + 174] = b"\x00\x00\x00\x00"
                source = self.file("invalid.iso", data)
                result = self.invoke("ps2", source)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("root directory length", result.stderr)
                self.assertEqual(self.calls(), [])
        result = self.invoke("--iso-type", "cd", "ps2", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        conversion = next(call for call in self.calls() if call[0] == "chdman")
        self.assertEqual(conversion[1], "createcd")

    def test_invalid_iso_type_options_never_connect(self):
        source = self.file("Game.iso")
        self.assert_invalid("--iso-type")
        self.assert_invalid("--iso-type", "bluray", "ps2", source)
        self.assert_invalid("--iso-type", "cd", "--list")

    def test_dry_run_reports_the_actual_cd_or_dvd_command(self):
        iso = self.file("Game.iso")
        cue = self.file(
            "Game.cue",
            b'FILE "Game.iso" BINARY\nTRACK 01 MODE1/2048\nINDEX 01 00:00:00\n',
        )
        for source, media_type, command in (
            (iso, "auto", "createdvd"),
            (iso, "cd", "createcd"),
            (cue, "dvd", "createcd"),
            (self.file("PS2 CD.iso", ps2_iso_data(root_size=2048)), "auto", "createcd"),
        ):
            with self.subTest(source=source.name, media_type=media_type):
                self.log.unlink(missing_ok=True)
                result = self.invoke(
                    "--dry-run", "--iso-type", media_type, "ps2", source
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"chdman {command}", result.stdout)
                self.assertEqual([call[0] for call in self.calls()], ["ssh"])

    def test_output_collisions_are_rejected_before_conversion_or_transfer(self):
        for names in (
            ("Game.iso", "Game.chd"),
            ("Game Name.iso", "game_name.CHD"),
            ("one/Game.iso", "two/game.ISO"),
        ):
            with self.subTest(names=names):
                result = self.assert_invalid(
                    "ps2", *(self.file(name) for name in names)
                )
                self.assertTrue(
                    "collision" in result.stderr
                    or "duplicate destination" in result.stderr
                )

    def test_dry_run_only_previews_conversion_even_without_chdman(self):
        source = self.file("Game Name.iso")
        original = source.read_bytes()
        result = self.invoke(
            "--dry-run", "--move", "ps2", source, CHDMAN="/missing/chdman"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[0] for call in self.calls()], ["ssh"])
        self.assertIn("would convert and verify", result.stdout)
        self.assertIn("/storage/roms/ps2/Game_Name.chd", result.stdout)
        self.assertIn("no files copied or removed", result.stdout)
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(source.with_suffix(".chd").exists())

    def test_mixed_batch_dry_run_only_passes_existing_chds_to_rsync(self):
        iso = self.file("new.iso")
        chd = self.file("existing.chd")
        result = self.invoke("--dry-run", "ps2", iso, chd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[0] for call in self.calls()], ["ssh", "rsync"])
        self.assertIn(str(chd), self.calls()[-1])
        self.assertNotIn(str(iso), self.calls()[-1])

    def test_conversion_and_verification_failures_never_start_rsync(self):
        source = self.file("Game.iso")
        for environment in (
            {"NOVA_TEST_CHDMAN_STATUS": "1"},
            {"NOVA_TEST_VERIFY_STATUS": "1"},
        ):
            with self.subTest(environment=environment):
                self.log.unlink(missing_ok=True)
                result = self.invoke("--move", "ps2", source, **environment)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertFalse(any(call[0] == "rsync" for call in self.calls()))
                self.assertTrue(source.exists())
                self.assertFalse(source.with_suffix(".chd").exists())
                conversion = next(call for call in self.calls() if call[0] == "chdman")
                staged = Path(conversion[conversion.index("-o") + 1])
                self.assertFalse(staged.parent.parent.exists())

    def test_missing_chdman_is_reported_before_connecting(self):
        source = self.file("Game.iso")
        result = self.invoke("ps2", source, CHDMAN="/missing/chdman")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Cannot find chdman", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_source_arguments_preserve_spaces_quotes_colons_and_dashes(self):
        names = ("Game 'one'.chd", "rom:two.chd", "-three.chd")
        for name in names:
            self.file(name)
        result = self.invoke("--", "ps2", *names)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.calls()[-1]
        self.assertEqual(
            args[args.index("--") + 1 : -1], [str(self.root / n) for n in names]
        )

    def test_duplicate_basenames_are_rejected_including_case_differences(self):
        first = self.file("one/Game.chd")
        for second in (first, self.file("two/Game.chd"), self.file("two/game.CHD")):
            with self.subTest(second=second):
                result = self.assert_invalid("ps2", first, second)
                self.assertIn("duplicate destination", result.stderr)

    def test_list_only_invokes_ssh(self):
        result = self.invoke("--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.calls(), [["ssh", "root@test-device", "ls -1 /storage/roms"]]
        )

    def test_list_rejects_transfer_arguments(self):
        self.assert_invalid("--list", "ps2")
        self.assert_invalid("--move", "--list")
        self.assert_invalid("--dry-run", "--list")

    def test_dry_run_move_passes_checksum_and_source_removal_flags(self):
        source = self.file("game.chd")
        result = self.invoke("--move", "--dry-run", "ps2", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.calls()[-1]
        for option in ("--checksum", "--remove-source-files", "--dry-run", "--rsh=ssh"):
            self.assertIn(option, args)
        self.assertIn("no files copied or removed", result.stdout)
        self.assertTrue(source.exists())

    def test_failed_destination_check_does_not_start_rsync(self):
        source = self.file("game.chd")
        result = self.invoke("--move", "ps2", source, NOVA_TEST_SSH_STATUS="255")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertTrue(source.exists())
        self.assertIn("check SSH", result.stderr)

    def test_rsync_failure_preserves_exit_status_and_does_not_report_success(self):
        source = self.file("game.chd")
        result = self.invoke("--move", "ps2", source, NOVA_TEST_RSYNC_STATUS="23")
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertNotIn("(moved)", result.stdout)
        self.assertTrue(source.exists())


@unittest.skipUnless(RSYNC, "rsync is not installed")
class NovaPushIntegrationTests(TransferTestCase):
    def setUp(self):
        super().setUp()
        self.environment["NOVA_TEST_RSYNC"] = RSYNC

    def test_real_copy_preserves_source_and_other_remote_games(self):
        source = self.file("Game 'one':two.chd")
        unrelated = self.device / "ps2" / "other.chd"
        unrelated.write_bytes(b"other game")
        result = self.invoke("ps2", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.device / "ps2" / source.name).read_bytes(), source.read_bytes()
        )
        self.assertEqual(unrelated.read_bytes(), b"other game")

    def test_real_dry_run_does_not_write_or_remove_files(self):
        source = self.file("game.chd")
        result = self.invoke("--dry-run", "--move", "ps2", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(source.exists())
        self.assertEqual(list((self.device / "ps2").iterdir()), [])

    def test_real_move_replaces_different_content_with_same_size_and_mtime(self):
        source = self.file("game.chd", b"correct data")
        target = self.device / "ps2" / source.name
        target.write_bytes(b"outdated ROM")
        os.utime(target, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns))
        result = self.invoke("--move", "ps2", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(source.exists())
        self.assertEqual(target.read_bytes(), b"correct data")

    def test_real_move_removes_a_source_already_present_at_destination(self):
        source = self.file("game.chd")
        target = self.device / "ps2" / source.name
        shutil.copy2(source, target)
        result = self.invoke("--move", "ps2", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(source.exists())
        self.assertEqual(target.read_bytes(), b"game data")

    def test_converted_copy_uploads_only_chd_and_keeps_existing_local_chd(self):
        source = self.file("Game Name.iso")
        existing = self.file("Game_Name.chd", b"unrelated existing CHD")
        result = self.invoke("ps2", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.device / "ps2" / "Game_Name.chd").read_bytes(), b"converted CHD"
        )
        self.assertEqual(
            list((self.device / "ps2").iterdir()),
            [self.device / "ps2" / "Game_Name.chd"],
        )
        self.assertTrue(source.exists())
        self.assertEqual(existing.read_bytes(), b"unrelated existing CHD")

    def test_converted_move_removes_iso_only_after_successful_delivery(self):
        source = self.file("Game Name.iso")
        result = self.invoke("--move", "ps2", source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(source.exists())
        self.assertEqual(
            (self.device / "ps2" / "Game_Name.chd").read_bytes(), b"converted CHD"
        )

    def test_converted_move_failure_keeps_original_and_cleans_temporary_chd(self):
        source = self.file("Game.iso")
        result = self.invoke("--move", "ps2", source, NOVA_TEST_RSYNC_STATUS="23")
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertTrue(source.exists())
        args = self.calls()[-1]
        self.assertFalse(Path(args[args.index("--") + 1]).parent.exists())

    @unittest.skipUnless(CHDMAN, "chdman is not installed")
    def test_real_iso_conversion_and_upload_round_trip(self):
        for system, media_type, expected in (
            ("psx", "auto", "cd"),
            ("ps2", "auto", "dvd"),
            ("ps2", "auto", "cd"),
            ("ps2", "cd", "cd"),
        ):
            with self.subTest(system=system, media_type=media_type, expected=expected):
                data = ps2_iso_data(root_size=2048 if expected == "cd" else 300)
                command = f"extract{expected}"
                name = f"{system}_{media_type}_{expected} Game"
                source = self.file(f"{name}.iso", data)
                options = ("--iso-type", media_type) if media_type != "auto" else ()
                result = self.invoke(*options, system, source, CHDMAN=CHDMAN)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                uploaded = self.device / system / f"{name.replace(' ', '_')}.chd"
                recovered = (
                    self.root
                    / f"{system}_{media_type}_{expected}-recovered.{'cue' if command == 'extractcd' else 'iso'}"
                )
                result = subprocess.run(
                    [CHDMAN, command, "-i", str(uploaded), "-o", str(recovered)],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                image = (
                    recovered.with_suffix(".bin")
                    if command == "extractcd"
                    else recovered
                )
                self.assertEqual(image.read_bytes(), data)
                self.assertTrue(source.exists())
