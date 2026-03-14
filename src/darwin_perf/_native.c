/**
 * darwin-perf: System performance monitoring for macOS Apple Silicon.
 *
 * Reads per-client GPU accounting from the IORegistry's AGXDeviceUserClient
 * entries. Each GPU client (Metal command queue) is a child of the AGX
 * accelerator and carries:
 *   - "IOUserClientCreator" = "pid <N>, <process_name>"
 *   - "AppUsage" = [{"API"="Metal", "accumulatedGPUTime"=<ns>}, ...]
 *
 * This is the same data Activity Monitor reads. No sudo or entitlements
 * needed — the IORegistry is world-readable.
 *
 * Copyright 2026 Adam Mikulis. MIT License.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <IOKit/IOKitLib.h>
#include <CoreFoundation/CoreFoundation.h>
#include <libproc.h>
#include <sys/proc_info.h>
#include <sys/resource.h>
#include <sys/sysctl.h>
#include <mach/mach.h>
#include <mach/host_info.h>
#include <mach/mach_host.h>

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

/**
 * Extract PID from "IOUserClientCreator" value like "pid 418, WindowServer".
 * Returns -1 on parse failure.
 */
static int parse_creator_pid(CFStringRef creator) {
    char buf[256];
    if (!CFStringGetCString(creator, buf, sizeof(buf), kCFStringEncodingUTF8))
        return -1;
    int pid = -1;
    if (sscanf(buf, "pid %d", &pid) == 1)
        return pid;
    return -1;
}

/**
 * Extract process name from "IOUserClientCreator" value.
 * Writes into out_name (up to name_size bytes). Returns 0 on success.
 */
static int parse_creator_name(CFStringRef creator, char *out_name, size_t name_size) {
    char buf[256];
    if (!CFStringGetCString(creator, buf, sizeof(buf), kCFStringEncodingUTF8))
        return -1;
    /* Format: "pid <N>, <name>" */
    char *comma = strchr(buf, ',');
    if (!comma || *(comma + 1) != ' ')
        return -1;
    strncpy(out_name, comma + 2, name_size - 1);
    out_name[name_size - 1] = '\0';
    return 0;
}

/**
 * Sum accumulatedGPUTime from an "AppUsage" CFArray.
 * Each element is a CFDictionary with "accumulatedGPUTime" -> CFNumber.
 */
static long long sum_app_usage_gpu_time(CFArrayRef app_usage) {
    long long total = 0;
    CFIndex count = CFArrayGetCount(app_usage);
    for (CFIndex i = 0; i < count; i++) {
        CFDictionaryRef entry = CFArrayGetValueAtIndex(app_usage, i);
        if (!entry || CFGetTypeID(entry) != CFDictionaryGetTypeID())
            continue;
        CFNumberRef gpu_time = CFDictionaryGetValue(entry,
            CFSTR("accumulatedGPUTime"));
        if (!gpu_time || CFGetTypeID(gpu_time) != CFNumberGetTypeID())
            continue;
        long long ns = 0;
        CFNumberGetValue(gpu_time, kCFNumberSInt64Type, &ns);
        total += ns;
    }
    return total;
}

/* ------------------------------------------------------------------ */
/* Core: iterate AGXDeviceUserClient entries in IORegistry             */
/* ------------------------------------------------------------------ */

/**
 * Result struct for one GPU client.
 */
typedef struct {
    int pid;
    char name[128];
    long long gpu_ns;
} gpu_client_t;

/**
 * Read all AGXDeviceUserClient entries from the IORegistry.
 *
 * User client objects are !registered, so IOServiceGetMatchingServices
 * won't find them. Instead we find the AGXAccelerator parent and iterate
 * its children in the IOService plane.
 *
 * Allocates *out_clients (caller must free). Returns count, or -1 on error.
 */
static int read_gpu_clients(gpu_client_t **out_clients) {
    *out_clients = NULL;

    /* Find the AGX accelerator (parent of all GPU user clients) */
    io_iterator_t accel_iter;
    kern_return_t kr = IOServiceGetMatchingServices(
        kIOMainPortDefault,
        IOServiceMatching("AGXAccelerator"),
        &accel_iter);
    if (kr != KERN_SUCCESS)
        return -1;

    int capacity = 64;
    gpu_client_t *clients = malloc(capacity * sizeof(gpu_client_t));
    if (!clients) {
        IOObjectRelease(accel_iter);
        return -1;
    }
    int count = 0;

    /* Iterate each accelerator (typically just one) */
    io_service_t accel;
    while ((accel = IOIteratorNext(accel_iter)) != 0) {
        io_iterator_t child_iter;
        kr = IORegistryEntryGetChildIterator(accel, kIOServicePlane, &child_iter);
        IOObjectRelease(accel);
        if (kr != KERN_SUCCESS)
            continue;

        io_service_t child;
        while ((child = IOIteratorNext(child_iter)) != 0) {
            /* Read IOUserClientCreator */
            CFStringRef creator = IORegistryEntryCreateCFProperty(
                child, CFSTR("IOUserClientCreator"),
                kCFAllocatorDefault, 0);
            if (!creator || CFGetTypeID(creator) != CFStringGetTypeID()) {
                if (creator) CFRelease(creator);
                IOObjectRelease(child);
                continue;
            }

            int pid = parse_creator_pid(creator);
            if (pid < 0) {
                CFRelease(creator);
                IOObjectRelease(child);
                continue;
            }

            /* Read AppUsage */
            CFArrayRef app_usage = IORegistryEntryCreateCFProperty(
                child, CFSTR("AppUsage"),
                kCFAllocatorDefault, 0);

            long long gpu_ns = 0;
            if (app_usage && CFGetTypeID(app_usage) == CFArrayGetTypeID())
                gpu_ns = sum_app_usage_gpu_time(app_usage);

            /* Grow array if needed */
            if (count >= capacity) {
                capacity *= 2;
                gpu_client_t *tmp = realloc(clients, capacity * sizeof(gpu_client_t));
                if (!tmp) {
                    if (app_usage) CFRelease(app_usage);
                    CFRelease(creator);
                    IOObjectRelease(child);
                    break;
                }
                clients = tmp;
            }

            clients[count].pid = pid;
            clients[count].gpu_ns = gpu_ns;
            parse_creator_name(creator, clients[count].name, sizeof(clients[count].name));
            count++;

            if (app_usage) CFRelease(app_usage);
            CFRelease(creator);
            IOObjectRelease(child);
        }
        IOObjectRelease(child_iter);
    }

    IOObjectRelease(accel_iter);
    *out_clients = clients;
    return count;
}

/* ------------------------------------------------------------------ */
/* Python API                                                          */
/* ------------------------------------------------------------------ */

PyDoc_STRVAR(gpu_time_ns_doc,
"gpu_time_ns(pid=0) -> int\n\n"
"Return cumulative GPU time in nanoseconds for a process.\n\n"
"Reads accumulatedGPUTime from AGXDeviceUserClient entries in the\n"
"IORegistry. Multiple command queues for the same PID are summed.\n\n"
"Args:\n"
"    pid: Process ID. 0 means the calling process.\n\n"
"Returns:\n"
"    Cumulative GPU nanoseconds, or 0 if the process has no GPU clients.\n\n"
"Note:\n"
"    No special privileges required — IORegistry is world-readable.");

