/**
 * darwin-perf: System-wide network I/O counters via sysctl.
 *
 * Copyright 2026 Adam Mikulis. MIT License.
 */

#include "_native.h"
#include <net/if.h>
#include <net/if_dl.h>
#include <net/route.h>
#include <net/if_var.h>

/* ------------------------------------------------------------------ */
/* Python API: net_io_counters                                         */
/* ------------------------------------------------------------------ */

const char net_io_counters_doc[] =
"net_io_counters() -> dict\n\n"
"Return system-wide network I/O counters via sysctl NET_RT_IFLIST2.\n"
"Same data source as netstat/Activity Monitor. No sudo, no psutil.\n\n"
"Returns dict with keys:\n"
"    - 'bytes_sent': int -- total bytes sent across all interfaces\n"
"    - 'bytes_recv': int -- total bytes received\n"
"    - 'packets_sent': int -- total packets sent\n"
"    - 'packets_recv': int -- total packets received\n"
"    - 'errin': int -- total input errors\n"
"    - 'errout': int -- total output errors\n"
"    - 'dropin': int -- total input drops\n"
"    - 'dropout': int -- total output drops\n";

PyObject* py_net_io_counters(PyObject* self, PyObject* args) {
    (void)self; (void)args;

    int mib[6] = {CTL_NET, PF_ROUTE, 0, 0, NET_RT_IFLIST2, 0};
    size_t len = 0;

    if (sysctl(mib, 6, NULL, &len, NULL, 0) < 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }

    char *buf = malloc(len);
    if (!buf) {
        return PyErr_NoMemory();
    }

    if (sysctl(mib, 6, buf, &len, NULL, 0) < 0) {
        free(buf);
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }

    uint64_t bytes_sent = 0, bytes_recv = 0;
    uint64_t packets_sent = 0, packets_recv = 0;
    uint64_t errin = 0, errout = 0;
    uint64_t dropin = 0, dropout = 0;

    char *end = buf + len;
    char *ptr = buf;

    while (ptr < end) {
        struct if_msghdr *ifm = (struct if_msghdr *)ptr;
        if (ifm->ifm_msglen == 0) break;  /* avoid infinite loop on corrupt data */

        if (ifm->ifm_type == RTM_IFINFO2) {
            struct if_msghdr2 *ifm2 = (struct if_msghdr2 *)ptr;
            bytes_sent   += ifm2->ifm_data.ifi_obytes;
            bytes_recv   += ifm2->ifm_data.ifi_ibytes;
            packets_sent += ifm2->ifm_data.ifi_opackets;
            packets_recv += ifm2->ifm_data.ifi_ipackets;
            errin        += ifm2->ifm_data.ifi_ierrors;
            errout       += ifm2->ifm_data.ifi_oerrors;
            dropin       += ifm2->ifm_data.ifi_iqdrops;
            dropout      += ifm2->ifm_data.ifi_oerrors;  /* macOS has no separate odrops */
        }

        ptr += ifm->ifm_msglen;
    }

    free(buf);

    PyObject *result = PyDict_New();
    if (!result) return NULL;

    PyObject *v;
    v = PyLong_FromUnsignedLongLong(bytes_sent); PyDict_SetItemString(result, "bytes_sent", v); Py_DECREF(v);
    v = PyLong_FromUnsignedLongLong(bytes_recv); PyDict_SetItemString(result, "bytes_recv", v); Py_DECREF(v);
    v = PyLong_FromUnsignedLongLong(packets_sent); PyDict_SetItemString(result, "packets_sent", v); Py_DECREF(v);
    v = PyLong_FromUnsignedLongLong(packets_recv); PyDict_SetItemString(result, "packets_recv", v); Py_DECREF(v);
    v = PyLong_FromUnsignedLongLong(errin); PyDict_SetItemString(result, "errin", v); Py_DECREF(v);
    v = PyLong_FromUnsignedLongLong(errout); PyDict_SetItemString(result, "errout", v); Py_DECREF(v);
    v = PyLong_FromUnsignedLongLong(dropin); PyDict_SetItemString(result, "dropin", v); Py_DECREF(v);
    v = PyLong_FromUnsignedLongLong(dropout); PyDict_SetItemString(result, "dropout", v); Py_DECREF(v);

    return result;
}


