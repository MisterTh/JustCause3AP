# JC3 AP bridge protocol

The client/plugin transport is UTF-8 NDJSON over TCP, bound to
`127.0.0.1:54321` by default. Each request and response is one JSON object per
line. No Archipelago password or server credentials are sent to the game
plugin.

## Status

Request:

```json
{"cmd":"get_status"}
```

The plugin returns `event: "status"` with `backend_mode`, `in_world`,
`active_session_id`, the persisted next `delivery_index`, and catalog entry
counts. A `null` session/cursor means no delivery session has been received or
loaded yet.

## Item delivery

```json
{"cmd":"grant_item","seed_name":"seed","slot_name":"Player","delivery_index":0,"item_code":1245904897,"item_id":"uvk_13","count":1}
```

`grant_queued` confirms volatile queue acceptance only. After an adapter
callback succeeds and the cursor is persisted, the plugin pushes
`grant_applied` with the session and delivery index. A repeated applied index
is acknowledged without repeating the callback. While the game is not in
world, the plugin retains the request and pushes a retryable failure:

```json
{"event":"grant_failed","delivery_index":0,"seed_name":"seed","slot_name":"Player","item_id":"uvk_13","count":1,"reason":"world_not_loaded","retryable":true}
```

The Python client retries retryable failures. A callback must be idempotent
by delivery index to close the crash window between a game-side effect and the
durable cursor write.

## Location events

The plugin pushes confirmed catalogued location IDs:

```json
{"event":"location_checked","location_id":1245905153}
```

The Python client validates the ID and forwards it to the Archipelago server
as `LocationChecks`. The plugin queues events while disconnected and
deduplicates confirmed IDs during its process lifetime.

## Other commands

- `{"cmd":"ping"}` returns the JC3 pong handshake.
- `read_u32` and `write_u32` are explicitly rejected as
  `memory_commands_disabled`.

The AP client exposes `/status`, `/check <location name or AP ID>`, and
`/list_missing [region]`. The manual check command uses the AP world tables and
submits a normal `LocationChecks` packet; it does not require game memory
access.
