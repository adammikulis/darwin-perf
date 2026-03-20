/**
 * darwin-perf: Per-process stats — proc_info, ppid, proc_connections.
 *
 * Copyright 2026 Adam Mikulis. MIT License.
 */

#include "_native.h"
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>

/* ------------------------------------------------------------------ */
/* Python API: proc_info                                               */
/* ------------------------------------------------------------------ */

const char proc_info_doc[] =
"proc_info(pid) -> dict | None\n\n"
"Return comprehensive process stats from rusage_info_v6 and proc_pidinfo.\n\n"
"Returns dict with keys:\n"
"    CPU:\n"
"    - 'cpu_ns': int -- cumulative CPU time (user + system) in nanoseconds\n"
"    - 'cpu_user_ns' / 'cpu_system_ns': int -- split CPU time\n"
"    - 'instructions': int -- retired instructions\n"
"    - 'cycles': int -- CPU cycles\n"
"    - 'runnable_time': int -- time process was runnable (ns)\n"
"    - 'billed_system_time' / 'serviced_system_time': int -- billed CPU (ns)\n"
"    Memory:\n"
"    - 'memory': int -- physical footprint in bytes\n"
"    - 'real_memory': int -- resident memory in bytes\n"
"    - 'wired_size': int -- wired (non-pageable) memory in bytes\n"
"    - 'peak_memory': int -- lifetime peak physical footprint\n"
"    - 'neural_footprint': int -- Neural Engine memory in bytes\n"
"    - 'pageins': int -- page-in count (memory pressure indicator)\n"
"    Disk:\n"
"    - 'disk_read_bytes' / 'disk_write_bytes': int -- cumulative disk I/O\n"
"    - 'logical_writes': int -- logical writes including CoW (bytes)\n"
"    Energy:\n"
"    - 'energy_nj': int -- cumulative energy in nanojoules (delta for watts)\n"
"    - 'idle_wakeups': int -- package idle wakeups\n"
"    - 'interrupt_wakeups': int -- interrupt wakeups\n"
"    Other:\n"
"    - 'threads': int -- current thread count\n\n"
"Returns None on error. No special privileges needed for same-user processes.";

PyObject* py_proc_info(PyObject* self, PyObject* args) {
    int pid;
    if (!PyArg_ParseTuple(args, "i", &pid))
        return NULL;

    struct rusage_info_v6 ri;
    int ret = proc_pid_rusage(pid, RUSAGE_INFO_V6, (rusage_info_t *)&ri);
    if (ret != 0)
        Py_RETURN_NONE;

    /* Thread count via proc_pidinfo */
    struct proc_taskinfo pti;
    int pti_size = proc_pidinfo(pid, PROC_PIDTASKINFO, 0, &pti, sizeof(pti));

    PyObject* d = PyDict_New();
    if (!d) return NULL;

    #define SET_LL(key, val) do { \
        PyObject *v = PyLong_FromLongLong((long long)(val)); \
        PyDict_SetItemString(d, key, v); Py_DECREF(v); \
    } while(0)
    #define SET_ULL(key, val) do { \
        PyObject *v = PyLong_FromUnsignedLongLong((unsigned long long)(val)); \
        PyDict_SetItemString(d, key, v); Py_DECREF(v); \
    } while(0)

    /* CPU time */
    SET_LL("cpu_ns", (long long)ri.ri_user_time + (long long)ri.ri_system_time);
    SET_LL("cpu_user_ns", ri.ri_user_time);
    SET_LL("cpu_system_ns", ri.ri_system_time);

    /* Memory */
    SET_ULL("memory", ri.ri_phys_footprint);
    SET_ULL("real_memory", ri.ri_resident_size);
    SET_ULL("wired_size", ri.ri_wired_size);
    SET_ULL("peak_memory", ri.ri_lifetime_max_phys_footprint);
    SET_ULL("neural_footprint", ri.ri_neural_footprint);
    SET_ULL("pageins", ri.ri_pageins);

    /* Disk I/O */
    SET_ULL("disk_read_bytes", ri.ri_diskio_bytesread);
    SET_ULL("disk_write_bytes", ri.ri_diskio_byteswritten);
    SET_ULL("logical_writes", ri.ri_logical_writes);

    /* Energy (nanojoules) */
    SET_ULL("energy_nj", ri.ri_energy_nj);

    /* CPU perf counters */
    SET_ULL("instructions", ri.ri_instructions);
    SET_ULL("cycles", ri.ri_cycles);
    SET_ULL("runnable_time", ri.ri_runnable_time);
    SET_LL("billed_system_time", ri.ri_billed_system_time);
    SET_LL("serviced_system_time", ri.ri_serviced_system_time);

    /* Wakeups (energy efficiency) */
    SET_ULL("idle_wakeups", ri.ri_pkg_idle_wkups);
    SET_ULL("interrupt_wakeups", ri.ri_interrupt_wkups);

    /* Thread count */
    if (pti_size >= (int)sizeof(pti)) {
        SET_LL("threads", pti.pti_threadnum);
    } else {
        SET_LL("threads", 0);
    }

    #undef SET_LL
    #undef SET_ULL

    return d;
}

