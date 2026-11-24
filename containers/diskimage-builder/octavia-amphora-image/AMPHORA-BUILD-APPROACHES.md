# Octavia amphora image build: QEMU appliance vs. native ublk

For an alternative **source-image input** to the QEMU build, see
[SOURCE-IMAGE.md](SOURCE-IMAGE.md) and `Containerfile.source-image`. That
variant prepares a local CentOS qcow2 and invokes Octavia's wrapper with
`DIB_LOCAL_IMAGE`; it changes OS sourcing, not the device/isolation backend
compared below.

Spike summary comparing two ways of running diskimage-builder (DIB) **inside a
container build** to produce an Octavia amphora image
(`amphora-x64-haproxy.qcow2`, CentOS Stream 10, `centos-minimal`).

Both variants live on this branch and share the same inputs: the container-patched
DIB pin from `../sources.txt` (commit `cfcf54b`), the pinned Octavia
`stable/2026.2` checkout, the Octavia element sequence from
`diskimage-create.sh`, and the shared `copy_out.sh` artifact-extraction container.

| | QEMU appliance | Native ublk |
| --- | --- | --- |
| Containerfile | `Containerfile` | `Containerfile.ublk` |
| Details | `README.md` | `UBLK.md` |
| Status | **Works end to end** | **No amphora produced** (smoke only) |

## Background

DIB normally builds a disk image by attaching it to a loop device (`losetup`),
partitioning it (`kpartx`/device-mapper), formatting and mounting it, and
chrooting into it. None of that is available in an unprivileged container
build. The original IPA prototype in this repo needs `CAP_SYS_ADMIN` just for
chroot/bind mounts. The spike goal was to find a path to an amphora build
**without "do whatever" permissions**.

Initial findings that shaped the options:

* The pinned DIB has no ublk support. Gerrit change 984479 (ublk block-device
  backend) is an unmerged, untested WIP, and only covers the *output* image;
  the CentOS qcow2-extraction path still uses loop devices.
* Switching the base from the GenericCloud qcow2 to `centos-minimal` (RPM
  bootstrap) removes the need to loop-mount a downloaded image, so only the
  output image needs block devices.
* FUSE-based alternatives were rejected as the highest-risk option (large DIB
  and bootloader changes).

## Approach 1: QEMU software-emulated appliance

**How it works.** The container build creates a small Linux appliance
(CentOS kernel + dracut initramfs, filesystem populated with `mkfs.ext4 -d`
so nothing is mounted). The `build` stage boots it with
`qemu-system-x86_64 -accel tcg`. Unmodified DIB runs *inside the VM*, where
loop, device-mapper, mount and chroot are all legal. A read-only config disk
passes the build config in; the result is read back with `debugfs` (no mount)
and verified with `qemu-img check`. Packaged QEMU is used if it advertises TCG;
otherwise pinned QEMU 11.1 is built from source (TCG + slirp, no KVM).

**Permissions needed.** None beyond what the container builder itself needs:
no extra capabilities, no `--privileged`, no host devices, no `/dev/kvm`, no
seccomp/SELinux relaxation. User-mode networking (slirp) needs no TAP device.

**Results.**

* Full amphora build completed under **rootless Podman** with SELinux and the
  default seccomp profile enabled.
* Artifact: ~517 MiB qcow2 (~1.9 GiB virtual), SHA256 files and `qemu-img check`
  pass.
* Build time: ~26 minutes (local TCG; will vary with CPU).
* Smoke target also passes with `podman run --cap-drop=all`.
* Booted through GRUB and the CentOS kernel to a login prompt and finished
  cloud-init under QEMU/TCG with all capabilities dropped.
* 11 offline unit tests for the runner and failure handling.
* **Not tested:** Octavia controller/agent integration (no amphora health
  check, no config drive).

**Pros**

* Only approach proven to produce a bootable image under the original
  restricted-environment requirement.
* Unmodified DIB and unmodified Octavia elements; guest SELinux is handled as
  upstream intends (`selinux-permissive`, offline relabel works inside the VM
  because the guest kernel/policy match the guest userland).
