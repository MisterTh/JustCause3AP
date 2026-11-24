# JC3 AP game plugin

`jc3_ap_plugin.dll` is the Windows x64 loopback bridge, and `dinput8.dll` is a
drop-in DirectInput proxy that loads `jc3_ap_plugin.dll` from the same directory
as itself. The plugin may also be installed directly through a supported JC3
mod loader. Neither reads or writes game memory or installs hooks. Game actions
and state queries are exposed through adapter callbacks because this project
does not have a verified JC3 loader SDK.

The bridge listens on `127.0.0.1:54321` and exchanges newline-delimited JSON.
Set `JC3AP_PORT` before launching the game to change the port.

## Protocol and adapter

Supported commands include `ping`, `get_status`, and indexed `grant_item`
requests. Raw-memory commands are explicitly disabled. See
[`../docs/bridge-protocol.md`](../docs/bridge-protocol.md) for request and
response examples.

The adapter should call `JC3AP_SetInWorld(FALSE)` until it has verified that
the player is in Medici, then call `JC3AP_SetInWorld(TRUE)` from a supported
game callback. Queued grants are deferred out of world and report a retryable
`grant_failed` event. Invoke `JC3AP_ProcessPendingGrants` from the game's
supported game-thread callback.

`JC3AP_RegisterGameCallbacks` supplies category-specific item dispatch and
location polling callbacks. The callback receives the catalog's `game_key`,
which is descriptive AP source metadata—not a verified engine hash or SDK
handle. Successful location callbacks are polled at most every 100 ms and
reported once per plugin lifetime. Real game effects and event detectors must
be added only after their IDs and APIs have been verified.

### C ABI SDK

External mod loaders and adapter DLLs can link against
[`include/jc3_ap_sdk.h`](./include/jc3_ap_sdk.h). The header is the documented
C ABI contract (`JC3AP_SDK_VERSION` is currently `1`) and declares:

- `JC3AP_Initialize()` and `JC3AP_Shutdown()`
- `JC3AP_GetBuildTimestamp()`
- `JC3AP_SetInWorld(int in_world)`
- `JC3AP_RegisterGrantCallback(void* context, JC3AP_GrantCallback callback)`
- `JC3AP_ReportLocationCompleted(uint32_t ap_location_id)`
- `JC3AP_ReportLocationByKey(const char* category, const char* game_key)`

The grant callback receives the ordered AP delivery index, AP item ID, catalog
game key, display name, item category, and count. Register it once after the
supported mod loader has loaded the plugin, set in-world state only from a
supported game callback, and call `JC3AP_ProcessPendingGrants()` periodically
from the game thread. Location callbacks may report a catalog ID directly or
use category plus game key. The plugin never scans memory or probes pointers;
an adapter is responsible for obtaining supported in-world and action state
and forwarding it through these callbacks.

At runtime, `get_status` reports `grant_callback_registered`, `in_world`,
`catalog_entries`, `session_id`, `last_applied_index`, and
`pending_queue_size`, in addition to compatibility status fields.

## Hybrid console commands

The Just Cause 3 AP client supports:

- `/status` - show bridge, callback, session, and pending queue state
- `/inworld <on|off>` - manually toggle the bridge world gate for hybrid testing
- `/check <location_name_or_id>` - fuzzily match a location name or accept an AP ID
- `/missing [region]` - list unchecked locations, optionally in
  `Insula Fonte`, `Insula Dracon`, or `Insula Striate`
- `/received` - list received AP items with delivery index and applied/queued status
- `/snapshot_save <label>` - copy a save profile into a labeled local snapshot

On successful AP authentication the client syncs the seed and slot identity to
the bridge, independently of the in-world state. When the bridge reports that
no game grant callback is registered, received items are logged with their
item ID and delivery index for manual playtesting instead of being retried
against unavailable game hooks. This reports items to the player; it does not
apply their in-game effects.

The read-only telemetry watcher is enabled automatically. It watches
`jc3_ap_events.json` in the client working directory and can also watch
`Documents\Square Enix\Just Cause 3\Saves\` or a custom directory supplied with
`--save-directory`. Use `--telemetry-file <path>` to choose another JSON drop
file. JSON entries may contain location IDs, AP names, catalog `asset_id`
values, or `locations`/`checks` arrays.

Save snapshots are copied read-only into `save_snapshots\` under the client
working directory. The standard `Saves\` root is resolved to its SteamID
profile; if several profiles exist, the client chooses the one with the newest
save-file modification time and logs the selected profile. Use
`--save-directory` to target a specific profile. Compare snapshots with:

```powershell
py scripts\diff_jc3_saves.py --before save_snapshots\<before> --after save_snapshots\<after>
py scripts\diff_jc3_saves.py --before save_snapshots\<before> --after save_snapshots\<after> --summary
py scripts\diff_jc3_saves.py --correlate save_snapshots\<before> save_snapshots\<after>
```

For a single location, take a before snapshot, liberate only that location,
take an after snapshot, and learn its record:

```powershell
py scripts\diff_jc3_saves.py --learn-location "Settlement: Agro" `
  --before save_snapshots\<before> `
  --after save_snapshots\<after> `
  --mapping save_snapshots\jc3_settlement_save_map.json
```