/* ------------------------------------------------------------------ */
/* Python API: net_io_per_iface                                        */
/* ------------------------------------------------------------------ */

const char net_io_per_iface_doc[] =
"net_io_per_iface() -> dict[str, dict]\n\n"
"Return network I/O counters per interface via sysctl NET_RT_IFLIST2.\n"
"Keys are interface names (e.g., 'en0', 'lo0', 'utun0').\n\n"
"Each value dict has the same keys as net_io_counters():\n"
"    bytes_sent, bytes_recv, packets_sent, packets_recv,\n"
"    errin, errout, dropin, dropout\n";

PyObject* py_net_io_per_iface(PyObject* self, PyObject* args) {
    (void)self; (void)args;

    int mib[6] = {CTL_NET, PF_ROUTE, 0, 0, NET_RT_IFLIST2, 0};
    size_t len = 0;

    if (sysctl(mib, 6, NULL, &len, NULL, 0) < 0) {
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }

    char *buf = malloc(len);
    if (!buf) return PyErr_NoMemory();

    if (sysctl(mib, 6, buf, &len, NULL, 0) < 0) {
        free(buf);
        PyErr_SetFromErrno(PyExc_OSError);
        return NULL;
    }

    PyObject *result = PyDict_New();
    if (!result) { free(buf); return NULL; }

    char *end = buf + len;
    char *ptr = buf;

    while (ptr < end) {
        struct if_msghdr *ifm = (struct if_msghdr *)ptr;
        if (ifm->ifm_msglen == 0) break;  /* avoid infinite loop on corrupt data */

        if (ifm->ifm_type == RTM_IFINFO2) {
            struct if_msghdr2 *ifm2 = (struct if_msghdr2 *)ptr;

            /* Extract interface name from sockaddr_dl following the header */
            struct sockaddr_dl *sdl = (struct sockaddr_dl *)(ifm2 + 1);
            char ifname[IF_NAMESIZE] = {0};
            if (sdl->sdl_nlen > 0 && sdl->sdl_nlen < IF_NAMESIZE) {
                memcpy(ifname, sdl->sdl_data, sdl->sdl_nlen);
                ifname[sdl->sdl_nlen] = '\0';
            } else {
                /* Fallback: use if_indextoname */
                if_indextoname(ifm2->ifm_index, ifname);
            }

            if (ifname[0] == '\0') {
                ptr += ifm->ifm_msglen;
                continue;
            }

            PyObject *d = Py_BuildValue(
                "{s:K, s:K, s:K, s:K, s:K, s:K, s:K, s:K}",
                "bytes_sent",   (unsigned long long)ifm2->ifm_data.ifi_obytes,
                "bytes_recv",   (unsigned long long)ifm2->ifm_data.ifi_ibytes,
                "packets_sent", (unsigned long long)ifm2->ifm_data.ifi_opackets,
                "packets_recv", (unsigned long long)ifm2->ifm_data.ifi_ipackets,
                "errin",        (unsigned long long)ifm2->ifm_data.ifi_ierrors,
                "errout",       (unsigned long long)ifm2->ifm_data.ifi_oerrors,
                "dropin",       (unsigned long long)ifm2->ifm_data.ifi_iqdrops,
                "dropout",      (unsigned long long)ifm2->ifm_data.ifi_oerrors
            );
            if (d) {
                PyDict_SetItemString(result, ifname, d);
                Py_DECREF(d);
            }
        }

        ptr += ifm->ifm_msglen;
    }

    free(buf);
    return result;
}
