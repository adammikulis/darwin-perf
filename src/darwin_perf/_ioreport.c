/**
 * darwin-perf: IOReport infrastructure — shared between _gpu.c and _cpu.c.
 *
 * Provides load_ioreport(), cfstr_eq(), cfstr_to_pystr(), and
 * extract_channel_pairs() used by both GPU and CPU power sampling.
 *
 * Copyright 2026 Adam Mikulis. MIT License.
 */

#include "_native.h"
#include "_ioreport.h"
#include <dlfcn.h>

/* Cached function pointers */
static int ior_loaded = 0;
IOReportCopyChannelsInGroup_t     ior_CopyChannelsInGroup;
IOReportMergeChannels_t           ior_MergeChannels;
IOReportGetChannelCount_t         ior_GetChannelCount;
IOReportCreateSubscription_t      ior_CreateSubscription;
IOReportCreateSamples_t           ior_CreateSamples;
IOReportCreateSamplesDelta_t      ior_CreateSamplesDelta;
IOReportSimpleGetIntegerValue_t   ior_SimpleGetIntegerValue;
IOReportChannelGetChannelName_t   ior_ChannelGetChannelName;
IOReportChannelGetSubGroup_t      ior_ChannelGetSubGroup;
IOReportChannelGetFormat_t        ior_ChannelGetFormat;
IOReportStateGetCount_t           ior_StateGetCount;
IOReportStateGetResidency_t       ior_StateGetResidency;
IOReportStateGetNameForIndex_t    ior_StateGetNameForIndex;

int load_ioreport(void) {
    if (ior_loaded) return ior_loaded > 0 ? 0 : -1;

    void *lib = dlopen("/usr/lib/libIOReport.dylib", RTLD_LAZY);
    if (!lib) {
        /* Try without path -- dyld shared cache */
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

int cfstr_eq(CFStringRef cf, const char *c) {
    if (!cf) return 0;
    char buf[256];
    if (!CFStringGetCString(cf, buf, sizeof(buf), kCFStringEncodingUTF8))
        return 0;
    return strcmp(buf, c) == 0;
}

PyObject* cfstr_to_pystr(CFStringRef cf) {
    if (!cf) Py_RETURN_NONE;
    char buf[256];
    if (CFStringGetCString(cf, buf, sizeof(buf), kCFStringEncodingUTF8))
        return PyUnicode_FromString(buf);
    Py_RETURN_NONE;
}

int extract_channel_pairs(CFDictionaryRef s1, CFDictionaryRef s2,
                          channel_pair_t **out_pairs) {
    channel_pair_t *pairs = malloc(MAX_CHANNELS * sizeof(channel_pair_t));
    if (!pairs) { *out_pairs = NULL; return 0; }
    int n_pairs = 0;

    CFIndex sn = CFDictionaryGetCount(s1);
    CFIndex sn2 = CFDictionaryGetCount(s2);
    if (sn > 0 && sn2 > 0) {
        const void **sk1 = malloc(sn * sizeof(void*));
        const void **sv1 = malloc(sn * sizeof(void*));
        const void **sk2 = malloc(sn2 * sizeof(void*));
        const void **sv2 = malloc(sn2 * sizeof(void*));
        if (!sk1 || !sv1 || !sk2 || !sv2) {
            free(sk1); free(sv1); free(sk2); free(sv2);
            *out_pairs = pairs;
            return 0;
        }
        CFDictionaryGetKeysAndValues(s1, sk1, sv1);
        CFDictionaryGetKeysAndValues(s2, sk2, sv2);

        for (CFIndex tv = 0; tv < sn2 && n_pairs < MAX_CHANNELS; tv++) {
            CFTypeID vtype = CFGetTypeID(sv2[tv]);

            if (vtype == CFArrayGetTypeID()) {
                /* Flat array of channels */
                CFArrayRef arr2 = (CFArrayRef)sv2[tv];
                CFArrayRef arr1 = NULL;
                for (CFIndex j = 0; j < sn; j++) {
                    if (CFEqual(sk1[j], sk2[tv]) && CFGetTypeID(sv1[j]) == CFArrayGetTypeID()) {
                        arr1 = (CFArrayRef)sv1[j];
                        break;
                    }
                }
                if (arr1) {
                    CFIndex nc = CFArrayGetCount(arr2);
                    CFIndex nc1 = CFArrayGetCount(arr1);
                    if (nc1 < nc) nc = nc1;
                    for (CFIndex c = 0; c < nc && n_pairs < MAX_CHANNELS; c++) {
                        pairs[n_pairs].ch = CFArrayGetValueAtIndex(arr2, c);
                        pairs[n_pairs].ch1 = CFArrayGetValueAtIndex(arr1, c);
                        n_pairs++;
                    }
                }
            } else if (vtype == CFDictionaryGetTypeID()) {
                /* Nested dict of drivers */
                CFDictionaryRef drivers2 = (CFDictionaryRef)sv2[tv];
                CFDictionaryRef drivers1 = NULL;
                for (CFIndex j = 0; j < sn; j++) {
                    if (CFEqual(sk1[j], sk2[tv]) && CFGetTypeID(sv1[j]) == CFDictionaryGetTypeID()) {
                        drivers1 = (CFDictionaryRef)sv1[j];
                        break;
                    }
                }
                if (!drivers1) continue;

                CFIndex nd = CFDictionaryGetCount(drivers2);
                const void **dk = malloc(nd * sizeof(void*));
                const void **dv = malloc(nd * sizeof(void*));
                if (!dk || !dv) { free(dk); free(dv); continue; }
                CFDictionaryGetKeysAndValues(drivers2, dk, dv);

                for (CFIndex d = 0; d < nd; d++) {
                    if (CFGetTypeID(dv[d]) != CFDictionaryGetTypeID()) continue;
                    CFDictionaryRef drv2 = (CFDictionaryRef)dv[d];
                    CFDictionaryRef drv1 = (CFDictionaryRef)CFDictionaryGetValue(drivers1, dk[d]);

                    CFIndex dnk = CFDictionaryGetCount(drv2);
                    const void **ddk = malloc(dnk * sizeof(void*));
                    const void **ddv = malloc(dnk * sizeof(void*));
                    if (!ddk || !ddv) { free(ddk); free(ddv); continue; }
                    CFDictionaryGetKeysAndValues(drv2, ddk, ddv);

                    CFArrayRef ch_arr2 = NULL, ch_arr1 = NULL;
                    for (CFIndex k = 0; k < dnk; k++) {
                        if (CFGetTypeID(ddv[k]) == CFArrayGetTypeID()) {
                            ch_arr2 = (CFArrayRef)ddv[k];
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

        free(sk1); free(sv1);
        free(sk2); free(sv2);
    }

    *out_pairs = pairs;
    return n_pairs;
}
