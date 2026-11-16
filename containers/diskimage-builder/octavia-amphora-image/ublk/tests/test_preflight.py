"""Exercise diagnostics without changing the host or requiring privileges."""

import importlib.machinery
import importlib.util
import os
import stat
import subprocess
import unittest
from unittest import mock


loader = importlib.machinery.SourceFileLoader("preflight", "/usr/local/bin/amphora-ublk-preflight")
spec = importlib.util.spec_from_loader(loader.name, loader)
preflight = importlib.util.module_from_spec(spec)
loader.exec_module(preflight)


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.uid_map = "0 0 4294967295\n"
        self.caps = (1 << 21) | (1 << 27) | (1 << 18)
        def read(path):
            if str(path).endswith("uid_map"):
                return self.uid_map
            return "CapEff:\t%x\n" % self.caps
        mock.patch.object(preflight.Path, "read_text", autospec=True, side_effect=read).start()
        mock.patch.object(preflight.os, "stat", return_value=os.stat_result(
            (stat.S_IFCHR | 0o600, 0, 0, 0, 0, 0, 0, 0, 0, 0))).start()
        self.open = mock.patch.object(preflight.os, "open", return_value=123).start()
        mock.patch.object(preflight.os, "close").start()
        self.run = mock.patch.object(preflight.subprocess, "run", return_value=
                                     subprocess.CompletedProcess([], 0, "", "")).start()
        self.addCleanup(mock.patch.stopall)

    def test_permitted_environment(self):
        self.assertEqual(preflight.problems(), [])

    def test_rootless_is_rejected_even_with_namespaced_capabilities(self):
        self.uid_map = "0 20509 1\n1 100000 65536\n"
        self.assertIn("Rootless/user-namespaced", preflight.problems()[0])

    def test_reports_all_missing_capabilities(self):
        self.caps = 0
        errors = preflight.problems()
        for name in ("SYS_ADMIN", "MKNOD", "SYS_CHROOT"):
            self.assertTrue(any("CAP_" + name in error for error in errors))

    def test_missing_devices_reported(self):
        self.open.side_effect = FileNotFoundError("missing")
        errors = preflight.problems()
        self.assertTrue(any("/dev/ublk-control" in error for error in errors))
        self.assertTrue(any("/dev/mapper/control" in error for error in errors))

    def test_io_uring_policy_failure_reported(self):
        self.run.return_value = subprocess.CompletedProcess([], 1, "", "io_uring setup: blocked\n")
        self.assertIn("io_uring setup: blocked", preflight.problems())


class CharMajorTest(unittest.TestCase):
    def test_reads_character_major_not_block_major(self):
        import amphora_dib_ublk as ublk
        text = "Character devices:\n245 ublk-char\nBlock devices:\n259 ublk-char\n"
        with mock.patch.object(ublk.Path, "read_text", return_value=text):
            self.assertEqual(ublk._char_major(), 245)


if __name__ == "__main__":
    unittest.main()
