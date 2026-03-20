/**
 * darwin-perf: Module entry point.
 *
 * All implementation lives in domain-specific .c files.
 * This file only defines the method table and PyInit.
 *
 * Copyright 2026 Adam Mikulis. MIT License.
 */

#include "_native.h"

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
    {"cpu_power",         py_cpu_power,         METH_VARARGS, cpu_power_doc},
    {"temperatures",      py_temperatures,      METH_NOARGS,  temperatures_doc},
    {"net_io_counters",   py_net_io_counters,   METH_NOARGS,  net_io_counters_doc},
    {"proc_connections",  py_proc_connections,  METH_VARARGS, proc_connections_doc},
    {"proc_lineage",      py_proc_lineage,      METH_VARARGS, proc_lineage_doc},
    {"proc_open_files",   py_proc_open_files,   METH_VARARGS, proc_open_files_doc},
    {"proc_pidpath",      py_proc_pidpath,      METH_VARARGS, proc_pidpath_doc},
    {"net_io_per_iface",  py_net_io_per_iface,  METH_NOARGS,  net_io_per_iface_doc},
    {"hid_idle_ns",       py_hid_idle_ns,       METH_NOARGS,  hid_idle_ns_doc},
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
