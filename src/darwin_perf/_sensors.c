/**
 * darwin-perf: Thermal sensors (AppleSMC) and HID idle time.
 *
 * Copyright 2026 Adam Mikulis. MIT License.
 */

#include "_native.h"

/* ------------------------------------------------------------------ */
/* SMC data structures                                                 */
/* ------------------------------------------------------------------ */

typedef struct {
    char     major;
    char     minor;
    char     build;
    char     reserved[1];
    uint16_t release;
} smc_vers_t;

typedef struct {
    uint16_t version;
    uint16_t length;
    uint32_t cpuPLimit;
    uint32_t gpuPLimit;
    uint32_t memPLimit;
} smc_plimit_t;

typedef struct {
    uint32_t dataSize;
    uint32_t dataType;
    char     dataAttributes;
} smc_keyinfo_t;

typedef struct {
    uint32_t     key;
    smc_vers_t   vers;
    smc_plimit_t pLimitData;
    smc_keyinfo_t keyInfo;
    uint8_t      result;
    uint8_t      status;
    uint8_t      data8;
    uint32_t     data32;
    char         bytes[32];
} smc_keydata_t;

/**
 * Read a single SMC key's float value. Returns temperature in deg C,
 * or -1.0 on failure. Handles 'flt ' (float32) and 2-byte sp78 formats.
 */
static double smc_read_temp(io_connect_t conn, const char *key_str) {
    smc_keydata_t inp = {0}, out = {0};
    inp.key = ((uint32_t)key_str[0] << 24) | ((uint32_t)key_str[1] << 16) |
              ((uint32_t)key_str[2] << 8)  |  (uint32_t)key_str[3];
    inp.data8 = 9;  /* kSMCGetKeyInfo */

    size_t out_size = sizeof(smc_keydata_t);
    kern_return_t kr = IOConnectCallStructMethod(
        conn, 2, &inp, sizeof(inp), &out, &out_size);
    if (kr != KERN_SUCCESS || out.keyInfo.dataSize == 0)
        return -1.0;

    smc_keydata_t inp2 = {0}, out2 = {0};
    inp2.key = inp.key;
    inp2.keyInfo = out.keyInfo;
    inp2.data8 = 5;  /* kSMCReadKey */

    out_size = sizeof(smc_keydata_t);
    kr = IOConnectCallStructMethod(
        conn, 2, &inp2, sizeof(inp2), &out2, &out_size);
    if (kr != KERN_SUCCESS)
        return -1.0;

    uint32_t size = out.keyInfo.dataSize;
    uint32_t type = out.keyInfo.dataType;

    if (type == 0x666c7420 && size == 4) {  /* "flt " */
        float val;
        memcpy(&val, out2.bytes, 4);
        return (double)val;
    }
    if (size == 2) {  /* sp78: signed 8.8 fixed point */
        int16_t raw = ((uint8_t)out2.bytes[0] << 8) | (uint8_t)out2.bytes[1];
        return raw / 256.0;
    }
    return -1.0;
}

/* ------------------------------------------------------------------ */
/* Python API: temperatures                                            */
/* ------------------------------------------------------------------ */

const char temperatures_doc[] =
"temperatures() -> dict\n\n"
"Read thermal sensor temperatures via AppleSMC. No sudo needed.\n\n"
"Returns dict with keys:\n"
"    - 'cpu_avg': float -- average CPU die temperature in deg C\n"
"    - 'gpu_avg': float -- average GPU die temperature in deg C\n"
"    - 'system_avg': float -- average system/SoC temperature in deg C\n"
"    - 'cpu_sensors': dict -- individual CPU sensors {name: deg C}\n"
"    - 'gpu_sensors': dict -- individual GPU sensors {name: deg C}\n"
"    - 'system_sensors': dict -- individual system sensors {name: deg C}\n\n"
"Sensor naming: Tp* = CPU, Tg* = GPU, Ts* = system.\n"
"Returns empty dict if AppleSMC is not available.";

