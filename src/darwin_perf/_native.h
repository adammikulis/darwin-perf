/**
 * darwin-perf: Shared header for the split C extension modules.
 *
 * Copyright 2026 Adam Mikulis. MIT License.
 */

#ifndef DARWIN_PERF_NATIVE_H
#define DARWIN_PERF_NATIVE_H

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
/* Shared GPU client type and helper (defined in _gpu.c)               */
/* ------------------------------------------------------------------ */

typedef struct {
    int pid;
    char name[128];
    char api[16];
    long long gpu_ns;
} gpu_client_t;

int read_gpu_clients(gpu_client_t **out_clients);

/* ------------------------------------------------------------------ */
/* Forward declarations for all py_* functions                         */
/* ------------------------------------------------------------------ */

/* _gpu.c */
PyObject* py_gpu_time_ns(PyObject*, PyObject*);
PyObject* py_gpu_time_ns_multi(PyObject*, PyObject*);
PyObject* py_gpu_clients(PyObject*, PyObject*);
PyObject* py_system_gpu_stats(PyObject*, PyObject*);
PyObject* py_gpu_power(PyObject*, PyObject*);
PyObject* py_gpu_freq_table(PyObject*, PyObject*);

/* _cpu.c */
PyObject* py_cpu_time_ns(PyObject*, PyObject*);
PyObject* py_cpu_power(PyObject*, PyObject*);

/* _memory.c */
PyObject* py_system_stats(PyObject*, PyObject*);

/* _proc.c */
PyObject* py_proc_info(PyObject*, PyObject*);
PyObject* py_ppid(PyObject*, PyObject*);
PyObject* py_proc_connections(PyObject*, PyObject*);
PyObject* py_proc_lineage(PyObject*, PyObject*);
PyObject* py_proc_open_files(PyObject*, PyObject*);
PyObject* py_proc_pidpath(PyObject*, PyObject*);

/* _net.c */
PyObject* py_net_io_counters(PyObject*, PyObject*);
PyObject* py_net_io_per_iface(PyObject*, PyObject*);

/* _sensors.c */
PyObject* py_temperatures(PyObject*, PyObject*);
PyObject* py_hid_idle_ns(PyObject*, PyObject*);

/* ------------------------------------------------------------------ */
/* PyDoc strings (extern, defined in each .c file)                     */
/* ------------------------------------------------------------------ */

extern const char gpu_time_ns_doc[];
extern const char gpu_time_ns_multi_doc[];
extern const char gpu_clients_doc[];
extern const char system_gpu_stats_doc[];
extern const char gpu_power_doc[];
extern const char gpu_freq_table_doc[];
extern const char cpu_time_ns_doc[];
extern const char cpu_power_doc[];
extern const char system_stats_doc[];
extern const char proc_info_doc[];
extern const char ppid_doc[];
extern const char proc_connections_doc[];
extern const char proc_lineage_doc[];
extern const char proc_open_files_doc[];
extern const char proc_pidpath_doc[];
extern const char net_io_counters_doc[];
extern const char net_io_per_iface_doc[];
extern const char temperatures_doc[];
extern const char hid_idle_ns_doc[];

#endif /* DARWIN_PERF_NATIVE_H */
