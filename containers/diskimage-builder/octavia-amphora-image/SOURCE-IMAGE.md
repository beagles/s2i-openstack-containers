# CentOS amphora from a source image

`Containerfile.source-image` is an opt-in alternative to the RPM-bootstrap
build in `Containerfile`. It implements:

```text
CentOS Stream 10 container + baseline RPMs
    -> sanitized OS filesystem
    -> partitioned centos-source.qcow2 (no mounts)
    -> DIB_LOCAL_IMAGE + Octavia diskimage-create.sh inside the QEMU appliance
    -> amphora-x64-haproxy.qcow2
```

The existing minimal and native ublk variants are unchanged. This variant
uses the same capability-free QEMU/TCG boundary as the default build; source
image extraction, loop devices, mounts, chroot and bootloader installation
all run **inside the VM**, not on the container host.

## Relationship to EDPM and the RHEL build

The [EDPM support on the `dib` branch][edpm] separates an `os_source` container
from its builder, installs a baseline package inventory, and passes that OS
content to DIB. This variant follows that separation but assembles a qcow2,
rather than passing EDPM's filesystem tarball directly to DIB. It deliberately
does not inherit the full EDPM compute-node package set or hardening/partition
layout.

The downstream [RHEL `amphora.tdl`][rhel] converts the input disk to qcow2,
sets `DIB_LOCAL_IMAGE`, and invokes Octavia with `-i rhel`. Here the source
disk is assembled from a CentOS container using regular-file operations:
`sfdisk`, `mkfs.ext4 -d` with a partition offset, and `qemu-img convert`.
Like the appliance filesystem, it needs no mount or loop device to create.
Unlike the appliance, it has a partition table because DIB's image extraction
expects partitions.

The intermediate has one ext4 root partition, a kernel and a real generic
initramfs. It is an **input filesystem image**, not an independently bootable
cloud image: DIB installs the final image's bootloader and partition layout.
The build tools and pinned Octavia checkout live only in the appliance.
Because a container filesystem has no BLS kernel entries, its GRUB defaults
explicitly disable BLS mode: DIB generates direct kernel/initramfs menu entries
instead of an empty BLS menu. `dracut-config-generic` keeps the regenerated
initramfs independent of the builder VM's hardware.

### Why the small CentOS element override?