/* ------------------------------------------------------------------ */
/* Python API: ppid                                                    */
/* ------------------------------------------------------------------ */

const char ppid_doc[] =
"ppid(pid) -> int\n\n"
"Return the parent process ID for the given PID.\n\n"
"Uses proc_pidinfo(PROC_PIDTBSDINFO). Returns -1 on error.";

PyObject* py_ppid(PyObject* self, PyObject* args) {
    int pid;
    if (!PyArg_ParseTuple(args, "i", &pid))
        return NULL;

    struct proc_bsdinfo bsdinfo;
    int ret = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &bsdinfo, sizeof(bsdinfo));
    if (ret <= 0)
        return PyLong_FromLong(-1);

    return PyLong_FromLong(bsdinfo.pbi_ppid);
}

/* ------------------------------------------------------------------ */
/* Python API: proc_connections                                        */
/* ------------------------------------------------------------------ */

const char proc_connections_doc[] =
"proc_connections(pid=0) -> list[dict]\n\n"
"Return all TCP/UDP sockets for a process via proc_pidinfo.\n"
"If pid is 0, enumerate all processes with network sockets.\n"
"No subprocess calls, no lsof, no sudo needed.\n\n"
"Each dict has keys:\n"
"    - 'pid': int\n"
"    - 'name': str -- process name\n"
"    - 'family': str -- 'ipv4' or 'ipv6'\n"
"    - 'type': str -- 'tcp' or 'udp'\n"
"    - 'local_addr': str\n"
"    - 'local_port': int\n"
"    - 'remote_addr': str\n"
"    - 'remote_port': int\n"
"    - 'status': str -- TCP state (e.g., 'ESTABLISHED', 'LISTEN')\n";

static const char* tcp_state_str(int state) {
    switch (state) {
        case 0: return "CLOSED";
        case 1: return "LISTEN";
        case 2: return "SYN_SENT";
        case 3: return "SYN_RECEIVED";
        case 4: return "ESTABLISHED";
        case 5: return "CLOSE_WAIT";
        case 6: return "FIN_WAIT_1";
        case 7: return "CLOSING";
        case 8: return "LAST_ACK";
        case 9: return "FIN_WAIT_2";
        case 10: return "TIME_WAIT";
        default: return "";
    }
}

static void addr_to_str(int family, const void *addr, char *buf, size_t buflen) {
    if (family == AF_INET) {
        inet_ntop(AF_INET, addr, buf, (socklen_t)buflen);
    } else if (family == AF_INET6) {
        inet_ntop(AF_INET6, addr, buf, (socklen_t)buflen);
    } else {
        buf[0] = '\0';
    }
}

