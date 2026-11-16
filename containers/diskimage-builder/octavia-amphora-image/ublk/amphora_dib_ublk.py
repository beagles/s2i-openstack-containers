# Copyright 2026 Red Hat, Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy at http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
"""Experimental level0 backend; see Gerrit DIB change 984479 for the proposal.

Registered separately so the required container-patched DIB stays unchanged.
Only loop attachment is replaced: DIB still uses kpartx, device-mapper and mount.
Unlike the WIP, this uses the actual ublksrv CLI (no sector-size ``-b``), isolates
the daemon's mount namespace, and creates its device nodes in a private /dev.
"""

import os
from pathlib import Path
import re

from diskimage_builder.block_device.exception import BlockDeviceSetupException
from diskimage_builder.block_device.level0.localloop import image_create
from diskimage_builder.block_device.level0.localloop import image_delete
from diskimage_builder.block_device.level0.localloop import LocalLoopNode
from diskimage_builder.block_device.plugin import PluginBase
from diskimage_builder.block_device.utils import exec_sudo


def _char_major():
    character_devices = False
    for line in Path("/proc/devices").read_text().splitlines():
        if line.endswith("devices:"):
            character_devices = line == "Character devices:"
        fields = line.split()
        if character_devices and len(fields) == 2 and fields[1] == "ublk-char":
            return int(fields[0])
    raise BlockDeviceSetupException("ublk-char is not registered; load ublk_drv on the host")


def _make_node(path, kind, major, minor):
    # Never overwrite a host-provided or another build's node (including symlinks).
    if os.path.lexists(path):
        raise BlockDeviceSetupException("Refusing existing device node: %s" % path)
    exec_sudo(["mknod", "-m", "0600", path, kind, str(major), str(minor)])


def ublkdev_detach(device_id):
    char = "/dev/ublkc%d" % device_id
    if not os.path.lexists(char):
        return  # This private node was removed by an earlier successful detach.
    # Never delete all devices, and never remove nodes if detach fails.
    exec_sudo(["ublk", "del", "-n", str(device_id)])
    exec_sudo(["rm", "-f", "/dev/ublkb%d" % device_id, char])


def ublkdev_attach(filename, block_size, device_id):
    if int(block_size) != 512:
        raise BlockDeviceSetupException("This ublk loop backend supports 512-byte sectors only")
    if not 0 <= device_id <= 4095:
        raise BlockDeviceSetupException("AMPHORA_UBLK_ID must be between 0 and 4095")
    if Path("/sys/class/ublk-char/ublkc%d" % device_id).exists():
        raise BlockDeviceSetupException("ublk ID %d is already in use; choose another" % device_id)

    # An explicit ID lets us provide the char node *before* the daemon opens it.
    # The kernel ADD_DEV operation refuses collisions; no existing ID is deleted.
    char = "/dev/ublkc%d" % device_id
    block = "/dev/ublkb%d" % device_id
    if os.path.lexists(block):
        raise BlockDeviceSetupException("Refusing existing device node: %s" % block)
    _make_node(char, "c", _char_major(), device_id)
    attached = False
    try:
        # A daemon sharing DIB's mount namespace can deadlock on namespace exit.
        # Buffered IO also supports backing files on container overlay filesystems.
        output = exec_sudo([
            "unshare", "--mount", "--propagation", "private",
            "ublk", "add", "-t", "loop", "-n", str(device_id),
            "-f", filename, "--buffered_io",
        ])
        attached = True
        match = re.search(r"dev id\s+(\d+)\b", output)
        if not match or int(match.group(1)) != device_id:
            raise BlockDeviceSetupException("Unexpected ublk add output: %r" % output)
        major, minor = Path("/sys/class/block/ublkb%d/dev" % device_id).read_text().strip().split(":")
        _make_node(block, "b", int(major), int(minor))
    except Exception:
        if attached:
            ublkdev_detach(device_id)
        else:
            # A failed ADD_DEV might be a collision: NEVER delete that device.
            exec_sudo(["rm", "-f", char])
        raise
    return block


class UblkNode(LocalLoopNode):
    def __init__(self, config, defaults, state):
        super().__init__(config, defaults, state)
        self.device_id = int(os.environ.get("AMPHORA_UBLK_ID", "0"))

    def create(self):
        self.add_rollback(image_delete, self.filename)
        image_create(self.filename, self.size)
        device = ublkdev_attach(self.filename, self.block_size, self.device_id)
        self.add_rollback(ublkdev_detach, self.device_id)
        self.state.setdefault("blockdev", {})[self.name] = {
            "device": device,
            "image": self.filename,
            "ublk_id": self.device_id,
        }

    def umount(self):
        ublkdev_detach(self.device_id)

    def delete(self):
        self.umount()
        super().delete()


class Ublk(PluginBase):
    def __init__(self, config, defaults, state):
        super().__init__()
        self.node = UblkNode(config, defaults, state)

    def get_nodes(self):
        return [self.node]
