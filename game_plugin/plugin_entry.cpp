#include "game_bridge.hpp"

#include <Windows.h>

#include <atomic>
#include <cstdint>
#include <iterator>

namespace {

auto g_live_backend = std::make_shared<jc3ap::LiveGameBackend>();
jc3ap::GameBridge g_bridge(g_live_backend);
std::atomic<bool> g_stop_requested{false};
HANDLE g_worker = nullptr;

DWORD WINAPI BridgeWorker(void*) {
    if (!g_bridge.Run(g_stop_requested)) {
        OutputDebugStringA("JC3 AP bridge: server stopped after an error.\n");
    }
    return 0;
}

}  // namespace

extern "C" __declspec(dllexport) BOOL WINAPI
JC3AP_StopBridge() {
    g_stop_requested.store(true);
    if (g_worker == nullptr) {
        return TRUE;
    }
    const DWORD result = WaitForSingleObject(g_worker, 2000);
    if (result != WAIT_OBJECT_0) {
        OutputDebugStringA(
            "JC3 AP bridge: worker did not stop within two seconds.\n");
        return FALSE;
    }
    CloseHandle(g_worker);
    g_worker = nullptr;
    return TRUE;
}

extern "C" __declspec(dllexport) BOOL WINAPI
JC3AP_RegisterGameCallbacks(
    const jc3ap::LiveGameBackend::Callbacks* callbacks) {
    if (callbacks == nullptr) {
        return FALSE;
    }
    g_live_backend->SetCallbacks(*callbacks);
    return TRUE;
}

extern "C" __declspec(dllexport) BOOL WINAPI
JC3AP_SetInWorld(BOOL in_world) {
    g_live_backend->SetInWorld(in_world != FALSE);
    return TRUE;
}

extern "C" __declspec(dllexport) std::size_t WINAPI
JC3AP_ProcessPendingGrants() {
    return g_bridge.ProcessPendingGrants();
}

extern "C" __declspec(dllexport) BOOL WINAPI
JC3AP_PublishLocationChecked(std::uint64_t location_id) {
    return g_bridge.PublishLocationChecked(location_id) ? TRUE : FALSE;
}

extern "C" __declspec(dllexport) BOOL WINAPI
JC3AP_OnSettlementLiberated(std::uint64_t location_id) {
    return g_bridge.OnSettlementLiberated(location_id) ? TRUE : FALSE;
}

extern "C" __declspec(dllexport) BOOL WINAPI
JC3AP_OnCollectibleCounterChanged(
    std::uint64_t location_id,
    std::uint32_t current,
    std::uint32_t required) {
    return g_bridge.OnCollectibleCounterChanged(
               location_id, current, required)
        ? TRUE
        : FALSE;
}

BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(module);
        g_worker = CreateThread(nullptr, 0, BridgeWorker, nullptr, 0, nullptr);
        if (g_worker == nullptr) {
            OutputDebugStringA("JC3 AP bridge: could not start worker.\n");
        }
    } else if (reason == DLL_PROCESS_DETACH) {
        g_stop_requested.store(true);
        if (g_worker != nullptr) {
            CloseHandle(g_worker);
            g_worker = nullptr;
        }
    }
    return TRUE;
}
