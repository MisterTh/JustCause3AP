# Just Cause 3 Archipelago

This repository contains the APWorld source, its build/sync scripts, and a
Windows C++20 loopback bridge plugin.

## Repository layout

- `worlds/justcause3/`: Archipelago world, client, and curated check data.
- `game_plugin/`: C++ IPC plugin, native test host, and protocol tests.
- `scripts/`: catalog synchronization, APWorld packaging, and check-data tools.
- `tests/`: generation, item/location balance, reachability, and victory tests.
- `docs/`: integration and protocol notes.

The world currently has 87 defined item unlocks, the Rebel Supply Drop filler,
and 304 candidate locations. These are curated wiki-derived entries, not
verified game-state identifiers. `Options.py` currently declares no
Just-Cause-3-specific options, and the world logic leaves the checks
independently reachable. Do not treat the source candidates as confirmed
in-game checks until validated.

## Build and test

Generate candidate definitions and the matching C++ registry after changing
curated data:

```powershell
py scripts/get_checks.py
py scripts/sync_jc3_catalog.py
py scripts/sync_jc3_catalog.py --check
```

Build the distributable archive:

```powershell
py scripts/build_apworld.py
```

The generated `justcause3.apworld` is ignored locally; CI builds and publishes
it as an artifact. Install it through Archipelago's normal custom-world
workflow.

The C++ bridge is built on Windows with Visual Studio Build Tools and CMake:

```powershell
cmake -S game_plugin -B build/cmake -A x64
cmake --build build/cmake --config Release
py game_plugin/test_bridge.py --mock
py game_plugin/test_bridge.py --native-host build/Release/jc3_ap_bridge_test_host.exe
```

CI checks the generated catalog, builds the APWorld, runs real Archipelago
world-generation tests, and builds/tests the x64 plugin and native host.
Generation tests assert all 304 checks are reachable, the item pool balances
the unfilled locations, and the victory condition is beatable. There are no
world-specific YAML option presets yet because the world does not define any
Just Cause 3 options.

## Manual / hybrid tracker

The AP client exposes these console commands:

- `/status` — query `get_status` from the local game bridge.
- `/check <location name or AP ID>` — manually report a location check.
- `/list_missing [region]` — list unchecked candidate locations, optionally
  filtered by their world region. The game bridge does not currently identify
  Rico's in-game region, so no automatic current-region filter is available.

The loopback bridge and loader adapter are documented in
[`game_plugin/README.md`](./game_plugin/README.md). Actual JC3 actions and
settlement/collectible state queries remain behind loader callbacks because
there is no verified supported JC3 SDK or API in this repository yet.

## Repository history note

The checked project directory had no `.git` metadata or nested Git repository
when this layout was organized. The files are in monorepo structure, but
preserving an earlier plugin repository's commit history requires access to
that repository's remote or clone. Do not initialize a new Git repository over
the only copy of any existing history.