[Octavia's script][octavia] rewrites `-i centos` to `centos-minimal`. That
element bootstraps from RPMs and does not honor `DIB_LOCAL_IMAGE`.

Only this variant sets `DIB_LOCAL_ELEMENTS_PATH` to
`source-image-elements`. Its scoped `centos-minimal` adapter depends on the
upstream [`centos` element][centos], which extracts the supplied local qcow2,
plus `dracut-regenerate`. Octavia still chooses the standard amphora elements,
SELinux-permissive behavior, ConfigDrive datasource, networking, nftables
and SSH defaults. Neither Octavia nor DIB is patched for this selection.

This is architectural parity with the RHEL source-image workflow, not an
attempt to reproduce its package inventory: the agent still comes from the
pinned Octavia source, not downstream RPMs (`-p`), and SELinux remains
permissive rather than downstream's `-e` enforcement.

## Build

Run from the repository root, with sources and constraints prepared as for
the default build (see [README.md](README.md)). First package the existing
appliance stage; **use the current checkout**, since its runner/init scripts
must support the read-only source-image input disk:

```sh
podman build \
  --build-arg BASE_IMAGE=localhost/openstack/openstack-base:master-latest \
  --build-arg CONSTRAINTS_FILE=requirements.lock.master \
  --target appliance \
  -t localhost/openstack/octavia-amphora-appliance:latest \
  -f containers/diskimage-builder/octavia-amphora-image/Containerfile \
  containers/diskimage-builder
```

Then build the alternative:

```sh
podman build \
  --build-arg BASE_IMAGE=localhost/openstack/openstack-base:master-latest \
  --build-arg APPLIANCE_IMAGE=localhost/openstack/octavia-amphora-appliance:latest \
  -t localhost/openstack/openstack-octavia-amphora-image:source-image \
  -f containers/diskimage-builder/octavia-amphora-image/Containerfile.source-image \
  containers/diskimage-builder

mkdir -p output
podman run --rm -v "$PWD/output:/target:Z" \
  localhost/openstack/openstack-octavia-amphora-image:source-image
```

No extra capabilities, host devices or privileged flags are required.
Appliance resource, proxy/network and platform requirements are the same as
in [README.md](README.md). The source qcow2 and scoped elements are attached
on a fourth read-only virtio disk. The existing status, timeout, log extraction,
artifact checks and `qemu-img check` failure handling are retained.

To build/export **only the reusable input disk**, add `--target source-image`
to the second build command and use a distinct tag. Running that image with
the same `/target` mount copies `centos-source.qcow2` and its checksums.
Do not upload that intermediate as an amphora.

| Build argument | Default | Purpose |
| --- | --- | --- |
| `CENTOS_SOURCE_IMAGE` | `quay.io/centos/centos:stream10` | OS content, independent of the builder's `BASE_IMAGE`; must be CentOS 10 |
| `APPLIANCE_IMAGE` | `localhost/openstack/octavia-amphora-appliance:latest` | Current checkout's prepared QEMU/DIB appliance |
| `AMPHORA_SOURCE_SIZE` | `4G` | Input disk's virtual capacity; must fit the baseline OS |
| `AMPHORA_IMAGE_SIZE` | `3` | Final amphora disk size in GiB, passed to Octavia's `-s` |
| `IMAGE_NAME` | `amphora-x64-haproxy` | Final artifact basename |
| `AMPHORA_MEMORY`, `AMPHORA_CPUS`, `AMPHORA_WORK_DISK`, `AMPHORA_TIMEOUT` | `4096`, `2`, `32G`, `6h` | Existing appliance runner settings |

The source stage applies EDPM's `coreutils-single`/`libcurl-minimal` swaps
when present, then installs `source-image-packages.txt`. Machine identity,
SSH host keys, cloud-init state, container markers, caches, logs and
container-specific mounts/network files are omitted from the disk.

This is **not a hermetic build**: unlike EDPM's frozen local-RPM-repository
path, Octavia/DIB may install/update packages from the source OS repositories
and fetch constrained Python dependencies. Pin `CENTOS_SOURCE_IMAGE` and
`APPLIANCE_IMAGE` by digest and use repository snapshots for repeatable inputs.
The appliance carries the pinned local Octavia agent checkout and full guest
constraints, just as the default build does. `--no-cache` refreshes moving
OS/package inputs.

## Validation

```sh
cd containers/diskimage-builder/octavia-amphora-image
python3 -m unittest discover -s tests -v
```

Tests cover source-image invocation and failure propagation, read-only disk
transport, source partition construction and identity cleanup. The regular-file
disk test runs when `qemu-img`, `sfdisk`, `mkfs.ext4` and `debugfs` are available;
it needs no root privileges. The upstream-wrapper dependency-resolution test
also needs the prepared DIB/Octavia tooling. To run all tests in that environment
(from the same directory):

```sh
podman run --rm --entrypoint python3 -v "$PWD:/tests:ro,Z" \
  localhost/openstack/octavia-amphora-appliance:latest \
  -m unittest discover -s /tests/tests -v
```

These tests alone do not prove that the final amphora boots or works with an
Octavia controller.

Local validation has passed all 20 tests in the prepared tooling container,
rootless builds of the appliance and `source-image` targets, artifact copy-out,
SHA256 verification and `qemu-img check`. The source artifact was approximately
519 MiB with a 4 GiB virtual disk. The corrected full build produced an
approximately 498 MiB amphora with a 3 GiB virtual disk, passed artifact and
integrity checks, and booted through GRUB and the CentOS kernel to a serial login
prompt under QEMU/TCG with all outer-container capabilities dropped.

An initial boot test caught an empty BLS menu; the direct-entry GRUB setting
above fixes that container-to-VM conversion issue. The successful boot used a
temporary snapshot without an Octavia configuration drive and stopped at login.
It does not validate Octavia controller/agent integration.

[edpm]: https://github.com/steveb/s2i-openstack-containers/tree/dib/containers/diskimage-builder/edpm
[rhel]: https://pkgs.devel.redhat.com/cgit/rpms/rhosp-director-images/tree/amphora.tdl?h=rhos-18.0-rhel-9-trunk
[octavia]: https://opendev.org/openstack/octavia/src/branch/stable/2026.2/diskimage-create/diskimage-create.sh
[centos]: https://opendev.org/openstack/diskimage-builder/src/branch/master/diskimage_builder/elements/centos
