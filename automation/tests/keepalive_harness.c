/* Test the real shim with a fake next connect: no remote connect syscall. */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <assert.h>
#include <dlfcn.h>
#include <errno.h>
#include <netinet/tcp.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

static int fake_connect(int fd, const struct sockaddr *addr, socklen_t len)
{
    (void)fd; (void)addr; (void)len;
    errno = EINPROGRESS;
    return -1;
}
static void *lookup(void *handle, const char *name)
{
    (void)handle;
    assert(strcmp(name, "connect") == 0);
    return fake_connect;
}
#define connect intercepted_connect
#define dlsym lookup
#include "../tools/line_keepalive.c"
#undef connect
#undef dlsym

static int option(int fd, int level, int name)
{
    int value;
    socklen_t len = sizeof(value);
    assert(getsockopt(fd, level, name, &value, &len) == 0);
    return value;
}

int main(void)
{
    const char *hosts[] = {"147.92.1.2", "43.223.2.3", "127.0.0.1"};
    for (int i = 0; i < 3; ++i) {
        int fd = socket(AF_INET, SOCK_STREAM, 0);
        assert(fd >= 0);
        struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons(443)};
        assert(inet_pton(AF_INET, hosts[i], &addr.sin_addr) == 1);
        assert(intercepted_connect(fd, (struct sockaddr *)&addr, sizeof(addr)) == -1);
        assert(errno == EINPROGRESS);
        assert(option(fd, SOL_SOCKET, SO_KEEPALIVE) == (i < 2));
        if (i < 2) {
            assert(option(fd, IPPROTO_TCP, TCP_KEEPIDLE) == 10);
            assert(option(fd, IPPROTO_TCP, TCP_KEEPINTVL) == 5);
            assert(option(fd, IPPROTO_TCP, TCP_KEEPCNT) == 3);
        }
        close(fd);
    }
    int fd = socket(AF_INET6, SOCK_STREAM, 0);
    assert(fd >= 0);
    struct sockaddr_in6 addr6 = {.sin6_family = AF_INET6, .sin6_port = htons(443)};
    assert(inet_pton(AF_INET6, "::ffff:147.92.1.2", &addr6.sin6_addr) == 1);
    assert(intercepted_connect(fd, (struct sockaddr *)&addr6, sizeof(addr6)) == -1);
    assert(errno == EINPROGRESS);
    assert(option(fd, SOL_SOCKET, SO_KEEPALIVE) == 1);
    close(fd);
    puts("keepalive shim tests passed (connect stubbed; no network)");
    return 0;
}
