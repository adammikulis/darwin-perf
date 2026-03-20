/**
 * darwin-perf: GPU monitoring — IORegistry client enumeration, GPU stats,
 * power/frequency/throttle via IOReport, DVFS frequency table.
 *
 * Copyright 2026 Adam Mikulis. MIT License.
 */

#include "_native.h"
#include "_ioreport.h"

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

static int parse_creator_pid(CFStringRef creator) {
    char buf[256];
    if (!CFStringGetCString(creator, buf, sizeof(buf), kCFStringEncodingUTF8))
        return -1;
    int pid = -1;
    if (sscanf(buf, "pid %d", &pid) == 1)
        return pid;
    return -1;
}

static int parse_creator_name(CFStringRef creator, char *out_name, size_t name_size) {
    char buf[256];
    if (!CFStringGetCString(creator, buf, sizeof(buf), kCFStringEncodingUTF8))
        return -1;
    char *comma = strchr(buf, ',');
    if (!comma || *(comma + 1) != ' ')
        return -1;
    strncpy(out_name, comma + 2, name_size - 1);
    out_name[name_size - 1] = '\0';
    return 0;
}

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

static int extract_api_name(CFArrayRef app_usage, char *out_api, size_t api_size) {
    if (!app_usage) return -1;
    CFIndex count = CFArrayGetCount(app_usage);
    for (CFIndex i = 0; i < count; i++) {
        CFDictionaryRef entry = CFArrayGetValueAtIndex(app_usage, i);
        if (!entry || CFGetTypeID(entry) != CFDictionaryGetTypeID())
            continue;
        CFStringRef api = CFDictionaryGetValue(entry, CFSTR("API"));
        if (api && CFGetTypeID(api) == CFStringGetTypeID()) {
            if (CFStringGetCString(api, out_api, api_size, kCFStringEncodingUTF8))
                return 0;
        }
    }
    return -1;
}

/* ------------------------------------------------------------------ */
/* read_gpu_clients — shared helper (declared in _native.h)            */
/* ------------------------------------------------------------------ */

