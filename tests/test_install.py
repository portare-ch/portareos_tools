import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY / "install.sh"
BASH = shutil.which("bash")

# Isolated tools simulate each OS and package manager without network access,
# sudo, or changing the host's packages. Package installation makes the missing
# dependency stubs available so the installer's post-install checks run too.
STAND_IN = r"""
import json
import os
import shutil
import sys
from pathlib import Path

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
bin_dir = Path(os.environ["INSTALL_TEST_BIN"])
with open(os.environ["INSTALL_TEST_LOG"], "a") as log:
    log.write(json.dumps([tool, *args]) + "\n")
if tool == "uname":
    print(os.environ.get("INSTALL_TEST_OS", "Linux"))
elif tool == "id":
    print(os.environ.get("INSTALL_TEST_UID", "1000"))
elif tool == "python3":
    sys.exit(int(os.environ.get("INSTALL_TEST_OLD_PYTHON", "0")))
elif tool == "sudo":
    os.execv(bin_dir / args[0], args)
elif tool in {"brew", "apt-get", "dnf", "pacman"}:
    status = int(os.environ.get("INSTALL_TEST_PACKAGE_STATUS", "0"))
    if status:
        sys.exit(status)
    if args[0] != "update" and not os.environ.get("INSTALL_TEST_NO_PACKAGES"):
        for name in ("python3", "chdman", "ffmpeg", "ssh", "rsync"):
            if not (bin_dir / name).exists():
                (bin_dir / name).symlink_to(bin_dir / "stub")
elif tool in {"curl", "wget"}:
    if os.environ.get("INSTALL_TEST_DOWNLOAD_FAIL"):
        sys.exit(22)
    flag = "--output" if tool == "curl" else "-O"
    destination = Path(args[args.index(flag) + 1])
    if os.environ.get("INSTALL_TEST_EMPTY_DOWNLOAD"):
        destination.touch()
    else:
        url = next(arg for arg in args if arg.startswith("https://"))
        shutil.copyfile(Path(os.environ["INSTALL_TEST_REPO"]) / url.rsplit("/", 1)[1], destination)
"""


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "tools"
        self.bin.mkdir()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.log = self.root / "calls.jsonl"
        # Exclude host package managers and dependencies from PATH.
        for name in (
            "cat",
            "dirname",
            "cp",
            "mktemp",
            "rm",
            "mkdir",
            "install",
            "mv",
            "tr",
        ):
            (self.bin / name).symlink_to(shutil.which(name))
        stub = self.bin / "stub"
        stub.write_text(f"#!{sys.executable}\n{STAND_IN}")
        stub.chmod(0o755)
        self.add_tools("uname", "id", "sudo")
        self.environment = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": str(self.bin),
            "TMPDIR": str(self.scratch),
            "BASH_ENV": "/dev/null",
            "INSTALL_TEST_BIN": str(self.bin),
            "INSTALL_TEST_LOG": str(self.log),
            "INSTALL_TEST_REPO": str(REPOSITORY),
        }

    def add_tools(self, *names):
        for name in names:
            (self.bin / name).symlink_to(self.bin / "stub")

    def invoke(self, *args, piped=False, **environment):
        return subprocess.run(
            [BASH, "-s", "--", *args] if piped else [BASH, str(INSTALLER), *args],
            input=INSTALLER.read_text() if piped else None,
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

    def assert_installed(self, prefix=None):
        destination = (prefix or self.home / ".local") / "bin"
        for name, source in (
            ("nova-push", "nova-push"),
            ("portare-chd", "portare_chd.py"),
        ):
            installed = destination / name
            self.assertEqual(installed.read_bytes(), (REPOSITORY / source).read_bytes())
            self.assertTrue(os.access(installed, os.X_OK))
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertEqual(
            sorted(p.name for p in destination.iterdir()), ["nova-push", "portare-chd"]
        )

    def test_local_install_is_offline_and_reinstall_replaces_commands(self):
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                result = self.invoke("--no-deps")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_installed()
                self.assertIn("export PATH=", result.stdout)
                (self.home / ".local/bin/nova-push").write_text("old version")
        self.assertEqual(self.calls(), [["uname", "-s"], ["uname", "-s"]])

    def test_custom_prefix_with_spaces_and_installed_help(self):
        prefix = self.root / "custom tools"
        result = self.invoke("--no-deps", "--prefix", str(prefix))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_installed(prefix)
        for name in ("nova-push", "portare-chd"):
            help_result = subprocess.run(
                [str(prefix / "bin" / name), "--help"],
                env={
                    **os.environ,
                    "PATH": str(Path(sys.executable).parent)
                    + os.pathsep
                    + os.environ["PATH"],
                },
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("usage:", help_result.stdout)

    def test_brew_installs_missing_dependencies_without_sudo(self):
        self.add_tools("brew", "ssh")
        result = self.invoke(INSTALL_TEST_OS="Darwin")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_installed()
        self.assertIn(
            ["brew", "install", "python", "rom-tools", "ffmpeg", "rsync"], self.calls()
        )
        self.assertFalse(any(call[0] == "sudo" for call in self.calls()))

    def test_linux_package_managers(self):
        for manager, arguments in (
            (
                "apt-get",
                [
                    "install",
                    "-y",
                    "python3",
                    "mame-tools",
                    "ffmpeg",
                    "openssh-client",
                    "rsync",
                ],
            ),
            (
                "dnf",
                [
                    "install",
                    "-y",
                    "python3",
                    "mame-tools",
                    "ffmpeg-free",
                    "openssh-clients",
                    "rsync",
                ],
            ),
            (
                "pacman",
                [
                    "-S",
                    "--needed",
                    "--noconfirm",
                    "python",
                    "mame-tools",
                    "ffmpeg",
                    "openssh",
                    "rsync",
                ],
            ),
        ):
            with self.subTest(manager=manager):
                self.add_tools(manager)
                result = self.invoke()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_installed()
                self.assertIn(["sudo", manager, *arguments], self.calls())
                if manager == "apt-get":
                    self.assertIn(["sudo", "apt-get", "update"], self.calls())
                (self.bin / manager).unlink()
                for name in ("python3", "chdman", "ffmpeg", "ssh", "rsync"):
                    (self.bin / name).unlink()

    def test_no_package_manager_needed_when_dependencies_exist(self):
        self.add_tools("python3", "chdman", "ffmpeg", "ssh", "rsync")
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_installed()

    def test_root_installs_dependencies_without_sudo(self):
        self.add_tools("apt-get")
        result = self.invoke(INSTALL_TEST_UID="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call[0] == "sudo" for call in self.calls()))

    def test_missing_homebrew_has_actionable_error(self):
        result = self.invoke(INSTALL_TEST_OS="Darwin")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("https://brew.sh", result.stderr)
        self.assertFalse((self.home / ".local/bin").exists())
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_package_failure_keeps_existing_install(self):
        self.assertEqual(self.invoke("--no-deps").returncode, 0)
        destination = self.home / ".local/bin/nova-push"
        destination.write_text("keep installed version")
        self.add_tools("apt-get")
        result = self.invoke(INSTALL_TEST_PACKAGE_STATUS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(destination.read_text(), "keep installed version")
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_old_python_is_rejected_after_package_install(self):
        self.add_tools("apt-get", "python3", "chdman", "ffmpeg", "ssh", "rsync")
        result = self.invoke(INSTALL_TEST_OLD_PYTHON="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python must be 3.10 or newer", result.stderr)
        self.assertFalse((self.home / ".local/bin").exists())

    def test_piped_installer_downloads_both_tools(self):
        for downloader in ("curl", "wget"):
            with self.subTest(downloader=downloader):
                self.add_tools(downloader)
                result = self.invoke("--no-deps", piped=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_installed()
                calls = [call for call in self.calls() if call[0] == downloader]
                self.assertEqual(len(calls), 2)
                (self.bin / downloader).unlink()

    def test_failed_or_empty_download_does_not_install(self):
        self.add_tools("curl")
        for option in ("INSTALL_TEST_DOWNLOAD_FAIL", "INSTALL_TEST_EMPTY_DOWNLOAD"):
            with self.subTest(option=option):
                result = self.invoke("--no-deps", piped=True, **{option: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.home / ".local/bin").exists())
                self.assertEqual(list(self.scratch.iterdir()), [])

    def test_help_invalid_arguments_and_unsupported_platform_do_not_install(self):
        self.assertEqual(self.invoke("--help").returncode, 0)
        for args in (("--unknown",), ("--prefix",), ("--prefix", "relative")):
            with self.subTest(args=args):
                self.assertNotEqual(self.invoke(*args).returncode, 0)
        result = self.invoke("--no-deps", INSTALL_TEST_OS="FreeBSD")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported platform", result.stderr)
        self.assertFalse((self.home / ".local/bin").exists())

    def test_existing_directory_is_not_replaced(self):
        directory = self.home / ".local/bin/nova-push"
        directory.mkdir(parents=True)
        result = self.invoke("--no-deps")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot replace a directory", result.stderr)
        self.assertTrue(directory.is_dir())


if __name__ == "__main__":
    unittest.main()
