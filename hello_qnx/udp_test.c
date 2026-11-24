#include <stdio.h>
#include <string.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>

int main(void) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) { perror("socket"); return 1; }

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(9999);
    inet_pton(AF_INET, "10.0.2.2", &addr.sin_addr);

    const char *msg = "PING_FROM_QNX_8_0_QEMU";
    ssize_t sent = sendto(fd, msg, strlen(msg), 0, (struct sockaddr *)&addr, sizeof(addr));
    if (sent < 0) { perror("sendto"); close(fd); return 1; }

    printf("[OK] Sent %zd bytes via UDP to 10.0.2.2:9999\n", sent);
    close(fd);
    return 0;
}
