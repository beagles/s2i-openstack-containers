CentOS local-image adapter
=========================

This is an intentionally scoped override of DIB's ``centos-minimal`` element.
Octavia's diskimage-create.sh maps both ``-i centos`` and ``-i centos-minimal``
to that element. Its normal RPM-bootstrap implementation does not consume
``DIB_LOCAL_IMAGE``.

Only the source-image variant adds this directory's parent to
``DIB_LOCAL_ELEMENTS_PATH``. Delegating to the upstream ``centos`` element
selects its local qcow2 extraction path, while retaining Octavia's CentOS
SELinux, makecache and networking choices. ``centos`` provides the operating
system; do not also provide it here. The adapter needs no additional patches
to Octavia or DIB beyond the repository's existing DIB pin.

``dracut-regenerate`` ensures the initramfs follows the final disk layout.
