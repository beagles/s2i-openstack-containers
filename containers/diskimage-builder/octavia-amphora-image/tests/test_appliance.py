"""Offline runner contract tests: python3 -m unittest discover -s tests -v."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


RUNNER = Path(__file__).resolve().parents[1] / "scripts" / "run-appliance"
MOCK = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["MOCK_CALLS"], "a") as stream:
    stream.write(json.dumps([tool, args]) + "\n")
if tool == "qemu-system-x86_64":
    # Retain the configuration for quoting tests before runner cleanup.
    drive = next(arg for arg in args if "config.ext4" in arg)
    scratch = Path(drive.split("file=", 1)[1].split(",", 1)[0]).parent
    config = (scratch / "config" / "build.env").read_text()
    Path(os.environ["MOCK_CONFIG"]).write_text(config)
    sys.exit(int(os.environ.get("MOCK_QEMU_STATUS", "0")))
if tool == "debugfs":
    command = args[1].split()
    if command[0] == "cat":
        status = os.environ.get("MOCK_GUEST_STATUS", "0")
        if status != "missing":
            print(status)
    elif command[0] == "dump":
        if not (command[1].endswith(".qcow2") and os.environ.get("MOCK_MISSING_ARTIFACT")):
            Path(command[2]).write_text("mock data")
if tool == "qemu-img" and args[0] == "check":
    sys.exit(int(os.environ.get("MOCK_CHECK_STATUS", "0")))
'''


class ApplianceRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="amphora-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        mock = self.bin / "mock"
        mock.write_text(MOCK)
        mock.chmod(0o755)
        for name in ("qemu-system-x86_64", "qemu-img", "debugfs", "mkfs.ext4"):
            (self.bin / name).symlink_to(mock)
        self.calls = self.root / "calls.jsonl"
        self.config = self.root / "config.env"
        self.output = self.root / "output"

    def run_appliance(self, *args, **settings):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                   MOCK_CALLS=str(self.calls), MOCK_CONFIG=str(self.config),
                   AMPHORA_OUTPUT_DIR=str(self.output))
        env.update(settings)
        return subprocess.run(["bash", str(RUNNER), *args], env=env,
                              capture_output=True, text=True)

    def test_success_uses_tcg_and_no_host_devices(self):
        result = self.run_appliance()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        args = next(args for tool, args in calls if tool == "qemu-system-x86_64")
        self.assertEqual(args[args.index("-accel") + 1], "tcg,thread=multi")
        self.assertNotIn("-enable-kvm", args)
        self.assertNotIn("-fsdev", args)
        for arg in args:
            self.assertNotIn("file=/dev/", arg)
        self.assertTrue((self.output / "amphora-x64-haproxy.qcow2").is_file())
        self.assertFalse(list(self.output.glob("appliance.*")))

    def test_qemu_failure_overrides_guest_success(self):
        self.assertNotEqual(self.run_appliance(MOCK_QEMU_STATUS="1").returncode, 0)

    def test_timeout_overrides_guest_success(self):
        self.assertNotEqual(self.run_appliance(MOCK_QEMU_STATUS="124").returncode, 0)

    def test_guest_failure(self):
        self.assertNotEqual(self.run_appliance(MOCK_GUEST_STATUS="7").returncode, 0)

    def test_missing_guest_status(self):
        self.assertNotEqual(self.run_appliance(MOCK_GUEST_STATUS="missing").returncode, 0)

    def test_missing_artifact(self):
        self.assertNotEqual(self.run_appliance(MOCK_MISSING_ARTIFACT="1").returncode, 0)

    def test_bad_qcow2(self):
        self.assertNotEqual(self.run_appliance(MOCK_CHECK_STATUS="2").returncode, 0)

    def test_stale_artifact_cannot_mask_missing_output(self):
        self.output.mkdir()
        (self.output / "amphora-x64-haproxy.qcow2").write_text("stale")
        self.assertNotEqual(self.run_appliance(MOCK_MISSING_ARTIFACT="1").returncode, 0)

    def test_smoke_is_explicit(self):
        self.assertEqual(self.run_appliance("--smoke").returncode, 0)
        self.assertIn("export AMPHORA_SMOKE=1\n", self.config.read_text())

    def test_proxy_values_are_shell_quoted(self):
        value = "http://user:p'ass;$(exit 9)@proxy:3128"
        result = self.run_appliance(https_proxy=value)
        self.assertEqual(result.returncode, 0, result.stderr)
        check = subprocess.run(["bash", "-c", 'source "$1"; printf %s "$https_proxy"',
                                "bash", str(self.config)], capture_output=True, text=True)
        self.assertEqual(check.stdout, value)

    def test_source_image_is_a_fourth_readonly_drive(self):
        source = self.root / "source-image.ext4"
        source.write_text("mock input disk")
        result = self.run_appliance(AMPHORA_BUILD_METHOD="source-image",
                                    AMPHORA_SOURCE_DISK=str(source),
                                    AMPHORA_IMAGE_SIZE="5")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        args = next(args for tool, args in calls if tool == "qemu-system-x86_64")
        drives = [args[i + 1] for i, arg in enumerate(args) if arg == "-drive"]
        self.assertEqual(len(drives), 4)
        self.assertEqual(drives[-1],
                         f"file={source},format=raw,if=virtio,readonly=on")
        config = self.config.read_text()
        self.assertIn("export AMPHORA_BUILD_METHOD=source-image\n", config)
        self.assertIn("export AMPHORA_IMAGE_SIZE=5\n", config)
        self.assertNotIn("AMPHORA_SOURCE_DISK", config)

    def test_source_image_requires_nonempty_input_disk(self):
        source = self.root / "absent.ext4"
        self.assertNotEqual(self.run_appliance(
            AMPHORA_BUILD_METHOD="source-image",
            AMPHORA_SOURCE_DISK=str(source)).returncode, 0)
        source.touch()
        self.assertNotEqual(self.run_appliance(
            AMPHORA_BUILD_METHOD="source-image",
            AMPHORA_SOURCE_DISK=str(source)).returncode, 0)

    def test_invalid_source_configuration(self):
        for settings in ({"AMPHORA_BUILD_METHOD": "unknown"},
                         {"AMPHORA_IMAGE_SIZE": "0"},
                         {"AMPHORA_IMAGE_SIZE": "3;false"},
                         {"AMPHORA_BUILD_METHOD": "source-image",
                          "AMPHORA_SOURCE_DISK": "/input,readonly=off"}):
            self.assertNotEqual(self.run_appliance(**settings).returncode, 0)

    def test_invalid_names_and_arguments(self):
        for name in ("../escape", "bad name", "x;false", "-option"):
            self.assertNotEqual(self.run_appliance(IMAGE_NAME=name).returncode, 0)
        self.assertNotEqual(self.run_appliance("--unknown").returncode, 0)


if __name__ == "__main__":
    unittest.main()
