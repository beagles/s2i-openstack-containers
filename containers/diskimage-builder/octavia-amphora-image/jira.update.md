**Spike update: building the Octavia amphora image in a container build (diskimage-builder)**

Two approaches were prototyped on the branch (`Containerfile` for QEMU, `Containerfile.ublk` for ublk). Both use the container-patched diskimage-builder pin, `centos-minimal` (CentOS Stream 10) and the pinned Octavia `stable/2026.2` elements.

*1. QEMU software-emulated appliance (works)*
- DIB runs unmodified inside a TCG-emulated VM started during the container build. Loop, device-mapper, mount and chroot happen inside the VM.
- Needs no extra capabilities, host devices, KVM or seccomp/SELinux changes. Full build tested under rootless Podman with SELinux enforcing.
- Produced a ~517 MiB qcow2. Checksums and `qemu-img check` pass. The image booted to a login prompt and completed cloud-init.
- Build time is ~26 min locally under TCG. Memory use is high (4 GiB guest RAM plus a 32G sparse work disk).
- Risks: build time and CI quotas, and QEMU sourcing. If the RHEL `qemu-kvm` package has no TCG, QEMU has to be built from source. Octavia controller/agent integration is not yet tested.

*2. Native ublk (not working)*
- A DIB out-of-tree plugin replaces the loop device with ublk. ublksrv 1.9 is built from source because it is not packaged in EL10.
- ublk only replaces `losetup`. DIB still needs device-mapper, filesystem mounts and chroot. This needs a rootful builder with SYS_ADMIN, MKNOD and SYS_CHROOT, host `ublk_drv`, and io_uring allowed by seccomp. On SELinux-enforcing hosts it also needs a custom SELinux policy and device-cgroup rules. It does not meet the "no full permissions" goal.
- The smoke build (attach, partition, mount, qcow2) passes on a configured host. The full build fails at offline guest SELinux relabeling. CentOS guest contexts are unknown to the host policy, and CAP_MAC_ADMIN and unconfined labels did not help.
- An `AMPHORA_SELINUX=disabled` workaround was added but is untested. It would ship the amphora without SELinux. No amphora qcow2, boot test or timing exists for this approach.
- Risks: privileged builders, host kernel and policy coupling, unresolved guest SELinux, immature tooling, and the dependency on unmerged DIB ublk work (Gerrit 984479).

*Comparison*

| | QEMU | ublk |
| --- | --- | --- |
| Bootable amphora | Yes | No |
| Rootless / no extra caps | Yes | No |
| Host dependencies | None | `ublk_drv`, dm, io_uring, SELinux policy |
| Guest SELinux | Permissive (upstream-like) | Unresolved |
| Build time | ~26 min (TCG) | Unknown, likely faster |
| Overall risk | Low-medium | High |

*Recommendation:* use the QEMU appliance approach. ublk is only worth revisiting if the pipeline provides a dedicated privileged builder, a guest SELinux strategy is agreed, and upstream DIB ublk support merges.

*Follow-ups*
- Octavia controller/agent integration test with the QEMU-built image.
- Confirm Konflux allows QEMU/TCG and measure build time within its quotas.
- Decide how to source QEMU for RHEL builds.
- Revert the `[DNM]` DIB source pin once the upstream patches merge.

Full write-up: `containers/diskimage-builder/octavia-amphora-image/AMPHORA-BUILD-APPROACHES.md`
