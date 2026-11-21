#include "game_bridge.hpp"

#include <Windows.h>

#include <atomic>
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <string>
#include <thread>

namespace {

struct TestBackendState {
    std::atomic<std::size_t> apply_count{0};
    std::uint32_t completed_location_id = 0x4A430101;
};

BOOL WINAPI ApplyMockLiveItem(
    void* context,
    std::uint64_t,
    std::uint32_t,
    const char*,
    const char*,
    int) {
    auto* state = static_cast<TestBackendState*>(context);
    state->apply_count.fetch_add(1);
    return TRUE;
}

BOOL WINAPI IsMockLocationCompleted(
    void* context,
    std::uint32_t ap_location_id,
    const char*,
    const char*) {
    const auto* state = static_cast<const TestBackendState*>(context);
    return ap_location_id == state->completed_location_id ? TRUE : FALSE;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc != 3 && argc != 4) {
        std::cerr << "usage: jc3_ap_bridge_test_host <port> <state-directory> [mock|live]\n";
        return 2;
    }

    char* end = nullptr;
    const unsigned long parsed_port = std::strtoul(argv[1], &end, 10);
    if (end == argv[1] || *end != '\0' || parsed_port == 0 ||
        parsed_port > 65535) {
        std::cerr << "invalid port\n";
        return 2;
    }

    const std::string mode = argc == 4 ? argv[3] : "mock";
    auto mock_backend = std::make_shared<jc3ap::MockGameBackend>();
    auto live_backend = std::make_shared<jc3ap::LiveGameBackend>();
    TestBackendState live_state;
    if (mode == "live") {
        jc3ap::LiveGameBackend::Callbacks callbacks;
        callbacks.context = &live_state;
        callbacks.weapon_unlock = ApplyMockLiveItem;
        callbacks.location_completed = IsMockLocationCompleted;
        live_backend->SetCallbacks(callbacks);
    } else if (mode != "mock") {
        std::cerr << "invalid backend mode\n";
        return 2;
    }

    std::shared_ptr<jc3ap::IGameBackend> backend;
    if (mode == "live") {
        backend = live_backend;
    } else {
        backend = mock_backend;
    }
    jc3ap::GameBridge bridge(backend, std::filesystem::path(argv[2]));
    std::atomic<bool> stop_requested{false};
    std::atomic<bool> server_started{false};
    std::thread server([&] {
        server_started.store(true);
        if (!bridge.Run(static_cast<std::uint16_t>(parsed_port),
                        stop_requested)) {
            std::cerr << "bridge server failed\n";
            stop_requested.store(true);
        }
    });

    while (!server_started.load() && !stop_requested.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }

    std::thread input([&] {
        std::string command;
        while (std::getline(std::cin, command)) {
            if (command == "quit") {
                stop_requested.store(true);
                return;
            }
            if (command == "world" && mode == "live") {
                live_backend->SetInWorld(true);
            }
        }
    });
    while (!stop_requested.load()) {
        bridge.ProcessPendingGrants();
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }

    stop_requested.store(true);
    CancelSynchronousIo(input.native_handle());
    input.join();
    if (server.joinable()) {
        server.join();
    }
    if (mode == "live") {
        std::cout << "live_apply_count=" << live_state.apply_count.load() << '\n';
    } else {
        std::cout << "mock_apply_count=" << mock_backend->ApplyCount() << '\n';
    }
    return 0;
}
