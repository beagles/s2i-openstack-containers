"""Guest SELinux opt-out tests; no mounts, devices or host policy changes."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import diskimage_builder
from diskimage_builder import element_dependencies


ROOT = Path(__file__).resolve().parents[1]
ELEMENT = ROOT / "elements/amphora-no-selinux"
BUILTIN = Path(diskimage_builder.__file__).parent / "elements"
ENVIRONMENT = ELEMENT / "environment.d/20-amphora-no-selinux.bash"
CONFIGURE = ELEMENT / "post-install.d/14-amphora-no-selinux"
CLEANUP = ELEMENT / "cleanup.d/99-amphora-no-selinux"


class GuestSelinuxTest(unittest.TestCase):
    def test_guest_hooks_are_executable_for_dib_run_parts(self):
        for hook in (CONFIGURE, CLEANUP):
            self.assertTrue(hook.stat().st_mode & 0o111, str(hook))

    def test_disabled_element_suppresses_the_real_rpm_relabel_dependency(self):
        elements = dict(element_dependencies.get_elements(
            ["base", "vm", "centos-minimal", "block-device-mbr", "amphora-no-selinux"],
            paths=f"{ROOT / 'elements'}:{BUILTIN}"))
        self.assertIn("rpm-distro", elements)
        self.assertIn("disable-selinux", elements)
        self.assertIn("amphora-no-selinux", elements)
        self.assertNotIn("selinux-fixfiles-restore", elements)
        self.assertNotIn("selinux-permissive", elements)
        hooks = [hook.name for path in elements.values()
                 for hook in (Path(path) / "cleanup.d").glob("*")]
        self.assertNotIn("99-selinux-fixfiles-restore", hooks)
        self.assertIn("99-amphora-no-selinux", hooks)

    def test_permissive_mode_retains_the_original_relabel_element(self):
        elements = dict(element_dependencies.get_elements(
            ["base", "vm", "centos-minimal", "block-device-mbr", "selinux-permissive"],
            paths=f"{ROOT / 'elements'}:{BUILTIN}"))
        self.assertIn("selinux-fixfiles-restore", elements)
        self.assertIn("selinux-permissive", elements)
        self.assertNotIn("amphora-no-selinux", elements)
        self.assertNotIn("disable-selinux", elements)

    def command_line(self, value=None):
        env = dict(os.environ)
        env.pop("DIB_BOOTLOADER_DEFAULT_CMDLINE", None)
        if value is not None:
            env["DIB_BOOTLOADER_DEFAULT_CMDLINE"] = value
        # Match DIB hook ordering and repeated environment sourcing.
        result = subprocess.run([
            "bash", "-c", 'set -euo pipefail; source "$1"; source "$2"; source "$2"; '
            'printf %s "$DIB_BOOTLOADER_DEFAULT_CMDLINE"', "bash",
            str(BUILTIN / "bootloader/environment.d/10-bootloader-default-cmdline"),
            str(ENVIRONMENT),
        ], env=env, capture_output=True, text=True, check=True)
        return result.stdout.split()

    def test_default_boot_arguments_are_retained_with_selinux_zero(self):
        self.assertEqual(self.command_line(), ["nofb", "nomodeset", "gfxpayload=text", "selinux=0"])

    def test_conflicting_arguments_are_replaced_without_duplicates(self):
        self.assertEqual(self.command_line("quiet selinux=1 console=ttyS0 selinux=0"),
                         ["quiet", "console=ttyS0", "selinux=0"])

    def configure(self, root):
        subprocess.run(["bash", "-c", 'source "$1"; amphora_disable_selinux "$2"',
                        "bash", str(CONFIGURE), str(root)], check=True)

    def test_missing_guest_configuration_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.configure(root)
            self.assertEqual((root / "etc/selinux/config").read_text().strip(), "SELINUX=disabled")

    def test_guest_configuration_is_disabled_preserving_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "etc/selinux/config"
            config.parent.mkdir(parents=True)
            config.write_text("# Guest config\nSELINUX=permissive\n SELINUX = enforcing\nSELINUXTYPE=targeted")
            (root / ".autorelabel").touch()
            self.configure(root)
            self.configure(root)
            text = config.read_text()
            self.assertIn("# Guest config\n", text)
            self.assertIn("SELINUXTYPE=targeted\n", text)
            self.assertEqual(text.count("SELINUX=disabled"), 1)
            self.assertNotIn("permissive", text)
            self.assertNotIn("enforcing", text)
            self.assertFalse((root / ".autorelabel").exists())

    def test_final_cleanup_removes_a_reintroduced_autorelabel_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.configure(root)
            (root / ".autorelabel").touch()
            subprocess.run(["bash", str(CLEANUP)], env=dict(os.environ, TARGET_ROOT=str(root)), check=True)
            self.assertFalse((root / ".autorelabel").exists())

    def test_cleanup_fails_if_guest_selinux_is_not_actually_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.configure(root)
            (root / "etc/selinux/config").write_text("SELINUX=permissive\n")
            (root / ".autorelabel").touch()
            result = subprocess.run(["bash", str(CLEANUP)],
                                    env=dict(os.environ, TARGET_ROOT=str(root)), capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue((root / ".autorelabel").exists())

    def test_cleanup_refuses_the_builder_root_and_missing_target(self):
        for target in ("", "/"):
            result = subprocess.run(["bash", str(CLEANUP)],
                                    env=dict(os.environ, TARGET_ROOT=target), capture_output=True)
            self.assertNotEqual(result.returncode, 0)


MOCK = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

tool = Path(sys.argv[0]).name
with open(os.environ["MOCK_CALLS"], "a") as stream:
    stream.write(json.dumps({"tool": tool, "args": sys.argv[1:],
                            "elements_path": os.environ.get("ELEMENTS_PATH")}) + "\n")
if tool == "git":
    print("2900ed67cac248607cdfeb42780d0ce19bbea0f5")
'''


class RunnerSelinuxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="amphora-selinux-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        executable = self.bin / "mock"
        executable.write_text(MOCK)
        executable.chmod(0o755)
        for name in ("amphora-ublk-preflight", "mkdir", "touch", "git", "disk-image-create", "qemu-img"):
            (self.bin / name).symlink_to(executable)
        self.calls = self.root / "calls.jsonl"

    def run_builder(self, mode=None):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", MOCK_CALLS=str(self.calls))
        env.pop("AMPHORA_SELINUX", None)
        if mode is not None:
            env["AMPHORA_SELINUX"] = mode
        return subprocess.run(["bash", "/usr/local/bin/build-amphora-ublk"], env=env,
                              capture_output=True, text=True)

    def dib_call(self):
        return next(call for call in map(json.loads, self.calls.read_text().splitlines())
                    if call["tool"] == "disk-image-create")

    def test_default_runner_mode_is_still_permissive(self):
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        call = self.dib_call()
        self.assertIn("selinux-permissive", call["args"])
        self.assertNotIn("amphora-no-selinux", call["args"])
        self.assertEqual(call["elements_path"], "/src/octavia/elements")

    def test_disabled_runner_selects_local_element_without_changing_other_elements(self):
        result = self.run_builder("disabled")
        self.assertEqual(result.returncode, 0, result.stderr)
        call = self.dib_call()
        self.assertEqual(call["elements_path"], "/src/dib-ublk/elements:/src/octavia/elements")
        args = call["args"]
        self.assertEqual(args[args.index("--no-tmpfs") + 1:], [
            "base", "vm", "centos-minimal", "haproxy-octavia", "rebind-sshd", "no-resolvconf",
            "amphora-agent", "octavia-lib", "sos", "cloud-init-datasources", "remove-default-ints",
            "amphora-no-selinux", "disable-makecache", "keepalived-octavia", "ipvsadmin",
            "pip-cache", "certs-ramfs",
        ])
        # Resolve the actual full Octavia sequence, not just the OS subset.
        paths = f"{ROOT / 'elements'}:/src/octavia/elements:{BUILTIN}"
        elements = dict(element_dependencies.get_elements(
            args[args.index("--no-tmpfs") + 1:] + ["block-device-mbr"], paths=paths))
        self.assertIn("amphora-agent", elements)
        self.assertIn("disable-selinux", elements)
        self.assertNotIn("selinux-fixfiles-restore", elements)
        self.assertNotIn("selinux-permissive", elements)
        self.assertNotIn("amphora-selinux", elements)

    def test_invalid_mode_fails_before_preflight_or_any_external_command(self):
        for mode in ("enforcing", "disable", "invalid"):
            result = self.run_builder(mode)
            self.assertEqual(result.returncode, 2)
            self.assertIn("AMPHORA_SELINUX", result.stderr)
            self.assertFalse(self.calls.exists())


if __name__ == "__main__":
    unittest.main()
