#include "game_bridge.hpp"

#include <Windows.h>

#include <string>
#include <utility>

namespace jc3ap {
namespace {

const CatalogEntry* FindItem(std::uint32_t ap_id) {
    for (const CatalogEntry& entry : kItemCatalog) {
        if (entry.ap_id == ap_id) {
            return &entry;
        }
    }
    return nullptr;
}

}  // namespace

bool MockGameBackend::ApplyItem(
    const GrantRequest&,
    ItemCategory,
    std::string*) {
    std::lock_guard lock(mutex_);
    ++apply_count_;
    return true;
}

bool MockGameBackend::IsInWorld() const {
    return in_world_.load();
}

void MockGameBackend::SetInWorld(bool in_world) {
    in_world_.store(in_world);
}

std::string_view MockGameBackend::Mode() const {
    return "mock";
}

std::size_t MockGameBackend::RegisteredCatalogHandlerCount() const {
    return kItemCatalog.size() + kLocationCatalog.size();
}

bool MockGameBackend::GrantCallbackRegistered() const {
    return false;
}

std::vector<std::uint32_t> MockGameBackend::PollCompletedLocations() {
    return {};
}

std::size_t MockGameBackend::ApplyCount() const {
    std::lock_guard lock(mutex_);
    return apply_count_;
}

void LiveGameBackend::SetCallbacks(const Callbacks& callbacks) {
    std::lock_guard lock(mutex_);
    callbacks_ = callbacks;
}

void LiveGameBackend::SetGrantCallback(void* context, GrantCallback callback) {
    std::lock_guard lock(mutex_);
    grant_context_ = context;
    grant_callback_ = callback;
}

void LiveGameBackend::SetInWorld(bool in_world) {
    in_world_.store(in_world);
}

bool LiveGameBackend::IsInWorld() const {
    return in_world_.load();
}

std::string_view LiveGameBackend::Mode() const {
    return "live";
}

std::size_t LiveGameBackend::RegisteredCatalogHandlerCount() const {
    return kItemCatalog.size() + kLocationCatalog.size();
}

bool LiveGameBackend::GrantCallbackRegistered() const {
    std::lock_guard lock(mutex_);
    return grant_callback_ != nullptr ||
           callbacks_.vehicle_unlock != nullptr ||
           callbacks_.weapon_unlock != nullptr ||
           callbacks_.rebel_drop != nullptr ||
           callbacks_.gear_mod != nullptr ||
           callbacks_.hazard_disabler != nullptr ||
           callbacks_.tether_progression != nullptr ||
           callbacks_.wingsuit_progression != nullptr;
}

bool LiveGameBackend::ApplyItem(
    const GrantRequest& request,
    ItemCategory category,
    std::string* failure_reason) {
    if (!IsInWorld()) {
        if (failure_reason != nullptr) {
            *failure_reason = "world_not_loaded";
        }
        return false;
    }

    const CatalogEntry* entry = FindItem(request.item_code);
    if (entry == nullptr) {
        if (failure_reason != nullptr) {
            *failure_reason = "catalog_entry_missing";
        }
        return false;
    }

    Callbacks callbacks;
    void* grant_context = nullptr;
    GrantCallback grant_callback = nullptr;
    {
        std::lock_guard lock(mutex_);
        callbacks = callbacks_;
        grant_context = grant_context_;
        grant_callback = grant_callback_;
    }

    const char* category_name = "unknown";
    switch (category) {
        case ItemCategory::VehicleUnlock: category_name = "vehicle_unlock"; break;
        case ItemCategory::WeaponUnlock: category_name = "weapon_unlock"; break;
        case ItemCategory::RebelDrop: category_name = "rebel_drop"; break;
        case ItemCategory::GearMod: category_name = "gear_mod"; break;
        case ItemCategory::HazardDisabler: category_name = "hazard_disabler"; break;
        case ItemCategory::TetherProgression: category_name = "tether_progression"; break;
        case ItemCategory::WingsuitProgression: category_name = "wingsuit_progression"; break;
        case ItemCategory::Unknown: break;
    }

    if (grant_callback != nullptr) {
        if (grant_callback(
                grant_context,
                request.delivery_index,
                entry->ap_id,
                entry->game_key.data(),
                entry->name.data(),
                category_name,
                request.count) == FALSE) {
            if (failure_reason != nullptr) {
                *failure_reason = "game_action_rejected";
            }
            return false;
        }
        return true;
    }

    ApplyCallback callback = nullptr;
    switch (category) {
        case ItemCategory::VehicleUnlock:
            callback = callbacks.vehicle_unlock;
            break;
        case ItemCategory::WeaponUnlock:
            callback = callbacks.weapon_unlock;
            break;
        case ItemCategory::RebelDrop:
            callback = callbacks.rebel_drop;
            break;
        case ItemCategory::GearMod:
            callback = callbacks.gear_mod;
            break;
        case ItemCategory::HazardDisabler:
            callback = callbacks.hazard_disabler;
            break;
        case ItemCategory::TetherProgression:
            callback = callbacks.tether_progression;
            break;
        case ItemCategory::WingsuitProgression:
            callback = callbacks.wingsuit_progression;
            break;
        case ItemCategory::Unknown:
            break;
    }

    if (callback == nullptr) {
        if (failure_reason != nullptr) {
            *failure_reason = "adapter_not_ready";
        }
        return false;
    }

    if (callback(callbacks.context, request.delivery_index, entry->ap_id,
                 entry->game_key.data(), entry->name.data(),
                 request.count) == FALSE) {
        if (failure_reason != nullptr) {
            *failure_reason = "game_action_rejected";
        }
        return false;
    }
    return true;
}

std::vector<std::uint32_t> LiveGameBackend::PollCompletedLocations() {
    if (!IsInWorld()) {
        return {};
    }

    Callbacks callbacks;
    {
        std::lock_guard lock(mutex_);
        callbacks = callbacks_;
    }
    if (callbacks.location_completed == nullptr) {
        return {};
    }

    std::vector<std::uint32_t> completed;
    for (const CatalogEntry& entry : kLocationCatalog) {
        if (callbacks.location_completed(
                callbacks.context, entry.ap_id, entry.category.data(),
                entry.game_key.data()) != FALSE) {
            completed.push_back(entry.ap_id);
        }
    }
    return completed;
}

}  // namespace jc3ap
