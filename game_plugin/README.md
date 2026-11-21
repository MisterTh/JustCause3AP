# JC3 AP game plugin

`jc3_ap_plugin.dll` and `dinput8.dll` are two Windows x64 builds of the
loopback-only TCP bridge. `jc3_ap_plugin.dll` is for a supported JC3 mod loader;
`dinput8.dll` is a drop-in DirectInput proxy. Neither reads or writes game
memory or installs hooks. Game actions and state queries are exposed through
adapter callbacks because this project does not have a verified JC3 loader SDK.

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

## Catalog

[`jc3_catalog.hpp`](./jc3_catalog.hpp) is generated from
[`worlds/justcause3/archi_defs.py`](../worlds/justcause3/archi_defs.py). It
contains 87 defined items, Rebel Supply Drop filler, and 304 location IDs.
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
```

The outputs are under `build/Release/`. The native host tests persistent
duplicate suppression, out-of-world deferral, live callback dispatch, status
output, and location polling.

## Drop-In Installation

Copy `dinput8.dll` from the Windows x64 release into the game installation
folder, typically
`steamapps\common\Just Cause 3\` (the folder containing `JustCause3.exe`).
The proxy forwards DirectInput exports to the Windows system
`System32\dinput8.dll` and starts the bridge on a background thread. Remove the
proxy DLL to disable it. Do not install both this proxy and another
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
`/status`, `/check <location name or AP ID>`, and
`/list_missing [region]` for hybrid/manual tracking. Since no loader currently
reports Rico's current region, the missing-location command accepts an
optional region name or lists all remaining checks.
