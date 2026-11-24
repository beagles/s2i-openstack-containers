# Octavia amphora image

This Containerfile runs diskimage-builder **during the container build** and
packages `amphora-x64-haproxy.qcow2` and `amphora-x64-haproxy.sha256`. Running
the resulting container copies these artifacts to `/target`, using the same
`copy_out.sh` as the IPA image. The default guest is x86_64 CentOS Stream 10,
built from RPMs using DIB's `centos-minimal` element. No GenericCloud qcow2
is downloaded or extracted.

For an **alternative CentOS source-image build**, use
[`Containerfile.source-image`](Containerfile.source-image) and see
[SOURCE-IMAGE.md](SOURCE-IMAGE.md). It prepares a local CentOS qcow2 from
container OS content (following EDPM's separation of OS content and builder),
then passes it as `DIB_LOCAL_IMAGE` to Octavia's `diskimage-create.sh` inside
the same QEMU appliance, rather than bootstrapping with `centos-minimal`.

For the **native ublk comparison**, use [`Containerfile.ublk`](Containerfile.ublk)
and see [UBLK.md](UBLK.md). That experimental variant has different host/device
requirements; the capability-free validation below applies only to the QEMU path.
Its native smoke build passes. The original full build is blocked at guest
SELinux labeling on this host; the explicit `AMPHORA_SELINUX=disabled` build
argument now disables guest SELinux and skips offline relabeling. Full-image
build/boot validation of that option is pending; no native amphora qcow2 has
been produced.

## Sources and constraints

Use `containers/diskimage-builder` as the build context. The repository's
`build.sh` prepares the project and image-specific `src/` directories using
their respective `sources.txt` files. The project pin includes the container
support patches referenced by commit `cfcf54b554401309a2fe9a94a7b9ce0b19d5316a`;
do not replace it with an unpatched PyPI diskimage-builder package.

Octavia is pinned to the same stable/2026.2 commit as the service containers.
The element list follows Octavia's `diskimage-create.sh`, including its
`centos-minimal` root filesystem and disabled bootstrap network interfaces.
The appliance invokes DIB directly. The amphora agent
comes from the pinned local Octavia checkout; octavia-lib is installed from
PyPI according to the full upstream constraints. `CONSTRAINTS_FILE` controls
the host-side DIB dependencies; `AMPHORA_CONSTRAINTS_FILE` independently selects
the full guest constraints (default `upper-constraints.txt.${SOURCE_VERSION_STREAM}`).
The reduced DIB lockfile is not a complete set of amphora constraints.

## Restricted build environment

The build runs DIB inside a **QEMU software-emulated Linux appliance**, still
during the Containerfile build. It requires no additional container capabilities,
`--privileged`, host device mounts, `/dev/fuse`, `/dev/kvm`, nested virtualization,
or relaxed seccomp/SELinux policy. The normal permissions needed to run your
container builder itself are separate; this does not make an otherwise unusable
Buildah/Podman setup work inside a sandbox.

The outer container needs x86_64 userspace, permission to execute QEMU and create
threads, outbound package/source access, at least 4 GiB **guest** RAM plus QEMU
and builder overhead, and substantial writable disk space. TCG is slower than
KVM; CPU/memory/time quotas can still prevent a build in a restricted CI job.

The stages are:

* `tooling`: install patched DIB, the pinned Octavia sources, a CentOS kernel,
  and guest build tools; compile the missing `sgdisk` from pinned source.
* `emulator`: use CentOS's QEMU package if it advertises TCG. Otherwise compile
  the pinned QEMU checkout with TCG and libslirp, without KVM. QEMU's own wrap
  files pin its build-time subprojects. `FORCE_SOURCE_QEMU=1` tests this fallback.
* `appliance`: generate a boot initramfs with dracut, then use `mkfs.ext4 -d`
  to populate an appliance filesystem from `tooling`, **without mounting it**.
* `build`: boot that filesystem with `-accel tcg -cpu max`, a private writable
  root overlay, a work disk, and a read-only configuration disk. All loop,
  device-mapper, chroot, and mount operations occur inside the VM. Userspace
  networking needs no TAP device or network-administration capabilities.
* Final stage: package only the extracted amphora qcow2 and checksums.

No container filesystem is shared writable with the VM. After QEMU stops,
`debugfs` reads a guest-written completion status and extracts the artifact
without mounting the work disk. Missing status, guest errors, QEMU failure,
timeout, missing output, or failed `qemu-img check` fail the build. Serial output
streams to the build log. A successful build stage also retains
`/build/amphora-x64-haproxy.build.log` (not included in the final image).

With sources prepared by `build.sh`, build from the repository root:

```sh
podman build \
  --build-arg BASE_IMAGE=localhost/openstack/openstack-base:master-latest \
  --build-arg CONSTRAINTS_FILE=requirements.lock.master \
  -t localhost/openstack/openstack-octavia-amphora-image:master-latest \
  -f containers/diskimage-builder/octavia-amphora-image/Containerfile \
  containers/diskimage-builder
```

Use `--target tooling` to check DIB installation, or `--target smoke` to boot
the appliance and test loop allocation, partitioning, device-mapper, formatting,
mounting, chroot, qcow2 conversion, and extraction without downloading guest
RPMs. **The smoke target is not an amphora image.** Full builds use the same
appliance but execute the Octavia element sequence.

Offline runner/failure-handling tests can be run from this directory with
`python3 -m unittest discover -s tests -v`.

Both the packaged and source-built QEMU paths have passed the appliance smoke
test under rootless Podman with SELinux and the default seccomp policy enabled,
including a repeat run using `podman run --cap-drop=all` with no device mounts.
The full amphora build has also completed under rootless Podman, producing a
roughly 517 MiB qcow2 (about 1.9 GiB virtual disk); extraction, both SHA256 files,
and `qemu-img check` passed. A local TCG build took approximately 26 minutes;
timings and artifact sizes depend on packages and builder resources. These
checks alone do not validate amphora service health against an Octavia controller.
The resulting disk has also booted through GRUB and the CentOS kernel to a
serial login prompt and completed cloud-init under source-built QEMU/TCG with
all outer-container capabilities dropped. That boot test used a temporary
snapshot and no Octavia configuration drive, then deliberately stopped the VM;
it is not an Octavia controller/agent integration test.

Build arguments for full builds:

| Argument | Default | Meaning |
| --- | --- | --- |
| `AMPHORA_MEMORY` | `4096` | Guest memory in MiB, excluding host/QEMU overhead |
| `AMPHORA_CPUS` | `2` | Emulated vCPUs |
| `AMPHORA_WORK_DISK` | `32G` | Sparse work disk capacity, not amphora disk size |
| `AMPHORA_TIMEOUT` | `6h` | QEMU time limit (`timeout` syntax) |
| `IMAGE_NAME` | `amphora-x64-haproxy` | Artifact basename |

Guest RPMs come from the CentOS Stream 10 repositories configured by
`centos-minimal`. DIB calculates the amphora filesystem size automatically.
Use `--no-cache` to rebuild against current repositories. Standard HTTP/HTTPS
proxy and no-proxy variables are explicitly passed into the appliance; the
rest of the container environment is not inherited. A proxy must be reachable
from QEMU's userspace network (guest `127.0.0.1` is not the outer container).
The source checkouts, constraints, and tooling-stage CA trust are copied into
the appliance; transient builder secret/config mounts are not automatically
forwarded. No SSH server or inbound forwarded port is used.

## Extract

```sh
mkdir -p output
podman run --rm -v "$PWD/output:/target:Z" \
  localhost/openstack/openstack-octavia-amphora-image:master-latest
```

Extraction needs no additional capabilities. The copy-out script also creates
the per-image `amphora-x64-haproxy.qcow2.sha256` checksum.
