"""Run in the tooling image; no capabilities, network or block devices needed."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

import diskimage_builder
from diskimage_builder.block_device import config
from diskimage_builder.block_device.blockdevice import BlockDevice
from diskimage_builder.block_device.exception import BlockDeviceSetupException
import yaml

import amphora_dib_ublk as ublk


class AttachTest(unittest.TestCase):
    def setUp(self):
        self.command = mock.patch.object(ublk, "exec_sudo").start()
        self.command.return_value = "dev id 12: nr_hw_queues 1 queue_depth 128\n"
        self.major = mock.patch.object(ublk, "_char_major", return_value=245).start()
        self.exists = mock.patch.object(ublk.Path, "exists", return_value=False).start()
        self.lexists = mock.patch.object(ublk.os.path, "lexists", return_value=False).start()
        self.read = mock.patch.object(ublk.Path, "read_text", return_value="259:20\n").start()
        self.addCleanup(mock.patch.stopall)

    def attach(self, block_size=512, device_id=12):
        return ublk.ublkdev_attach("/build/image0.raw", block_size, device_id)

    def test_correct_cli_namespace_and_nodes(self):
        self.assertEqual(self.attach(), "/dev/ublkb12")
        self.assertEqual(self.command.call_args_list, [
            mock.call(["mknod", "-m", "0600", "/dev/ublkc12", "c", "245", "12"]),
            mock.call(["unshare", "--mount", "--propagation", "private", "ublk", "add",
                       "-t", "loop", "-n", "12", "-f", "/build/image0.raw", "--buffered_io"]),
            mock.call(["mknod", "-m", "0600", "/dev/ublkb12", "b", "259", "20"]),
        ])

    def test_non_512_sector_size_fails_before_creating_anything(self):
        with self.assertRaises(BlockDeviceSetupException):
            self.attach(block_size=4096)
        self.command.assert_not_called()

    def test_invalid_id_fails_before_creating_anything(self):
        for device_id in (-1, 4096):
            with self.assertRaises(BlockDeviceSetupException):
                self.attach(device_id=device_id)
        self.command.assert_not_called()

    def test_busy_id_is_never_touched(self):
        self.exists.return_value = True
        with self.assertRaises(BlockDeviceSetupException):
            self.attach()
        self.command.assert_not_called()

    def test_existing_nodes_including_symlinks_are_never_overwritten(self):
        self.lexists.return_value = True
        with self.assertRaises(BlockDeviceSetupException):
            self.attach()
        self.command.assert_not_called()

    def test_failed_add_does_not_delete_a_possibly_foreign_device(self):
        def command(args):
            if "add" in args:
                raise BlockDeviceSetupException("ADD_DEV failed (possible collision)")
        self.command.side_effect = command
        with self.assertRaises(BlockDeviceSetupException):
            self.attach()
        self.command.assert_any_call(["rm", "-f", "/dev/ublkc12"])
        self.assertFalse(any("del" in call.args[0] for call in self.command.call_args_list))

    def test_bad_output_after_success_detaches_owned_id(self):
        for output in ("unknown output", "dev id 99: unexpected id"):
            self.command.return_value = output
            with mock.patch.object(ublk, "ublkdev_detach") as detach:
                with self.assertRaises(BlockDeviceSetupException):
                    self.attach()
                detach.assert_called_once_with(12)

    def test_failed_block_node_setup_detaches_owned_id(self):
        self.read.side_effect = OSError("missing sysfs dev")
        with mock.patch.object(ublk, "ublkdev_detach") as detach:
            with self.assertRaises(OSError):
                self.attach()
            detach.assert_called_once_with(12)

    def test_detach_deletes_only_owned_id_then_nodes(self):
        self.lexists.return_value = True
        ublk.ublkdev_detach(12)
        self.assertEqual(self.command.call_args_list, [
            mock.call(["ublk", "del", "-n", "12"]),
            mock.call(["rm", "-f", "/dev/ublkb12", "/dev/ublkc12"]),
        ])

    def test_detach_failure_preserves_nodes(self):
        self.lexists.return_value = True
        self.command.side_effect = BlockDeviceSetupException("busy")
        with self.assertRaises(BlockDeviceSetupException):
            ublk.ublkdev_detach(12)
        self.command.assert_called_once_with(["ublk", "del", "-n", "12"])

    def test_detach_is_noop_after_private_node_removed(self):
        ublk.ublkdev_detach(12)
        self.command.assert_not_called()


class NodeTest(unittest.TestCase):
    def test_create_records_dib_state_and_rolls_back_in_safe_order(self):
        with tempfile.TemporaryDirectory() as directory:
            state = {}
            node = ublk.UblkNode({"name": "image0", "size": "1MiB"},
                                 {"image-dir": directory}, state)
            node.device_id = 12
            with mock.patch.object(ublk, "ublkdev_attach", return_value="/dev/ublkb12") as attach:
                node.create()
            attach.assert_called_once_with(node.filename, 512, 12)
            self.assertEqual(Path(node.filename).stat().st_size, 1024 * 1024)
            self.assertEqual(state["blockdev"]["image0"], {
                "device": "/dev/ublkb12", "image": node.filename, "ublk_id": 12})
            with mock.patch.object(ublk, "ublkdev_detach") as detach:
                # Rollbacks retain the original function object; replace its entry.
                node.rollbacks[-1] = (detach, (12,), {})
                node.rollback()
                detach.assert_called_once_with(12)
            self.assertFalse(Path(node.filename).exists())

    def test_delete_does_not_remove_backing_file_if_detach_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            node = ublk.UblkNode({"name": "image0", "size": "1MiB"},
                                 {"image-dir": directory}, {})
            with mock.patch.object(ublk, "ublkdev_detach", side_effect=RuntimeError("busy")):
                with mock.patch.object(ublk.LocalLoopNode, "delete") as delete:
                    with self.assertRaises(RuntimeError):
                        node.delete()
                    delete.assert_not_called()


class IntegrationTest(unittest.TestCase):
    def test_plugin_registered_in_real_dib(self):
        self.assertTrue(config.is_a_plugin("ublk"))

    def test_layout_matches_baseline_except_level0(self):
        baseline = Path(diskimage_builder.__file__).parent / "elements/block-device-mbr/block-device-default.yaml"
        expected = yaml.safe_load(baseline.read_text())
        expected[0] = {"ublk": expected[0]["local_loop"]}
        actual = yaml.safe_load((Path(__file__).parents[1] / "block-device.yaml").read_text())
        self.assertEqual(actual, expected)

    def test_real_dib_initializes_and_constructs_entire_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            params = {"build-dir": directory, "image-dir": directory,
                      "mount-base": directory + "/mnt", "image-size": "128MiB",
                      "root-fs-type": "ext4", "root-label": "cloudimg-rootfs",
                      "config": str(Path(__file__).parents[1] / "block-device.yaml")}
            bd = BlockDevice(params)
            bd.cmd_init()
            graph, _ = config.create_graph(bd.config, params, {})
            self.assertIsInstance(graph.nodes["image0"]["obj"], ublk.UblkNode)
            self.assertIn("mount_mkfs_root", graph.nodes)


if __name__ == "__main__":
    unittest.main()
