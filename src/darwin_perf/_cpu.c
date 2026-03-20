/**
 * darwin-perf: CPU time and CPU power/frequency/residency via IOReport.
 *
 * Copyright 2026 Adam Mikulis. MIT License.
 */

#include "_native.h"
#include "_ioreport.h"

/* ------------------------------------------------------------------ */
/* Python API: cpu_time_ns                                             */
/* ------------------------------------------------------------------ */

const char cpu_time_ns_doc[] =
"cpu_time_ns(pid) -> int\n\n"
"Return cumulative CPU time (user + system) in nanoseconds for a process.\n\n"
"Uses proc_pid_rusage (RUSAGE_INFO_V2). No special privileges needed\n"
"for processes owned by the same user.\n\n"
"Args:\n"
"    pid: Process ID.\n\n"
"Returns:\n"
"    Cumulative CPU nanoseconds, or -1 on error.";

PyObject* py_cpu_time_ns(PyObject* self, PyObject* args) {
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

/* ------------------------------------------------------------------ */
/* Python API: cpu_power                                               */
/* ------------------------------------------------------------------ */

const char cpu_power_doc[] =
"cpu_power(interval=1.0) -> dict\n\n"
"Sample CPU power, frequency, and per-cluster P-state residency via IOReport.\n\n"
"Takes two IOReport samples separated by ``interval`` seconds and\n"
"returns the delta. No sudo or special privileges needed.\n\n"
"Args:\n"
"    interval: Sampling interval in seconds (default 1.0).\n\n"
"Returns dict with keys:\n"
"    - 'cpu_power_w': float -- CPU package power in watts\n"
"    - 'cpu_energy_nj': int -- CPU energy delta in nanojoules\n"
"    - 'clusters': dict -- per-cluster frequency state data\n"
"        e.g. {'ECPU': {'freq_mhz': 1020, 'frequency_states': [...], 'active_pct': 85.2}, ...}\n\n"
"Returns empty dict if libIOReport is unavailable.";

PyObject* py_cpu_power(PyObject* self, PyObject* args) {
    double interval = 1.0;
    if (!PyArg_ParseTuple(args, "|d", &interval))
        return NULL;

    if (load_ioreport() < 0)
        return PyDict_New();

    /* Get channels for Energy Model + CPU Stats */
    CFStringRef energy_group = CFStringCreateWithCString(kCFAllocatorDefault,
        "Energy Model", kCFStringEncodingUTF8);
    CFStringRef cpu_group = CFStringCreateWithCString(kCFAllocatorDefault,
        "CPU Stats", kCFStringEncodingUTF8);

    CFMutableDictionaryRef channels = (CFMutableDictionaryRef)
        ior_CopyChannelsInGroup(energy_group, NULL);
    CFDictionaryRef cpu_channels = ior_CopyChannelsInGroup(cpu_group, NULL);

    if (!channels || !cpu_channels) {
        if (channels) CFRelease(channels);
        if (cpu_channels) CFRelease(cpu_channels);
        CFRelease(energy_group);
        CFRelease(cpu_group);
        return PyDict_New();
    }

    ior_MergeChannels(channels, cpu_channels, NULL);
    CFRelease(cpu_channels);

    /* Subscribe and take two samples */
    CFMutableDictionaryRef subbed = NULL;
    CFTypeRef sub = ior_CreateSubscription(NULL, channels, &subbed, 0, NULL);
    if (!sub || !subbed) {
        CFRelease(channels);
        CFRelease(energy_group);
        CFRelease(cpu_group);
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
        CFRelease(cpu_group);
        return PyDict_New();
    }

    PyObject *result = PyDict_New();
    if (!result) {
        CFRelease(s1);
        CFRelease(s2);
        CFRelease(channels);
        CFRelease(energy_group);
        CFRelease(cpu_group);
        return NULL;
    }

    /* Extract channel pairs using shared helper */
    channel_pair_t *pairs = NULL;
    int n_pairs = extract_channel_pairs(s1, s2, &pairs);

    /* ---- First pass: discover ECPU/PCPU active state counts ---- */
    int ecpu_active_states = 0, pcpu_active_states = 0;
    for (int i = 0; i < n_pairs; i++) {
        CFDictionaryRef entry = pairs[i].ch;
        CFStringRef name = ior_ChannelGetChannelName(entry);
        int32_t fmt = ior_ChannelGetFormat(entry);
        if (!name || fmt != kIOReportFormatState) continue;

        int is_ecpu = cfstr_eq(name, "ECPU");
        int is_pcpu = cfstr_eq(name, "PCPU");
        if (!is_ecpu && !is_pcpu) continue;
        if (is_ecpu && ecpu_active_states > 0) continue;
        if (is_pcpu && pcpu_active_states > 0) continue;

        int32_t sc = ior_StateGetCount(entry);
        int active = 0;
        for (int32_t s = 0; s < sc; s++) {
            CFStringRef sn = ior_StateGetNameForIndex(entry, s);
            if (sn && !cfstr_eq(sn, "OFF") && !cfstr_eq(sn, "IDLE"))
                active++;
        }
        if (is_ecpu) ecpu_active_states = active;
        else         pcpu_active_states = active;
    }

    /* ---- Read CPU DVFS frequency tables from pmgr ---- */
    long ecpu_mhz[MAX_PSTATES], pcpu_mhz[MAX_PSTATES];
    int ecpu_count = 0, pcpu_count = 0;
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
                    /* P-core table: voltage-states8 */
                    CFDataRef pdata = IORegistryEntryCreateCFProperty(
                        svc, CFSTR("voltage-states8"), kCFAllocatorDefault, 0);
                    if (pdata && CFGetTypeID(pdata) == CFDataGetTypeID()) {
                        CFIndex len = CFDataGetLength(pdata);
                        const uint8_t *p = CFDataGetBytePtr(pdata);
                        for (CFIndex off = 0; off + 7 < len && pcpu_count < MAX_PSTATES; off += 8) {
                            uint32_t fhz;
                            memcpy(&fhz, p + off, 4);
                            if (fhz > 0)
                                pcpu_mhz[pcpu_count++] = fhz / 1000000;
                        }
                    }
                    if (pdata) CFRelease(pdata);

                    /* E-core table: scan voltage-states0..31 (skip 8=P, 9=GPU) */
                    if (ecpu_active_states > 0) {
                        for (int idx = 0; idx < 32 && ecpu_count == 0; idx++) {
                            if (idx == 8 || idx == 9) continue;
                            char prop[32];
                            snprintf(prop, sizeof(prop), "voltage-states%d", idx);
                            CFStringRef key = CFStringCreateWithCString(
                                kCFAllocatorDefault, prop, kCFStringEncodingUTF8);
                            CFDataRef edata = IORegistryEntryCreateCFProperty(
                                svc, key, kCFAllocatorDefault, 0);
                            CFRelease(key);
                            if (!edata) continue;
                            if (CFGetTypeID(edata) != CFDataGetTypeID()) {
                                CFRelease(edata);
                                continue;
                            }
                            CFIndex len = CFDataGetLength(edata);
                            const uint8_t *p = CFDataGetBytePtr(edata);
                            long tmp_mhz[MAX_PSTATES];
                            int tmp_count = 0;
                            for (CFIndex off = 0; off + 7 < len && tmp_count < MAX_PSTATES; off += 8) {
                                uint32_t fhz;
                                memcpy(&fhz, p + off, 4);
                                long mhz = fhz / 1000000;
                                if (mhz >= 100)
                                    tmp_mhz[tmp_count++] = mhz;
                            }
                            CFRelease(edata);
                            if (tmp_count == ecpu_active_states
                                && tmp_count > 0
                                && (pcpu_count == 0 || tmp_mhz[0] < pcpu_mhz[0])) {
                                memcpy(ecpu_mhz, tmp_mhz, tmp_count * sizeof(long));
                                ecpu_count = tmp_count;
                            }
                        }
                    }

                    IOObjectRelease(svc);
                    break;
                }
                IOObjectRelease(svc);
            }
            IOObjectRelease(dvfs_iter);
        }
    }

    /* ---- Second pass: parse CPU energy + cluster residency ---- */
    PyObject *clusters = PyDict_New();

    for (int i = 0; i < n_pairs; i++) {
        CFDictionaryRef entry = pairs[i].ch;
        CFDictionaryRef entry1 = pairs[i].ch1;
        CFStringRef name = ior_ChannelGetChannelName(entry);
        int32_t fmt = ior_ChannelGetFormat(entry);

        if (!name) continue;

        /* CPU Energy (Simple, delta nanojoules) */
        if (fmt == kIOReportFormatSimple && cfstr_eq(name, "CPU Energy")) {
            int64_t e2 = ior_SimpleGetIntegerValue(entry, NULL);
            int64_t e1 = ior_SimpleGetIntegerValue(entry1, NULL);
            int64_t energy_nj = e2 - e1;
            double watts = (double)energy_nj / (interval * 1e9);

            PyObject *v;
            v = PyFloat_FromDouble(watts);
            PyDict_SetItemString(result, "cpu_power_w", v);
            Py_DECREF(v);
            v = PyLong_FromLongLong(energy_nj);
            PyDict_SetItemString(result, "cpu_energy_nj", v);
            Py_DECREF(v);
        }

        /* ECPU / PCPU P-state residency (State channels) */
        if (fmt == kIOReportFormatState) {
            char nbuf[64];
            if (!CFStringGetCString(name, nbuf, sizeof(nbuf), kCFStringEncodingUTF8))
                continue;

            int is_ecpu = (strcmp(nbuf, "ECPU") == 0);
            int is_pcpu = (strcmp(nbuf, "PCPU") == 0);
            if (!is_ecpu && !is_pcpu) continue;

            const char *cluster_name = is_ecpu ? "ECPU" : "PCPU";
            long *dvfs = is_ecpu ? ecpu_mhz : pcpu_mhz;
            int dvfs_cnt = is_ecpu ? ecpu_count : pcpu_count;

            PyObject *existing = PyDict_GetItemString(clusters, cluster_name);
            if (existing) continue;

            int32_t state_count = ior_StateGetCount(entry);
            int64_t total_res = 0;
            for (int32_t s = 0; s < state_count; s++) {
                int64_t r2 = ior_StateGetResidency(entry, s);
                int64_t r1 = ior_StateGetResidency(entry1, s);
                total_res += (r2 - r1);
            }

            PyObject *freq_states = PyList_New(0);
            double weighted_freq = 0;
            double active_pct = 0;

            for (int32_t s = 0; s < state_count; s++) {
                CFStringRef sname = ior_StateGetNameForIndex(entry, s);
                int64_t r2 = ior_StateGetResidency(entry, s);
                int64_t r1 = ior_StateGetResidency(entry1, s);
                int64_t res = r2 - r1;
                if (res <= 0 || !sname) continue;

                double pct = total_res > 0 ? (double)res / total_res * 100.0 : 0;
                if (cfstr_eq(sname, "OFF") || cfstr_eq(sname, "IDLE")) continue;

                char sbuf[16];
                long freq = 0;
                if (CFStringGetCString(sname, sbuf, sizeof(sbuf), kCFStringEncodingUTF8)) {
                    if (sbuf[0] == 'P') {
                        int pindex = atoi(sbuf + 1);
                        if (pindex >= 1 && pindex <= dvfs_cnt)
                            freq = dvfs[pindex - 1];
                    } else if (sbuf[0] == 'V') {
                        int vindex = atoi(sbuf + 1);
                        if (vindex >= 0 && vindex < dvfs_cnt)
                            freq = dvfs[vindex];
                    }
                }

                active_pct += pct;
                if (freq > 0)
                    weighted_freq += freq * (pct / 100.0);

                PyObject *sd = PyDict_New();
                PyObject *v;
                v = cfstr_to_pystr(sname);
                PyDict_SetItemString(sd, "state", v);
                Py_DECREF(v);
                v = PyFloat_FromDouble(pct);
                PyDict_SetItemString(sd, "residency_pct", v);
                Py_DECREF(v);
                if (freq > 0) {
                    v = PyLong_FromLong(freq);
                    PyDict_SetItemString(sd, "freq_mhz", v);
                    Py_DECREF(v);
                }
                PyList_Append(freq_states, sd);
                Py_DECREF(sd);
            }

            PyObject *cluster_dict = PyDict_New();
            PyDict_SetItemString(cluster_dict, "frequency_states", freq_states);
            Py_DECREF(freq_states);

            if (active_pct > 0) {
                double avg_freq = weighted_freq / (active_pct / 100.0);
                PyObject *v = PyLong_FromLong((long)avg_freq);
                PyDict_SetItemString(cluster_dict, "freq_mhz", v);
                Py_DECREF(v);
            }

            {
                PyObject *v = PyFloat_FromDouble(active_pct);
                PyDict_SetItemString(cluster_dict, "active_pct", v);
                Py_DECREF(v);
            }

            PyDict_SetItemString(clusters, cluster_name, cluster_dict);
            Py_DECREF(cluster_dict);
        }
    }

    PyDict_SetItemString(result, "clusters", clusters);
    Py_DECREF(clusters);

    /* Cleanup */
    free(pairs);
    CFRelease(s1);
    CFRelease(s2);
    CFRelease(channels);
    CFRelease(energy_group);
    CFRelease(cpu_group);

    return result;
}
