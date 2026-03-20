/**
 * darwin-perf: System-wide CPU + memory stats via Mach host APIs.
 *
 * Copyright 2026 Adam Mikulis. MIT License.
 */

#include "_native.h"

/* ------------------------------------------------------------------ */
/* Python API: system_stats                                            */
/* ------------------------------------------------------------------ */

const char system_stats_doc[] =
"system_stats() -> dict\n\n"
"Return system-wide CPU and memory statistics via Mach host APIs.\n\n"
"No subprocess calls, no sudo, no psutil. Uses the same Mach APIs\n"
"that Activity Monitor uses internally.\n\n"
"Returns dict with keys:\n"
"    - 'memory_total': int -- total physical memory in bytes\n"
"    - 'memory_used': int -- active + wired memory in bytes\n"
"    - 'memory_available': int -- free + inactive + speculative in bytes\n"
"    - 'memory_active': int -- active pages in bytes\n"
"    - 'memory_inactive': int -- inactive pages in bytes\n"
"    - 'memory_wired': int -- wired (non-pageable) in bytes\n"
"    - 'memory_free': int -- free pages in bytes\n"
"    - 'memory_compressed': int -- compressed pages in bytes\n"
"    - 'cpu_count': int -- logical CPU core count\n"
"    - 'cpu_user_pct': float -- user CPU percent (since boot)\n"
"    - 'cpu_system_pct': float -- system CPU percent (since boot)\n"
"    - 'cpu_idle_pct': float -- idle CPU percent (since boot)\n"
"    - 'cpu_name': str -- CPU brand string\n"
"    - 'per_core': list[dict] -- per-core CPU ticks and percentages\n"
"        Each dict: core, user_pct, system_pct, idle_pct, active_pct,\n"
"                   ticks_user, ticks_system, ticks_idle\n";

PyObject* py_system_stats(PyObject* self, PyObject* args) {
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

    /* --- Per-core CPU ticks via host_processor_info --- */
    natural_t num_cpus = 0;
    processor_info_array_t cpu_info = NULL;
    mach_msg_type_number_t num_cpu_info = 0;
    kern_return_t kr = host_processor_info(host, PROCESSOR_CPU_LOAD_INFO,
                                            &num_cpus, &cpu_info, &num_cpu_info);
    if (kr == KERN_SUCCESS && cpu_info != NULL) {
        PyObject *per_core = PyList_New(num_cpus);
        for (natural_t i = 0; i < num_cpus; i++) {
            integer_t *ticks = &cpu_info[i * CPU_STATE_MAX];
            uint64_t u = ticks[CPU_STATE_USER] + ticks[CPU_STATE_NICE];
            uint64_t s = ticks[CPU_STATE_SYSTEM];
            uint64_t d = ticks[CPU_STATE_IDLE];
            uint64_t t = u + s + d;
            PyObject *core_dict = PyDict_New();
            PyObject *cv;
            cv = PyLong_FromLong(i); PyDict_SetItemString(core_dict, "core", cv); Py_DECREF(cv);
            if (t > 0) {
                cv = PyFloat_FromDouble(100.0 * u / t); PyDict_SetItemString(core_dict, "user_pct", cv); Py_DECREF(cv);
                cv = PyFloat_FromDouble(100.0 * s / t); PyDict_SetItemString(core_dict, "system_pct", cv); Py_DECREF(cv);
                cv = PyFloat_FromDouble(100.0 * d / t); PyDict_SetItemString(core_dict, "idle_pct", cv); Py_DECREF(cv);
                cv = PyFloat_FromDouble(100.0 * (u + s) / t); PyDict_SetItemString(core_dict, "active_pct", cv); Py_DECREF(cv);
            }
            cv = PyLong_FromUnsignedLongLong(u); PyDict_SetItemString(core_dict, "ticks_user", cv); Py_DECREF(cv);
            cv = PyLong_FromUnsignedLongLong(s); PyDict_SetItemString(core_dict, "ticks_system", cv); Py_DECREF(cv);
            cv = PyLong_FromUnsignedLongLong(d); PyDict_SetItemString(core_dict, "ticks_idle", cv); Py_DECREF(cv);
            PyList_SetItem(per_core, i, core_dict);
        }
        PyDict_SetItemString(result, "per_core", per_core);
        Py_DECREF(per_core);
        vm_deallocate(mach_task_self(), (vm_address_t)cpu_info,
                      num_cpu_info * sizeof(integer_t));
    }

    return result;
}
