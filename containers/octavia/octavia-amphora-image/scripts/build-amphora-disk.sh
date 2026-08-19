#!/bin/bash
#
# Assemble a bootable UEFI qcow2 disk image from a root filesystem directory
# using ONLY userspace tools -- no privileged host operations.
#
# What makes this work without root/loopback:
#   * mke2fs -d <dir>     populates an ext4 image directly from a directory,
#                         so we never have to mount anything.
#   * mkfs.vfat + mtools  create and populate the FAT32 EFI System Partition
#                         (mmd/mcopy) again without mounting.
#   * sgdisk              writes a GPT partition table straight onto a regular
#                         file (it does not require a block device).
#   * grub2-mkstandalone  builds a self-contained UEFI bootloader binary with
#                         its config embedded, so there is no grub2-install to
#                         a block device.
#   * dd conv=notrunc     places each partition image at its byte offset in the
#                         whole-disk file.
#   * qemu-img convert    wraps the raw image as qcow2 (pure userspace, no KVM).
#
# The result boots via UEFI (OVMF) using the removable-media fallback path
# /EFI/BOOT/BOOTX64.EFI, which firmware tries when there is no NVRAM boot entry
# -- exactly the situation for a freshly created disk image.
#
# Layout: GPT, [1 MiB gap][ESP FAT32][root ext4][GPT backup]
set -euo pipefail

ROOTDIR="${1:?usage: build-amphora-disk.sh <rootfs-dir> <output.qcow2>}"
OUTPUT="${2:?}"

# Deterministic identifiers -> reproducible images. Override to pin per build.
ROOT_FS_UUID="${ROOT_FS_UUID:-a1b2c3d4-0001-0001-0001-000000000001}"
ROOT_PARTUUID="${ROOT_PARTUUID:-a1b2c3d4-0002-0002-0002-000000000002}"
ESP_PARTUUID="${ESP_PARTUUID:-a1b2c3d4-0003-0003-0003-000000000003}"
ESP_SIZE_MIB="${ESP_SIZE_MIB:-200}"
# Slack added on top of the used rootfs size (percent) and floor size (MiB).
ROOT_SLACK_PCT="${ROOT_SLACK_PCT:-35}"
ROOT_MIN_MIB="${ROOT_MIN_MIB:-1536}"
# Serial console + predictable NIC naming are appropriate for a cloud guest.
KERNEL_CMDLINE="${KERNEL_CMDLINE:-ro console=tty0 console=ttyS0,115200n8 net.ifnames=0 biosdevname=0}"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

# --- 0. Discover the installed kernel and write the on-disk grub.cfg --------
# grub.cfg is generated into the rootfs *before* the ext4 image is created, so
# it is captured by mke2fs -d along with the kernel and initramfs.
KVER="$(ls "$ROOTDIR/lib/modules" 2>/dev/null | head -1 || true)"
[ -n "$KVER" ] || { echo "ERROR: no kernel under $ROOTDIR/lib/modules" >&2; exit 1; }
[ -f "$ROOTDIR/boot/vmlinuz-$KVER" ] || { echo "ERROR: missing /boot/vmlinuz-$KVER" >&2; exit 1; }
[ -f "$ROOTDIR/boot/initramfs-$KVER.img" ] || { echo "ERROR: missing /boot/initramfs-$KVER.img" >&2; exit 1; }

mkdir -p "$ROOTDIR/boot/grub2"
cat > "$ROOTDIR/boot/grub2/grub.cfg" <<EOF
set timeout=1
set default=0
insmod all_video
insmod gzio
insmod part_gpt
insmod ext2

menuentry 'Octavia Amphora' {
    search --no-floppy --fs-uuid --set=root ${ROOT_FS_UUID}
    linux /boot/vmlinuz-${KVER} root=UUID=${ROOT_FS_UUID} ${KERNEL_CMDLINE}
    initrd /boot/initramfs-${KVER}.img
}
EOF

