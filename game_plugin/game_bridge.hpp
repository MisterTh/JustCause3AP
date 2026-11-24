#pragma once

#include "ipc_server.hpp"
#include "jc3_catalog.hpp"

#include <Windows.h>

#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <filesystem>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <tuple>
#include <vector>

namespace jc3ap {

enum class ItemCategory {
    VehicleUnlock,
    WeaponUnlock,
    RebelDrop,
    GearMod,
    HazardDisabler,
    TetherProgression,
    WingsuitProgression,
    Unknown,
};

struct GrantRequest {
    std::string seed_name;
    std::string slot_name;
    std::uint64_t delivery_index = 0;
    std::uint32_t item_code = 0;
    std::string item_id;
    int count = 0;
};

class IGameBackend {
public:
    virtual ~IGameBackend() = default;
    virtual bool ApplyItem(
        const GrantRequest& request,
        ItemCategory category,
        std::string* failure_reason) = 0;
    virtual bool IsInWorld() const = 0;
    virtual void SetInWorld(bool in_world) = 0;
    virtual std::string_view Mode() const = 0;
    virtual std::size_t RegisteredCatalogHandlerCount() const = 0;
    virtual bool GrantCallbackRegistered() const = 0;
    virtual std::vector<std::uint32_t> PollCompletedLocations() = 0;
};

class MockGameBackend final : public IGameBackend {
public:
    bool ApplyItem(
        const GrantRequest& request,
        ItemCategory category,
        std::string* failure_reason) override;
    bool IsInWorld() const override;
    void SetInWorld(bool in_world) override;
    std::string_view Mode() const override;
    std::size_t RegisteredCatalogHandlerCount() const override;
    bool GrantCallbackRegistered() const override;
    std::vector<std::uint32_t> PollCompletedLocations() override;
    std::size_t ApplyCount() const;

private:
    mutable std::mutex mutex_;
    std::size_t apply_count_ = 0;
    std::atomic<bool> in_world_{true};
};

class LiveGameBackend final : public IGameBackend {
public:
    using ApplyCallback = BOOL(WINAPI*)(
        void* context,
        std::uint64_t delivery_index,
        std::uint32_t ap_item_id,
        const char* game_key,
        const char* display_name,
        int count);
    using LocationCompletedCallback = BOOL(WINAPI*)(
        void* context,
        std::uint32_t ap_location_id,
        const char* category,
        const char* game_key);
    using GrantCallback = BOOL(WINAPI*)(
        void* context,
        std::uint64_t delivery_index,
        std::uint32_t ap_item_id,
        const char* game_key,
        const char* display_name,
        const char* category,
        int count);

    struct Callbacks {
        void* context = nullptr;
        ApplyCallback vehicle_unlock = nullptr;
        ApplyCallback weapon_unlock = nullptr;
        ApplyCallback rebel_drop = nullptr;
        ApplyCallback gear_mod = nullptr;
        ApplyCallback hazard_disabler = nullptr;
        ApplyCallback tether_progression = nullptr;
        ApplyCallback wingsuit_progression = nullptr;
        LocationCompletedCallback location_completed = nullptr;
    };

    void SetCallbacks(const Callbacks& callbacks);
    void SetGrantCallback(void* context, GrantCallback callback);
    bool ApplyItem(
        const GrantRequest& request,
        ItemCategory category,
        std::string* failure_reason) override;
    bool IsInWorld() const override;
    void SetInWorld(bool in_world) override;
    std::string_view Mode() const override;
    std::size_t RegisteredCatalogHandlerCount() const override;
    bool GrantCallbackRegistered() const override;
    std::vector<std::uint32_t> PollCompletedLocations() override;

private:
    mutable std::mutex mutex_;
    Callbacks callbacks_;
    void* grant_context_ = nullptr;
    GrantCallback grant_callback_ = nullptr;
    std::atomic<bool> in_world_{false};
};

class GameBridge {
public:
    explicit GameBridge(
        std::shared_ptr<IGameBackend> backend,
        std::filesystem::path state_directory = {});

    bool Run(
        std::uint16_t port,
        const std::atomic<bool>& stop_requested);
    bool Run(const std::atomic<bool>& stop_requested);
    std::size_t ProcessPendingGrants();
    bool PublishLocationChecked(std::uint64_t location_id);
    bool OnSettlementLiberated(std::uint64_t location_id);
    bool OnCollectibleCounterChanged(
        std::uint64_t location_id,
        std::uint32_t current,
        std::uint32_t required);
    nlohmann::json GetStatus();

private:
    using SessionKey = std::pair<std::string, std::string>;

    nlohmann::json HandleCommand(const nlohmann::json& request);
    bool LoadNextDeliveryIndex(const SessionKey& key, std::uint64_t* next_index);
    bool PersistNextDeliveryIndex(
        const SessionKey& key,
        std::uint64_t next_index);
    std::filesystem::path StateFilePath(const SessionKey& key) const;
    static ItemCategory ClassifyItem(const GrantRequest& request);
    static nlohmann::json GrantAppliedEvent(const GrantRequest& request);
    static nlohmann::json GrantFailedEvent(
        const GrantRequest& request,
        std::string_view reason);
    void PollLocationEvents();

    IpcServer server_;
    std::shared_ptr<IGameBackend> backend_;
    std::filesystem::path state_directory_;
    std::mutex grant_mutex_;
    std::deque<GrantRequest> grant_queue_;
    std::set<std::tuple<std::string, std::string, std::uint64_t>> in_flight_;
    std::set<std::tuple<std::string, std::string, std::uint64_t>>
        failure_notified_;
    std::map<SessionKey, std::uint64_t> next_delivery_index_;
    std::map<SessionKey, std::uint64_t> last_applied_index_;
    std::map<SessionKey, bool> state_loaded_;
    std::optional<SessionKey> active_session_;
    std::mutex location_mutex_;
    std::mutex location_poll_mutex_;
    std::chrono::steady_clock::time_point last_location_poll_{};
    std::set<std::uint64_t> reported_location_ids_;
};

}  // namespace jc3ap
