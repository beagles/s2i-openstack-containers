/* Probe the container's io_uring policy without creating a block device. */
#include <liburing.h>
#include <stdio.h>
#include <string.h>

int main(void)
{
    struct io_uring ring;
    int ret = io_uring_queue_init(8, &ring, IORING_SETUP_SQE128);

    if (ret < 0) {
        fprintf(stderr, "io_uring setup: %s (check seccomp and host io_uring policy)\n",
                strerror(-ret));
        return 1;
    }
    io_uring_queue_exit(&ring);
    return 0;
}