PyObject* py_temperatures(PyObject* self, PyObject* args) {
    (void)self; (void)args;

    /* Open AppleSMC connection */
    io_iterator_t iter;
    kern_return_t kr = IOServiceGetMatchingServices(
        kIOMainPortDefault, IOServiceMatching("AppleSMC"), &iter);
    if (kr != KERN_SUCCESS)
        return PyDict_New();

    io_service_t svc = IOIteratorNext(iter);
    IOObjectRelease(iter);
    if (!svc)
        return PyDict_New();

    io_connect_t conn;
    kr = IOServiceOpen(svc, mach_task_self(), 0, &conn);
    IOObjectRelease(svc);
    if (kr != KERN_SUCCESS)
        return PyDict_New();

    PyObject *result = PyDict_New();
    PyObject *cpu_sensors = PyDict_New();
    PyObject *gpu_sensors = PyDict_New();
    PyObject *sys_sensors = PyDict_New();
    double cpu_sum = 0, gpu_sum = 0, sys_sum = 0;
    int cpu_count = 0, gpu_count = 0, sys_count = 0;

    /* Scan known temperature key patterns */
    const char prefixes[][3] = {"Tp", "Tg", "Ts"};
    char key[5] = {0};

    for (int p = 0; p < 3; p++) {
        key[0] = prefixes[p][0];
        key[1] = prefixes[p][1];

        /* Numeric 00-99 */
        for (int hi = '0'; hi <= '9'; hi++) {
            for (int lo = '0'; lo <= '9'; lo++) {
                key[2] = hi; key[3] = lo;
                double t = smc_read_temp(conn, key);
                if (t <= 0 || t >= 150) continue;

                PyObject *v = PyFloat_FromDouble(t);
                if (p == 0) { PyDict_SetItemString(cpu_sensors, key, v); cpu_sum += t; cpu_count++; }
                else if (p == 1) { PyDict_SetItemString(gpu_sensors, key, v); gpu_sum += t; gpu_count++; }
                else { PyDict_SetItemString(sys_sensors, key, v); sys_sum += t; sys_count++; }
                Py_DECREF(v);
            }
        }

        /* Hex suffixes 0a-0f */
        for (int lo = 'a'; lo <= 'f'; lo++) {
            key[2] = '0'; key[3] = lo;
            double t = smc_read_temp(conn, key);
            if (t <= 0 || t >= 150) continue;

            PyObject *v = PyFloat_FromDouble(t);
            if (p == 0) { PyDict_SetItemString(cpu_sensors, key, v); cpu_sum += t; cpu_count++; }
            else if (p == 1) { PyDict_SetItemString(gpu_sensors, key, v); gpu_sum += t; gpu_count++; }
            else { PyDict_SetItemString(sys_sensors, key, v); sys_sum += t; sys_count++; }
            Py_DECREF(v);
        }

        /* Special suffixes */
        const char *specials[] = {"0P", "0S", "0D", "0H", "0J", "1P", NULL};
        for (int s = 0; specials[s]; s++) {
            key[2] = specials[s][0]; key[3] = specials[s][1];
            double t = smc_read_temp(conn, key);
            if (t <= 0 || t >= 150) continue;

            PyObject *v = PyFloat_FromDouble(t);
            if (p == 0) { PyDict_SetItemString(cpu_sensors, key, v); cpu_sum += t; cpu_count++; }
            else if (p == 1) { PyDict_SetItemString(gpu_sensors, key, v); gpu_sum += t; gpu_count++; }
            else { PyDict_SetItemString(sys_sensors, key, v); sys_sum += t; sys_count++; }
            Py_DECREF(v);
        }
    }

    IOServiceClose(conn);

    /* Build result */
    PyObject *v;
    if (cpu_count > 0) {
        v = PyFloat_FromDouble(cpu_sum / cpu_count);
        PyDict_SetItemString(result, "cpu_avg", v); Py_DECREF(v);
    }
    if (gpu_count > 0) {
        v = PyFloat_FromDouble(gpu_sum / gpu_count);
        PyDict_SetItemString(result, "gpu_avg", v); Py_DECREF(v);
    }
    if (sys_count > 0) {
        v = PyFloat_FromDouble(sys_sum / sys_count);
        PyDict_SetItemString(result, "system_avg", v); Py_DECREF(v);
    }

    PyDict_SetItemString(result, "cpu_sensors", cpu_sensors); Py_DECREF(cpu_sensors);
    PyDict_SetItemString(result, "gpu_sensors", gpu_sensors); Py_DECREF(gpu_sensors);
    PyDict_SetItemString(result, "system_sensors", sys_sensors); Py_DECREF(sys_sensors);

    return result;
}

/* ------------------------------------------------------------------ */
/* Python API: hid_idle_ns                                             */
/* ------------------------------------------------------------------ */

const char hid_idle_ns_doc[] =
"hid_idle_ns() -> int\n\n"
"Return nanoseconds since last HID (keyboard/mouse/trackpad) event.\n"
"Uses IOKit IOHIDSystem directly -- no subprocess, no ioreg.\n"
"Returns 0 on error.\n";

PyObject* py_hid_idle_ns(PyObject* self, PyObject* args) {
    (void)self; (void)args;

    uint64_t idle_ns = 0;
    io_iterator_t iter = 0;
    kern_return_t kr;

    kr = IOServiceGetMatchingServices(
        kIOMainPortDefault,
        IOServiceMatching("IOHIDSystem"),
        &iter
    );
    if (kr != KERN_SUCCESS || iter == 0) {
        return PyLong_FromUnsignedLongLong(0);
    }

    io_registry_entry_t entry = IOIteratorNext(iter);
    IOObjectRelease(iter);

    if (entry == 0) {
        return PyLong_FromUnsignedLongLong(0);
    }

    CFMutableDictionaryRef props = NULL;
    kr = IORegistryEntryCreateCFProperties(entry, &props, kCFAllocatorDefault, 0);
    IOObjectRelease(entry);

    if (kr != KERN_SUCCESS || props == NULL) {
        return PyLong_FromUnsignedLongLong(0);
    }

    CFNumberRef idle_ref = CFDictionaryGetValue(props, CFSTR("HIDIdleTime"));
    if (idle_ref && CFGetTypeID(idle_ref) == CFNumberGetTypeID()) {
        CFNumberGetValue(idle_ref, kCFNumberSInt64Type, &idle_ns);
    }

    CFRelease(props);
    return PyLong_FromUnsignedLongLong(idle_ns);
}