static PyObject* py_gpu_time_ns(PyObject* self, PyObject* args) {
    int pid = 0;
    if (!PyArg_ParseTuple(args, "|i", &pid))
        return NULL;

    if (pid == 0)
        pid = getpid();

    gpu_client_t *clients;
    int count = read_gpu_clients(&clients);
    if (count < 0)
        return PyLong_FromLongLong(0);

    long long total = 0;
    for (int i = 0; i < count; i++) {
        if (clients[i].pid == pid)
            total += clients[i].gpu_ns;
    }
    free(clients);
    return PyLong_FromLongLong(total);
}


PyDoc_STRVAR(gpu_time_ns_multi_doc,
"gpu_time_ns_multi(pids: list[int]) -> dict[int, int]\n\n"
"Batch read GPU nanoseconds for multiple PIDs in one IORegistry scan.\n\n"
"Args:\n"
"    pids: List of process IDs. Use 0 for the calling process.\n\n"
"Returns:\n"
"    Dict mapping each PID to its cumulative GPU nanoseconds.\n"
"    PIDs with no GPU clients map to 0.");

static PyObject* py_gpu_time_ns_multi(PyObject* self, PyObject* args) {
    PyObject* pid_list;
    if (!PyArg_ParseTuple(args, "O!", &PyList_Type, &pid_list))
        return NULL;

    pid_t my_pid = getpid();

    gpu_client_t *clients;
    int count = read_gpu_clients(&clients);

    Py_ssize_t n = PyList_GET_SIZE(pid_list);
    PyObject* result = PyDict_New();
    if (!result) {
        free(clients);
        return NULL;
    }

    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject* item = PyList_GET_ITEM(pid_list, i);
        long pid_long = PyLong_AsLong(item);
        if (pid_long == -1 && PyErr_Occurred()) {
            Py_DECREF(result);
            free(clients);
            return NULL;
        }
        int pid = (int)pid_long;
        if (pid == 0) pid = my_pid;

        long long total = 0;
        if (count > 0) {
            for (int j = 0; j < count; j++) {
                if (clients[j].pid == pid)
                    total += clients[j].gpu_ns;
            }
        }

        PyObject* val = PyLong_FromLongLong(total);
        if (PyDict_SetItem(result, item, val) < 0) {
            Py_DECREF(val);
            Py_DECREF(result);
            free(clients);
            return NULL;
        }
        Py_DECREF(val);
    }

    free(clients);
    return result;
}


PyDoc_STRVAR(gpu_clients_doc,
"gpu_clients() -> list[dict]\n\n"
"Return all active GPU clients from the IORegistry.\n\n"
"Each dict has keys:\n"
"    - 'pid': int — process ID\n"
"    - 'name': str — process name (truncated)\n"
"    - 'gpu_ns': int — cumulative GPU nanoseconds\n\n"
"Multiple entries may exist for the same PID (one per command queue).\n"
"No special privileges required.");

static PyObject* py_gpu_clients(PyObject* self, PyObject* args) {
    gpu_client_t *clients;
    int count = read_gpu_clients(&clients);
    if (count < 0)
        return PyList_New(0);

    PyObject* list = PyList_New(count);
    if (!list) {
        free(clients);
        return NULL;
    }

    for (int i = 0; i < count; i++) {
        PyObject* d = PyDict_New();
        if (!d) {
            Py_DECREF(list);
            free(clients);
            return NULL;
        }
        PyObject* pid_obj = PyLong_FromLong(clients[i].pid);
        PyObject* name_obj = PyUnicode_FromString(clients[i].name);
        PyObject* ns_obj = PyLong_FromLongLong(clients[i].gpu_ns);

        PyDict_SetItemString(d, "pid", pid_obj);
        PyDict_SetItemString(d, "name", name_obj);
        PyDict_SetItemString(d, "gpu_ns", ns_obj);

        Py_DECREF(pid_obj);
        Py_DECREF(name_obj);
        Py_DECREF(ns_obj);

        PyList_SET_ITEM(list, i, d);  /* steals ref */
    }

    free(clients);
    return list;
}


/* ------------------------------------------------------------------ */
/* CPU time via proc_pid_rusage                                        */
/* ------------------------------------------------------------------ */

PyDoc_STRVAR(cpu_time_ns_doc,
"cpu_time_ns(pid) -> int\n\n"
"Return cumulative CPU time (user + system) in nanoseconds for a process.\n\n"
"Uses proc_pid_rusage (RUSAGE_INFO_V2). No special privileges needed\n"
"for processes owned by the same user.\n\n"
"Args:\n"
"    pid: Process ID.\n\n"
"Returns:\n"
"    Cumulative CPU nanoseconds, or -1 on error.");

static PyObject* py_cpu_time_ns(PyObject* self, PyObject* args) {
    int pid;
    if (!PyArg_ParseTuple(args, "i", &pid))
        return NULL;

    struct rusage_info_v6 ri;
    int ret = proc_pid_rusage(pid, RUSAGE_INFO_V6, (rusage_info_t *)&ri);
    if (ret != 0)
        return PyLong_FromLongLong(-1);

    long long total = (long long)ri.ri_user_time + (long long)ri.ri_system_time;
    return PyLong_FromLongLong(total);
}


PyDoc_STRVAR(proc_info_doc,
"proc_info(pid) -> dict | None\n\n"
"Return comprehensive process stats from rusage_info_v6 and proc_pidinfo.\n\n"
"Returns dict with keys:\n"
"    CPU:\n"
"    - 'cpu_ns': int — cumulative CPU time (user + system) in nanoseconds\n"
"    - 'cpu_user_ns' / 'cpu_system_ns': int — split CPU time\n"
"    - 'instructions': int — retired instructions\n"
"    - 'cycles': int — CPU cycles\n"
"    - 'runnable_time': int — time process was runnable (ns)\n"
"    - 'billed_system_time' / 'serviced_system_time': int — billed CPU (ns)\n"
"    Memory:\n"
"    - 'memory': int — physical footprint in bytes\n"
"    - 'real_memory': int — resident memory in bytes\n"
"    - 'wired_size': int — wired (non-pageable) memory in bytes\n"
"    - 'peak_memory': int — lifetime peak physical footprint\n"
"    - 'neural_footprint': int — Neural Engine memory in bytes\n"
"    - 'pageins': int — page-in count (memory pressure indicator)\n"
"    Disk:\n"
"    - 'disk_read_bytes' / 'disk_write_bytes': int — cumulative disk I/O\n"
"    - 'logical_writes': int — logical writes including CoW (bytes)\n"
"    Energy:\n"
"    - 'energy_nj': int — cumulative energy in nanojoules (delta for watts)\n"
"    - 'idle_wakeups': int — package idle wakeups\n"
"    - 'interrupt_wakeups': int — interrupt wakeups\n"
"    Other:\n"
"    - 'threads': int — current thread count\n\n"
"Returns None on error. No special privileges needed for same-user processes.");

