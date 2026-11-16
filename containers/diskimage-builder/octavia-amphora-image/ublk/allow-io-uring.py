#!/usr/bin/env python3
"""Print a build-local seccomp profile allowing only the three io_uring syscalls.

Usage: python3 allow-io-uring.py /usr/share/containers/seccomp.json > build-seccomp.json
Does not install or modify the host's default profile.
"""

import json
import sys


with open(sys.argv[1]) as stream:
    profile = json.load(stream)
names = {"io_uring_setup", "io_uring_enter", "io_uring_register"}
# Remove only existing io_uring-specific rules, retaining other syscall names
# if a rule groups them with an io_uring syscall.
rules = []
for rule in profile["syscalls"]:
    rule["names"] = [name for name in rule["names"] if name not in names]
    if rule["names"]:
        rules.append(rule)
rules.append({"names": sorted(names), "action": "SCMP_ACT_ALLOW", "args": []})
profile["syscalls"] = rules
json.dump(profile, sys.stdout, indent=2)
print()
