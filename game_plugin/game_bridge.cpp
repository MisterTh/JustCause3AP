#include "game_bridge.hpp"

#include <Windows.h>

#include <algorithm>
#include <charconv>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iomanip>
#include <iterator>
#include <limits>
#include <sstream>
#include <exception>
#include <stdexcept>
#include <string_view>
#include <system_error>
#include <utility>

namespace jc3ap {
namespace {

constexpr std::uint16_t kDefaultPort = 54321;
constexpr std::size_t kMaxQueuedGrants = 1024;
constexpr std::size_t kMaxGrantsPerTick = 32;
constexpr std::size_t kMaxIdentityLength = 128;
std::uint16_t ConfiguredPort() {
    char value[16]{};
    const DWORD length = GetEnvironmentVariableA(
        "JC3AP_PORT", value, static_cast<DWORD>(sizeof(value)));
    if (length == 0 || length >= sizeof(value)) {
        return kDefaultPort;
    }

    unsigned int port = 0;
    const auto result = std::from_chars(value, value + length, port);
    if (result.ec != std::errc{} || result.ptr != value + length ||
        port == 0 || port > 65535) {
        OutputDebugStringA(
            "JC3 AP bridge: invalid JC3AP_PORT; using port 54321.\n");
        return kDefaultPort;
    }
    return static_cast<std::uint16_t>(port);
}

void LogMessage(const char* message) {
    OutputDebugStringA(message);
    OutputDebugStringA("\n");
}

bool IsSafeItemId(std::string_view item_id) {
    if (item_id.empty() || item_id.size() > 96) {
        return false;
    }
    for (const unsigned char character : item_id) {
        if (!((character >= 'a' && character <= 'z') ||
              (character >= 'A' && character <= 'Z') ||
              (character >= '0' && character <= '9') ||
              character == '_' || character == '-')) {
            return false;
        }
    }
    return true;
}

bool IsSafeIdentity(const nlohmann::json& value) {
    return value.is_string() && !value.get_ref<const std::string&>().empty() &&
           value.get_ref<const std::string&>().size() <= kMaxIdentityLength;
}

bool ReadUnsignedInteger(const nlohmann::json& value, std::uint64_t* result) {
    if (value.is_number_unsigned()) {
        *result = value.get<std::uint64_t>();
        return true;
    }
    if (value.is_number_integer()) {
        const std::int64_t signed_value = value.get<std::int64_t>();
        if (signed_value >= 0) {
            *result = static_cast<std::uint64_t>(signed_value);
            return true;
        }
    }
    return false;
}

bool ReadJsonFile(const std::filesystem::path& path, nlohmann::json* value) {
    std::ifstream file(path, std::ios::binary);
    if (!file) {
        return false;
    }
    std::string contents(
        (std::istreambuf_iterator<char>(file)),
        std::istreambuf_iterator<char>());
    try {
        *value = nlohmann::json::parse(contents);
        return true;
    } catch (const nlohmann::json::exception&) {
        LogMessage("JC3 AP bridge: persistent grant state is invalid JSON.");
        return false;
    }
}

}  // namespace

GameBridge::GameBridge(
    std::shared_ptr<IGameBackend> backend,
    std::filesystem::path state_directory)
    : backend_(std::move(backend)),
      state_directory_(std::move(state_directory)) {
    if (!backend_) {
        throw std::invalid_argument("GameBridge requires a game backend.");
    }
    if (!state_directory_.empty()) {
        return;
    }

    wchar_t configured_path[32768]{};
    DWORD length = GetEnvironmentVariableW(
        L"JC3AP_STATE_DIR", configured_path,
        static_cast<DWORD>(std::size(configured_path)));
    if (length > 0 && length < std::size(configured_path)) {
        state_directory_ = configured_path;
        return;
    }

    length = GetEnvironmentVariableW(
        L"LOCALAPPDATA", configured_path,
        static_cast<DWORD>(std::size(configured_path)));
    if (length > 0 && length < std::size(configured_path)) {
        state_directory_ =
            std::filesystem::path(configured_path) / L"JustCause3AP" / L"BridgeState";
    }
}

bool GameBridge::Run(
    std::uint16_t port,
    const std::atomic<bool>& stop_requested) {
    return server_.Run(
        port,
        [this](const nlohmann::json& request) {
            return HandleCommand(request);
        },
        stop_requested);
}

bool GameBridge::Run(const std::atomic<bool>& stop_requested) {
    return Run(ConfiguredPort(), stop_requested);
}

std::size_t GameBridge::ProcessPendingGrants() {
    PollLocationEvents();
    std::size_t processed = 0;
    while (processed < kMaxGrantsPerTick) {
        GrantRequest request;
        SessionKey key;
        bool already_applied = false;
        {
            std::lock_guard lock(grant_mutex_);
            auto candidate = grant_queue_.end();
            for (auto current = grant_queue_.begin();
                 current != grant_queue_.end(); ++current) {
                key = {current->seed_name, current->slot_name};
                if (!LoadNextDeliveryIndex(key, nullptr)) {
                    continue;
                }
                const std::uint64_t next = next_delivery_index_.at(key);
                if (current->delivery_index <= next) {
                    candidate = current;
                    already_applied = current->delivery_index < next;
                    break;
                }
            }
            if (candidate == grant_queue_.end()) {
                break;
            }

            request = *candidate;
            if (already_applied) {
                grant_queue_.erase(candidate);
            } else {
                if (in_flight_.contains(
                        {request.seed_name, request.slot_name,
                         request.delivery_index})) {
                    break;
                }
                in_flight_.insert(
                    {request.seed_name, request.slot_name,
                     request.delivery_index});
            }
        }

        if (already_applied) {
            server_.Publish(GrantAppliedEvent(request));
            ++processed;
            continue;
        }

        if (!backend_->IsInWorld()) {
            bool notify_failure = false;
            {
                std::lock_guard lock(grant_mutex_);
                const auto key = std::make_tuple(
                    request.seed_name, request.slot_name,
                    request.delivery_index);
                in_flight_.erase(key);
                notify_failure = failure_notified_.insert(key).second;
            }
            if (notify_failure) {
                server_.Publish(GrantFailedEvent(request, "world_not_loaded"));
            }
            break;
        }

        bool applied = false;
        std::string failure_reason;
        try {
            applied = backend_->ApplyItem(
                request, ClassifyItem(request), &failure_reason);
        } catch (const std::exception& error) {
            OutputDebugStringA(
                "JC3 AP bridge: game backend threw while applying an item.\n");
            OutputDebugStringA(error.what());
            OutputDebugStringA("\n");
        } catch (...) {
            LogMessage("JC3 AP bridge: game backend raised an unknown exception.");
        }
        if (!applied) {
            bool notify_failure = false;
            {
                std::lock_guard lock(grant_mutex_);
                const auto key = std::make_tuple(
                    request.seed_name, request.slot_name,
                    request.delivery_index);
                in_flight_.erase(key);
                notify_failure = failure_notified_.insert(key).second;
            }
            if (notify_failure) {
                server_.Publish(GrantFailedEvent(
                    request, failure_reason.empty()
                        ? "game_action_rejected"
                        : failure_reason));
            }
            break;
        }

        const SessionKey request_key{request.seed_name, request.slot_name};
        bool persisted = false;
        {
            std::lock_guard lock(grant_mutex_);
            const auto next = next_delivery_index_.at(request_key);
            if (next != std::numeric_limits<std::uint64_t>::max() &&
                PersistNextDeliveryIndex(request_key, next + 1)) {
                next_delivery_index_[request_key] = next + 1;
                auto queued = std::find_if(
                    grant_queue_.begin(), grant_queue_.end(),
                    [&request](const GrantRequest& candidate) {
                        return candidate.seed_name == request.seed_name &&
                               candidate.slot_name == request.slot_name &&
                               candidate.delivery_index == request.delivery_index;
                    });
                if (queued != grant_queue_.end()) {
                    grant_queue_.erase(queued);
                }
                failure_notified_.erase(
                    {request.seed_name, request.slot_name,
                     request.delivery_index});
                persisted = true;
            }
            in_flight_.erase(
                {request.seed_name, request.slot_name,
                 request.delivery_index});
        }
        if (!persisted) {
            LogMessage("JC3 AP bridge: could not persist applied grant state.");
            break;
        }

        server_.Publish(GrantAppliedEvent(request));
        ++processed;
    }
    return processed;
}

nlohmann::json GameBridge::HandleCommand(const nlohmann::json& request) {
    if (!request.is_object() || !request.contains("cmd") ||
        !request["cmd"].is_string()) {
        return {{"event", "error"}, {"error", "invalid_command"}};
    }

    const std::string command = request["cmd"].get<std::string>();
    if (command == "ping") {
        return {{"event", "pong"},
                {"game", "JC3"},
                {"base_addr", "unavailable"}};
    }
    if (command == "get_status") {
        return GetStatus();
    }
    if (command == "read_u32" || command == "write_u32") {
        return {{"event", "error"}, {"error", "memory_commands_disabled"}};
    }
    if (command != "grant_item") {
        return {{"event", "error"}, {"error", "unknown_command"}};
    }
    if (!request.contains("seed_name") ||
        !IsSafeIdentity(request["seed_name"]) ||
        !request.contains("slot_name") ||
        !IsSafeIdentity(request["slot_name"]) ||
        !request.contains("delivery_index") ||
        (!request["delivery_index"].is_number_unsigned() &&
         !request["delivery_index"].is_number_integer()) ||
        !request.contains("item_code") ||
        (!request["item_code"].is_number_unsigned() &&
         !request["item_code"].is_number_integer()) ||
        !request.contains("item_id") ||
        !request["item_id"].is_string() ||
        !request.contains("count") ||
        !request["count"].is_number_integer()) {
        return {{"event", "error"}, {"error", "invalid_grant"}};
    }

    std::uint64_t delivery_index = 0;
    std::uint64_t item_code = 0;
    if (!ReadUnsignedInteger(request["delivery_index"], &delivery_index) ||
        !ReadUnsignedInteger(request["item_code"], &item_code)) {
        return {{"event", "error"}, {"error", "invalid_grant"}};
    }
    const nlohmann::json& count_value = request["count"];
    std::uint64_t count = 0;
    if (count_value.is_number_unsigned()) {
        count = count_value.get<std::uint64_t>();
    } else {
        const std::int64_t signed_count = count_value.get<std::int64_t>();
        if (signed_count < 1 || signed_count > 99) {
            return {{"event", "error"}, {"error", "invalid_grant"}};
        }
        count = static_cast<std::uint64_t>(signed_count);
    }

    const std::string seed_name = request["seed_name"].get<std::string>();
    const std::string slot_name = request["slot_name"].get<std::string>();
    const std::string item_id = request["item_id"].get<std::string>();
    if (item_code > std::numeric_limits<std::uint32_t>::max() ||
        !IsSafeItemId(item_id) || count < 1 || count > 99 ||
        delivery_index == std::numeric_limits<std::uint64_t>::max()) {
        return {{"event", "error"}, {"error", "invalid_grant"}};
    }
    const auto catalog_entry = std::lower_bound(
        kItemCatalog.begin(), kItemCatalog.end(),
        static_cast<std::uint32_t>(item_code),
        [](const CatalogEntry& entry, std::uint32_t id) {
            return entry.ap_id < id;
        });
    if (catalog_entry == kItemCatalog.end() ||
        catalog_entry->ap_id != item_code) {
        return {{"event", "error"}, {"error", "unknown_item_id"}};
    }

    GrantRequest grant{
        seed_name, slot_name, delivery_index,
        static_cast<std::uint32_t>(item_code), item_id,
        static_cast<int>(count)};
    if (ClassifyItem(grant) == ItemCategory::Unknown) {
        return {{"event", "error"}, {"error", "unmapped_item_category"}};
    }
    const SessionKey key{seed_name, slot_name};
    {
        std::lock_guard lock(grant_mutex_);
        active_session_ = key;
        if (!LoadNextDeliveryIndex(key, nullptr)) {
            return {{"event", "error"}, {"error", "state_unavailable"}};
        }
        if (delivery_index < next_delivery_index_.at(key)) {
            return GrantAppliedEvent(grant);
        }

        const auto queued = std::find_if(
            grant_queue_.begin(), grant_queue_.end(),
            [&grant](const GrantRequest& current) {
                return current.seed_name == grant.seed_name &&
                       current.slot_name == grant.slot_name &&
                       current.delivery_index == grant.delivery_index;
            });
        if (queued != grant_queue_.end()) {
            if (queued->item_code != grant.item_code ||
                queued->item_id != grant.item_id ||
                queued->count != grant.count) {
                return {{"event", "error"}, {"error", "delivery_conflict"}};
            }
            failure_notified_.erase(
                {grant.seed_name, grant.slot_name, grant.delivery_index});
            return {{"event", "grant_queued"},
                    {"delivery_index", delivery_index},
                    {"seed_name", seed_name},
                    {"slot_name", slot_name},
                    {"item_id", item_id},
                    {"count", count}};
        }
        if (grant_queue_.size() >= kMaxQueuedGrants) {
            return {{"event", "error"}, {"error", "grant_queue_full"}};
        }
        grant_queue_.push_back(std::move(grant));
    }

    return {{"event", "grant_queued"},
            {"delivery_index", delivery_index},
            {"seed_name", seed_name},
            {"slot_name", slot_name},
            {"item_id", item_id},
            {"count", count}};
}

bool GameBridge::LoadNextDeliveryIndex(
    const SessionKey& key,
    std::uint64_t* next_index) {
    const auto loaded = state_loaded_.find(key);
    if (loaded != state_loaded_.end()) {
        if (next_index != nullptr) {
            *next_index = next_delivery_index_.at(key);
        }
        return loaded->second;
    }

    if (state_directory_.empty()) {
        LogMessage("JC3 AP bridge: no persistent state directory is available.");
        state_loaded_[key] = false;
        return false;
    }

    const std::filesystem::path path = StateFilePath(key);
    nlohmann::json state;
    std::uint64_t next = 0;
    const DWORD file_attributes = GetFileAttributesW(path.c_str());
    if (file_attributes != INVALID_FILE_ATTRIBUTES) {
        if (!ReadJsonFile(path, &state) ||
            !state.is_object() ||
            !state.contains("seed_name") ||
            !state["seed_name"].is_string() ||
            state["seed_name"].get<std::string>() != key.first ||
            !state.contains("slot_name") ||
            !state["slot_name"].is_string() ||
            state["slot_name"].get<std::string>() != key.second ||
            !state.contains("next_delivery_index") ||
            (!state["next_delivery_index"].is_number_unsigned() &&
             !state["next_delivery_index"].is_number_integer())) {
            LogMessage("JC3 AP bridge: persistent grant state does not match its session.");
            state_loaded_[key] = false;
            return false;
        }
        if (!ReadUnsignedInteger(state["next_delivery_index"], &next)) {
            state_loaded_[key] = false;
            return false;
        }
    } else {
        const DWORD error = GetLastError();
        if (error != ERROR_FILE_NOT_FOUND && error != ERROR_PATH_NOT_FOUND) {
            LogMessage("JC3 AP bridge: could not read persistent grant state.");
            state_loaded_[key] = false;
            return false;
        }
    }

    next_delivery_index_[key] = next;
    state_loaded_[key] = true;
    if (next_index != nullptr) {
        *next_index = next;
    }
    return true;
}

bool GameBridge::PersistNextDeliveryIndex(
    const SessionKey& key,
    std::uint64_t next_index) {
    std::error_code error;
    std::filesystem::create_directories(state_directory_, error);
    if (error) {
        return false;
    }

    const std::filesystem::path path = StateFilePath(key);
    std::filesystem::path temporary = path;
    temporary += L".tmp." + std::to_wstring(GetCurrentProcessId()) +
                 L"." + std::to_wstring(GetCurrentThreadId());
    const std::string contents = nlohmann::json{
        {"seed_name", key.first},
        {"slot_name", key.second},
        {"next_delivery_index", next_index}}.dump();

    HANDLE file = CreateFileW(
        temporary.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS,
        FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) {
        return false;
    }

    DWORD written = 0;
    const bool write_ok =
        WriteFile(file, contents.data(),
                  static_cast<DWORD>(contents.size()), &written, nullptr) &&
        written == static_cast<DWORD>(contents.size()) &&
        FlushFileBuffers(file);
    CloseHandle(file);
    if (!write_ok ||
        !MoveFileExW(temporary.c_str(), path.c_str(),
                     MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        DeleteFileW(temporary.c_str());
        return false;
    }
    return true;
}

std::filesystem::path GameBridge::StateFilePath(const SessionKey& key) const {
    std::uint64_t hash = 14695981039346656037ull;
    const auto update_hash = [&hash](std::string_view text) {
        for (const unsigned char byte : text) {
            hash ^= byte;
            hash *= 1099511628211ull;
        }
    };
    update_hash(key.first);
    hash ^= 0;
    hash *= 1099511628211ull;
    update_hash(key.second);

    std::wostringstream file_name;
    file_name << L"delivery_" << std::hex << std::setw(16)
              << std::setfill(L'0') << hash << L".json";
    return state_directory_ / file_name.str();
}

ItemCategory GameBridge::ClassifyItem(const GrantRequest& request) {
    const auto entry = std::lower_bound(
        kItemCatalog.begin(), kItemCatalog.end(), request.item_code,
        [](const CatalogEntry& item, std::uint32_t id) {
            return item.ap_id < id;
        });
    if (entry == kItemCatalog.end() || entry->ap_id != request.item_code) {
        return ItemCategory::Unknown;
    }
    if (entry->category == "vehicle") {
        return ItemCategory::VehicleUnlock;
    }
    if (entry->category == "weapon") {
        return ItemCategory::WeaponUnlock;
    }
    if (entry->category == "rebel_drop") {
        return ItemCategory::RebelDrop;
    }
    if (entry->category == "gear_mod") {
        return ItemCategory::GearMod;
    }
    if (entry->category == "fow_hazard" ||
        entry->category == "hazard_disabler") {
        return ItemCategory::HazardDisabler;
    }
    if (entry->category == "tether") {
        return ItemCategory::TetherProgression;
    }
    if (entry->category == "wingsuit") {
        return ItemCategory::WingsuitProgression;
    }
    return ItemCategory::Unknown;
}

nlohmann::json GameBridge::GrantAppliedEvent(const GrantRequest& request) {
    return {{"event", "grant_applied"},
            {"delivery_index", request.delivery_index},
            {"seed_name", request.seed_name},
            {"slot_name", request.slot_name},
            {"item_id", request.item_id},
            {"count", request.count}};
}

nlohmann::json GameBridge::GrantFailedEvent(
    const GrantRequest& request,
    std::string_view reason) {
    return {{"event", "grant_failed"},
            {"delivery_index", request.delivery_index},
            {"seed_name", request.seed_name},
            {"slot_name", request.slot_name},
            {"item_id", request.item_id},
            {"count", request.count},
            {"reason", std::string(reason)},
            {"retryable", true}};
}

nlohmann::json GameBridge::GetStatus() {
    std::lock_guard lock(grant_mutex_);
    nlohmann::json active_session = nullptr;
    nlohmann::json delivery_index = nullptr;
    if (active_session_.has_value()) {
        const SessionKey& key = *active_session_;
        active_session = key.first + "/" + key.second;
        if (LoadNextDeliveryIndex(key, nullptr)) {
            delivery_index = next_delivery_index_.at(key);
        }
    }
    return {{"event", "status"},
            {"backend_mode", std::string(backend_->Mode())},
            {"in_world", backend_->IsInWorld()},
            {"active_session_id", active_session},
            {"delivery_index", delivery_index},
            {"registered_catalog_handlers",
             backend_->RegisteredCatalogHandlerCount()},
            {"item_catalog_entries", kItemCatalog.size()},
            {"location_catalog_entries", kLocationCatalog.size()}};
}

void GameBridge::PollLocationEvents() {
    if (!backend_->IsInWorld()) {
        return;
    }
    {
        std::lock_guard lock(location_poll_mutex_);
        const auto now = std::chrono::steady_clock::now();
        if (last_location_poll_.time_since_epoch().count() != 0 &&
            now - last_location_poll_ < std::chrono::milliseconds(100)) {
            return;
        }
        last_location_poll_ = now;
    }
    try {
        for (const std::uint32_t location_id :
             backend_->PollCompletedLocations()) {
            OnSettlementLiberated(location_id);
        }
    } catch (const std::exception& error) {
        LogMessage("JC3 AP bridge: location poll callback failed.");
        OutputDebugStringA(error.what());
        OutputDebugStringA("\n");
    } catch (...) {
        LogMessage("JC3 AP bridge: location poll callback raised an unknown exception.");
    }
}

bool GameBridge::PublishLocationChecked(std::uint64_t location_id) {
    const auto entry = std::lower_bound(
        kLocationCatalog.begin(), kLocationCatalog.end(), location_id,
        [](const CatalogEntry& location, std::uint64_t id) {
            return location.ap_id < id;
        });
    if (entry == kLocationCatalog.end() || entry->ap_id != location_id) {
        LogMessage("JC3 AP bridge: ignored location ID absent from catalog.");
        return false;
    }
    std::lock_guard lock(location_mutex_);
    if (reported_location_ids_.contains(location_id)) {
        return true;
    }
    if (!server_.Publish({{"event", "location_checked"},
                          {"location_id", location_id}})) {
        return false;
    }
    reported_location_ids_.insert(location_id);
    return true;
}

bool GameBridge::OnSettlementLiberated(std::uint64_t location_id) {
    return PublishLocationChecked(location_id);
}

bool GameBridge::OnCollectibleCounterChanged(
    std::uint64_t location_id,
    std::uint32_t current,
    std::uint32_t required) {
    return required != 0 && current >= required &&
           PublishLocationChecked(location_id);
}

}  // namespace jc3ap
