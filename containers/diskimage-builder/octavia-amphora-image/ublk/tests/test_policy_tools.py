"""Generated policies must preserve defaults and never grant device wildcards."""

import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).parents[1]


class PolicyToolsTest(unittest.TestCase):
    def test_seccomp_changes_only_io_uring(self):
        original = {"defaultAction": "SCMP_ACT_ERRNO", "syscalls": [
            {"names": ["read", "io_uring_setup"], "action": "SCMP_ACT_ALLOW"},
            {"names": ["io_uring_enter"], "action": "SCMP_ACT_ERRNO"},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "seccomp.json"
            source.write_text(json.dumps(original))
            output = io.StringIO()
            with mock.patch.object(sys, "argv", ["allow-io-uring.py", str(source)]):
                with mock.patch.object(sys, "stdout", output):
                    runpy.run_path(str(ROOT / "allow-io-uring.py"), run_name="__main__")
            result = json.loads(output.getvalue())
            self.assertEqual(result["defaultAction"], original["defaultAction"])
            self.assertEqual(result["syscalls"][0], {"names": ["read"], "action": "SCMP_ACT_ALLOW"})
            self.assertEqual(set(result["syscalls"][1]["names"]), {
                "io_uring_setup", "io_uring_enter", "io_uring_register"})
            self.assertEqual(json.loads(source.read_text()), original)

    def filter(self, *devices, allocated=False):
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["oci-device-filter.py", *devices]):
            with mock.patch.object(sys, "stdin", io.StringIO('{"ociVersion":"1.0.2"}')):
                with mock.patch.object(sys, "stdout", output):
                    with mock.patch.object(Path, "exists", return_value=allocated):
                        runpy.run_path(str(ROOT / "oci-device-filter.py"), run_name="__main__")
        return json.loads(output.getvalue())

    def test_only_explicit_device_numbers_are_granted(self):
        result = self.filter("c:511:0", "b:259:4", "b:252:1")
        self.assertEqual(result["ociVersion"], "1.0.2")
        self.assertEqual(result["linux"]["resources"]["devices"], [
            {"allow": True, "type": "c", "major": 511, "minor": 0, "access": "rwm"},
            {"allow": True, "type": "b", "major": 259, "minor": 4, "access": "rwm"},
            {"allow": True, "type": "b", "major": 252, "minor": 1, "access": "rwm"},
        ])

    def test_allocated_host_device_is_refused(self):
        with self.assertRaises(RuntimeError):
            self.filter("b:259:0", allocated=True)

    def test_wildcards_and_invalid_types_are_refused(self):
        for device in ("b:259:*", "a:1:1", "c:-1:0"):
            with self.assertRaises(ValueError):
                self.filter(device)

    def test_runtime_wrapper_filters_only_create_spec(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            config.write_text('{"ociVersion":"1.0.2"}')
            args = ["crun-ublk", "create", "--bundle", directory, "build-test"]
            result = subprocess.CompletedProcess([], 0, '{"filtered":true}', "")
            with mock.patch.object(sys, "argv", args):
                with mock.patch.dict(os.environ, {"AMPHORA_UBLK_DEVICE_RULES": "c:511:0"}):
                    with mock.patch.object(subprocess, "run", return_value=result) as run:
                        with mock.patch.object(os, "execv") as execute:
                            runpy.run_path(str(ROOT / "crun-ublk"), run_name="__main__")
            self.assertEqual(json.loads(config.read_text()), {"filtered": True})
            self.assertEqual(run.call_args.kwargs["input"], '{"ociVersion":"1.0.2"}')
            execute.assert_called_once_with("/usr/bin/crun", ["/usr/bin/crun", *args[1:]])

    def test_runtime_state_command_is_not_filtered(self):
        import os
        with mock.patch.object(sys, "argv", ["crun-ublk", "state", "build-test"]):
            with mock.patch.object(subprocess, "run") as run:
                with mock.patch.object(os, "execv") as execute:
                    runpy.run_path(str(ROOT / "crun-ublk"), run_name="__main__")
        run.assert_not_called()
        execute.assert_called_once_with("/usr/bin/crun", ["/usr/bin/crun", "state", "build-test"])

    def test_runtime_refuses_create_without_explicit_bundle(self):
        with mock.patch.object(sys, "argv", ["crun-ublk", "create", "build-test"]):
            with self.assertRaises(RuntimeError):
                runpy.run_path(str(ROOT / "crun-ublk"), run_name="__main__")


if __name__ == "__main__":
    unittest.main()
