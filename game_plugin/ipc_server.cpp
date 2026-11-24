#include "ipc_server.hpp"

#include <Windows.h>

#include <array>
#include <cstdio>
#include <exception>
#include <string>
#include <utility>

namespace jc3ap {

const char* BuildTimestamp() noexcept {
    return __DATE__ " " __TIME__;
}

namespace {

constexpr std::size_t kMaxLineBytes = 64 * 1024;
constexpr std::size_t kMaxQueuedEvents = 256;
constexpr long kSelectTimeoutMicroseconds = 200'000;
std::string ErrorDescription(const std::string& code) {
    if (code == "invalid_command") return "The request must include a string cmd.";
    if (code == "invalid_json") return "The request line is not valid JSON.";
    if (code == "unknown_command") return "The bridge does not recognize this command.";
    if (code == "invalid_world_state") return "in_world must be a JSON boolean.";
    if (code == "invalid_session") return "The session seed and slot must be non-empty strings.";
    if (code == "state_unavailable") return "The bridge could not load persistent delivery state.";
    if (code == "memory_commands_disabled") return "Raw memory commands are disabled.";
    if (code == "handler_failed") return "The bridge command handler failed.";
    return "Bridge request failed: " + code;
}

void LogSocketError(const char* operation) {
    char message[160]{};
    _snprintf_s(message, sizeof(message), _TRUNCATE,
                "JC3 AP bridge: %s failed (Winsock error %d).\n",
                operation, WSAGetLastError());
    OutputDebugStringA(message);
}

bool SendAll(SOCKET socket, const std::string& data) {
    std::size_t sent = 0;
    while (sent < data.size()) {
        const int result = send(
            socket, data.data() + sent,
            static_cast<int>(data.size() - sent), 0);
        if (result == SOCKET_ERROR || result == 0) {
            return false;
        }
        sent += static_cast<std::size_t>(result);
    }
    return true;
}

}  // namespace

bool IpcServer::Run(
    std::uint16_t port,
    const Handler& handler,
    const std::atomic<bool>& stop_requested) {
    WSADATA data{};
    const int startup_result = WSAStartup(MAKEWORD(2, 2), &data);
    if (startup_result != 0) {
        OutputDebugStringA("JC3 AP bridge: WSAStartup failed.\n");
        return false;
    }

    SOCKET listener = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (listener == INVALID_SOCKET) {
        LogSocketError("socket");
        WSACleanup();
        return false;
    }

    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(port);
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);

    if (bind(listener, reinterpret_cast<const sockaddr*>(&address),
             sizeof(address)) == SOCKET_ERROR) {
        LogSocketError("bind");
        closesocket(listener);
        WSACleanup();
        return false;
    }
    if (listen(listener, SOMAXCONN) == SOCKET_ERROR) {
        LogSocketError("listen");
        closesocket(listener);
        WSACleanup();
        return false;
    }

    OutputDebugStringA("JC3 AP bridge: listening on 127.0.0.1.\n");
    while (!stop_requested.load()) {
        fd_set readable;
        FD_ZERO(&readable);
        FD_SET(listener, &readable);
        timeval timeout{0, kSelectTimeoutMicroseconds};
        const int ready = select(0, &readable, nullptr, nullptr, &timeout);
        if (ready == SOCKET_ERROR) {
            if (!stop_requested.load()) {
                LogSocketError("select");
            }
            break;
        }
        if (ready == 0) {
            continue;
        }

        SOCKET client = accept(listener, nullptr, nullptr);
        if (client == INVALID_SOCKET) {
            if (!stop_requested.load()) {
                LogSocketError("accept");
            }
            continue;
        }

        DWORD send_timeout_ms = 1000;
        setsockopt(client, SOL_SOCKET, SO_SNDTIMEO,
                   reinterpret_cast<const char*>(&send_timeout_ms),
                   sizeof(send_timeout_ms));
        client_socket_.store(client);
        ServeClient(client, handler, stop_requested);
        ClearClient(client);
    }

    closesocket(listener);
    WSACleanup();
    return true;
}

