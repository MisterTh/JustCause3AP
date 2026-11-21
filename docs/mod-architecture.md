# Game-side integration architecture

The AP client owns the Archipelago server connection. The C++ plugin exposes a
loopback NDJSON bridge to the client and a callback boundary for a supported
JC3 mod loader.

The repository does not currently include verified JC3 engine hashes or a
supported loader SDK. Consequently, item dispatch and location polling are
callback interfaces, not fabricated native game actions. The AP client also
supports manual/hybrid checks when no loader callback is available.

See [`../game_plugin/README.md`](../game_plugin/README.md) for the plugin build
and adapter callbacks, [`bridge-protocol.md`](./bridge-protocol.md) for the
wire format, and [`../README.md`](../README.md) for the monorepo and test
layout.