static int _collect_pid_sockets(int pid, const char *proc_name, PyObject *result_list) {
    int buf_size = proc_pidinfo(pid, PROC_PIDLISTFDS, 0, NULL, 0);
    if (buf_size <= 0) return 0;

    int num_fds = buf_size / (int)sizeof(struct proc_fdinfo);
    struct proc_fdinfo *fds = malloc(buf_size);
    if (!fds) return 0;

    int actual = proc_pidinfo(pid, PROC_PIDLISTFDS, 0, fds, buf_size);
    if (actual <= 0) {
        free(fds);
        return 0;
    }
    num_fds = actual / (int)sizeof(struct proc_fdinfo);

    int count = 0;
    for (int i = 0; i < num_fds; i++) {
        if (fds[i].proc_fdtype != PROX_FDTYPE_SOCKET) continue;

        struct socket_fdinfo si;
        int si_size = proc_pidfdinfo(pid, fds[i].proc_fd,
                                      PROC_PIDFDSOCKETINFO, &si, sizeof(si));
        if (si_size < (int)sizeof(si)) continue;

        int family = si.psi.soi_family;
        int sotype = si.psi.soi_type;

        if (family != AF_INET && family != AF_INET6) continue;
        if (sotype != SOCK_STREAM && sotype != SOCK_DGRAM) continue;

        char local_addr[INET6_ADDRSTRLEN] = "";
        char remote_addr[INET6_ADDRSTRLEN] = "";
        int local_port = 0, remote_port = 0;
        const char *status = "";
        const char *type_str = (sotype == SOCK_STREAM) ? "tcp" : "udp";
        const char *fam_str = (family == AF_INET) ? "ipv4" : "ipv6";

        if (family == AF_INET) {
            struct in_sockinfo *in = &si.psi.soi_proto.pri_in;
            addr_to_str(AF_INET, &in->insi_laddr.ina_46.i46a_addr4, local_addr, sizeof(local_addr));
            addr_to_str(AF_INET, &in->insi_faddr.ina_46.i46a_addr4, remote_addr, sizeof(remote_addr));
            local_port = ntohs(in->insi_lport);
            remote_port = ntohs(in->insi_fport);
        } else {
            struct in_sockinfo *in = &si.psi.soi_proto.pri_in;
            addr_to_str(AF_INET6, &in->insi_laddr.ina_6, local_addr, sizeof(local_addr));
            addr_to_str(AF_INET6, &in->insi_faddr.ina_6, remote_addr, sizeof(remote_addr));
            local_port = ntohs(in->insi_lport);
            remote_port = ntohs(in->insi_fport);
        }

        if (sotype == SOCK_STREAM) {
            struct tcp_sockinfo *tcp = &si.psi.soi_proto.pri_tcp;
            status = tcp_state_str(tcp->tcpsi_state);
        }

        PyObject *entry = Py_BuildValue(
            "{s:i, s:s, s:s, s:s, s:s, s:i, s:s, s:i, s:s}",
            "pid", pid,
            "name", proc_name,
            "family", fam_str,
            "type", type_str,
            "local_addr", local_addr,
            "local_port", local_port,
            "remote_addr", remote_addr,
            "remote_port", remote_port,
            "status", status
        );
        if (entry) {
            PyList_Append(result_list, entry);
            Py_DECREF(entry);
            count++;
        }
    }

    free(fds);
    return count;
}

/* ------------------------------------------------------------------ */
/* Python API: proc_lineage                                            */
/* ------------------------------------------------------------------ */

const char proc_lineage_doc[] =
"proc_lineage(pid) -> list[dict]\n\n"
"Return the process ancestry chain from pid up to PID 1 (launchd).\n"
"Each dict has 'pid' and 'name' keys. First element is the given process,\n"
"last is the root ancestor. Stops at PID 1 or on error.\n\n"
"Useful for IDS: 'sshd -> bash -> nc' is suspicious,\n"
"'launchd -> Chrome -> helper' is normal.\n";

PyObject* py_proc_lineage(PyObject* self, PyObject* args) {
    (void)self;
    int pid;
    if (!PyArg_ParseTuple(args, "i", &pid))
        return NULL;

    PyObject *result = PyList_New(0);
    if (!result) return NULL;

    int visited[128];  /* cycle protection */
    int depth = 0;

    while (pid > 0 && depth < 128) {
        /* Check for cycles */
        int cycle = 0;
        for (int i = 0; i < depth; i++) {
            if (visited[i] == pid) { cycle = 1; break; }
        }
        if (cycle) break;
        visited[depth] = pid;

        char name[256] = "?";
        proc_name(pid, name, sizeof(name));

        PyObject *entry = Py_BuildValue("{s:i, s:s}", "pid", pid, "name", name);
        if (entry) {
            PyList_Append(result, entry);
            Py_DECREF(entry);
        }

        if (pid == 1) break;  /* reached launchd */

        /* Get parent */
        struct proc_bsdinfo bsdinfo;
        int ret = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &bsdinfo, sizeof(bsdinfo));
        if (ret <= 0) break;

        int parent = (int)bsdinfo.pbi_ppid;
        if (parent == pid) break;  /* self-parent (kernel) */
        pid = parent;
        depth++;
    }

    return result;
}


/* ------------------------------------------------------------------ */
/* Python API: proc_open_files                                         */
/* ------------------------------------------------------------------ */

const char proc_open_files_doc[] =
"proc_open_files(pid) -> list[dict]\n\n"
"Return all open file descriptors for a process via proc_pidinfo.\n\n"
"Each dict has keys:\n"
"    - 'fd': int -- file descriptor number\n"
"    - 'type': str -- 'vnode', 'socket', 'pipe', 'kqueue', etc.\n"
"    - 'path': str -- file path (for vnodes only, empty otherwise)\n\n"
"Useful for IDS: detect unexpected file access (keychain, ssh keys, etc.).\n"
"No sudo needed for same-user processes.\n";

