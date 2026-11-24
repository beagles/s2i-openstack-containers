"""Source disk assembly and Octavia invocation contracts (no mounts required)."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MOCK = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

tool = Path(sys.argv[0]).name
with open(os.environ["MOCK_CALLS"], "a") as stream:
    stream.write(json.dumps({
        "tool": tool, "args": sys.argv[1:],
        "env": {key: value for key, value in os.environ.items()
                if key.startswith("DIB_")},
    }) + "\n")
if tool == "git":
    print("pinned-octavia-commit")
if tool == "bash":
    sys.exit(int(os.environ.get("MOCK_OCTAVIA_STATUS", "0")))
'''


class SourceInvocationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="amphora-source-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        mock = self.bin / "mock"
        mock.write_text(MOCK)
        mock.chmod(0o755)
        for name in ("git", "bash", "qemu-img", "disk-image-create"):
            (self.bin / name).symlink_to(mock)
        self.calls = self.root / "calls"
        self.source = self.root / "source.qcow2"
        self.source.write_text("mock image")

    def run_builder(self, **settings):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}",
                   MOCK_CALLS=str(self.calls), AMPHORA_BUILD_METHOD="source-image",
                   DIB_LOCAL_IMAGE=str(self.source), IMAGE_NAME="test-amphora",
                   AMPHORA_IMAGE_SIZE="4")
        env.update(settings)
        return subprocess.run(["/bin/bash", str(ROOT / "scripts/build-image")],
                              env=env, capture_output=True, text=True)

    def test_calls_upstream_wrapper_with_local_image(self):
        result = self.run_builder()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        wrapper = next(call for call in calls if call["tool"] == "bash")
        self.assertEqual(wrapper["args"], [
            "/src/octavia/diskimage-create/diskimage-create.sh",
            "-a", "amd64", "-i", "centos", "-d", "10-stream", "-f", "-s", "4",
            "-c", "/work/dib-cache", "-w", "/work",
            "-o", "/work/output/test-amphora.qcow2", "-t", "qcow2",
        ])
        self.assertEqual(wrapper["env"]["DIB_LOCAL_IMAGE"], str(self.source))
        self.assertEqual(wrapper["env"]["DIB_LOCAL_ELEMENTS_PATH"],
                         "/source-image/elements")
        self.assertEqual(wrapper["env"]["DIB_REPOREF_amphora_agent"],
                         "pinned-octavia-commit")
        self.assertEqual(wrapper["env"]["DIB_REPOLOCATION_upper_constraints"],
                         "file:///amphora-upper-constraints.txt")
        self.assertEqual(wrapper["env"]["DIB_INSTALLTYPE_octavia_lib"], "package")
        self.assertFalse(any(call["tool"] == "disk-image-create" for call in calls))
        self.assertEqual(calls[-1]["tool"], "qemu-img")

    def test_wrapper_failure_is_not_hidden_by_check(self):
        result = self.run_builder(MOCK_OCTAVIA_STATUS="7")
        self.assertEqual(result.returncode, 7)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertFalse(any(call["tool"] == "qemu-img" for call in calls))

    def test_missing_source_fails_before_octavia(self):
        self.source.unlink()
        self.assertNotEqual(self.run_builder().returncode, 0)
        self.assertFalse(self.calls.exists())

    def test_scoped_element_delegates_to_image_based_centos(self):
        element = ROOT / "source-image-elements/centos-minimal"
        self.assertEqual((element / "element-deps").read_text().split(),
                         ["centos", "dracut-regenerate"])
        self.assertFalse((element / "element-provides").exists())


class UpstreamWrapperTest(unittest.TestCase):
    def test_pinned_wrapper_resolves_the_local_centos_root(self):
        if importlib.util.find_spec("diskimage_builder") is None:
            self.skipTest("requires the prepared DIB tooling environment")
        octavia = ROOT / "src/octavia"
        if not octavia.is_dir():
            octavia = Path("/src/octavia")
        wrapper = octavia / "diskimage-create/diskimage-create.sh"
        if not wrapper.is_file():
            self.skipTest("requires the pinned Octavia checkout")
        with tempfile.TemporaryDirectory(prefix="amphora-wrapper-test-") as tmp:
            root = Path(tmp)
            binary = root / "disk-image-create"
            binary.write_text(r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import diskimage_builder
from diskimage_builder.element_dependencies import get_elements

args = sys.argv[1:]
# Match DIB's vm/amd64 default-block-device expansion before resolution.
elements = args[-1].split() + ["block-device-mbr"]
paths = (os.environ["ELEMENTS_PATH"] + ":" +
         str(Path(diskimage_builder.__file__).parent / "elements"))
expanded = dict(get_elements(elements, paths))
Path(os.environ["TEST_RESULT"]).write_text(json.dumps(expanded))
Path(args[args.index("-o") + 1]).touch()
''')
            binary.chmod(0o755)
            result_path = root / "resolved.json"
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}",
                       OCTAVIA_REPO_PATH=str(octavia),
                       DIB_LOCAL_ELEMENTS_PATH=str(ROOT / "source-image-elements"),
                       DIB_LOCAL_IMAGE=str(root / "source.qcow2"),
                       TEST_RESULT=str(result_path))
            env.pop("DIB_ELEMENTS", None)
            env.pop("DIB_LOCAL_ELEMENTS", None)
            result = subprocess.run(
                ["bash", str(wrapper), "-a", "amd64", "-i", "centos",
                 "-d", "10-stream", "-f", "-s", "3", "-w", str(root),
                 "-c", str(root / "cache"), "-o", str(root / "amphora"),
                 "-t", "qcow2"], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            expanded = json.loads(result_path.read_text())
            self.assertEqual(expanded["centos-minimal"],
                             str(ROOT / "source-image-elements/centos-minimal"))
            for element in ("centos", "dracut-regenerate",
                            "selinux-permissive", "disable-makecache"):
                self.assertIn(element, expanded)
            self.assertNotIn("yum-minimal", expanded)


@unittest.skipUnless(all(shutil.which(tool) for tool in
                         ("qemu-img", "sfdisk", "mkfs.ext4", "debugfs")),
                     "requires qemu-img, util-linux and e2fsprogs")
class SourceDiskTest(unittest.TestCase):
    def test_partitioned_qcow2_and_sanitized_root(self):
        with tempfile.TemporaryDirectory(prefix="amphora-disk-test-") as tmp:
            root = Path(tmp)
            source = root / "os"
            for directory in ("etc/ssh", "etc/default", "var/lib/cloud",
                              "var/log", "usr/bin"):
                (source / directory).mkdir(parents=True)
            for name in (".dockerenv", "etc/machine-id", "etc/fstab",
                         "etc/hosts", "etc/ssh/ssh_host_rsa_key",
                         "var/lib/cloud/instance-id", "var/log/build.log"):
                (source / name).write_text("must not ship")
            (source / "etc/os-release").write_text("ID=centos\nVERSION_ID=10\n")
            (source / "etc/default/grub").write_text(
                "GRUB_ENABLE_BLSCFG=true\nGRUB_TIMEOUT=5\n")
            output = root / "source.qcow2"
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/make-source-image"), str(source),
                 str(output)], capture_output=True, text=True,
                env=dict(os.environ, AMPHORA_SOURCE_SIZE="64M"))
            self.assertEqual(result.returncode, 0, result.stderr)
            info = json.loads(subprocess.check_output(
                ["qemu-img", "info", "--output=json", str(output)], text=True))
            self.assertEqual(info["format"], "qcow2")
            self.assertEqual(info["virtual-size"], 64 * 1024 * 1024)
            raw = root / "source.raw"
            subprocess.run(["qemu-img", "convert", "-f", "qcow2", "-O", "raw",
                            str(output), str(raw)], check=True)
            table = json.loads(subprocess.check_output(
                ["sfdisk", "--json", str(raw)], text=True))["partitiontable"]
            self.assertEqual(table["label"], "dos")
            self.assertEqual(len(table["partitions"]), 1)
            self.assertEqual(table["partitions"][0]["start"], 2048)
            filesystem = root / "root.ext4"
            with raw.open("rb") as stream, filesystem.open("wb") as target:
                stream.seek(1048576)
                shutil.copyfileobj(stream, target)

            def cat(path):
                return subprocess.run(["debugfs", "-R", f"cat {path}",
                                       str(filesystem)], capture_output=True,
                                      text=True)

            self.assertIn("ID=centos", cat("/etc/os-release").stdout)
            self.assertEqual(cat("/etc/machine-id").stdout, "")
            self.assertEqual(cat("/etc/default/grub").stdout,
                             "GRUB_TIMEOUT=5\nGRUB_ENABLE_BLSCFG=false\n")
            for name in ("/.dockerenv", "/etc/fstab", "/etc/hosts",
                         "/etc/ssh/ssh_host_rsa_key", "/var/log/build.log",
                         "/var/lib/cloud/instance-id"):
                self.assertIn("File not found", cat(name).stderr, name)
            self.assertFalse(list(root.glob("source-image.*")))


if __name__ == "__main__":
    unittest.main()