# --- 1. Root ext4 filesystem, populated from the directory (no mount) -------
used_kib="$(du -sk --apparent-size "$ROOTDIR" | cut -f1)"
root_mib=$(( used_kib * (100 + ROOT_SLACK_PCT) / 100 / 1024 + 1 ))
[ "$root_mib" -ge "$ROOT_MIN_MIB" ] || root_mib="$ROOT_MIN_MIB"

root_img="$workdir/root.img"
echo ">> building root ext4 (${root_mib} MiB) from $ROOTDIR"
mke2fs -q -t ext4 -L img-rootfs -U "$ROOT_FS_UUID" \
    -E lazy_itable_init=0,lazy_journal_init=0 \
    -d "$ROOTDIR" "$root_img" "${root_mib}M"

# --- 2. EFI System Partition (FAT32), populated with mtools (no mount) ------
echo ">> building ESP (${ESP_SIZE_MIB} MiB) with standalone GRUB"
esp_img="$workdir/esp.img"
truncate -s "${ESP_SIZE_MIB}M" "$esp_img"
mkfs.vfat -F 32 -n EFI "$esp_img" >/dev/null

# Standalone GRUB with a fully self-contained boot config embedded in its
# memdisk -- no grub2-install and no dependency on reading a config off the
# disk. The config is placed at BOTH candidate memdisk paths (grub vs grub2)
# because which one grub auto-loads depends on how grub2-mkstandalone was built;
# if the embedded config isn't at the expected prefix, grub silently drops to a
# rescue prompt instead of booting.
cat > "$workdir/embedded.cfg" <<EOF
set timeout=1
set default=0
insmod all_video
insmod gzio
insmod part_gpt
insmod ext2
search --no-floppy --fs-uuid --set=root ${ROOT_FS_UUID}
menuentry 'Octavia Amphora' {
    linux /boot/vmlinuz-${KVER} root=UUID=${ROOT_FS_UUID} ${KERNEL_CMDLINE}
    initrd /boot/initramfs-${KVER}.img
}
EOF

grub2-mkstandalone -O x86_64-efi \
    --modules="part_gpt part_msdos fat ext2 normal linux echo all_video gzio search search_fs_uuid configfile serial terminal gfxterm" \
    -o "$workdir/BOOTX64.EFI" \
    "boot/grub2/grub.cfg=$workdir/embedded.cfg" \
    "boot/grub/grub.cfg=$workdir/embedded.cfg"

mmd -i "$esp_img" ::/EFI ::/EFI/BOOT
mcopy -i "$esp_img" "$workdir/BOOTX64.EFI" ::/EFI/BOOT/BOOTX64.EFI

# --- 3. Assemble the whole-disk GPT image ----------------------------------
esp_start_mib=1
root_start_mib=$(( esp_start_mib + ESP_SIZE_MIB ))
# +1 MiB tail leaves room for the backup GPT.
total_mib=$(( root_start_mib + root_mib + 1 ))

disk="$workdir/disk.raw"
echo ">> assembling GPT disk (${total_mib} MiB)"
truncate -s "${total_mib}M" "$disk"

sgdisk -Z "$disk" >/dev/null
sgdisk \
    -n 1:${esp_start_mib}MiB:+${ESP_SIZE_MIB}MiB -t 1:ef00 -c 1:EFI  -u 1:"$ESP_PARTUUID" \
    -n 2:${root_start_mib}MiB:0                  -t 2:8304 -c 2:root -u 2:"$ROOT_PARTUUID" \
    "$disk" >/dev/null
sgdisk -p "$disk"

# Place each filesystem image at its partition offset.
dd if="$esp_img"  of="$disk" bs=1M seek=${esp_start_mib}  conv=notrunc,sparse status=none
dd if="$root_img" of="$disk" bs=1M seek=${root_start_mib} conv=notrunc,sparse status=none

# --- 4. Wrap as qcow2 (compressed) -----------------------------------------
echo ">> converting to qcow2: $OUTPUT"
mkdir -p "$(dirname "$OUTPUT")"
qemu-img convert -f raw -O qcow2 -c "$disk" "$OUTPUT"

echo "Amphora image built: $OUTPUT (kernel ${KVER})"