* Builder-host-independent: host kernel modules (`ublk_drv`, `dm`), host SELinux
  policy and device numbering are irrelevant. Portable across Konflux/CI
  runners.
* Strong isolation: a failed or hostile build cannot leave devices or
  mappings on the host.
* Failures are fail-closed (missing guest status, QEMU failure, timeout, bad
  output all fail the build).

**Cons / risks**

* Slow: TCG emulation (~26 min locally); no KVM acceleration assumed. CI
  CPU/time quotas could be a problem.
* Memory/disk heavy: 4 GiB guest RAM by default plus QEMU overhead and a 32G
  sparse work disk.
* More moving parts to maintain: appliance image, init script, runner, guest
  kernel/dracut handling, potential source-built QEMU (and its large build
  time/dependency set if the packaged one lacks TCG).
* CentOS/RHEL packaging risk: RHEL's `qemu-kvm` may not ship TCG; the fallback
  is building QEMU from source, which has supply-chain/FIPS/support
  implications for a productized image.
* Proxy/CA/secret plumbing must be explicitly forwarded into the VM.
* Debugging is through a serial console log rather than a normal shell.

**Overall risk: Low-to-medium.** Technically de-risked; remaining risk is
operational (build time, resource limits, QEMU sourcing and supportability).

## Approach 2: Native ublk backend

**How it works.** An out-of-tree DIB block-device plugin (`ublk/amphora_dib_ublk.py`,
inspired by Gerrit 984479) replaces `local_loop` at level0 with a
userspace-served ublk device. ublksrv v1.9 is built from source (not packaged in
EL10; liburing is). DIB then runs directly in the container build, using the
host kernel's `ublk_drv`.

**What ublk does and does not remove.** ublk replaces `losetup` only.
DIB still uses `kpartx`/device-mapper, kernel block-filesystem mounts and
chroot, so the build is **not** capability-free or rootless.

**Permissions needed (per `UBLK.md`).**

* Rootful builder in the initial user namespace (rootless Podman cannot mount
  block filesystems).
* `CAP_SYS_ADMIN`, `CAP_MKNOD`, `CAP_SYS_CHROOT`.
* Host `ublk_drv` and device-mapper loaded; access to `/dev/ublk-control` and
  `/dev/mapper/control`.
* io_uring (`SQE128`, `URING_CMD`) permitted by seccomp and host policy.
* Device-cgroup/LSM rules for dynamically numbered `ublkc*`, `ublkb*` and
  dm partition nodes.
* On an SELinux-enforcing host, a custom `amphora_ublk_t` policy module
  (experimental, not audited), a build-local seccomp profile, and a custom
  crun wrapper adding exact-major/minor device rules (Buildah lacked
  `--device-cgroup-rule`).

**Results.**

* ublksrv and the io_uring probe build; 42 offline tests pass (plugin,
  preflight, policy tools, SELinux option, real DIB graph construction).
* Rootless build fails at preflight, as expected.
* On an administrator-configured native builder, `--target smoke` **passes**
  (attach, MBR, dm partition mapping, ext4 mount, copy, fstrim, detach, qcow2).
* Full build got through CentOS bootstrap, package install and GRUB install,
  then **failed at offline guest SELinux relabeling** (`selinux-fixfiles-restore`):
  guest contexts such as `/etc/my.cnf*` are unknown to the Fedora host policy.
  `CAP_MAC_ADMIN` plus unconfined/`spc_t` labels did not help; even host root
  could not write the unknown context.
* A workaround was added (`AMPHORA_SELINUX=disabled`: disable guest SELinux,
  skip relabel) but a full build/boot with it has **not been run**.
* **No amphora qcow2, boot test or performance number exists** for this
  approach. The failed ~6m44s run is not a build time.

**Pros**

* Near-native speed: no emulation, so expected to be much faster than TCG
  (unmeasured), and low memory overhead.
* No VM appliance, kernel or QEMU to build and maintain.
* Closer to upstream direction if Gerrit 984479 (or similar) merges; smaller
  runtime footprint once the plugin is upstreamed.
