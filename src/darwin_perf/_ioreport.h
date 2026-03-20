/**
 * darwin-perf: IOReport infrastructure shared between _gpu.c and _cpu.c.
 *
 * Declarations only — definitions live in _ioreport.c.
 *
 * Copyright 2026 Adam Mikulis. MIT License.
 */

#ifndef DARWIN_PERF_IOREPORT_H
#define DARWIN_PERF_IOREPORT_H

#include <CoreFoundation/CoreFoundation.h>
#include <stdint.h>

/* IOReport format types */
#define kIOReportFormatSimple    1
#define kIOReportFormatState     2
#define kIOReportFormatHistogram 3

/* Max channels per sample extraction */
#define MAX_CHANNELS 1024

/* Max DVFS P-states */
#define MAX_PSTATES 32

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

/* Cached function pointers (defined in _ioreport.c) */
extern IOReportCopyChannelsInGroup_t     ior_CopyChannelsInGroup;
extern IOReportMergeChannels_t           ior_MergeChannels;
extern IOReportGetChannelCount_t         ior_GetChannelCount;
extern IOReportCreateSubscription_t      ior_CreateSubscription;
extern IOReportCreateSamples_t           ior_CreateSamples;
extern IOReportCreateSamplesDelta_t      ior_CreateSamplesDelta;
extern IOReportSimpleGetIntegerValue_t   ior_SimpleGetIntegerValue;
extern IOReportChannelGetChannelName_t   ior_ChannelGetChannelName;
extern IOReportChannelGetSubGroup_t      ior_ChannelGetSubGroup;
extern IOReportChannelGetFormat_t        ior_ChannelGetFormat;
extern IOReportStateGetCount_t           ior_StateGetCount;
extern IOReportStateGetResidency_t       ior_StateGetResidency;
extern IOReportStateGetNameForIndex_t    ior_StateGetNameForIndex;

/* Load libIOReport at runtime. Returns 0 on success, -1 on failure. */
int load_ioreport(void);

/* Helper: compare CFString to C string. */
int cfstr_eq(CFStringRef cf, const char *c);

/* Helper: convert CFString to Python string (or None). */
PyObject* cfstr_to_pystr(CFStringRef cf);

/* Channel pair for sample extraction */
typedef struct {
    CFDictionaryRef ch;   /* channel dict from s2 */
    CFDictionaryRef ch1;  /* matching channel from s1 (same index) */
} channel_pair_t;

/**
 * Extract matching channel pairs from two IOReport samples.
 * Allocates pairs array (caller must free). Returns count.
 */
int extract_channel_pairs(CFDictionaryRef s1, CFDictionaryRef s2,
                          channel_pair_t **out_pairs);

#endif /* DARWIN_PERF_IOREPORT_H */