int read_gpu_clients(gpu_client_t **out_clients) {
    *out_clients = NULL;

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

    io_service_t accel;
    while ((accel = IOIteratorNext(accel_iter)) != 0) {
        io_iterator_t child_iter;
        kr = IORegistryEntryGetChildIterator(accel, kIOServicePlane, &child_iter);
        IOObjectRelease(accel);
        if (kr != KERN_SUCCESS)
            continue;

        io_service_t child;
        while ((child = IOIteratorNext(child_iter)) != 0) {
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

            CFArrayRef app_usage = IORegistryEntryCreateCFProperty(
                child, CFSTR("AppUsage"),
                kCFAllocatorDefault, 0);

            long long gpu_ns = 0;
            if (app_usage && CFGetTypeID(app_usage) == CFArrayGetTypeID())
                gpu_ns = sum_app_usage_gpu_time(app_usage);

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
            clients[count].api[0] = '\0';
            if (app_usage)
                extract_api_name(app_usage, clients[count].api, sizeof(clients[count].api));
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
/* Python API: gpu_time_ns                                             */
/* ------------------------------------------------------------------ */

const char gpu_time_ns_doc[] =
"gpu_time_ns(pid=0) -> int\n\n"
"Return cumulative GPU time in nanoseconds for a process.\n\n"
"Reads accumulatedGPUTime from AGXDeviceUserClient entries in the\n"
"IORegistry. Multiple command queues for the same PID are summed.\n\n"
"Args:\n"
"    pid: Process ID. 0 means the calling process.\n\n"
"Returns:\n"
"    Cumulative GPU nanoseconds, or 0 if the process has no GPU clients.\n\n"
"Note:\n"
"    No special privileges required -- IORegistry is world-readable.";

PyObject* py_gpu_time_ns(PyObject* self, PyObject* args) {
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

/* ------------------------------------------------------------------ */
/* Python API: gpu_time_ns_multi                                       */
/* ------------------------------------------------------------------ */

const char gpu_time_ns_multi_doc[] =
"gpu_time_ns_multi(pids: list[int]) -> dict[int, int]\n\n"
"Batch read GPU nanoseconds for multiple PIDs in one IORegistry scan.\n\n"
"Args:\n"
"    pids: List of process IDs. Use 0 for the calling process.\n\n"
"Returns:\n"
"    Dict mapping each PID to its cumulative GPU nanoseconds.\n"
"    PIDs with no GPU clients map to 0.";

PyObject* py_gpu_time_ns_multi(PyObject* self, PyObject* args) {
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

/* ------------------------------------------------------------------ */
/* Python API: gpu_clients                                             */
/* ------------------------------------------------------------------ */

const char gpu_clients_doc[] =
"gpu_clients() -> list[dict]\n\n"
"Return all active GPU clients from the IORegistry.\n\n"
"Each dict has keys:\n"
"    - 'pid': int -- process ID\n"
"    - 'name': str -- process name (truncated)\n"
"    - 'gpu_ns': int -- cumulative GPU nanoseconds\n\n"
"Multiple entries may exist for the same PID (one per command queue).\n"
"No special privileges required.";

PyObject* py_gpu_clients(PyObject* self, PyObject* args) {
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
        PyObject* api_obj = PyUnicode_FromString(
            clients[i].api[0] ? clients[i].api : "unknown");

        PyDict_SetItemString(d, "pid", pid_obj);
        PyDict_SetItemString(d, "name", name_obj);
        PyDict_SetItemString(d, "gpu_ns", ns_obj);
        PyDict_SetItemString(d, "api", api_obj);

        Py_DECREF(pid_obj);
        Py_DECREF(name_obj);
        Py_DECREF(ns_obj);
        Py_DECREF(api_obj);

        PyList_SET_ITEM(list, i, d);
    }

    free(clients);
    return list;
}

/* ------------------------------------------------------------------ */
/* Python API: system_gpu_stats                                        */
/* ------------------------------------------------------------------ */

const char system_gpu_stats_doc[] =
"system_gpu_stats() -> dict\n\n"
"Return system-wide GPU performance statistics from the IORegistry.\n\n"
"Reads 'PerformanceStatistics' from the AGXAccelerator entry.\n\n"
"Returns dict with keys:\n"
"    - 'device_utilization': int -- Device Utilization %% (0-100)\n"
"    - 'tiler_utilization': int -- Tiler Utilization %%\n"
"    - 'renderer_utilization': int -- Renderer Utilization %%\n"
"    - 'alloc_system_memory': int -- total GPU-allocated system memory (bytes)\n"
"    - 'in_use_system_memory': int -- in-use GPU system memory (bytes)\n"
"    - 'in_use_system_memory_driver': int -- driver-side in-use memory\n"
"    - 'allocated_pb_size': int -- parameter buffer allocation (bytes)\n"
"    - 'recovery_count': int -- GPU recovery (crash) count\n"
"    - 'last_recovery_time': int -- timestamp of last GPU recovery\n"
"    - 'split_scene_count': int -- tiler split scene events\n"
"    - 'tiled_scene_bytes': int -- current tiled scene buffer size\n"
"    - 'model': str -- GPU model name\n"
"    - 'gpu_core_count': int -- number of GPU cores";

PyObject* py_system_gpu_stats(PyObject* self, PyObject* args) {
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
/* Python API: gpu_freq_table                                          */
/* ------------------------------------------------------------------ */

const char gpu_freq_table_doc[] =
"gpu_freq_table() -> list[int]\n\n"
"Return the GPU DVFS frequency table in MHz, one entry per P-state.\n\n"
"Reads 'voltage-states9' from the pmgr IOService. Index 0 = P1 frequency,\n"
"index 1 = P2, etc. Empty list if the table cannot be read.\n\n"
"Example::\n\n"
"    >>> gpu_freq_table()\n"
"    [338, 618, 796, 924, 952, 1056, 1062, 1182, 1182, 1312, 1242, 1380, 1326, 1470, 1578]\n";

PyObject* py_gpu_freq_table(PyObject* self, PyObject* args) {
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
/* Python API: gpu_power                                               */
/* ------------------------------------------------------------------ */

const char gpu_power_doc[] =
"gpu_power(interval=1.0) -> dict\n\n"
"Sample GPU power, temperature, and frequency state via IOReport.\n\n"
"Takes two IOReport samples separated by ``interval`` seconds and\n"
"returns the delta. No sudo or special privileges needed.\n\n"
"Args:\n"
"    interval: Sampling interval in seconds (default 1.0).\n\n"
"Returns dict with keys:\n"
"    - 'gpu_power_w': float -- GPU power draw in watts\n"
"    - 'gpu_energy_nj': int -- GPU energy delta in nanojoules\n"
"    - 'temperatures': dict -- GPU die sensor temperatures (deg C)\n"
"        e.g. {'avg': 42.1, 'sensors': {'Tg1a': 41, 'Tg5a': 43, ...}}\n"
"    - 'frequency_states': list -- P-state residency during interval\n"
"        e.g. [{'state': 'P1', 'residency_pct': 20.5}, ...]\n"
"    - 'active_state': str -- current GPU power state ('PERF' or 'IDLE_OFF')\n"
"    - 'throttled': bool -- whether GPU is thermally throttled\n"
"    - 'power_limit_pct': int -- PPM target as %% of max GPU power\n\n"
"Returns empty dict if libIOReport is unavailable.";

PyObject* py_gpu_power(PyObject* self, PyObject* args) {
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

    PyObject *result = PyDict_New();
    if (!result) {
        CFRelease(s1);
        CFRelease(s2);
        CFRelease(channels);
        CFRelease(energy_group);
        CFRelease(gpu_group);
        return NULL;
    }

    /* Extract channel pairs using shared helper */
    channel_pair_t *pairs = NULL;
    int n_pairs = extract_channel_pairs(s1, s2, &pairs);

    /* Frequency states list */
    PyObject *freq_list = PyList_New(0);

    /* Read GPU DVFS frequency table for MHz mapping */
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
            CFDictionaryRef entry = pairs[i].ch;
            CFDictionaryRef entry1 = pairs[i].ch1;
            CFStringRef name = ior_ChannelGetChannelName(entry);
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
                    if (cfstr_eq(sname, "OFF")) continue;

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

    /* Read temperatures via AppleSMC (instant, no IOReport) */
    {
        PyObject *temps = py_temperatures(NULL, NULL);
        if (temps) {
            PyDict_SetItemString(result, "temperatures", temps);
            Py_DECREF(temps);
        }
    }

    /* Add frequency states */
    PyDict_SetItemString(result, "frequency_states", freq_list);
    Py_DECREF(freq_list);

    /* Cleanup */
    free(pairs);
    CFRelease(s1);
    CFRelease(s2);
    CFRelease(channels);
    CFRelease(energy_group);
    CFRelease(gpu_group);

    return result;
}