static PyObject* py_proc_info(PyObject* self, PyObject* args) {
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

    /* Helper macro to reduce boilerplate */
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

    /* Energy (nanojoules) — cumulative, take deltas for power rate */
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
/* System-wide GPU stats from IORegistry PerformanceStatistics         */
/* ------------------------------------------------------------------ */

PyDoc_STRVAR(system_gpu_stats_doc,
"system_gpu_stats() -> dict\n\n"
"Return system-wide GPU performance statistics from the IORegistry.\n\n"
"Reads 'PerformanceStatistics' from the AGXAccelerator entry.\n\n"
"Returns dict with keys:\n"
"    - 'device_utilization': int — Device Utilization %% (0-100)\n"
"    - 'tiler_utilization': int — Tiler Utilization %%\n"
"    - 'renderer_utilization': int — Renderer Utilization %%\n"
"    - 'alloc_system_memory': int — total GPU-allocated system memory (bytes)\n"
"    - 'in_use_system_memory': int — in-use GPU system memory (bytes)\n"
"    - 'in_use_system_memory_driver': int — driver-side in-use memory\n"
"    - 'allocated_pb_size': int — parameter buffer allocation (bytes)\n"
"    - 'recovery_count': int — GPU recovery (crash) count\n"
"    - 'last_recovery_time': int — timestamp of last GPU recovery\n"
"    - 'split_scene_count': int — tiler split scene events\n"
"    - 'tiled_scene_bytes': int — current tiled scene buffer size\n"
"    - 'model': str — GPU model name\n"
"    - 'gpu_core_count': int — number of GPU cores");

static PyObject* py_system_gpu_stats(PyObject* self, PyObject* args) {
    io_iterator_t iter;
    kern_return_t kr = IOServiceGetMatchingServices(
        kIOMainPortDefault,
        IOServiceMatching("AGXAccelerator"),
        &iter);
    if (kr != KERN_SUCCESS)
        return PyDict_New();

    PyObject* result = PyDict_New();
    if (!result) {
        IOObjectRelease(iter);
        return NULL;
    }

    io_service_t accel;
    if ((accel = IOIteratorNext(iter)) != 0) {
        CFDictionaryRef perf = IORegistryEntryCreateCFProperty(
            accel, CFSTR("PerformanceStatistics"),
            kCFAllocatorDefault, 0);
        if (perf && CFGetTypeID(perf) == CFDictionaryGetTypeID()) {
            /* Extract known keys */
            struct { const char *cf_key; const char *py_key; } keys[] = {
                {"Device Utilization %", "device_utilization"},
                {"Tiler Utilization %", "tiler_utilization"},
                {"Renderer Utilization %", "renderer_utilization"},
                {"Alloc system memory", "alloc_system_memory"},
                {"In use system memory", "in_use_system_memory"},
                {"In use system memory (driver)", "in_use_system_memory_driver"},
                {"Allocated PB Size", "allocated_pb_size"},
                {"recoveryCount", "recovery_count"},
                {"lastRecoveryTime", "last_recovery_time"},
                {"SplitSceneCount", "split_scene_count"},
                {"TiledSceneBytes", "tiled_scene_bytes"},
                {NULL, NULL}
            };
            for (int i = 0; keys[i].cf_key; i++) {
                CFStringRef cf_key = CFStringCreateWithCString(
                    kCFAllocatorDefault, keys[i].cf_key, kCFStringEncodingUTF8);
                CFNumberRef num = CFDictionaryGetValue(perf, cf_key);
                CFRelease(cf_key);
                if (num && CFGetTypeID(num) == CFNumberGetTypeID()) {
                    long long val = 0;
                    CFNumberGetValue(num, kCFNumberSInt64Type, &val);
                    PyObject *v = PyLong_FromLongLong(val);
                    PyDict_SetItemString(result, keys[i].py_key, v);
                    Py_DECREF(v);
                }
            }
        }
        if (perf) CFRelease(perf);

        /* Also grab model name and core count */
        CFStringRef model = IORegistryEntryCreateCFProperty(
            accel, CFSTR("model"), kCFAllocatorDefault, 0);
        if (model && CFGetTypeID(model) == CFStringGetTypeID()) {
            char buf[128];
            if (CFStringGetCString(model, buf, sizeof(buf), kCFStringEncodingUTF8)) {
                PyObject *v = PyUnicode_FromString(buf);
                PyDict_SetItemString(result, "model", v);
                Py_DECREF(v);
            }
        }
        if (model) CFRelease(model);

        CFNumberRef cores = IORegistryEntryCreateCFProperty(
            accel, CFSTR("gpu-core-count"), kCFAllocatorDefault, 0);
        if (cores && CFGetTypeID(cores) == CFNumberGetTypeID()) {
            long long val = 0;
            CFNumberGetValue(cores, kCFNumberSInt64Type, &val);
            PyObject *v = PyLong_FromLongLong(val);
            PyDict_SetItemString(result, "gpu_core_count", v);
            Py_DECREF(v);
        }
        if (cores) CFRelease(cores);

        IOObjectRelease(accel);
    }

    IOObjectRelease(iter);
    return result;
}


/* ------------------------------------------------------------------ */
/* Parent PID lookup via proc_pidinfo                                  */
/* ------------------------------------------------------------------ */

PyDoc_STRVAR(ppid_doc,
"ppid(pid) -> int\n\n"
"Return the parent process ID for the given PID.\n\n"
"Uses proc_pidinfo(PROC_PIDTBSDINFO). Returns -1 on error.");

static PyObject* py_ppid(PyObject* self, PyObject* args) {
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
/* GPU DVFS frequency table from pmgr IOService                        */
/* ------------------------------------------------------------------ */

/**
 * Read the GPU DVFS frequency table from the "pmgr" (power manager)
 * IOService entry. The "voltage-states9" property contains pairs of
 * (frequency_hz, voltage) as little-endian uint32s.
 *
 * Returns a Python list of frequencies in MHz, ordered by P-state index.
 * P1 = index 0, P2 = index 1, etc. The OFF state has no entry.
 */

PyDoc_STRVAR(gpu_freq_table_doc,
"gpu_freq_table() -> list[int]\n\n"
"Return the GPU DVFS frequency table in MHz, one entry per P-state.\n\n"
"Reads 'voltage-states9' from the pmgr IOService. Index 0 = P1 frequency,\n"
"index 1 = P2, etc. Empty list if the table cannot be read.\n\n"
"Example::\n\n"
"    >>> gpu_freq_table()\n"
"    [338, 618, 796, 924, 952, 1056, 1062, 1182, 1182, 1312, 1242, 1380, 1326, 1470, 1578]\n");

static PyObject* py_gpu_freq_table(PyObject* self, PyObject* args) {
    io_iterator_t iter;
    kern_return_t kr = IOServiceGetMatchingServices(
        kIOMainPortDefault,
        IOServiceMatching("AppleARMIODevice"),
        &iter);
    if (kr != KERN_SUCCESS)
        return PyList_New(0);

    PyObject *result = PyList_New(0);
    io_service_t service;
    while ((service = IOIteratorNext(iter)) != 0) {
        io_name_t name;
        IORegistryEntryGetName(service, name);
        if (strcmp(name, "pmgr") != 0) {
            IOObjectRelease(service);
            continue;
        }

        CFDataRef data = IORegistryEntryCreateCFProperty(
            service, CFSTR("voltage-states9"),
            kCFAllocatorDefault, 0);
        if (data && CFGetTypeID(data) == CFDataGetTypeID()) {
            CFIndex length = CFDataGetLength(data);
            const uint8_t *ptr = CFDataGetBytePtr(data);

            /* Each entry is 8 bytes: uint32 freq_hz + uint32 voltage */
            for (CFIndex i = 0; i + 7 < length; i += 8) {
                uint32_t freq_hz;
                memcpy(&freq_hz, ptr + i, 4);
                if (freq_hz == 0) continue;
                long freq_mhz = freq_hz / 1000000;
                PyObject *v = PyLong_FromLong(freq_mhz);
                PyList_Append(result, v);
                Py_DECREF(v);
            }
        }
        if (data) CFRelease(data);
        IOObjectRelease(service);
        break;
    }
    IOObjectRelease(iter);
    return result;
}


/* ------------------------------------------------------------------ */
/* GPU power/frequency/temperature via libIOReport                     */
/* ------------------------------------------------------------------ */

/*
 * libIOReport is an undocumented Apple library that provides access to
 * IOReport channels — the same data source powermetrics uses. No sudo
 * needed. We dlopen it at runtime to avoid hard linking.
 *
 * Key functions:
 *   IOReportCopyChannelsInGroup(group, subgroup) -> CFDictionary
 *   IOReportCreateSubscription(NULL, channels, &subbed, 0, NULL) -> sub
 *   IOReportCreateSamples(sub, subbed, NULL) -> sample
 *   IOReportCreateSamplesDelta(s1, s2, NULL) -> delta
 *   IOReportSimpleGetIntegerValue(entry, NULL) -> int64
 *   IOReportStateGetCount/Residency/NameForIndex -> state data
 *   IOReportChannelGetChannelName/SubGroup/Format -> channel metadata
 */

#include <dlfcn.h>

/* IOReport format types */
#define kIOReportFormatSimple    1
#define kIOReportFormatState     2
#define kIOReportFormatHistogram 3

/* Function pointer typedefs */
typedef CFDictionaryRef (*IOReportCopyChannelsInGroup_t)(CFStringRef, CFStringRef);
typedef void (*IOReportMergeChannels_t)(CFMutableDictionaryRef, CFDictionaryRef, CFTypeRef);
typedef int (*IOReportGetChannelCount_t)(CFDictionaryRef);
typedef CFTypeRef (*IOReportCreateSubscription_t)(void*, CFMutableDictionaryRef, CFMutableDictionaryRef*, uint64_t, CFTypeRef);
typedef CFDictionaryRef (*IOReportCreateSamples_t)(CFTypeRef, CFMutableDictionaryRef, CFTypeRef);
typedef CFDictionaryRef (*IOReportCreateSamplesDelta_t)(CFDictionaryRef, CFDictionaryRef, CFTypeRef);
typedef int64_t (*IOReportSimpleGetIntegerValue_t)(CFDictionaryRef, void*);
typedef CFStringRef (*IOReportChannelGetChannelName_t)(CFDictionaryRef);
typedef CFStringRef (*IOReportChannelGetSubGroup_t)(CFDictionaryRef);
typedef int32_t (*IOReportChannelGetFormat_t)(CFDictionaryRef);
typedef int32_t (*IOReportStateGetCount_t)(CFDictionaryRef);
typedef int64_t (*IOReportStateGetResidency_t)(CFDictionaryRef, int32_t);
typedef CFStringRef (*IOReportStateGetNameForIndex_t)(CFDictionaryRef, int32_t);

/* Cached function pointers */
static int ior_loaded = 0;
static IOReportCopyChannelsInGroup_t     ior_CopyChannelsInGroup;
static IOReportMergeChannels_t           ior_MergeChannels;
static IOReportGetChannelCount_t         ior_GetChannelCount;
static IOReportCreateSubscription_t      ior_CreateSubscription;
static IOReportCreateSamples_t           ior_CreateSamples;
static IOReportCreateSamplesDelta_t      ior_CreateSamplesDelta;
static IOReportSimpleGetIntegerValue_t   ior_SimpleGetIntegerValue;
static IOReportChannelGetChannelName_t   ior_ChannelGetChannelName;
static IOReportChannelGetSubGroup_t      ior_ChannelGetSubGroup;
static IOReportChannelGetFormat_t        ior_ChannelGetFormat;
static IOReportStateGetCount_t           ior_StateGetCount;
static IOReportStateGetResidency_t       ior_StateGetResidency;
static IOReportStateGetNameForIndex_t    ior_StateGetNameForIndex;

static int load_ioreport(void) {
    if (ior_loaded) return ior_loaded > 0 ? 0 : -1;

    void *lib = dlopen("/usr/lib/libIOReport.dylib", RTLD_LAZY);
    if (!lib) {
        /* Try without path — dyld shared cache */
        lib = dlopen("libIOReport.dylib", RTLD_LAZY);
    }
    if (!lib) { ior_loaded = -1; return -1; }

    #define LOAD(name) ior_##name = (IOReport##name##_t)dlsym(lib, "IOReport" #name); \
        if (!ior_##name) { ior_loaded = -1; return -1; }

    LOAD(CopyChannelsInGroup)
    LOAD(MergeChannels)
    LOAD(GetChannelCount)
    LOAD(CreateSubscription)
    LOAD(CreateSamples)
    LOAD(CreateSamplesDelta)
    LOAD(SimpleGetIntegerValue)
    LOAD(ChannelGetChannelName)
    LOAD(ChannelGetSubGroup)
    LOAD(ChannelGetFormat)
    LOAD(StateGetCount)
    LOAD(StateGetResidency)
    LOAD(StateGetNameForIndex)

    #undef LOAD
    ior_loaded = 1;
    return 0;
}

/**
 * Helper: compare CFString to C string via extraction.
 * Uses CFStringGetCString + strcmp rather than CFStringCompare,
 * which avoids issues with tagged/constant string representations.
 */
static int cfstr_eq(CFStringRef cf, const char *c) {
    if (!cf) return 0;
    char buf[256];
    if (!CFStringGetCString(cf, buf, sizeof(buf), kCFStringEncodingUTF8))
        return 0;
    return strcmp(buf, c) == 0;
}

/**
 * Helper: convert CFString to a Python string (or None).
 */
static PyObject* cfstr_to_pystr(CFStringRef cf) {
    if (!cf) Py_RETURN_NONE;
    char buf[256];
    if (CFStringGetCString(cf, buf, sizeof(buf), kCFStringEncodingUTF8))
        return PyUnicode_FromString(buf);
    Py_RETURN_NONE;
}


PyDoc_STRVAR(gpu_power_doc,
"gpu_power(interval=1.0) -> dict\n\n"
"Sample GPU power, temperature, and frequency state via IOReport.\n\n"
"Takes two IOReport samples separated by ``interval`` seconds and\n"
"returns the delta. No sudo or special privileges needed.\n\n"
"Args:\n"
"    interval: Sampling interval in seconds (default 1.0).\n\n"
"Returns dict with keys:\n"
"    - 'gpu_power_w': float — GPU power draw in watts\n"
"    - 'gpu_energy_nj': int — GPU energy delta in nanojoules\n"
"    - 'temperatures': dict — GPU die sensor temperatures (°C)\n"
"        e.g. {'avg': 42.1, 'sensors': {'Tg1a': 41, 'Tg5a': 43, ...}}\n"
"    - 'frequency_states': list — P-state residency during interval\n"
"        e.g. [{'state': 'P1', 'residency_pct': 20.5}, ...]\n"
"    - 'active_state': str — current GPU power state ('PERF' or 'IDLE_OFF')\n"
"    - 'throttled': bool — whether GPU is thermally throttled\n"
"    - 'power_limit_pct': int — PPM target as %% of max GPU power\n\n"
"Returns empty dict if libIOReport is unavailable.");

static PyObject* py_gpu_power(PyObject* self, PyObject* args) {
    double interval = 1.0;
    if (!PyArg_ParseTuple(args, "|d", &interval))
        return NULL;

    if (load_ioreport() < 0)
        return PyDict_New();

    /* Get channels for Energy Model + GPU Stats */
    CFStringRef energy_group = CFStringCreateWithCString(kCFAllocatorDefault,
        "Energy Model", kCFStringEncodingUTF8);
    CFStringRef gpu_group = CFStringCreateWithCString(kCFAllocatorDefault,
        "GPU Stats", kCFStringEncodingUTF8);

    CFMutableDictionaryRef channels = (CFMutableDictionaryRef)
        ior_CopyChannelsInGroup(energy_group, NULL);
    CFDictionaryRef gpu_channels = ior_CopyChannelsInGroup(gpu_group, NULL);

    if (!channels || !gpu_channels) {
        if (channels) CFRelease(channels);
        if (gpu_channels) CFRelease(gpu_channels);
        CFRelease(energy_group);
        CFRelease(gpu_group);
        return PyDict_New();
    }

    ior_MergeChannels(channels, gpu_channels, NULL);
    CFRelease(gpu_channels);

    /* Subscribe and take two samples */
    CFMutableDictionaryRef subbed = NULL;
    CFTypeRef sub = ior_CreateSubscription(NULL, channels, &subbed, 0, NULL);
    if (!sub || !subbed) {
        if (channels) CFRelease(channels);
        CFRelease(energy_group);
        CFRelease(gpu_group);
        return PyDict_New();
    }

    CFDictionaryRef s1 = ior_CreateSamples(sub, subbed, NULL);

    /* Release GIL during sleep */
    Py_BEGIN_ALLOW_THREADS
    usleep((useconds_t)(interval * 1e6));
    Py_END_ALLOW_THREADS

    CFDictionaryRef s2 = ior_CreateSamples(sub, subbed, NULL);

    if (!s1 || !s2) {
        if (s1) CFRelease(s1);
        if (s2) CFRelease(s2);
        CFRelease(channels);
        CFRelease(energy_group);
        CFRelease(gpu_group);
        return PyDict_New();
    }

    /* Keep both samples for manual delta computation */
    CFDictionaryRef s1_keep = s1;
    /* s1_keep and s2 both retained */

    /* Build result dict */
    PyObject *result = PyDict_New();
    if (!result) {
        CFRelease(s1_keep);
        CFRelease(s2);
        CFRelease(channels);
        CFRelease(energy_group);
        CFRelease(gpu_group);
        return NULL;
    }

    /* Parse delta channels */
    /* Note: must use the dict's own key for lookup. Extract via GetKeysAndValues
     * since CFSTR() constant strings may not match IOReport's internal keys. */
    CFArrayRef delta_arr = NULL;
    CFArrayRef abs_arr = NULL;
    /**
     * Extract channels from IOReport sample. The dict has one key
     * ("IOReportChannels" or "IOReportDrivers"). We use CFEqual for
     * key comparison since CFSTR() constants and IOReport's internal
     * keys may use different string representations.
     */
    /* Use IOReportIterate to collect all channels from delta/sample.
     * This avoids the CFDictionary key matching issue entirely. */
    /* Actually, we need to use IOReportCreateSamplesProcessed or
     * just pass subbed (which has IOReportChannels) through
     * IOReportCreateSamplesDelta differently.
     *
     * Alternative approach: don't use delta at all. Take two raw samples
     * and compute deltas ourselves in C, same as we do for GPU time. */

    /* Approach: use s1 and s2 directly (both have IOReportChannels
     * since they come from IOReportCreateSamples, not delta). */
    /**
     * IOReport sample structure (empirically determined):
     *   {"IOReportDrivers": {
     *       "DriverName <id>": {
     *           "IOReportChannels": [channel, ...]
     *       }, ...
     *   }}
     *
     * We need to find all IOReportChannels arrays across all drivers
     * and concatenate them. For simplicity, we collect them into a
     * flat C array of channel refs.
     */
    #define MAX_CHANNELS 1024

    typedef struct {
        CFDictionaryRef ch;  /* channel dict from s2 */
        CFDictionaryRef ch1; /* matching channel from s1 (same index) */
    } channel_pair_t;

    channel_pair_t *pairs = malloc(MAX_CHANNELS * sizeof(channel_pair_t));
    int n_pairs = 0;

    /* Extract channels from the nested IOReportDrivers structure */
    {
        /* Get IOReportDrivers dict (first value in the sample dict) */
        CFIndex sn = CFDictionaryGetCount(s1_keep);
        CFIndex sn2 = CFDictionaryGetCount(s2);
        if (sn > 0 && sn2 > 0) {
            const void *sk1[4], *sv1[4], *sk2[4], *sv2[4];
            CFDictionaryGetKeysAndValues(s1_keep, sk1, sv1);
            CFDictionaryGetKeysAndValues(s2, sk2, sv2);

            /* First value should be the IOReportDrivers dict */
            CFDictionaryRef drivers1 = (CFGetTypeID(sv1[0]) == CFDictionaryGetTypeID())
                ? (CFDictionaryRef)sv1[0] : NULL;
            CFDictionaryRef drivers2 = (CFGetTypeID(sv2[0]) == CFDictionaryGetTypeID())
                ? (CFDictionaryRef)sv2[0] : NULL;

            if (drivers1 && drivers2) {
                /* Iterate driver dicts in drivers2 */
                CFIndex nd = CFDictionaryGetCount(drivers2);
                const void **dk = malloc(nd * sizeof(void*));
                const void **dv = malloc(nd * sizeof(void*));
                CFDictionaryGetKeysAndValues(drivers2, dk, dv);

                for (CFIndex d = 0; d < nd; d++) {
                    if (CFGetTypeID(dv[d]) != CFDictionaryGetTypeID()) continue;
                    CFDictionaryRef drv2 = (CFDictionaryRef)dv[d];
                    /* Get matching driver from s1 */
                    CFDictionaryRef drv1 = (CFDictionaryRef)CFDictionaryGetValue(drivers1, dk[d]);

                    /* Find IOReportChannels array in this driver */
                    CFIndex dnk = CFDictionaryGetCount(drv2);
                    const void **ddk = malloc(dnk * sizeof(void*));
                    const void **ddv = malloc(dnk * sizeof(void*));
                    CFDictionaryGetKeysAndValues(drv2, ddk, ddv);

                    CFArrayRef ch_arr2 = NULL, ch_arr1 = NULL;
                    for (CFIndex k = 0; k < dnk; k++) {
                        if (CFGetTypeID(ddv[k]) == CFArrayGetTypeID()) {
                            ch_arr2 = (CFArrayRef)ddv[k];
                            /* Find matching array in drv1 */
                            if (drv1 && CFGetTypeID(drv1) == CFDictionaryGetTypeID()) {
                                CFTypeRef v1 = CFDictionaryGetValue(drv1, ddk[k]);
                                if (v1 && CFGetTypeID(v1) == CFArrayGetTypeID())
                                    ch_arr1 = (CFArrayRef)v1;
                            }
                            break;
                        }
                    }
                    free(ddk);
                    free(ddv);

                    if (ch_arr2 && ch_arr1) {
                        CFIndex nc = CFArrayGetCount(ch_arr2);
                        CFIndex nc1 = CFArrayGetCount(ch_arr1);
                        if (nc1 < nc) nc = nc1;
                        for (CFIndex c = 0; c < nc && n_pairs < MAX_CHANNELS; c++) {
                            pairs[n_pairs].ch = CFArrayGetValueAtIndex(ch_arr2, c);
                            pairs[n_pairs].ch1 = CFArrayGetValueAtIndex(ch_arr1, c);
                            n_pairs++;
                        }
                    }
                }
                free(dk);
                free(dv);
            }
        }
    }

    /* Also collect abs_arr for temperatures from s2 pairs */

    /* Temps dict and sensors sub-dict */
    PyObject *temps_dict = PyDict_New();
    PyObject *sensors_dict = PyDict_New();
    long long temp_sum = 0;
    int temp_count = 0;

    /* Frequency states list */
    PyObject *freq_list = PyList_New(0);

    /* Read GPU DVFS frequency table for MHz mapping */
    #define MAX_PSTATES 32
    long dvfs_mhz[MAX_PSTATES];
    int dvfs_count = 0;
    {
        io_iterator_t dvfs_iter;
        kern_return_t dvfs_kr = IOServiceGetMatchingServices(
            kIOMainPortDefault, IOServiceMatching("AppleARMIODevice"), &dvfs_iter);
        if (dvfs_kr == KERN_SUCCESS) {
            io_service_t svc;
            while ((svc = IOIteratorNext(dvfs_iter)) != 0) {
                io_name_t svc_name;
                IORegistryEntryGetName(svc, svc_name);
                if (strcmp(svc_name, "pmgr") == 0) {
                    CFDataRef dvfs_data = IORegistryEntryCreateCFProperty(
                        svc, CFSTR("voltage-states9"), kCFAllocatorDefault, 0);
                    if (dvfs_data && CFGetTypeID(dvfs_data) == CFDataGetTypeID()) {
                        CFIndex len = CFDataGetLength(dvfs_data);
                        const uint8_t *p = CFDataGetBytePtr(dvfs_data);
                        for (CFIndex off = 0; off + 7 < len && dvfs_count < MAX_PSTATES; off += 8) {
                            uint32_t fhz;
                            memcpy(&fhz, p + off, 4);
                            if (fhz > 0)
                                dvfs_mhz[dvfs_count++] = fhz / 1000000;
                        }
                    }
                    if (dvfs_data) CFRelease(dvfs_data);
                    IOObjectRelease(svc);
                    break;
                }
                IOObjectRelease(svc);
            }
            IOObjectRelease(dvfs_iter);
        }
    }

    if (n_pairs > 0) {
        for (int i = 0; i < n_pairs; i++) {
            CFDictionaryRef entry = pairs[i].ch;    /* s2 */
            CFDictionaryRef entry1 = pairs[i].ch1;  /* s1 */
            CFStringRef name = ior_ChannelGetChannelName(entry);
            CFStringRef subgroup = ior_ChannelGetSubGroup(entry);
            int32_t fmt = ior_ChannelGetFormat(entry);

            if (!name) continue;

            /* GPU Energy (Simple, delta = s2 - s1 nanojoules) */
            if (fmt == kIOReportFormatSimple && cfstr_eq(name, "GPU Energy")) {
                int64_t e2 = ior_SimpleGetIntegerValue(entry, NULL);
                int64_t e1 = ior_SimpleGetIntegerValue(entry1, NULL);
                int64_t energy_nj = e2 - e1;
                double watts = (double)energy_nj / (interval * 1e9);

                PyObject *v;
                v = PyFloat_FromDouble(watts);
                PyDict_SetItemString(result, "gpu_power_w", v);
                Py_DECREF(v);
                v = PyLong_FromLongLong(energy_nj);
                PyDict_SetItemString(result, "gpu_energy_nj", v);
                Py_DECREF(v);
            }

            /* GPU Performance States (State, delta = s2 - s1 residency) */
            if (fmt == kIOReportFormatState && cfstr_eq(name, "GPUPH")) {
                int32_t state_count = ior_StateGetCount(entry);
                int64_t total_res = 0;
                for (int32_t s = 0; s < state_count; s++) {
                    int64_t r2 = ior_StateGetResidency(entry, s);
                    int64_t r1 = ior_StateGetResidency(entry1, s);
                    total_res += (r2 - r1);
                }

                double weighted_freq = 0;
                double active_pct = 0;

                for (int32_t s = 0; s < state_count; s++) {
                    CFStringRef sname = ior_StateGetNameForIndex(entry, s);
                    int64_t r2 = ior_StateGetResidency(entry, s);
                    int64_t r1 = ior_StateGetResidency(entry1, s);
                    int64_t res = r2 - r1;
                    if (res <= 0 || !sname) continue;

                    double pct = total_res > 0 ? (double)res / total_res * 100.0 : 0;
                    /* Skip OFF state in the output */
                    if (cfstr_eq(sname, "OFF")) continue;

                    /* Map P-state name to frequency via DVFS table.
                     * State names are "P1", "P2", etc. Parse the number. */
                    char sbuf[16];
                    long freq = 0;
                    if (CFStringGetCString(sname, sbuf, sizeof(sbuf), kCFStringEncodingUTF8)
                        && sbuf[0] == 'P') {
                        int pindex = atoi(sbuf + 1);
                        if (pindex >= 1 && pindex <= dvfs_count)
                            freq = dvfs_mhz[pindex - 1];
                    }

                    active_pct += pct;
                    if (freq > 0)
                        weighted_freq += freq * (pct / 100.0);

                    PyObject *state_dict = PyDict_New();
                    PyObject *v;
                    v = cfstr_to_pystr(sname);
                    PyDict_SetItemString(state_dict, "state", v);
                    Py_DECREF(v);
                    v = PyFloat_FromDouble(pct);
                    PyDict_SetItemString(state_dict, "residency_pct", v);
                    Py_DECREF(v);
                    if (freq > 0) {
                        v = PyLong_FromLong(freq);
                        PyDict_SetItemString(state_dict, "freq_mhz", v);
                        Py_DECREF(v);
                    }
                    PyList_Append(freq_list, state_dict);
                    Py_DECREF(state_dict);
                }

                /* Weighted average GPU frequency */
                if (active_pct > 0) {
                    double avg_freq = weighted_freq / (active_pct / 100.0);
                    PyObject *v = PyLong_FromLong((long)avg_freq);
                    PyDict_SetItemString(result, "gpu_freq_mhz", v);
                    Py_DECREF(v);
                }
            }

            /* CLTM-induced throttling (use delta residency) */
            if (fmt == kIOReportFormatState && cfstr_eq(name, "GPU_CLTM")) {
                int32_t state_count = ior_StateGetCount(entry);
                int64_t total_res = 0;
                int64_t no_cltm_res = 0;
                for (int32_t s = 0; s < state_count; s++) {
                    int64_t res = ior_StateGetResidency(entry, s) - ior_StateGetResidency(entry1, s);
                    total_res += res;
                    CFStringRef sname = ior_StateGetNameForIndex(entry, s);
                    if (sname && cfstr_eq(sname, "NO_CLTM"))
                        no_cltm_res = res;
                }
                int throttled = (total_res > 0 && no_cltm_res < total_res);
                PyObject *v = PyBool_FromLong(throttled);
                PyDict_SetItemString(result, "throttled", v);
                Py_DECREF(v);
            }

            /* Power controller state (delta residency) */
            if (fmt == kIOReportFormatState && cfstr_eq(name, "PWRCTRL")) {
                int32_t state_count = ior_StateGetCount(entry);
                int64_t max_res = 0;
                CFStringRef best_name = NULL;
                for (int32_t s = 0; s < state_count; s++) {
                    int64_t res = ior_StateGetResidency(entry, s) - ior_StateGetResidency(entry1, s);
                    if (res > max_res) {
                        max_res = res;
                        best_name = ior_StateGetNameForIndex(entry, s);
                    }
                }
                if (best_name) {
                    PyObject *v = cfstr_to_pystr(best_name);
                    PyDict_SetItemString(result, "active_state", v);
                    Py_DECREF(v);
                }
            }

            /* PPM power limit (delta residency) */
            if (fmt == kIOReportFormatState && cfstr_eq(name, "GPU_PPM")) {
                int32_t state_count = ior_StateGetCount(entry);
                int64_t max_res = 0;
                CFStringRef best_name = NULL;
                for (int32_t s = 0; s < state_count; s++) {
                    int64_t res = ior_StateGetResidency(entry, s) - ior_StateGetResidency(entry1, s);
                    if (res > max_res) {
                        max_res = res;
                        best_name = ior_StateGetNameForIndex(entry, s);
                    }
                }
                if (best_name) {
                    char buf[32];
                    if (CFStringGetCString(best_name, buf, sizeof(buf), kCFStringEncodingUTF8)) {
                        int pct = 100;
                        sscanf(buf, "%d%%", &pct);
                        PyObject *v = PyLong_FromLong(pct);
                        PyDict_SetItemString(result, "power_limit_pct", v);
                        Py_DECREF(v);
                    }
                }
            }
        }
    }

    /* Parse temperatures from "TgXa Max" channels (absolute from s2).
     * IOReport provides Sum, Sum Count, Min, Max per sensor.
     * We use "Max" as the instantaneous reading. */
    {
        for (int i = 0; i < n_pairs; i++) {
            CFDictionaryRef entry = pairs[i].ch;
            CFStringRef name = ior_ChannelGetChannelName(entry);
            int32_t fmt = ior_ChannelGetFormat(entry);

            if (!name || fmt != kIOReportFormatSimple) continue;

            char name_buf[128];
            if (!CFStringGetCString(name, name_buf, sizeof(name_buf), kCFStringEncodingUTF8))
                continue;

            /* Match "TgXXa Max" pattern */
            if (strncmp(name_buf, "Tg", 2) != 0) continue;
            size_t nlen = strlen(name_buf);
            /* Check for " Max" suffix */
            if (nlen < 7) continue;
            if (strcmp(name_buf + nlen - 4, " Max") != 0) continue;
            name_buf[nlen - 4] = '\0';  /* truncate " Max" */

            int64_t raw = ior_SimpleGetIntegerValue(entry, NULL);
            /* Temperature value scale depends on kernel version.
             * Values >10000 are likely milli-degrees, >1000 centi-degrees. */
            double temp_c;
            if (raw > 100000)
                temp_c = (double)raw / 1000.0;  /* milli-degrees */
            else if (raw > 10000)
                temp_c = (double)raw / 100.0;   /* centi-degrees */
            else
                temp_c = (double)raw;            /* already Celsius */

            if (temp_c > 0 && temp_c < 200) {
                PyObject *v = PyFloat_FromDouble(temp_c);
                PyDict_SetItemString(sensors_dict, name_buf, v);
                Py_DECREF(v);
                temp_sum += (long long)(temp_c * 100);
                temp_count++;
            }
        }
    }

    /* Add temperature data */
    if (temp_count > 0) {
        double avg = (double)temp_sum / (temp_count * 100);
        PyObject *v = PyFloat_FromDouble(avg);
        PyDict_SetItemString(temps_dict, "avg", v);
        Py_DECREF(v);
    }
    PyDict_SetItemString(temps_dict, "sensors", sensors_dict);
    Py_DECREF(sensors_dict);
    PyDict_SetItemString(result, "temperatures", temps_dict);
    Py_DECREF(temps_dict);

    /* Add frequency states */
    PyDict_SetItemString(result, "frequency_states", freq_list);
    Py_DECREF(freq_list);

    /* Cleanup */
    free(pairs);
    CFRelease(s1_keep);
    CFRelease(s2);
    CFRelease(channels);
    CFRelease(energy_group);
    CFRelease(gpu_group);

    return result;
}


/* ------------------------------------------------------------------ */
/* system_stats() — system-wide CPU + memory via Mach host APIs        */
/* ------------------------------------------------------------------ */

PyDoc_STRVAR(system_stats_doc,
"system_stats() -> dict\n\n"
"Return system-wide CPU and memory statistics via Mach host APIs.\n\n"
"No subprocess calls, no sudo, no psutil. Uses the same Mach APIs\n"
"that Activity Monitor uses internally.\n\n"
"Returns dict with keys:\n"
"    - 'memory_total': int — total physical memory in bytes\n"
"    - 'memory_used': int — active + wired memory in bytes\n"
"    - 'memory_available': int — free + inactive + speculative in bytes\n"
"    - 'memory_active': int — active pages in bytes\n"
"    - 'memory_inactive': int — inactive pages in bytes\n"
"    - 'memory_wired': int — wired (non-pageable) in bytes\n"
"    - 'memory_free': int — free pages in bytes\n"
"    - 'memory_compressed': int — compressed pages in bytes\n"
"    - 'cpu_count': int — logical CPU core count\n"
"    - 'cpu_user_pct': float — user CPU percent (since boot)\n"
"    - 'cpu_system_pct': float — system CPU percent (since boot)\n"
"    - 'cpu_idle_pct': float — idle CPU percent (since boot)\n"
"    - 'cpu_name': str — CPU brand string\n"
);

static PyObject* py_system_stats(PyObject* self, PyObject* args) {
    (void)self; (void)args;

    PyObject *result = PyDict_New();
    if (!result) return NULL;

    /* --- Physical memory total via sysctl --- */
    uint64_t mem_total = 0;
    size_t mem_size = sizeof(mem_total);
    if (sysctlbyname("hw.memsize", &mem_total, &mem_size, NULL, 0) == 0) {
        PyObject *v = PyLong_FromUnsignedLongLong(mem_total);
        PyDict_SetItemString(result, "memory_total", v);
        Py_DECREF(v);
    }

    /* --- VM statistics via host_statistics64 --- */
    mach_port_t host = mach_host_self();
    vm_size_t page_size = 0;
    host_page_size(host, &page_size);

    vm_statistics64_data_t vm_stat;
    mach_msg_type_number_t count = HOST_VM_INFO64_COUNT;
    if (host_statistics64(host, HOST_VM_INFO64, (host_info64_t)&vm_stat, &count) == KERN_SUCCESS) {
        uint64_t active    = (uint64_t)vm_stat.active_count * page_size;
        uint64_t inactive  = (uint64_t)vm_stat.inactive_count * page_size;
        uint64_t wired     = (uint64_t)vm_stat.wire_count * page_size;
        uint64_t free_mem  = (uint64_t)vm_stat.free_count * page_size;
        uint64_t speculative = (uint64_t)vm_stat.speculative_count * page_size;
        uint64_t compressed = (uint64_t)vm_stat.compressor_page_count * page_size;
        uint64_t used      = active + wired + compressed;
        uint64_t available = free_mem + inactive + speculative;

        PyObject *v;
        v = PyLong_FromUnsignedLongLong(used); PyDict_SetItemString(result, "memory_used", v); Py_DECREF(v);
        v = PyLong_FromUnsignedLongLong(available); PyDict_SetItemString(result, "memory_available", v); Py_DECREF(v);
        v = PyLong_FromUnsignedLongLong(active); PyDict_SetItemString(result, "memory_active", v); Py_DECREF(v);
        v = PyLong_FromUnsignedLongLong(inactive); PyDict_SetItemString(result, "memory_inactive", v); Py_DECREF(v);
        v = PyLong_FromUnsignedLongLong(wired); PyDict_SetItemString(result, "memory_wired", v); Py_DECREF(v);
        v = PyLong_FromUnsignedLongLong(free_mem); PyDict_SetItemString(result, "memory_free", v); Py_DECREF(v);
        v = PyLong_FromUnsignedLongLong(compressed); PyDict_SetItemString(result, "memory_compressed", v); Py_DECREF(v);
    }

    /* --- CPU load via host_statistics (ticks since boot) --- */
    host_cpu_load_info_data_t cpu_load;
    count = HOST_CPU_LOAD_INFO_COUNT;
    if (host_statistics(host, HOST_CPU_LOAD_INFO, (host_info_t)&cpu_load, &count) == KERN_SUCCESS) {
        uint64_t user   = cpu_load.cpu_ticks[CPU_STATE_USER] + cpu_load.cpu_ticks[CPU_STATE_NICE];
        uint64_t sys    = cpu_load.cpu_ticks[CPU_STATE_SYSTEM];
        uint64_t idle   = cpu_load.cpu_ticks[CPU_STATE_IDLE];
        uint64_t total  = user + sys + idle;
        if (total > 0) {
            PyObject *v;
            v = PyFloat_FromDouble(100.0 * user / total); PyDict_SetItemString(result, "cpu_user_pct", v); Py_DECREF(v);
            v = PyFloat_FromDouble(100.0 * sys / total); PyDict_SetItemString(result, "cpu_system_pct", v); Py_DECREF(v);
            v = PyFloat_FromDouble(100.0 * idle / total); PyDict_SetItemString(result, "cpu_idle_pct", v); Py_DECREF(v);
        }
        /* Raw ticks for delta computation (instant CPU% between polls) */
        PyObject *v;
        v = PyLong_FromUnsignedLongLong(user); PyDict_SetItemString(result, "cpu_ticks_user", v); Py_DECREF(v);
        v = PyLong_FromUnsignedLongLong(sys); PyDict_SetItemString(result, "cpu_ticks_system", v); Py_DECREF(v);
        v = PyLong_FromUnsignedLongLong(idle); PyDict_SetItemString(result, "cpu_ticks_idle", v); Py_DECREF(v);
    }

    /* --- CPU count --- */
    int cpu_count = 0;
    size_t cpu_size = sizeof(cpu_count);
    if (sysctlbyname("hw.logicalcpu", &cpu_count, &cpu_size, NULL, 0) == 0) {
        PyObject *v = PyLong_FromLong(cpu_count);
        PyDict_SetItemString(result, "cpu_count", v);
        Py_DECREF(v);
    }

    /* --- CPU brand string --- */
    char cpu_brand[256] = {0};
    size_t brand_size = sizeof(cpu_brand);
    if (sysctlbyname("machdep.cpu.brand_string", cpu_brand, &brand_size, NULL, 0) == 0) {
        PyObject *v = PyUnicode_FromString(cpu_brand);
        PyDict_SetItemString(result, "cpu_name", v);
        Py_DECREF(v);
    }

    return result;
}


/* ------------------------------------------------------------------ */
/* Module definition                                                   */
/* ------------------------------------------------------------------ */

static PyMethodDef methods[] = {
    {"gpu_time_ns",       py_gpu_time_ns,       METH_VARARGS, gpu_time_ns_doc},
    {"gpu_time_ns_multi", py_gpu_time_ns_multi, METH_VARARGS, gpu_time_ns_multi_doc},
    {"gpu_clients",       py_gpu_clients,       METH_NOARGS,  gpu_clients_doc},
    {"cpu_time_ns",       py_cpu_time_ns,       METH_VARARGS, cpu_time_ns_doc},
    {"proc_info",         py_proc_info,         METH_VARARGS, proc_info_doc},
    {"system_gpu_stats",  py_system_gpu_stats,  METH_NOARGS,  system_gpu_stats_doc},
    {"ppid",              py_ppid,              METH_VARARGS, ppid_doc},
    {"gpu_power",         py_gpu_power,         METH_VARARGS, gpu_power_doc},
    {"gpu_freq_table",    py_gpu_freq_table,    METH_NOARGS,  gpu_freq_table_doc},
    {"system_stats",      py_system_stats,      METH_NOARGS,  system_stats_doc},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef module_def = {
    PyModuleDef_HEAD_INIT,
    "_native",
    "Per-process GPU time via IORegistry AGXDeviceUserClient on macOS Apple Silicon.",
    -1,
    methods
};

PyMODINIT_FUNC PyInit__native(void) {
    return PyModule_Create(&module_def);
}
