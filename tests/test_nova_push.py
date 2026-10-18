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
BASH = os.environ.get("NOVA_TEST_BASH", "/bin/bash")

# Stand-ins record argv without contacting a device. Integration tests let the
# SSH stand-in run a real rsync receiver inside the temporary device folder.
STAND_IN = r'''
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
'''


class TransferTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.jsonl"
        self.config_home = self.root / "config"
        self.config_file = self.config_home / "nova-push" / "host"
        self.device = self.root / "device"
        for system in ("ps2", "psp", "snes"):
            (self.device / system).mkdir(parents=True)
        for tool in ("ssh", "rsync"):
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
        }

    def file(self, name, content=b"game data"):
        source = self.root / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)
        return source

    def invoke(self, *arguments, **environment):
        return subprocess.run(
            [BASH, str(SCRIPT), *map(str, arguments)],
            cwd=self.root,
            env={**self.environment, **environment},
            capture_output=True,
            text=True,
            timeout=20,
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
        self.assertIn("Usage:", result.stdout)
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
            (("--host", "flag-device", "--list"), {"NOVA": "env-device"}, "flag-device"),
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

    def test_source_arguments_preserve_spaces_quotes_colons_and_dashes(self):
        names = ("Game 'one'.chd", "rom:two.chd", "-three.chd")
        for name in names:
            self.file(name)
        result = self.invoke("--", "ps2", *names)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.calls()[-1]
        self.assertEqual(args[args.index("--") + 1:-1], [str(self.root / n) for n in names])

    def test_duplicate_basenames_are_rejected_including_case_differences(self):
        first = self.file("one/Game.chd")
        for second in (first, self.file("two/Game.chd"), self.file("two/game.CHD")):
            with self.subTest(second=second):
                result = self.assert_invalid("ps2", first, second)
                self.assertIn("duplicate destination", result.stderr)

    def test_list_only_invokes_ssh(self):
        result = self.invoke("--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [["ssh", "root@test-device", "ls -1 /storage/roms"]])

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
        self.assertEqual((self.device / "ps2" / source.name).read_bytes(), source.read_bytes())
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
