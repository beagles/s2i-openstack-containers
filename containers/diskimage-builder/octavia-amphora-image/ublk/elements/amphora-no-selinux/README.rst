amphora-no-selinux
=================

An explicit guest-only SELinux opt-out for the native amphora comparison.

Depends on DIB's ``disable-selinux``, ensures ``SELINUX=disabled`` in the guest
configuration, and appends ``selinux=0`` to the bootloader command line. Existing
``selinux=...`` arguments are removed so a conflicting value cannot win.

Provides ``selinux-fixfiles-restore`` to satisfy the ``rpm-distro`` dependency
without running its offline relabel hook. No policy files are deleted, and RPM
dependencies may still install SELinux libraries/tools. Any ``/.autorelabel``
marker is removed: this is disabling SELinux, not deferring labeling to boot.

Select via ``AMPHORA_SELINUX=disabled`` in ``Containerfile.ublk``. The default
remains ``permissive``; this element is not used by the QEMU baseline. It does
not change host SELinux, container permissions, or the patched DIB source pin.
