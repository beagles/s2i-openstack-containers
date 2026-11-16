# Native ublk comparison (experimental)

Native smoke builds pass. The original, permissive-guest full build cannot
finish offline guest SELinux labeling on this host. The explicit
`AMPHORA_SELINUX=disabled` option now avoids that labeling step and disables
SELinux in the guest; see below. A full build/boot with this new option has not
yet been validated, and there is **no native amphora qcow2 or performance
comparison**. Temporary host security/module setup remains removed.

`Containerfile.ublk` builds the amphora during the container build, using native
host-kernel ublk rather than a QEMU appliance. `Containerfile` remains the working
QEMU baseline. This comparison is opt-in; `build.sh` still selects `Containerfile`
by default. No `capabilities.txt` is added that could silently change the baseline.

## What stays the same

* The container-patched DIB revision in `../sources.txt` is unchanged.
* The Octavia revision, `centos-minimal`/CentOS Stream 10, agent source, released
  constrained `octavia-lib`, and guest constraints are unchanged. The default
  element order is unchanged; the disabled mode replaces only the SELinux element.
* The block-device layout is the same MBR/root layout selected by the baseline's
  `vm` element. Only the level0 `local_loop` device becomes `ublk`.
* The final container contains only the qcow2/checksums and shared `copy_out.sh`.
  It is an artifact container, **not a runtime image builder**.

