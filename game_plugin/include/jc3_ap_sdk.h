#pragma once

#include <stdint.h>

#ifdef _WIN32
#include <Windows.h>
#endif

#if defined(_WIN32)
#ifdef JC3AP_BUILD_DLL
#define JC3AP_API extern "C" __declspec(dllexport)
#else
#define JC3AP_API extern "C" __declspec(dllimport)
#endif
#else
#define JC3AP_API extern "C"
#endif

#define JC3AP_SDK_VERSION 1

typedef int(__stdcall* JC3AP_GrantCallback)(
    void* context,
    uint64_t delivery_index,
    uint32_t ap_item_id,
    const char* game_key,
    const char* display_name,
    const char* category,
    int count);

JC3AP_API int __stdcall JC3AP_Initialize(void);
JC3AP_API int __stdcall JC3AP_Shutdown(void);
JC3AP_API const char* __stdcall JC3AP_GetBuildTimestamp(void);
JC3AP_API int __stdcall JC3AP_SetInWorld(int in_world);
JC3AP_API int __stdcall JC3AP_RegisterGrantCallback(
    void* context,
    JC3AP_GrantCallback callback);
JC3AP_API int __stdcall JC3AP_ReportLocationCompleted(
    uint32_t ap_location_id);
JC3AP_API int __stdcall JC3AP_ReportLocationByKey(
    const char* category,
    const char* game_key);

/* Compatibility exports used by the original plugin build. */
JC3AP_API int __stdcall JC3AP_StopBridge(void);
JC3AP_API uint64_t __stdcall JC3AP_ProcessPendingGrants(void);