`--learn-location` requires exactly one changed existing state record or one
new record and refuses ambiguous pairs. It records the little-endian record
key, changed state-bit mask (or record-presence match), expected
post-liberation state, and sample offset. Record offsets are not stable when
the save's record list shifts; the map matches by record key. Use the exact AP
location name. Repeat this process for each settlement or base and the JSON
map accumulates the entries.

Start the client with `--settlement-map <map.json>` (or set
`JC3AP_SETTLEMENT_MAP`) to enable read-only checks from
`settlement_achievement_status`. The client matches each mapped key and state
mask against the active profile and sends the corresponding AP
`LocationChecks` when the expected state is present. The data format is
empirical, so validate entries with isolated before/after saves first.

The report includes file sizes and SHA-256 hashes, contiguous changed byte
ranges, little-endian 32-bit/64-bit numeric interpretations, and nearby
printable ASCII strings. Numeric interpretations are raw values, not verified
game flags or hash formats. `--summary` omits candidates in size-shifted files
and ignores the first four bytes as a probable per-file checksum.
`--correlate` reports XOR masks for stable offsets and recognizes the observed
`settlement_achievement_status` (5-byte records from `0x62`) and
`unlocked_resupply_points` (3-byte state + 5-byte key records from `0x68`, with
one trailing byte) layouts. Record keys and state bytes are hypotheses until
confirmed by repeatable single-base captures.

## Local multiworld smoke test

1. Copy `Players/Templates/Just Cause 3.yaml` to `Players/`, adjust the slot
   name and goal, and add at least one YAML for another game or a second JC3 slot.
2. Run `ArchipelagoGenerate.exe` and confirm `output/*.zip` is created.
3. Start `ArchipelagoServer.exe` with the generated seed, then start the
   Just Cause 3 client and connect to `host:port`.
4. In the client, run `/status`, `/missing Insula Fonte`, and
   `/check <location name or ID>` to verify bridge/manual tracking.
5. Complete the configured goal; the client sends `CLIENT_GOAL` after story
   missions, the configured low-liberation percentage, or all locations as
   selected in the world YAML.

## Catalog

[`jc3_catalog.hpp`](./jc3_catalog.hpp) is generated from
[`worlds/justcause3/archi_defs.py`](../worlds/justcause3/archi_defs.py). It
contains 87 catalog items, three progression items, Rebel Supply Drop filler,
and 304 location IDs.
Regenerate and verify it from the repository root:

```powershell
py scripts/sync_jc3_catalog.py
py scripts/sync_jc3_catalog.py --check
```

The parity check verifies exact IDs, names, categories, and source keys. It
does not validate the candidate data against live game state.

## Build and tests

Use Visual Studio Build Tools with the C++ workload, Windows SDK, CMake 3.20+,
and Git (CMake FetchContent downloads `nlohmann_json`):

```powershell
cmake -S game_plugin -B build/cmake -A x64
cmake --build build/cmake --config Release
py game_plugin/test_bridge.py --mock
py game_plugin/test_bridge.py --native-host build/Release/jc3_ap_bridge_test_host.exe
py game_plugin/test_bridge.py --plugin-dll build/Release/jc3_ap_plugin.dll --port 55431
py game_plugin/test_bridge.py --proxy-dll build/Release/dinput8.dll --port 55432
```

The outputs are under `build/Release/`. The native host tests persistent
duplicate suppression, out-of-world deferral, live callback dispatch, status
output, and location polling.

## Drop-In Installation

Copy both `dinput8.dll` and `jc3_ap_plugin.dll` from the Windows x64 release
into the game installation folder, typically
`steamapps\common\Just Cause 3\` (the folder containing `JustCause3.exe`).
The proxy forwards DirectInput exports to the Windows system
`System32\dinput8.dll`, loads the sibling bridge plugin by its absolute path,
and logs that path and the plugin build timestamp through `OutputDebugString`.
Remove the proxy DLL to disable it. Do not install both this proxy and another
`dinput8.dll` mod loader in the game folder; they cannot occupy the same file.

Launch the game, then verify the local bridge is responding with:

```powershell
py game_plugin/test_bridge.py --host 127.0.0.1 --port 54321
```

The default port can be changed by setting `JC3AP_PORT` before launching the
game, and the same port must be passed with `--port`. Alternatively, connect
the Just Cause 3 client through Archipelago; the client uses this bridge, and
its `/status` command reports the connection state.

For users of an existing mod loader, copy `jc3_ap_plugin.dll` into its `mods`
directory instead of installing the drop-in proxy.

The AP client is [`Client.py`](../worlds/justcause3/Client.py). It supports
`/status`, `/inworld <on|off>`, `/check <location name or AP ID>`,
`/missing [region]`, and `/received` for hybrid/manual tracking. Since no
loader currently reports Rico's current region, the missing-location command
accepts an optional region name or lists all remaining checks.
