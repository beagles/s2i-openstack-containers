#!/bin/bash
#
# Provision the Octavia amphora root filesystem into a directory.
#
# This is the "content" half of the build. Everything here is ordinary package
# installation into a tree (dnf --installroot) plus a pip install of the amphora
# agent -- exactly the kind of work a Containerfile layer already does. No disk
# image is created here; that happens (unprivileged) in build-amphora-disk.sh.
#
# NOTE: this is a prototype. A production build should replace the hand-picked
# package list and unit files below with the upstream octavia diskimage-builder
# element set (containers/octavia/src/octavia/elements/*) so the amphora content
# stays in lockstep with upstream. The list here is the minimum needed to prove
# the unprivileged assembly path produces a plausibly-bootable guest.
set -euo pipefail

ROOTDIR="${1:?usage: provision-rootfs.sh <rootdir> <octavia-src> <constraints>}"
OCTAVIA_SRC="${2:?}"
CONSTRAINTS="${3:?}"

# Must match the UUID baked into fstab and grub by build-amphora-disk.sh.
ROOT_FS_UUID="${ROOT_FS_UUID:-a1b2c3d4-0001-0001-0001-000000000001}"

mkdir -p "$ROOTDIR"

# --- Base bootable guest + amphora runtime packages ------------------------
dnf -y --installroot="$ROOTDIR" --releasever=10 \
    --setopt=install_weak_deps=False --setopt=tsflags= \
    install \
      kernel dracut \
      systemd systemd-udev dbus \
      NetworkManager \
      glibc-langpack-en \
      cloud-init \
      haproxy keepalived \
      iproute iputils nftables \
      openssh-server sudo \
      chrony \
      python3 python3-pip \
      ca-certificates

dnf -y --installroot="$ROOTDIR" clean all
rm -rf "$ROOTDIR"/var/cache/dnf "$ROOTDIR"/var/lib/dnf/history* 2>/dev/null || true

# --- Amphora agent (octavia) installed from source into the rootfs ---------
# Provides the /usr/bin/amphora-agent console script (octavia.cmd.agent:main).
pip3 install --root="$ROOTDIR" --prefix=/usr --no-cache-dir \
    -c "$CONSTRAINTS" "$OCTAVIA_SRC"

# amphora-agent systemd unit. Upstream ships this via a DIB element; inlined
# here to keep the prototype self-contained.
install -D -m 0644 /dev/stdin \
    "$ROOTDIR/usr/lib/systemd/system/amphora-agent.service" <<'UNIT'
[Unit]
Description=Octavia Amphora Agent
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/amphora-agent --config-file /etc/octavia/amphora-agent.conf
Restart=always
User=root

[Install]
WantedBy=multi-user.target
UNIT

mkdir -p "$ROOTDIR/etc/octavia"

# --- Enable services offline (operates on the tree; no running systemd) -----
systemctl --root="$ROOTDIR" enable \
    NetworkManager \
    cloud-init-local cloud-init cloud-config cloud-final \
    sshd chronyd haproxy amphora-agent || true
# keepalived is managed on-demand by the agent for active/standby; not enabled.

# --- Boot / identity config ------------------------------------------------
cat > "$ROOTDIR/etc/fstab" <<EOF
UUID=${ROOT_FS_UUID} / ext4 defaults 0 1
EOF

# SELinux: mke2fs -d cannot write security xattrs, so labels are applied on
# first boot. Prototype starts permissive; production should relabel offline
# (setfiles against the ext4 image) or autorelabel then flip to enforcing.
if [ -f "$ROOTDIR/etc/selinux/config" ]; then
  sed -i 's/^SELINUX=.*/SELINUX=permissive/' "$ROOTDIR/etc/selinux/config"
fi
: > "$ROOTDIR/.autorelabel"

# Serial console for cloud/KVM debugging.
mkdir -p "$ROOTDIR/etc/systemd/system/getty.target.wants"
ln -sf /usr/lib/systemd/system/serial-getty@.service \
   "$ROOTDIR/etc/systemd/system/getty.target.wants/serial-getty@ttyS0.service" 2>/dev/null || true

# --- Generic initramfs with virtio drivers (for boot under Nova/KVM) -------
# Generated with `dracut --sysroot`, which collects files from the target tree
# WITHOUT chroot or bind-mounting /proc,/sys,/dev. That matters: rootless
# builders (buildah under Konflux) deny those mounts, so a chroot-based dracut
# would fail. This runs the build container's dracut against the rootfs.
KVER="$(ls "$ROOTDIR/lib/modules" 2>/dev/null | head -1 || true)"
if [ -z "$KVER" ]; then
  echo "ERROR: no kernel modules found under $ROOTDIR/lib/modules" >&2
  exit 1
fi

# Ensure the kernel image is at /boot/vmlinuz-<kver>. On el10, installing the
# 'kernel' package into an --installroot leaves vmlinuz under
# /lib/modules/<kver>/ because the kernel-install/BLS scriptlet does not run in
# that context. build-amphora-disk.sh expects it in /boot.
mkdir -p "$ROOTDIR/boot"
if [ ! -f "$ROOTDIR/boot/vmlinuz-${KVER}" ]; then
  if [ -f "$ROOTDIR/lib/modules/${KVER}/vmlinuz" ]; then
    cp -a "$ROOTDIR/lib/modules/${KVER}/vmlinuz" "$ROOTDIR/boot/vmlinuz-${KVER}"
  else
    echo "ERROR: cannot locate kernel image for ${KVER}" >&2
    exit 1
  fi
fi

dracut --no-hostonly --force \
    --sysroot "$ROOTDIR" \
    --kmoddir "$ROOTDIR/lib/modules/${KVER}" \
    --kver "${KVER}" \
    --add-drivers "virtio virtio_pci virtio_blk virtio_net virtio_scsi ext4" \
    "$ROOTDIR/boot/initramfs-${KVER}.img"

echo "Amphora rootfs provisioned (kernel ${KVER})."
