#include <Windows.h>
#include <unknwn.h>

#include <array>
#include <mutex>
#include <string>

extern "C" BOOL WINAPI JC3AP_Initialize();
extern "C" BOOL WINAPI JC3AP_StopBridge();

namespace {

using DirectInput8CreateFn =
    HRESULT(WINAPI*)(HINSTANCE, DWORD, REFIID, LPVOID*, LPUNKNOWN);
using DllCanUnloadNowFn = HRESULT(WINAPI*)();
using DllGetClassObjectFn = HRESULT(WINAPI*)(REFCLSID, REFIID, LPVOID*);
using DllRegisterServerFn = HRESULT(WINAPI*)();
using DllUnregisterServerFn = HRESULT(WINAPI*)();

HMODULE g_real_dinput8 = nullptr;
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

DWORD WINAPI InitializeBridgeWorker(void*) {
    if (!JC3AP_Initialize()) {
        OutputDebugStringA(
            "JC3 AP bridge: initialization from dinput8 proxy failed.\n");
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
            nullptr, 0, InitializeBridgeWorker, nullptr, 0, nullptr);
        if (worker == nullptr) {
            OutputDebugStringA(
                "JC3 AP bridge: could not start proxy initialization worker.\n");
        } else {
            CloseHandle(worker);
        }
    } else if (reason == DLL_PROCESS_DETACH && reserved == nullptr) {
        if (!JC3AP_StopBridge()) {
            OutputDebugStringA(
                "JC3 AP bridge: proxy detach could not stop the bridge.\n");
        }
    }
    return TRUE;
}
