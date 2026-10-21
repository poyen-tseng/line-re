/* Experimental LD_PRELOAD shim. No application data, TLS or LINE messages.
 * Compile: cc -shared -fPIC -Wall -Wextra -Werror -o /tmp/line_keepalive.so
 *          automation/tools/line_keepalive.c -ldl
 * Inject into the actual Wine processes (Flatpak strips host LD_PRELOAD).
 * Verify [line-keepalive] log AND timer:(keepalive,...) on the core sockets.
 * Ten-second probes may only reach the tun0 TCP stack, not the upstream
 * proxy/application. This is an experiment, not a validated repair.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <netinet/tcp.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>

static int is_core(const struct sockaddr *addr, socklen_t len)
{
    uint32_t ip;
    uint16_t port;
    if (!addr) return 0;
    if (addr->sa_family == AF_INET && len >= sizeof(struct sockaddr_in)) {
        const struct sockaddr_in *a = (const struct sockaddr_in *)addr;
        ip = ntohl(a->sin_addr.s_addr);
        port = ntohs(a->sin_port);
    } else if (addr->sa_family == AF_INET6 && len >= sizeof(struct sockaddr_in6)) {
        const struct sockaddr_in6 *a = (const struct sockaddr_in6 *)addr;
        if (!IN6_IS_ADDR_V4MAPPED(&a->sin6_addr)) return 0;
        memcpy(&ip, &a->sin6_addr.s6_addr[12], sizeof(ip));
        ip = ntohl(ip);
        port = ntohs(a->sin6_port);
    } else return 0;
    return port == 443 && ((ip >> 16) == 0x935c || (ip >> 16) == 0x2bdf);
}

int connect(int fd, const struct sockaddr *addr, socklen_t len)
{
    int (*next_connect)(int, const struct sockaddr *, socklen_t) =
        dlsym(RTLD_NEXT, "connect");
    if (!next_connect) { errno = ENOSYS; return -1; }
    int saved_errno = errno;
    int type = 0;
    socklen_t type_len = sizeof(type);
    if (is_core(addr, len) && getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &type_len) == 0
            && type == SOCK_STREAM) {
        int on = 1, idle = 10, interval = 5, count = 3;
        int failed = setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &on, sizeof(on));
        failed |= setsockopt(fd, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof(idle));
        failed |= setsockopt(fd, IPPROTO_TCP, TCP_KEEPINTVL, &interval, sizeof(interval));
        failed |= setsockopt(fd, IPPROTO_TCP, TCP_KEEPCNT, &count, sizeof(count));
        fprintf(stderr, "[line-keepalive] fd=%d keepalive 10/5/3 %s\n",
                fd, failed ? "FAILED" : "configured");
    }
    errno = saved_errno;
    return next_connect(fd, addr, len);
}