The new backend is an out-of-tree DIB entry-point plugin in `ublk/`, inspired by
[DIB Gerrit change 984479](https://review.opendev.org/c/openstack/diskimage-builder/+/984479)
(WIP revision `0fe7efef80e14a182ec865bfb3ced3fd6ecb0a5c`). It does not replace the
required DIB checkout with that older experimental branch. In particular:

* ublksrv v1.9 is built from pinned source in the `ublk-tools` stage because it is
  not packaged in the configured EL10 repositories; liburing is packaged.
* The WIP's `ublk add -b <sector-size>` is not used: current ublksrv has no such
  sector-size option (`-b` means batch IO). This trial requires 512-byte sectors.
* The daemon runs in a separate, private mount namespace, as required by ublksrv
  to avoid self-deadlocking when a client namespace with mounted ublk filesystems
  exits. The backing file uses buffered IO for container-overlay compatibility.
* An explicit, unused `AMPHORA_UBLK_ID` (default `0`, range `0..4095`) allows the
  char node to be created before the daemon opens it. The block node is created
  from its sysfs device number after attachment. Existing IDs/nodes are refused.
  Cleanup addresses only that ID; it never uses `ublk del --all` or removes
  unrelated devices. Use a private container `/dev`, never a host `/dev` bind mount.

## Guest SELinux mode

`Containerfile.ublk` accepts `--build-arg AMPHORA_SELINUX=disabled`. The default
is `permissive`, preserving the original comparison behavior. Other values
fail before preflight/bootstrap. The final container records the choice in
the `io.openstack.amphora.selinux` label. The QEMU Containerfile is unchanged.

Disabled mode substitutes the local `amphora-no-selinux` element, which:

* Depends on DIB's `disable-selinux` and ensures the guest configuration has
  `SELINUX=disabled`, preserving other settings.
* Adds `selinux=0` to the guest bootloader command line, preserving other
  arguments and removing conflicting/duplicate `selinux=...` values.
* Provides `selinux-fixfiles-restore`, so the `rpm-distro` dependency is
  satisfied **without including or executing the offline relabel hook**.
* Removes any guest `/.autorelabel` marker, including in final cleanup. This
  is disabling guest SELinux, not deferring labeling to the first boot.

No policy files are deleted and the patched DIB checkout is not modified.
SELinux libraries, tools and base policy RPMs may still be installed as normal
dependencies; the Octavia-specific `amphora-selinux` element is not selected.
This avoids relying on the discontinued `openstack-selinux` package without
attempting to strip every SELinux-related RPM.

For a full build using the administrator-configured native builder described
below, remove `--target smoke`, add `--build-arg AMPHORA_SELINUX=disabled`, and
choose a separate artifact tag. Do not add `MAC_ADMIN` or disable the builder's
SELinux confinement merely for guest relabeling: this mode does not perform
that relabel at all. Native mount, device, io_uring and host LSM permissions
are still required; this does not make ublk rootless or capability-free.

Guest SELinux will provide **no mandatory-access-control protection** in this
mode. The option does not change the host's SELinux mode or grant permissions.
Before using the resulting image, boot-test that `/proc/cmdline` contains
`selinux=0`, `getenforce` reports `Disabled`, and no autorelabel is scheduled.
Controller/agent integration remains a separate validation step.

## Host requirements: this is NOT capability-free

ublk replaces `losetup`, **not** DIB's privileged filesystem operations. The
unchanged partitioning plugin still calls `kpartx`/`dmsetup`, and DIB still mounts
block filesystems and uses chroot. A native full build needs:

* A dedicated **rootful builder in the initial user namespace**, with effective
  `CAP_SYS_ADMIN`, `CAP_MKNOD`, and `CAP_SYS_CHROOT`. Rootless Podman cannot mount
  block filesystems, even if capabilities are added inside its user namespace.
* Host `ublk_drv` and device-mapper support, with read/write access to
  `/dev/ublk-control` and `/dev/mapper/control` in build steps.
* io_uring support including `IORING_SETUP_SQE128` and `IORING_OP_URING_CMD`;
  seccomp and host io_uring policy must allow it. The setup probe alone cannot
  prove every operation needed by the server is allowed.
* Device-cgroup/LSM policy allowing the newly created `ublkcN`, `ublkbN`, and
  device-mapper partition nodes, plus filesystem mounts and namespace creation.
  Merely passing the two control devices and adding capabilities is not enough
  on every builder. Dynamic device numbers make a portable minimal allowlist
  nontrivial; build-engine support must be checked for the target host.

The preflight only inspects permissions and opens control devices; neither it
nor the Containerfile loads modules, changes udev rules/sysctls, grants devices,
or disables seccomp/SELinux. There is no privileged-mode or VM fallback in this
variant. Do not enable broad device access on a production host just for this
comparison. These requirements differ substantially from the original sandbox
goal; they need separate administrator approval/setup.

## Build and test

Prepare sources using the repository's source-cloning workflow first; the
image-specific `sources.txt` now includes ublksrv. The context remains
`containers/diskimage-builder`. From the repository root, the **tooling-only**
build works without extra permissions:

```sh
podman build --target tooling \
  --build-arg BASE_IMAGE=localhost/openstack/openstack-base:master-latest \
  --build-arg CONSTRAINTS_FILE=requirements.lock.master \
  -t localhost/amphora-ublk-tooling:test \
  -f containers/diskimage-builder/octavia-amphora-image/Containerfile.ublk \
  containers/diskimage-builder

podman run --rm --cap-drop=all --network=none --entrypoint python3 \
  localhost/amphora-ublk-tooling:test \
  -m unittest discover -s /src/dib-ublk/tests -v
```

On an **administrator-configured native builder**, use that same Containerfile
with `--target smoke` before attempting a full build. The smoke target uses the
real DIB graph to attach ublk, create the MBR, map partitions, format/mount ext4,
write/read a marker, unmount/detach, convert to qcow2, and run `qemu-img check`.
It does not download the amphora's RPMs and is not a bootable amphora image.
On failure it attempts unmount/detach and retains state/backing files for
diagnosis instead of blindly deleting a potentially active backing file.

Remove `--target` to build/package the amphora. Add
`--build-arg AMPHORA_UBLK_ID=<unused-id>` if ID 0 is occupied. The normal full
build also runs the preflight before downloading guest packages. Native builds
inherit normal builder network, proxy, time, memory and disk limits; there are
no `AMPHORA_MEMORY`, `AMPHORA_CPUS`, or `AMPHORA_WORK_DISK` VM arguments.

Do not interpret the unprivileged tooling command above as a working native
image build command. There is intentionally no `--privileged`, whole-`/dev`
mount, or global security-policy override example.

### Scoped native test-host setup

The following helpers were added for administrator-approved native testing:

* `ublk/allow-io-uring.py` derives a **build-local** seccomp profile from the
  installed default, changing only the three io_uring syscalls. It does not
  edit the installed default or disable seccomp.
* `ublk/amphora_ublk.cil` defines an opt-in `amphora_ublk_t` SELinux domain,
  with container file management, native mount capabilities, io_uring access,
  bootstrap proc/sysfs/devpts mounts, and label transitions for newly created
  content on the initially unlabeled image filesystem. It does
  not change `container_t`, enable global booleans, or make a domain permissive.
  This is an experimental policy, not a complete audited full-DIB policy.
* `ublk/crun-ublk` and `ublk/oci-device-filter.py` add **exact major/minor**
  device-cgroup rules to Buildah's generated OCI spec. The ordinary crun
  runtime still enforces that spec. Already allocated host device numbers and
  wildcard rules are refused. No host devices other than the two explicitly
  passed control devices are mounted into the build.

Buildah in this environment lacks a `--device-cgroup-rule` build option and
rejects the deprecated OCI `precreate` hook stage, so the build-local runtime
wrapper is used instead. It supports Buildah's explicit `create --bundle` form.

After checking/approving the source, an administrator can load `ublk_drv` and
install the separate policy:

```sh
sudo modprobe ublk_drv
sudo semodule -i containers/diskimage-builder/octavia-amphora-image/ublk/amphora_ublk.cil
python3 containers/diskimage-builder/octavia-amphora-image/ublk/allow-io-uring.py \
  /usr/share/containers/seccomp.json > /tmp/opencode/amphora-ublk-seccomp.json
```

On the tested host, ID 0 uses char `511:0`; the next free ublk block device was
`259:4` and device-mapper partition was `252:1`. **Do not copy these numbers
to another host or assume they remain free.** Inspect `/proc/devices` and
`/sys/class/block/*/dev` first. This free-slot approach is deliberately a
single-builder test aid, not a reservation mechanism; hotplug/concurrent
allocations can make it fail. It never compensates by granting a wildcard.

With those exact slots verified free, a native Containerfile smoke build uses:

```sh
image_dir="$PWD/containers/diskimage-builder/octavia-amphora-image"
sudo env AMPHORA_UBLK_DEVICE_RULES='c:511:0 b:259:4 b:252:1' \
  buildah bud --runtime="$image_dir/ublk/crun-ublk" --target smoke \
  --userns=host --cap-add=SYS_ADMIN --cap-add=MKNOD \
  --device=/dev/ublk-control --device=/dev/mapper/control \
  --security-opt seccomp=/tmp/opencode/amphora-ublk-seccomp.json \
  --security-opt label=type:amphora_ublk_t \
  --build-arg BASE_IMAGE=localhost/openstack/openstack-base:master-latest \
  --build-arg CONSTRAINTS_FILE=requirements.lock.master \
  -t localhost/amphora-ublk-smoke:test -f "$image_dir/Containerfile.ublk" \
  containers/diskimage-builder
```

The rootful image store must contain the base image independently of the
rootless store. The command above validates the enforcing **smoke** path;
the prototype policy is not sufficient for a full guest relabel. If further
access is denied, inspect it rather than silently widening device rules. After tests and after all build
processes have exited, the optional policy can be removed with
`sudo semodule -r amphora_ublk`; unload `ublk_drv` only when no devices/users remain.

### Build-only SELinux opt-out: attempted, still blocked

The enforcing full-build attempt reached GRUB installation successfully, then
failed at `selinux-fixfiles-restore`: setting the guest's SELinux labels needs
broader relabel privileges, and some CentOS guest contexts are unknown to this
Fedora host. **The enforcing smoke success is not a full enforcing-build success.**

For the full comparison, the user explicitly approved trying an unconfined
SELinux process label for this build only and adding `CAP_MAC_ADMIN` (needed
for unknown guest labels). This is a significant additional permission, not
an innocuous compatibility flag. Keep the same exact device rules and custom
seccomp profile, remove `--target smoke`, replace
`--security-opt label=type:amphora_ublk_t` with `--security-opt label=disable`,
add `--cap-add=MAC_ADMIN`, and choose the artifact image tag. These were the
attempted flags, **not a working full-build recipe on this host**. The optional
`amphora_ublk` SELinux module is not needed for this opt-out path.

This does **not** run `setenforce`, disable SELinux globally, enable any global
booleans, disable seccomp, grant device wildcards, bind all of `/dev`, or use
`--privileged`. It nevertheless removes the build process's SELinux confinement
and grants native privileged capabilities, so it is unsuitable for the original
restricted/rootless-builder requirement. Use only an explicitly approved,
dedicated test builder.

That attempt passed ordinary guest relabeling but still failed with `Invalid
argument` for `/etc/my.cnf`, `/etc/my.cnf.d`, and its client configuration: their
guest contexts are unknown to the host. An isolated ext4 smoke probe confirmed
that `CAP_MAC_ADMIN` with either `spc_t` (the build-only opt-out) or an explicit
`unconfined_t` label was insufficient. Even an ordinary host-root unconfined
process could not write a synthetic unknown context. This needs an appropriate
labeling domain/helper, a compatible labeling environment, or a deliberate
first-boot relabel strategy—not more broad device permissions.

To reproduce just the unknown-context failure on an administrator-configured
test host, use `--target smoke --build-arg AMPHORA_SMOKE_LABEL_PROBE=1` with the
opt-out flags. The default smoke does not require this extra labeling privilege.
Its failure path unmounts/detaches test resources; it does not ignore a label
failure and declare the image successful.

Deferring relabeling with `/.autorelabel` and using a dedicated labeling helper
were offered as alternatives. Those attempts were stopped and neither was
implemented. Subsequently, the explicit guest-disabled option above was added
at the user's request. It deliberately omits relabeling only when selected;
the original default remains unchanged.

Extraction from a successful final container is unchanged:

```sh
mkdir -p output-ublk
podman run --rm -v "$PWD/output-ublk:/target:Z" <ublk-image-tag>
```

## Validation on this host

For the guest-disabled option, the rootless `tooling` stage rebuilt successfully.
The native offline suite has 42 passing tests, including the pinned DIB's real
dependency resolution for the full Octavia element sequence, kernel-command-line
normalization, guest configuration/cleanup, unchanged permissive selection, and
invalid-mode rejection. Tests ran without network or container capabilities.
The 11 QEMU runner tests also pass. No host modules or policy were re-enabled
for this change; a full qcow2 build and guest boot with the new option are pending.

Historical validation before that option was added:

* ublksrv v1.9 and the io_uring probe compiled successfully in the tooling build.
* 29 plugin/preflight/policy-generation tests passed in a network-disabled, capability-free
  container, including real pinned-DIB plugin discovery and graph construction.
* The 11 existing QEMU runner tests still passed.
* An actual rootless `--target smoke` build failed at preflight, **before any
  ublk attachment or amphora bootstrap**: user namespace, missing
  `SYS_ADMIN`/`MKNOD`, absent control devices, and io_uring setup returning
  `Function not implemented` under the container policy. The host kernel has
  `CONFIG_IO_URING=y` and `CONFIG_BLK_DEV_UBLK=m`; initially `ublk_drv` was not
  loaded and `/dev/ublk-control` did not exist.

After explicit administrator-setup approval, both a native runtime smoke run
and the actual Containerfile `--target smoke` build passed with the scoped
setup above: real ublk attachment, DIB MBR/partition mapping, ext4
mount, DIB-style `cp -ra` of nested directories/files/symlinks and metadata,
readback, fstrim, unmount/detach, qcow2 conversion and validation. There
were no additional wildcard block-device grants, privileged containers, whole-`/dev` mounts,
or disabled security policies. Failed attempts' test-owned devices/mappings
were cleaned up before retrying.

The full native build has completed CentOS bootstrap and amphora package
installation, then reached final ublk partition/filesystem creation. Early
SELinux denials on bootstrap mounts and archive-copy creation were corrected
with the scoped rules above; the expanded archive-copy smoke target passed
before retrying the full build. Failed-build timings are not performance results.

The next enforcing attempt installed GRUB, then failed while applying guest
SELinux labels. Its test-owned partition mapping and ublk device were explicitly
cleaned up after DIB's partial cleanup. The approved opt-out comparison described
above is separate from those enforcing tests.

The approved opt-out full build also failed at guest labeling. Its roughly
6m44s elapsed time is a **failed attempt**, not a successful image build time.
No background builds remain. All test-owned ublk devices, device-mapper
partitions, and failed Buildah working containers were removed. The temporary
`amphora_ublk` SELinux module was removed and `ublk_drv` unloaded; host SELinux
remains Enforcing. No global seccomp profile, SELinux boolean/enforcing mode,
udev rule, or sysctl was changed. Cached/named tooling and smoke images remain.

Useful local evidence:

* `/tmp/opencode/amphora-ublk-native-smoke-copy.log`: enforcing Containerfile
  smoke success, including the real archive-copy operation.
* `/tmp/opencode/amphora-ublk-native-full-v3.log`: GRUB success, then enforcing
  guest-relabel failure.
* `/tmp/opencode/amphora-ublk-native-full-v4.log`: opt-out attempt; unknown
  guest contexts still fail.
* `/tmp/opencode/amphora-ublk-label-probe.log` and
  `/tmp/opencode/amphora-ublk-label-probe-unconfined.log`: isolated ext4
  unknown-context failures, followed by successful device teardown.

**No native full qcow2, boot validation, or performance comparison is claimed.**
The earlier 517 MiB artifact and approximately 26-minute build are the QEMU
baseline only. The QEMU Containerfile and runner implementation were left intact.
