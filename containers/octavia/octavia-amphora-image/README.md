# octavia-amphora-image (prototype)

Builds the Octavia **amphora** VM image (`amphora.qcow2`) inside a container,
**without any privileged host operation**, so it can run in Konflux's rootless,
hermetic build sandbox. The qcow2 is shipped inside a thin *carrier* image; a
pod extracts it and uploads it to Glance (the same delivery model as the
downstream `rhoso-images` amphora container).

This is a **proof-of-concept** for the approach described in OSPRH-26902. It
demonstrates that the disk-assembly step can be done unprivileged; several
production concerns are still open (see *Status* below).

## Why the usual approach doesn't fit Konflux

The amphora is a full bootable disk image, traditionally built with
`diskimage-builder`, which needs privileged host operations: loopback devices
(`losetup`), privileged `mount`, `kpartx`, `chroot` with device nodes, and
`grub2-install` to a block device. Konflux builds are rootless and hermetic, so
none of that is available. (The ironic-python-agent alternative in OSPRH-28719
doesn't help here: IPA is a *ramdisk* — `cpio | gzip` of a rootfs, no partition
table or bootloader — while the amphora is a real disk.)

## How it works

The build splits into two halves (see the `Containerfile`):

1. **`rootfs` stage** — `scripts/provision-rootfs.sh` installs a minimal
   bootable guest plus the amphora runtime (haproxy, keepalived, cloud-init)
   into a directory via `dnf --installroot`, pip-installs the amphora agent from
   the pinned octavia source, enables services offline (`systemctl --root`), and
   generates a generic virtio initramfs. This is ordinary content work.

2. **`assemble` stage** — `scripts/build-amphora-disk.sh` turns that directory
   into a bootable UEFI qcow2 using only userspace tools:

   | Step | Tool | Privileged? |
   |------|------|-------------|
   | ext4 root fs from a directory | `mke2fs -d` | no |
   | FAT32 ESP + files | `mkfs.vfat` + `mtools` (`mmd`/`mcopy`) | no |
   | GPT partition table on a file | `sgdisk` | no |
   | self-contained UEFI bootloader | `grub2-mkstandalone` | no |
   | place partitions at offsets | `dd conv=notrunc` | no |
   | raw → qcow2 | `qemu-img convert` | no (no KVM) |

   No loopback, no privileged mount, no `kpartx`, no `/dev/kvm`.

The image boots via UEFI (OVMF) using the removable-media fallback path
`/EFI/BOOT/BOOTX64.EFI`, which firmware uses when there is no NVRAM boot entry —
exactly the case for a fresh disk image.

## Build

Follows the standard repo flow (build context is `containers/octavia/`, and
`build.sh` clones the pinned octavia source into `containers/octavia/src/`):

```console
STREAM=master ./build.sh build octavia/octavia-amphora-image
```

## Test the resulting qcow2 boots (needs a host with KVM/qemu)

Building is unprivileged; *booting to verify* is a separate, developer-side step:

```console
# extract the qcow2 from the carrier image
id=$(podman create openstack-octavia-amphora-image:latest)
podman cp "$id":/usr/share/octavia-amphora-images/amphora.qcow2 .
podman rm "$id"

# boot under UEFI (OVMF) with a serial console
qemu-system-x86_64 -M q35 -m 2048 -accel kvm \
  -bios /usr/share/OVMF/OVMF_CODE.fd \
  -drive file=amphora.qcow2,format=qcow2,if=virtio \
  -nographic
```

## Status / open items for a production build

- [ ] **Boot validation** on OVMF + Nova/KVM (this PoC assembles a plausibly
      bootable image; it has not been booted in CI here).
- [ ] **Content parity**: replace the hand-picked package list and inlined unit
      file in `provision-rootfs.sh` with the upstream octavia diskimage-builder
      element set (`containers/octavia/src/octavia/elements/*`) so amphora
      content tracks upstream.
- [ ] **SELinux**: `mke2fs -d` cannot write security xattrs. The PoC boots
      permissive with `/.autorelabel`. Production should relabel offline
      (`setfiles` against the fs image) and run enforcing.
- [ ] **aarch64**: add a parallel path (`grub2-efi-aa64`, `BOOTAA64.EFI`);
      octavia builds amphorae per-arch.
- [ ] **Secure Boot**: currently uses standalone GRUB (SB off). If required, use
      `shim` + signed `grubx64.efi` instead.
- [ ] **Reproducibility**: honor `SOURCE_DATE_EPOCH` end-to-end and pin the
      generated timestamps/UUIDs (UUIDs are already fixed and overridable).
- [ ] **Image sizing / growpart**: verify cloud-init grows the root partition on
      first boot as expected.
- [ ] **Delivery contract**: confirm the path/name/tag the octavia-operator
      expects for the carried qcow2.