bool IpcServer::ServeClient(
    SOCKET client,
    const Handler& handler,
    const std::atomic<bool>& stop_requested) {
    std::array<char, 4096> chunk{};
    std::string pending;

    while (!stop_requested.load()) {
        fd_set readable;
        FD_ZERO(&readable);
        FD_SET(client, &readable);
        timeval timeout{0, kSelectTimeoutMicroseconds};
        const int ready = select(0, &readable, nullptr, nullptr, &timeout);
        if (ready == SOCKET_ERROR) {
            LogSocketError("client select");
            return false;
        }
        if (ready == 0) {
            if (!FlushPublished(client)) {
                return false;
            }
            continue;
        }

        const int received = recv(client, chunk.data(),
                                  static_cast<int>(chunk.size()), 0);
        if (received == 0) {
            return true;
        }
        if (received == SOCKET_ERROR) {
            LogSocketError("recv");
            return false;
        }
        pending.append(chunk.data(), static_cast<std::size_t>(received));
        if (pending.size() > kMaxLineBytes &&
            pending.find('\n') == std::string::npos) {
            SendJson(client, {{"event", "error"},
                              {"error", "line_too_large"}});
            return false;
        }

        std::size_t newline = std::string::npos;
        while ((newline = pending.find('\n')) != std::string::npos) {
            std::string line = pending.substr(0, newline);
            pending.erase(0, newline + 1);
            if (!line.empty() && line.back() == '\r') {
                line.pop_back();
            }
            if (line.size() > kMaxLineBytes) {
                SendJson(client, {{"event", "error"},
                                  {"error", "line_too_large"}});
                return false;
            }

            nlohmann::json request;
            nlohmann::json response;
            try {
                request = nlohmann::json::parse(line);
            } catch (const nlohmann::json::parse_error&) {
                response = {{"event", "error"},
                            {"error", "invalid_json"},
                            {"message", ErrorDescription("invalid_json")}};
            }
            if (response.is_null()) {
                try {
                    response = handler(request);
                } catch (const std::exception&) {
                    response = {{"event", "error"},
                                {"error", "handler_failed"},
                                {"message", ErrorDescription("handler_failed")}};
                }
            }
            if (!response.is_object()) {
                response = {{"event", "error"},
                            {"error", "invalid_response"},
                            {"message", "The bridge handler returned an invalid response."}};
            }
            if (request.is_object() && request.contains("id")) {
                response["id"] = request["id"];
            }
            if (response.value("event", std::string{}) == "error" &&
                (!response.contains("message") || !response["message"].is_string())) {
                const std::string code =
                    response.value("error", std::string("unknown_error"));
                response["message"] = ErrorDescription(code);
            }
            if (request.is_object() && request.contains("cmd") &&
                request["cmd"].is_string() &&
                request["cmd"].get<std::string>() == "get_status" &&
                response.value("event", std::string{}) == "status") {
                response["build_timestamp"] = BuildTimestamp();
            }
            if (!SendJson(client, response)) {
                return false;
            }
        }
        if (pending.size() > kMaxLineBytes) {
            SendJson(client, {{"event", "error"},
                              {"error", "line_too_large"}});
            return false;
        }
        if (!FlushPublished(client)) {
            return false;
        }
    }
    return true;
}

bool IpcServer::SendJson(SOCKET client, const nlohmann::json& message) {
    if (client_socket_.load() != client) {
        return false;
    }
    return SendAll(client, message.dump() + '\n');
}

bool IpcServer::Publish(const nlohmann::json& message) {
    std::lock_guard lock(published_mutex_);
    if (published_messages_.size() >= kMaxQueuedEvents) {
        return false;
    }
    published_messages_.push_back(message.dump() + '\n');
    return true;
}

bool IpcServer::FlushPublished(SOCKET client) {
    for (;;) {
        std::string message;
        {
            std::lock_guard lock(published_mutex_);
            if (published_messages_.empty()) {
                return true;
            }
            message = std::move(published_messages_.front());
            published_messages_.pop_front();
        }
        if (client_socket_.load() != client || !SendAll(client, message)) {
            std::lock_guard lock(published_mutex_);
            published_messages_.push_front(std::move(message));
            return false;
        }
    }
}

void IpcServer::ClearClient(SOCKET client) {
    SOCKET expected = client;
    if (client_socket_.compare_exchange_strong(expected, INVALID_SOCKET)) {
        shutdown(client, SD_BOTH);
        closesocket(client);
    }
}

}  // namespace jc3ap