* Simple artifact path (qcow2 converted directly from the ublk-backed image).

**Cons / risks**

* Does not meet the "no full permissions" goal: needs rootful builder, three
  capabilities, device access, relaxed seccomp (io_uring), and likely custom
  SELinux policy. This is a significant ask for Konflux/downstream pipelines
  and conflicts with the IPA prototype's already-contentious `SYS_ADMIN`
  requirement.
* Host-dependent: needs `ublk_drv` available/loaded on every builder kernel;
  dynamic device numbers make a portable device allowlist hard; concurrent
  builds can collide on ublk IDs / device numbers.
* Guest SELinux is unresolved: either the amphora ships with SELinux
  **disabled** (no MAC protection, diverges from upstream/permissive
  behavior and the supported amphora-selinux policy), or a labeling helper /
  first-boot relabel must be designed. Neither is implemented or tested.
* Immature stack: ublksrv built from an unpackaged pinned source, a plugin
  based on an untested WIP, 512-byte sector limitation, io_uring policy
  probing cannot prove every needed op.
* Failure cleanup leaves host-visible devices/mappings if something goes wrong.
* Unvalidated end to end; remaining unknowns (boot, agent, performance) are
  unquantified.

**Overall risk: High** for the stated goal. Moderate engineering effort remains
and, more importantly, the permission model is likely to be a blocker in the
target pipeline regardless of engineering.

## Side-by-side

| Criterion | QEMU appliance | Native ublk |
| --- | --- | --- |
| Produces a bootable amphora | Yes (boot-tested) | No (smoke only) |
| Rootless / no extra caps | Yes | No (rootful, SYS_ADMIN/MKNOD/SYS_CHROOT) |
| Host kernel/module dependency | None | `ublk_drv`, dm, io_uring |
| Host policy changes | None | seccomp, SELinux module, device cgroup |
| Guest SELinux | Permissive, upstream-like | Unresolved; disabled workaround untested |
| Build time | ~26 min (TCG) | Unknown, expected faster |
| Runtime memory/disk | High (4 GiB RAM + 32G sparse) | Low |
| DIB changes | None (unmodified) | Out-of-tree plugin, depends on WIP design |
| Extra source builds | Possibly QEMU, sgdisk | ublksrv, sgdisk |
| Portability across CI | High | Low |
| Isolation | Strong (VM) | Weak (host devices/mounts) |
| Maintenance burden | Appliance + runner | Plugin + policy + preflight + runtime wrapper |
| Overall risk | Low-medium | High |

## Recommendation

Adopt the **QEMU appliance** approach as the path forward. It is the only
variant that satisfies the restricted-build goal and has a working, booted
artifact. Its main costs (build time, RAM, QEMU supportability) are
operational and addressable, whereas ublk's blockers (privileged rootful
builder, host kernel/policy coupling, unresolved guest SELinux) are
structural.

Keep ublk as a reference/fallback only if:

* the downstream pipeline can provide a dedicated privileged builder with
  `ublk_drv`; and
* a guest SELinux strategy is agreed (labeling helper, first-boot relabel, or
  explicit sign-off for SELinux disabled); and
* upstream DIB ublk support lands, removing the out-of-tree plugin.

## Open items / follow-ups

* Octavia controller/agent integration test with the QEMU-built image
  (health manager, config drive, amphora agent).
* Confirm the target pipeline (Konflux) allows QEMU/TCG execution, thread
  and memory use, and measure build time within its quotas; try KVM if
  `/dev/kvm` is available.
* Decide QEMU sourcing for RHEL builds (packaged TCG vs. source build) and its
  FIPS/supportability implications.
* Optional: run the ublk `AMPHORA_SELINUX=disabled` full build on a
  configured host to complete the comparison (boot test and timing).
* Upstream DIB patches still pending (tarball element, `DIB_BIND_MOUNTS`,
  dnf-assert, etc.) — the `[DNM]` sources pin to `review.opendev.org` must be
  reverted once merged.
