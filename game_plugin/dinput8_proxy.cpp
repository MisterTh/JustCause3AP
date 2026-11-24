#include <Windows.h>
#include <unknwn.h>

#include <array>
#include <cstdio>
#include <mutex>
#include <string>

namespace {

using DirectInput8CreateFn =
    HRESULT(WINAPI*)(HINSTANCE, DWORD, REFIID, LPVOID*, LPUNKNOWN);
using DllCanUnloadNowFn = HRESULT(WINAPI*)();
using DllGetClassObjectFn = HRESULT(WINAPI*)(REFCLSID, REFIID, LPVOID*);
using DllRegisterServerFn = HRESULT(WINAPI*)();
using DllUnregisterServerFn = HRESULT(WINAPI*)();
using BridgeInitializeFn = BOOL(WINAPI*)();
using BridgeStopFn = BOOL(WINAPI*)();
using BridgeBuildTimestampFn = const char*(WINAPI*)();

HMODULE g_real_dinput8 = nullptr;
HMODULE g_bridge_plugin = nullptr;
BridgeStopFn g_stop_bridge = nullptr;
DirectInput8CreateFn g_direct_input8_create = nullptr;
DllCanUnloadNowFn g_dll_can_unload_now = nullptr;
DllGetClassObjectFn g_dll_get_class_object = nullptr;
DllRegisterServerFn g_dll_register_server = nullptr;
DllUnregisterServerFn g_dll_unregister_server = nullptr;
HRESULT g_load_result = E_FAIL;
std::once_flag g_load_once;

FARPROC ResolveExport(const char* name) {
    return g_real_dinput8 == nullptr
        ? nullptr
        : GetProcAddress(g_real_dinput8, name);
}

void LoadRealDinput8() {
    std::array<wchar_t, MAX_PATH> system_directory{};
    const UINT length = GetSystemDirectoryW(
        system_directory.data(), static_cast<UINT>(system_directory.size()));
    if (length == 0) {
        g_load_result = HRESULT_FROM_WIN32(GetLastError());
        return;
    }
    if (static_cast<std::size_t>(length) >= system_directory.size()) {
        g_load_result = HRESULT_FROM_WIN32(ERROR_INSUFFICIENT_BUFFER);
        return;
    }

    std::wstring path(system_directory.data(), length);
    if (path.back() != L'\\' && path.back() != L'/') {
        path.push_back(L'\\');
    }
    path += L"dinput8.dll";

    g_real_dinput8 = LoadLibraryW(path.c_str());
    if (g_real_dinput8 == nullptr) {
        g_load_result = HRESULT_FROM_WIN32(GetLastError());
        return;
    }

    g_direct_input8_create = reinterpret_cast<DirectInput8CreateFn>(
        ResolveExport("DirectInput8Create"));
    g_dll_can_unload_now =
        reinterpret_cast<DllCanUnloadNowFn>(ResolveExport("DllCanUnloadNow"));
    g_dll_get_class_object = reinterpret_cast<DllGetClassObjectFn>(
        ResolveExport("DllGetClassObject"));
    g_dll_register_server = reinterpret_cast<DllRegisterServerFn>(
        ResolveExport("DllRegisterServer"));
    g_dll_unregister_server = reinterpret_cast<DllUnregisterServerFn>(
        ResolveExport("DllUnregisterServer"));
    g_load_result = S_OK;
}

HRESULT EnsureRealDinput8Loaded() {
    std::call_once(g_load_once, LoadRealDinput8);
    return g_load_result;
}

HRESULT MissingExportResult() {
    return HRESULT_FROM_WIN32(ERROR_PROC_NOT_FOUND);
}

DWORD WINAPI InitializeBridgeWorker(void* module_context) {
    const HMODULE self_module = static_cast<HMODULE>(module_context);
    std::array<wchar_t, 32768> module_path{};
    const DWORD module_length = GetModuleFileNameW(
        self_module, module_path.data(),
        static_cast<DWORD>(module_path.size()));
    if (module_length == 0 ||
        module_length >= static_cast<DWORD>(module_path.size())) {
        OutputDebugStringA(
            "JC3 AP proxy: could not resolve the proxy module path.\n");
        return 0;
    }

    std::wstring plugin_path(module_path.data(), module_length);
    const std::size_t separator = plugin_path.find_last_of(L"\\/");
    if (separator == std::wstring::npos) {
        OutputDebugStringA(
            "JC3 AP proxy: proxy module path has no parent directory.\n");
        return 0;
    }
    plugin_path.resize(separator + 1);
    plugin_path += L"jc3_ap_plugin.dll";

    std::wstring path_message =
        L"JC3 AP proxy: loading bridge plugin from ";
    path_message += plugin_path;
    path_message += L"\n";
    OutputDebugStringW(path_message.c_str());

    g_bridge_plugin = LoadLibraryExW(
        plugin_path.c_str(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (g_bridge_plugin == nullptr) {
        char error_message[160]{};
        _snprintf_s(
            error_message, sizeof(error_message), _TRUNCATE,
            "JC3 AP proxy: LoadLibraryExW failed (Windows error %lu).\n",
            GetLastError());
        OutputDebugStringA(error_message);
        return 0;
    }

    const auto initialize = reinterpret_cast<BridgeInitializeFn>(
        GetProcAddress(g_bridge_plugin, "JC3AP_Initialize"));
    g_stop_bridge = reinterpret_cast<BridgeStopFn>(
        GetProcAddress(g_bridge_plugin, "JC3AP_StopBridge"));
    const auto get_build_timestamp = reinterpret_cast<BridgeBuildTimestampFn>(
        GetProcAddress(g_bridge_plugin, "JC3AP_GetBuildTimestamp"));
    if (get_build_timestamp != nullptr) {
        OutputDebugStringA("JC3 AP proxy: bridge build_timestamp=");
        OutputDebugStringA(get_build_timestamp());
        OutputDebugStringA("\n");
    } else {
        OutputDebugStringA(
            "JC3 AP proxy: bridge build_timestamp export is unavailable.\n");
    }

    if (initialize == nullptr || g_stop_bridge == nullptr) {
        OutputDebugStringA(
            "JC3 AP proxy: bridge plugin is missing a required lifecycle export.\n");
        return 0;
    }
    if (!initialize()) {
        OutputDebugStringA(
            "JC3 AP proxy: bridge initialization failed.\n");
    }
    return 0;
}

}  // namespace

extern "C" HRESULT WINAPI DirectInput8Create(
    HINSTANCE instance,
    DWORD version,
    REFIID interface_id,
    LPVOID* interface_pointer,
    LPUNKNOWN outer) {
    const HRESULT result = EnsureRealDinput8Loaded();
    if (FAILED(result)) {
        return result;
    }
    return g_direct_input8_create == nullptr
        ? MissingExportResult()
        : g_direct_input8_create(
              instance, version, interface_id, interface_pointer, outer);
}

extern "C" HRESULT WINAPI DllCanUnloadNow() {
    const HRESULT result = EnsureRealDinput8Loaded();
    if (FAILED(result)) {
        return result;
    }
    return g_dll_can_unload_now == nullptr
        ? MissingExportResult()
        : g_dll_can_unload_now();
}

extern "C" HRESULT WINAPI DllGetClassObject(
    REFCLSID class_id,
    REFIID interface_id,
    LPVOID* class_object) {
    const HRESULT result = EnsureRealDinput8Loaded();
    if (FAILED(result)) {
        return result;
    }
    return g_dll_get_class_object == nullptr
        ? MissingExportResult()
        : g_dll_get_class_object(class_id, interface_id, class_object);
}

extern "C" HRESULT WINAPI DllRegisterServer() {
    const HRESULT result = EnsureRealDinput8Loaded();
    if (FAILED(result)) {
        return result;
    }
    return g_dll_register_server == nullptr
        ? MissingExportResult()
        : g_dll_register_server();
}

extern "C" HRESULT WINAPI DllUnregisterServer() {
    const HRESULT result = EnsureRealDinput8Loaded();
    if (FAILED(result)) {
        return result;
    }
    return g_dll_unregister_server == nullptr
        ? MissingExportResult()
        : g_dll_unregister_server();
}

BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID reserved) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(module);
        HANDLE worker = CreateThread(
            nullptr, 0, InitializeBridgeWorker, module, 0, nullptr);
        if (worker == nullptr) {
            OutputDebugStringA(
                "JC3 AP bridge: could not start proxy initialization worker.\n");
        } else {
            CloseHandle(worker);
        }
    } else if (reason == DLL_PROCESS_DETACH && reserved == nullptr) {
        if (g_stop_bridge != nullptr && !g_stop_bridge()) {
            OutputDebugStringA(
                "JC3 AP bridge: proxy detach could not stop the bridge.\n");
        }
    }
    return TRUE;
}
