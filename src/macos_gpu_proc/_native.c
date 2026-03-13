/**
 * macos-gpu-proc: Per-process GPU utilization on macOS via Mach APIs.
 *
 * Uses task_info(TASK_POWER_INFO_V2) to read task_gpu_utilisation —
 * a cumulative GPU time counter in nanoseconds maintained by the kernel
 * for every process. This is the same data source Activity Monitor uses.
 *
 * Two access modes:
 *   - Own process (pid=0): no privileges needed
 *   - Other processes: requires task_for_pid privilege (sudo, or
 *     com.apple.system-task-ports entitlement)
 *
 * Copyright 2026 Adam Mikulis. MIT License.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <mach/mach.h>
#include <mach/task_info.h>
#include <unistd.h>

/* ------------------------------------------------------------------ */
/* Core: read cumulative GPU nanoseconds for a single task port       */
/* ------------------------------------------------------------------ */

static long long read_gpu_ns(mach_port_t task) {
    struct task_power_info_v2 info;
    mach_msg_type_number_t count = TASK_POWER_INFO_V2_COUNT;
    kern_return_t kr = task_info(task, TASK_POWER_INFO_V2,
                                (task_info_t)&info, &count);
    if (kr != KERN_SUCCESS)
        return -1;
    return (long long)info.gpu_energy.task_gpu_utilisation;
}

/* ------------------------------------------------------------------ */
/* Python API                                                         */
/* ------------------------------------------------------------------ */

PyDoc_STRVAR(gpu_time_ns_doc,
"gpu_time_ns(pid=0) -> int\n\n"
"Return cumulative GPU utilization time in nanoseconds for a process.\n\n"
"Args:\n"
"    pid: Process ID. 0 (default) means the calling process.\n\n"
"Returns:\n"
"    Cumulative GPU nanoseconds (monotonically increasing), or\n"
"    -1 if the process cannot be queried (permission denied).\n\n"
"Note:\n"
"    Reading your own process (pid=0) requires no special privileges.\n"
"    Reading other processes requires sudo or the\n"
"    com.apple.system-task-ports entitlement.");

static PyObject* py_gpu_time_ns(PyObject* self, PyObject* args) {
    int pid = 0;
    if (!PyArg_ParseTuple(args, "|i", &pid))
        return NULL;

    mach_port_t task;
    int need_dealloc = 0;

    if (pid == 0 || pid == getpid()) {
        task = mach_task_self();
    } else {
        kern_return_t kr = task_for_pid(mach_task_self(), pid, &task);
        if (kr != KERN_SUCCESS)
            return PyLong_FromLongLong(-1);
        need_dealloc = 1;
    }

    long long ns = read_gpu_ns(task);

    if (need_dealloc)
        mach_port_deallocate(mach_task_self(), task);

    return PyLong_FromLongLong(ns);
}


PyDoc_STRVAR(gpu_time_ns_multi_doc,
"gpu_time_ns_multi(pids: list[int]) -> dict[int, int]\n\n"
"Batch read GPU nanoseconds for multiple PIDs in one call.\n\n"
"Args:\n"
"    pids: List of process IDs. Use 0 for the calling process.\n\n"
"Returns:\n"
"    Dict mapping each PID to its cumulative GPU nanoseconds.\n"
"    PIDs that cannot be queried map to -1.");

static PyObject* py_gpu_time_ns_multi(PyObject* self, PyObject* args) {
    PyObject* pid_list;
    if (!PyArg_ParseTuple(args, "O!", &PyList_Type, &pid_list))
        return NULL;

    Py_ssize_t n = PyList_GET_SIZE(pid_list);
    PyObject* result = PyDict_New();
    if (!result) return NULL;

    pid_t my_pid = getpid();

    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject* item = PyList_GET_ITEM(pid_list, i);
        long pid_long = PyLong_AsLong(item);
        if (pid_long == -1 && PyErr_Occurred()) {
            Py_DECREF(result);
            return NULL;
        }
        int pid = (int)pid_long;

        mach_port_t task;
        int need_dealloc = 0;

        if (pid == 0 || pid == my_pid) {
            task = mach_task_self();
        } else {
            kern_return_t kr = task_for_pid(mach_task_self(), pid, &task);
            if (kr != KERN_SUCCESS) {
                PyObject* val = PyLong_FromLongLong(-1);
                if (PyDict_SetItem(result, item, val) < 0) {
                    Py_DECREF(val);
                    Py_DECREF(result);
                    return NULL;
                }
                Py_DECREF(val);
                continue;
            }
            need_dealloc = 1;
        }

        long long ns = read_gpu_ns(task);
        if (need_dealloc)
            mach_port_deallocate(mach_task_self(), task);

        PyObject* val = PyLong_FromLongLong(ns);
        if (PyDict_SetItem(result, item, val) < 0) {
            Py_DECREF(val);
            Py_DECREF(result);
            return NULL;
        }
        Py_DECREF(val);
    }

    return result;
}


/* ------------------------------------------------------------------ */
/* Module definition                                                  */
/* ------------------------------------------------------------------ */

static PyMethodDef methods[] = {
    {"gpu_time_ns",       py_gpu_time_ns,       METH_VARARGS, gpu_time_ns_doc},
    {"gpu_time_ns_multi", py_gpu_time_ns_multi, METH_VARARGS, gpu_time_ns_multi_doc},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef module_def = {
    PyModuleDef_HEAD_INIT,
    "_native",
    "Low-level Mach task_info bindings for per-process GPU time on macOS.",
    -1,
    methods
};

PyMODINIT_FUNC PyInit__native(void) {
    return PyModule_Create(&module_def);
}
