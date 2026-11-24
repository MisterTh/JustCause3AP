#pragma once

#include <winsock2.h>

#include <atomic>
#include <cstdint>
#include <deque>
#include <functional>
#include <mutex>
#include <string>

#include <nlohmann/json.hpp>

namespace jc3ap {

const char* BuildTimestamp() noexcept;

class IpcServer {
public:
    using Handler = std::function<nlohmann::json(const nlohmann::json&)>;

    bool Run(
        std::uint16_t port,
        const Handler& handler,
        const std::atomic<bool>& stop_requested);
    bool Publish(const nlohmann::json& message);

private:
    bool ServeClient(SOCKET client, const Handler& handler,
                     const std::atomic<bool>& stop_requested);
    bool SendJson(SOCKET client, const nlohmann::json& message);
    bool FlushPublished(SOCKET client);
    void ClearClient(SOCKET client);

    std::atomic<SOCKET> client_socket_{INVALID_SOCKET};
    std::mutex published_mutex_;
    std::deque<std::string> published_messages_;
};

}  // namespace jc3ap
