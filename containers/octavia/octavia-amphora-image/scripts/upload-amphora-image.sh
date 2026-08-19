#!/bin/bash
#
# Optional helper: upload the carried amphora qcow2 into Glance.
#
# In RHOSO the octavia-operator normally drives this (it extracts the file from
# the carrier image and creates the Glance image with the right tag). This
# script exists so the carrier image is usable stand-alone for testing: exec it
# in a pod/environment that has OpenStack credentials (clouds.yaml or OS_* env)
# and the openstack client available.
set -euo pipefail

IMAGE_FILE="${IMAGE_FILE:-/usr/share/octavia-amphora-images/amphora.qcow2}"
IMAGE_NAME="${IMAGE_NAME:-amphora-x64-haproxy}"
IMAGE_TAG="${AMPHORA_IMAGE_TAG:-amphora}"

if [ ! -f "$IMAGE_FILE" ]; then
  echo "ERROR: amphora image not found at $IMAGE_FILE" >&2
  exit 1
fi

# Verify integrity against the checksum shipped alongside the image.
if [ -f "${IMAGE_FILE}.sha256" ]; then
  expected="$(cat "${IMAGE_FILE}.sha256")"
  actual="$(sha256sum "$IMAGE_FILE" | awk '{print $1}')"
  if [ "$expected" != "$actual" ]; then
    echo "ERROR: checksum mismatch for $IMAGE_FILE" >&2
    echo "  expected: $expected" >&2
    echo "  actual:   $actual" >&2
    exit 1
  fi
fi

if ! command -v openstack >/dev/null 2>&1; then
  cat >&2 <<EOF
The 'openstack' client is not available in this image.
This carrier image only holds the amphora qcow2 at:
  $IMAGE_FILE
Copy it out and upload it with your tooling, e.g.:
  openstack image create --disk-format qcow2 --container-format bare \\
    --tag $IMAGE_TAG --file $IMAGE_FILE $IMAGE_NAME
EOF
  exit 0
fi

echo "Uploading $IMAGE_FILE as Glance image '$IMAGE_NAME' (tag: $IMAGE_TAG)"
exec openstack image create \
  --disk-format qcow2 --container-format bare \
  --tag "$IMAGE_TAG" --file "$IMAGE_FILE" "$IMAGE_NAME"
