#!/usr/bin/env python3
"""OCI config filter granting only explicitly named, currently free nodes.

Example arguments: c:511:0 b:259:4 b:252:1. Numbers are host-specific; never
guess or grant a whole block major (259 also contains physical NVMe disks).
This is a test-host setup aid, not a portable dynamic-device allocator.
"""

import json
from pathlib import Path
import sys


spec = json.load(sys.stdin)
rules = spec.setdefault("linux", {}).setdefault("resources", {}).setdefault("devices", [])
for value in sys.argv[1:]:
    kind, major, minor = value.split(":")
    if kind not in ("c", "b") or not major.isdecimal() or not minor.isdecimal():
        raise ValueError("Expected c:major:minor or b:major:minor")
    major, minor = int(major), int(minor)
    sysfs_kind = "char" if kind == "c" else "block"
    if Path("/sys/dev/%s/%d:%d" % (sysfs_kind, major, minor)).exists():
        raise RuntimeError("Refusing to grant an already allocated host device: " + value)
    rules.append({"allow": True, "type": kind, "major": major, "minor": minor, "access": "rwm"})
json.dump(spec, sys.stdout)
print()