PyObject* py_proc_open_files(PyObject* self, PyObject* args) {
    (void)self;
    int pid;
    if (!PyArg_ParseTuple(args, "i", &pid))
        return NULL;

    int buf_size = proc_pidinfo(pid, PROC_PIDLISTFDS, 0, NULL, 0);
    if (buf_size <= 0)
        return PyList_New(0);

    struct proc_fdinfo *fds = malloc(buf_size);
    if (!fds) return PyErr_NoMemory();

    int actual = proc_pidinfo(pid, PROC_PIDLISTFDS, 0, fds, buf_size);
    if (actual <= 0) {
        free(fds);
        return PyList_New(0);
    }
    int num_fds = actual / (int)sizeof(struct proc_fdinfo);

    PyObject *result = PyList_New(0);
    if (!result) { free(fds); return NULL; }

    for (int i = 0; i < num_fds; i++) {
        const char *type_str;
        char path[MAXPATHLEN] = "";

        switch (fds[i].proc_fdtype) {
            case PROX_FDTYPE_VNODE:  type_str = "vnode"; break;
            case PROX_FDTYPE_SOCKET: type_str = "socket"; break;
            case PROX_FDTYPE_PIPE:   type_str = "pipe"; break;
            case PROX_FDTYPE_KQUEUE: type_str = "kqueue"; break;
            default:                 type_str = "other"; break;
        }

        /* Get vnode path for file descriptors */
        if (fds[i].proc_fdtype == PROX_FDTYPE_VNODE) {
            struct vnode_fdinfowithpath vi;
            int vi_size = proc_pidfdinfo(pid, fds[i].proc_fd,
                                          PROC_PIDFDVNODEPATHINFO, &vi, sizeof(vi));
            if (vi_size >= (int)sizeof(vi)) {
                strncpy(path, vi.pvip.vip_path, MAXPATHLEN - 1);
                path[MAXPATHLEN - 1] = '\0';
            }
        }

        PyObject *entry = Py_BuildValue(
            "{s:i, s:s, s:s}",
            "fd", fds[i].proc_fd,
            "type", type_str,
            "path", path
        );
        if (entry) {
            PyList_Append(result, entry);
            Py_DECREF(entry);
        }
    }

    free(fds);
    return result;
}


/* ------------------------------------------------------------------ */
/* Python API: proc_connections                                        */
/* ------------------------------------------------------------------ */

PyObject* py_proc_connections(PyObject* self, PyObject* args) {
    (void)self;
    int target_pid = 0;
    if (!PyArg_ParseTuple(args, "|i", &target_pid))
        return NULL;

    PyObject *result = PyList_New(0);
    if (!result) return NULL;

    if (target_pid > 0) {
        char name[256] = "?";
        proc_name(target_pid, name, sizeof(name));
        _collect_pid_sockets(target_pid, name, result);
    } else {
        int buf_size = proc_listpids(PROC_ALL_PIDS, 0, NULL, 0);
        if (buf_size <= 0) return result;

        pid_t *pids = malloc(buf_size);
        if (!pids) return result;

        int actual = proc_listpids(PROC_ALL_PIDS, 0, pids, buf_size);
        if (actual <= 0) {
            free(pids);
            return result;
        }
        int num_pids = actual / (int)sizeof(pid_t);

        for (int i = 0; i < num_pids; i++) {
            if (pids[i] == 0) continue;
            char name[256] = "?";
            proc_name(pids[i], name, sizeof(name));
            _collect_pid_sockets(pids[i], name, result);
        }
        free(pids);
    }

    return result;
}


/* ------------------------------------------------------------------ */
/* Python API: proc_pidpath                                            */
/* ------------------------------------------------------------------ */

const char proc_pidpath_doc[] =
"proc_pidpath(pid) -> str\n\n"
"Return the full executable path for a process.\n\n"
"Uses libproc proc_pidpath(). Returns empty string on error.\n"
"No special privileges needed for same-user processes.\n";

PyObject* py_proc_pidpath(PyObject* self, PyObject* args) {
    (void)self;
    int pid;
    if (!PyArg_ParseTuple(args, "i", &pid))
        return NULL;

    char pathbuf[PROC_PIDPATHINFO_MAXSIZE];
    int ret = proc_pidpath(pid, pathbuf, sizeof(pathbuf));
    if (ret <= 0)
        return PyUnicode_FromString("");

    return PyUnicode_FromString(pathbuf);
}
