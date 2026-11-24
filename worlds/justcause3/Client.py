from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from difflib import get_close_matches
from collections.abc import Sequence
from typing import Any

from CommonClient import (
    ClientCommandProcessor,
    CommonContext,
    gui_enabled,
    get_base_parser,
    logger,
    server_loop,
)
from MultiServer import mark_raw
from Utils import async_start
try:
    from NetUtils import ClientStatus
except ImportError:
    class ClientStatus:
        CLIENT_GOAL = 30

from .Items import item_name_to_id
from .Locations import candidate_location_data, location_table

GAME_NAME = "Just Cause 3"
GAME_HOST = "127.0.0.1"
GAME_PORT = 54321
GAME_RETRY_MAX_SECONDS = 10
GAME_PING_INTERVAL_SECONDS = 10
GAME_ACK_TIMEOUT_SECONDS = 10

ITEM_ID_TO_NAME = {
    item_id: item_name
    for item_name, item_id in item_name_to_id.items()
    if item_id is not None
}
LOCATION_IDS = set(location_table.values())
LOCATION_ID_TO_NAME = {location_id: name for name, location_id in location_table.items()}
LOCATION_ID_TO_REGION = {
    location_table[name]: data["region"]
    for name, data in candidate_location_data.items()
}
LOCATION_ID_TO_NAME_CASEFOLD = {
    name.casefold(): location_id for location_id, name in LOCATION_ID_TO_NAME.items()
}
LOCATION_ASSET_KEYS = {
    data["asset_id"].casefold(): location_table[name]
    for name, data in candidate_location_data.items()
    if isinstance(data.get("asset_id"), str)
}
SETTLEMENT_SAVE_MAP_SCHEMA = 1
SETTLEMENT_SAVE_LAYOUT = {
    "file": "settlement_achievement_status",
    "record_start": 0x62,
    "record_size": 5,
    "key_size": 4,
    "key_byteorder": "little",
    "state_index": 4,
}


def resolve_location_id(value: str) -> int | None:
    query = value.strip()
    try:
        location_id = int(query, 0)
    except ValueError:
        location_id = LOCATION_ID_TO_NAME_CASEFOLD.get(query.casefold())
        if location_id is None:
            matches = [
                location_id
                for name, location_id in LOCATION_ID_TO_NAME_CASEFOLD.items()
                if query.casefold() in name
            ]
            if len(matches) == 1:
                location_id = matches[0]
            else:
                close = get_close_matches(
                    query.casefold(),
                    list(LOCATION_ID_TO_NAME_CASEFOLD.keys()),
                    n=2,
                    cutoff=0.72,
                )
                if len(close) == 1:
                    location_id = LOCATION_ID_TO_NAME_CASEFOLD[close[0]]
                else:
                    location_id = LOCATION_ASSET_KEYS.get(query.casefold())
    return location_id if location_id in LOCATION_IDS else None


def load_settlement_save_map(path: Path) -> dict[str, Any]:
    mapping = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(mapping, dict)
        or mapping.get("schema_version") != SETTLEMENT_SAVE_MAP_SCHEMA
        or mapping.get("layout") != SETTLEMENT_SAVE_LAYOUT
        or not isinstance(mapping.get("locations"), dict)
    ):
        raise ValueError(
            "expected settlement save map schema 1 with the known "
            "settlement_achievement_status record layout"
        )
    return mapping


def settlement_save_location_checks(
    data: bytes,
    mapping: dict[str, Any],
) -> set[int]:
    if (
        mapping.get("schema_version") != SETTLEMENT_SAVE_MAP_SCHEMA
        or mapping.get("layout") != SETTLEMENT_SAVE_LAYOUT
    ):
        raise ValueError("unsupported settlement save map schema or layout")
    start = SETTLEMENT_SAVE_LAYOUT["record_start"]
    record_size = SETTLEMENT_SAVE_LAYOUT["record_size"]
    key_size = SETTLEMENT_SAVE_LAYOUT["key_size"]
    state_index = SETTLEMENT_SAVE_LAYOUT["state_index"]
    if len(data) < start or (len(data) - start) % record_size:
        raise ValueError(
            "settlement save file does not match the observed 5-byte record layout"
        )

    states: dict[int, int] = {}
    for offset in range(start, len(data), record_size):
        record = data[offset : offset + record_size]
        key = int.from_bytes(
            record[:key_size],
            byteorder=SETTLEMENT_SAVE_LAYOUT["key_byteorder"],
        )
        if key in states:
            raise ValueError(f"duplicate settlement record key 0x{key:08X}")
        states[key] = record[state_index]

    locations = mapping.get("locations")
    if not isinstance(locations, dict):
        raise ValueError("settlement save map locations must be an object")

    found: set[int] = set()
    for location_name, entry in locations.items():
        if not isinstance(location_name, str) or not isinstance(entry, dict):
            raise ValueError("malformed settlement save map location entry")
        location_id = location_table.get(location_name)
        if location_id is None:
            raise ValueError(
                f"location {location_name!r} in the settlement save map "
                "is not present in the AP location catalog"
            )
        try:
            record_key = int(entry["record_key"], 0)
            state_mask = int(entry["state_mask"], 0)
            expected_state = int(entry["expected_state"], 0)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                f"invalid save-state values for {location_name!r}"
            ) from error
        match_type = entry.get("match", "state")
        if (
            not 0 <= record_key <= 0xFFFFFFFF
            or not 0 <= state_mask <= 0xFF
            or not 0 <= expected_state <= 0xFF
            or match_type not in {"state", "present"}
            or (match_type == "state" and state_mask == 0)
        ):
            raise ValueError(f"out-of-range save-state values for {location_name!r}")

        current_state = states.get(record_key)
        if current_state is not None and (
            match_type == "present"
            or (current_state & state_mask) == (expected_state & state_mask)
        ):
            found.add(location_id)
    return found if location_id in LOCATION_IDS else None


class JustCause3CommandProcessor(ClientCommandProcessor):
    def _cmd_status(self) -> bool:
        """Show the live JC3 bridge status, if the bridge is connected."""
        async_start(self.ctx.query_game_status(), name="JC3 bridge status")
        return True

    @mark_raw
    def _cmd_inworld(self, value: str = "") -> bool:
        """Set the bridge's playtest world state to on or off."""
        normalized = value.strip().casefold()
        if normalized not in {"on", "off"}:
            self.output("Usage: /inworld <on|off>")
            return False
        async_start(
            self.ctx.set_game_world_state(normalized == "on"),
            name="JC3 bridge world state",
        )
        return True

    @mark_raw
    def _cmd_check(self, location: str = "") -> bool:
        """Manually check a JC3 location by full name or AP ID."""
        if not location.strip():
            self.output("Usage: /check <location name or AP ID>")
            return False
        location_id = resolve_location_id(location)
        if location_id is None:
            self.output(
                f"Unknown or ambiguous JC3 location {location!r}; "
                "use its full name or AP ID."
            )
            return False
        async_start(self.ctx.manual_check(location_id), name="JC3 manual check")
        return True

    @mark_raw
    def _cmd_list_missing(self, region: str = "") -> bool:
        """List unchecked JC3 locations, optionally filtered by region."""
        return self.ctx.output_missing_locations(self, region.strip())

    @mark_raw
    def _cmd_missing(self, region: str = "") -> bool:
        """List remaining JC3 locations, optionally filtered by region."""
        return self.ctx.output_missing_locations(self, region.strip())

    @mark_raw
    def _cmd_received(self) -> bool:
        """List AP items received in this client session."""
        return self.ctx.output_received_items(self)

    @mark_raw
    def _cmd_snapshot_save(self, label: str = "") -> bool:
        """Copy the active JC3 save profile into a labeled snapshot."""
        if not label.strip():
            self.output("Usage: /snapshot_save <label>")
            return False
        async_start(
            self.ctx.snapshot_save(label),
            name="JC3 save snapshot",
        )
        return True


def game_item_id(item_name: str) -> str:
    """Convert the AP item display name to the C++ bridge's safe identifier."""
    identifier = re.sub(r"[^a-zA-Z0-9_-]+", "_", item_name).strip("_")
    if not identifier:
        raise ValueError(f"Item name has no bridge-safe characters: {item_name!r}")
    return identifier.lower()


def encode_game_message(message: dict[str, Any]) -> bytes:
    return (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")


class JustCause3Context(CommonContext):
    game = GAME_NAME
    items_handling = 0b111
    command_processor = JustCause3CommandProcessor

    def __init__(
        self,
        server_address: str | None,
        password: str | None,
        game_host: str = GAME_HOST,
        game_port: int = GAME_PORT,
        telemetry_file: str | os.PathLike[str] | None = None,
        save_directory: str | os.PathLike[str] | None = None,
        settlement_map_file: str | os.PathLike[str] | None = None,
    ) -> None:
        super().__init__(server_address, password)
        if game_host != "127.0.0.1":
            raise ValueError("The JC3 bridge must use loopback address 127.0.0.1.")
        if not 1 <= game_port <= 65535:
            raise ValueError("The game bridge port must be between 1 and 65535.")

        self.game_host = game_host
        self.game_port = game_port
        self.game_reader: asyncio.StreamReader | None = None
        self.game_writer: asyncio.StreamWriter | None = None
        self.game_connected = asyncio.Event()
        self.game_write_lock = asyncio.Lock()
        self.pending_grant_ack: asyncio.Future[dict[str, Any]] | None = None
        self.pending_grant_identity: tuple[str, str, int] | None = None
        self.pending_bridge_requests: dict[
            int, tuple[str, asyncio.Future[dict[str, Any]]]
        ] = {}
        self.next_bridge_request_id = 1
        self.status_query_lock = asyncio.Lock()
        self.cached_game_status: dict[str, Any] | None = None
        self.grant_task: asyncio.Task[None] | None = None
        self.game_task: asyncio.Task[None] | None = None
        self.next_item_index = 0
        self.pending_items: dict[int, Any] = {}
        self.pending_location_ids: set[int] = set()
        self.reported_location_ids: set[int] = set()
        self.ap_session_identity: tuple[Any, Any, Any, Any] | None = None
        self.ap_authenticated = False
        self.received_items: dict[int, dict[str, Any]] = {}
        self.goal_sent = False
        self.goal_option: int | str = 1
        self.liberation_goal = 25
        self.telemetry_task: asyncio.Task[None] | None = None
        self.telemetry_file = (
            Path(telemetry_file)
            if telemetry_file is not None
            else Path(os.environ.get("JC3AP_EVENTS_FILE", "jc3_ap_events.json"))
        )
        configured_map = settlement_map_file or os.environ.get(
            "JC3AP_SETTLEMENT_MAP"
        )
        self.settlement_map_file = (
            Path(configured_map) if configured_map is not None else None
        )
        if save_directory is not None:
            self.save_directory = Path(save_directory)
        else:
            configured = os.environ.get("JC3AP_SAVE_DIR")
            if configured:
                self.save_directory = Path(configured)
            else:
                documents = os.environ.get("USERPROFILE")
                self.save_directory = (
                    Path(documents) / "Documents" / "Square Enix" / "Just Cause 3" / "Saves"
                    if documents
                    else None
                )

    async def server_auth(self, password_requested: bool = False) -> None:
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.get_username()
        await self.send_connect()

    def on_package(self, cmd: str, args: dict[str, Any]) -> None:
        super().on_package(cmd, args)

        if cmd == "RoomInfo":
            seed_name = args.get("seed_name")
            if isinstance(seed_name, str):
                self.server_seed_name = seed_name
        elif cmd == "Connected":
            self.ap_authenticated = True
            identity = (
                self.server_address,
                self.server_seed_name,
                self.team,
                self.slot,
            )
            slot_data = args.get("slot_data")
            if isinstance(slot_data, dict):
                goal = slot_data.get("goal")
                if isinstance(goal, (int, str)):
                    self.goal_option = goal
                liberation_goal = slot_data.get("liberation_goal")
                if isinstance(liberation_goal, int):
                    self.liberation_goal = liberation_goal
            self.goal_sent = False
            if self.ap_session_identity is not None and identity != self.ap_session_identity:
                self.next_item_index = 0
                self.pending_items.clear()
                self.received_items.clear()
                self.pending_location_ids.clear()
                self.reported_location_ids.clear()
            self.ap_session_identity = identity
            asyncio.create_task(self._flush_locations(), name="JC3 location sync")
            asyncio.create_task(
                self._sync_game_session(), name="JC3 bridge session sync"
            )
        elif cmd in {"ConnectionRefused", "Disconnected"}:
            self.ap_authenticated = False
        elif cmd == "RoomUpdate":
            asyncio.create_task(self._flush_locations(), name="JC3 location sync")
        elif cmd == "ReceivedItems":
            start_index = args.get("index")
            items = args.get("items")
            if not isinstance(start_index, int) or start_index < 0 or not isinstance(items, list):
                logger.error("Ignoring malformed ReceivedItems packet for Just Cause 3.")
                return

            for offset, item in enumerate(items):
                delivery_index = start_index + offset
                if delivery_index >= self.next_item_index:
                    self.pending_items.setdefault(delivery_index, item)
                if delivery_index not in self.received_items:
                    item_code = getattr(item, "item", None)
                    if item_code is None and isinstance(item, Sequence) and item:
                        item_code = item[0]
                    self.received_items[delivery_index] = {
                        "name": ITEM_ID_TO_NAME.get(item_code, f"Unknown item {item_code}"),
                        "item_code": item_code,
                        "status": "applied" if delivery_index < self.next_item_index else "queued",
                    }
                    logger.info(
                        "Received AP item: %s (item_id=%s, delivery_index=%d).",
                        self.received_items[delivery_index]["name"],
                        item_code,
                        delivery_index,
                    )

            if self.grant_task is None or self.grant_task.done():
                self.grant_task = asyncio.create_task(
                    self._drain_received_items(), name="JC3 received-item bridge"
                )

    async def game_bridge_loop(self) -> None:
        retry_delay = 1
        while not self.exit_event.is_set():
            try:
                reader, writer = await asyncio.open_connection(
                    self.game_host, self.game_port
                )
                self.game_reader = reader
                self.game_writer = writer
                self.game_connected.set()
                retry_delay = 1
                logger.info(
                    "Connected to the JC3 bridge at %s:%d.",
                    self.game_host,
                    self.game_port,
                )
                await self._write_game_message({"cmd": "ping"})
                if self.ap_authenticated:
                    asyncio.create_task(
                        self._sync_game_session(), name="JC3 bridge session sync"
                    )
                asyncio.create_task(
                    self.query_game_status(), name="JC3 initial bridge status"
                )
                last_pong = asyncio.get_running_loop().time()

                while not self.exit_event.is_set():
                    try:
                        line = await asyncio.wait_for(
                            reader.readline(), timeout=GAME_PING_INTERVAL_SECONDS
                        )
                    except asyncio.TimeoutError:
                        if (
                            asyncio.get_running_loop().time() - last_pong
                            >= 2 * GAME_PING_INTERVAL_SECONDS
                        ):
                            raise ConnectionError("JC3 bridge heartbeat timed out.")
                        await self._write_game_message({"cmd": "ping"})
                        continue
                    if not line:
                        break
                    if await self._handle_game_line(line):
                        last_pong = asyncio.get_running_loop().time()
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError, asyncio.TimeoutError) as error:
                logger.debug("JC3 bridge connection failed: %s", error)
            finally:
                self.game_connected.clear()
                self.game_reader = None
                writer = self.game_writer
                self.game_writer = None
                if writer is not None:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except OSError:
                        pass
                if self.pending_grant_ack is not None:
                    if not self.pending_grant_ack.done():
                        self.pending_grant_ack.set_exception(
                            ConnectionError("JC3 bridge disconnected before grant acknowledgement.")
                        )
                    self.pending_grant_ack = None

            if not self.exit_event.is_set():
                logger.info(
                    "JC3 bridge unavailable; retrying in %d second(s).",
                    retry_delay,
                )
                try:
                    await asyncio.wait_for(
                        self.exit_event.wait(), timeout=retry_delay
                    )
                except asyncio.TimeoutError:
                    pass
                retry_delay = min(retry_delay * 2, GAME_RETRY_MAX_SECONDS)

    async def _write_game_message(self, message: dict[str, Any]) -> None:
        async with self.game_write_lock:
            writer = self.game_writer
            if writer is None or writer.is_closing():
                raise ConnectionError("JC3 bridge is not connected.")
            writer.write(encode_game_message(message))
            await writer.drain()

    async def _request_game_bridge(
        self,
        message: dict[str, Any],
        timeout: float = 5,
    ) -> dict[str, Any]:
        request_id = self.next_bridge_request_id
        self.next_bridge_request_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending_bridge_requests[request_id] = (
            str(message.get("cmd", "unknown")),
            future,
        )
        try:
            await self._write_game_message({**message, "id": request_id})
            try:
                response = await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError as error:
                raise TimeoutError(
                    f"Timed out waiting for JC3 bridge response to "
                    f"{message.get('cmd', 'unknown')} request {request_id} "
                    f"after {timeout:g}s."
                ) from error
            if response.get("event") == "error":
                reason = response.get("message") or response.get("error")
                raise RuntimeError(f"JC3 bridge request failed: {reason}")
            return response
        finally:
            self.pending_bridge_requests.pop(request_id, None)

    def _ap_session_names(self) -> tuple[str, str] | None:
        seed_name = self.server_seed_name
        slot_name = self.username or self.auth
        if (
            not isinstance(seed_name, str)
            or not seed_name
            or not isinstance(slot_name, str)
            or not slot_name
        ):
            return None
        return seed_name, slot_name

    async def _send_game_session(self) -> None:
        identity = self._ap_session_names()
        if identity is None:
            logger.warning(
                "Archipelago authenticated, but seed or slot identity is unavailable "
                "for the JC3 bridge."
            )
            return
        response = await self._request_game_bridge(
            {
                "cmd": "set_session",
                "session_id": f"{identity[0]}/{identity[1]}",
                "seed": identity[0],
                "slot": identity[1],
            }
        )
        if response.get("event") != "session_updated":
            raise RuntimeError(
                f"JC3 bridge returned an unexpected session response: {response!r}"
            )

    async def _sync_game_session(self) -> None:
        if not self.ap_authenticated or not self.game_connected.is_set():
            return
        try:
            await self._send_game_session()
        except (ConnectionError, OSError, asyncio.TimeoutError, RuntimeError) as error:
            logger.warning("Could not sync AP session with JC3 bridge: %s", error)

    async def set_game_world_state(self, in_world: bool) -> bool:
        if not self.game_connected.is_set():
            logger.warning("JC3 bridge is disconnected; world state was not changed.")
            return False
        try:
            response = await self._request_game_bridge(
                {"cmd": "set_in_world", "in_world": in_world}
            )
            if response.get("event") != "in_world_updated":
                logger.warning(
                    "JC3 bridge returned an unexpected world-state response: %s",
                    response,
                )
                return False
            if response.get("in_world") is not in_world:
                logger.warning("JC3 bridge did not confirm the requested world state.")
                return False
            if self.cached_game_status is not None:
                self.cached_game_status["in_world"] = in_world
            logger.info("JC3 bridge in_world=%s.", in_world)
            return True
        except (ConnectionError, OSError, asyncio.TimeoutError, RuntimeError) as error:
            logger.warning("Could not change JC3 bridge world state: %s", error)
            return False

    async def _handle_game_line(self, line: bytes) -> bool:
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Ignoring invalid JSON from the JC3 bridge.")
            return False
        if not isinstance(message, dict) or not isinstance(message.get("event"), str):
            logger.warning("Ignoring malformed event from the JC3 bridge.")
            return False

        event = message["event"]
        request_id = message.get("id")
        request = (
            self.pending_bridge_requests.get(request_id)
            if type(request_id) is int
            else None
        )
        if request is None and "id" not in message:
            expected_commands = {
                "status": "get_status",
                "session_updated": "set_session",
                "in_world_updated": "set_in_world",
            }
            expected_command = expected_commands.get(event)
            candidates = [
                pending
                for pending in self.pending_bridge_requests.values()
                if expected_command is None or pending[0] == expected_command
            ]
            if event == "error" and len(self.pending_bridge_requests) == 1:
                request = next(iter(self.pending_bridge_requests.values()))
            elif len(candidates) == 1:
                request = candidates[0]
        request_future = request[1] if request is not None else None
        if request_future is not None and not request_future.done():
            request_future.set_result(message)

        if event == "status":
            self.cached_game_status = message
            if request_future is None:
                logger.info("JC3 bridge status: %s", message)
            return False
        if event == "in_world_updated":
            if self.cached_game_status is not None:
                self.cached_game_status["in_world"] = message.get("in_world")
            return False
        if event == "session_updated":
            return False
        if event == "pong":
            if message.get("game") != "JC3":
                logger.warning("Connected to an unexpected game bridge.")
            return True
        if event == "grant_queued":
            return False
        if event == "error":
            if request_future is not None:
                return False
            ack = self.pending_grant_ack
            if ack is not None and not ack.done():
                ack.set_result(message)
            else:
                logger.warning(
                    "JC3 bridge request failed (%s): %s",
                    message.get("error", "unknown_error"),
                    message.get("message", "No error description provided."),
                )
            return False
        if event in {"grant_applied", "grant_failed"}:
            ack = self.pending_grant_ack
            identity = self.pending_grant_identity
            response_identity = (
                message.get("seed_name"),
                message.get("slot_name"),
                message.get("delivery_index"),
            )
            if (
                ack is not None
                and not ack.done()
                and response_identity == identity
            ):
                ack.set_result(message)
            else:
                logger.warning("Received an unsolicited JC3 bridge response: %s", event)
            return False
        if event == "location_checked":
            location_id = message.get("location_id")
            if not isinstance(location_id, int) or location_id not in LOCATION_IDS:
                logger.warning("Ignoring unknown JC3 location ID: %r", location_id)
                return False
            if location_id in self.reported_location_ids:
                return False
            self.pending_location_ids.add(location_id)
            await self._flush_locations()
            return False
        if event == "log":
            logger.info("JC3: %s", message.get("msg", ""))
            return False
        logger.warning("Unknown event from the JC3 bridge: %s", event)
        return False

    async def query_game_status(self) -> dict[str, Any] | None:
        if not self.game_connected.is_set():
            logger.warning("JC3 bridge is disconnected; status is unavailable.")
            return None

        async with self.status_query_lock:
            try:
                status = await self._request_game_bridge({"cmd": "get_status"})
                logger.info(
                    "JC3 bridge status: mode=%s in_world=%s session=%s "
                    "delivery_index=%s last_applied_index=%s pending_queue=%s "
                    "grant_callback=%s catalog_entries=%s build=%s",
                    status.get("backend_mode", "unknown"),
                    status.get("in_world", "unknown"),
                    status.get("session_id", status.get("active_session_id")),
                    status.get("delivery_index"),
                    status.get("last_applied_index"),
                    status.get("pending_queue_size"),
                    status.get("grant_callback_registered"),
                    status.get("catalog_entries",
                             status.get("registered_catalog_handlers", "unknown")),
                    status.get("build_timestamp", "unknown"),
                )
                return status
            except (
                ConnectionError,
                OSError,
                asyncio.TimeoutError,
                RuntimeError,
            ) as error:
                logger.warning("Could not query JC3 bridge status: %s", error)
                return None

    async def manual_check(self, location_id: int) -> set[int]:
        if not self.ap_authenticated or self.slot is None:
            logger.warning("Connect to an Archipelago server before using /check.")
            return set()

        if location_id not in LOCATION_IDS:
            logger.warning("Unknown JC3 AP location ID: %s", location_id)
            return set()
        checked = await self.check_locations({location_id})
        if checked:
            logger.info("Submitted manual location check: %s.", LOCATION_ID_TO_NAME[location_id])
        else:
            logger.info("Location is already checked or is not currently missing.")
        await self.send_goal_if_complete()
        return checked

    def output_missing_locations(self, command_processor: Any, region: str = "") -> bool:
        normalized_region = region.casefold()
        available_regions = {
            value.casefold(): value for value in LOCATION_ID_TO_REGION.values()
        }
        if normalized_region and normalized_region not in available_regions:
            command_processor.output(f"Unknown JC3 region: {region}")
            return False

        if self.missing_locations:
            remaining = self.missing_locations & LOCATION_IDS
        else:
            remaining = LOCATION_IDS - self.checked_locations
        if normalized_region:
            remaining = {
                location_id
                for location_id in remaining
                if LOCATION_ID_TO_REGION[location_id].casefold() == normalized_region
            }

        if not remaining:
            command_processor.output("No unchecked JC3 locations found.")
            return True
        region_label = available_regions.get(normalized_region, "all regions")
        command_processor.output(
            f"{len(remaining)} unchecked JC3 location(s) in {region_label}:"
        )
        for location_id in sorted(remaining):
            command_processor.output(
                f"{LOCATION_ID_TO_NAME[location_id]} ({location_id})"
            )
        return True

    def output_received_items(self, command_processor: Any) -> bool:
        if not self.received_items:
            command_processor.output("No AP items received in this session.")
            return True
        command_processor.output(
            f"{len(self.received_items)} AP item(s) received in this session:"
        )
        for delivery_index in sorted(self.received_items):
            item = self.received_items[delivery_index]
            command_processor.output(
                f"{item['name']} (delivery_index={delivery_index}, "
                f"status={item['status']})"
            )
        return True

    def _active_save_directory(self) -> Path:
        save_root = self.save_directory
        if save_root is None:
            raise RuntimeError(
                "JC3 save directory is unavailable; set --save-directory or "
                "JC3AP_SAVE_DIR."
            )
        if not save_root.is_dir():
            raise FileNotFoundError(f"JC3 save directory does not exist: {save_root}")

        if save_root.name.casefold() != "saves":
            return save_root

        profiles = sorted(path for path in save_root.iterdir() if path.is_dir())
        if not profiles:
            raise FileNotFoundError(
                f"No SteamID save profiles were found in {save_root}."
            )
        if len(profiles) == 1:
            return profiles[0]

        def latest_save_activity(profile: Path) -> int:
            latest = profile.stat().st_mtime_ns
            for path in profile.rglob("*"):
                if path.is_file():
                    latest = max(latest, path.stat().st_mtime_ns)
            return latest

        profile = max(profiles, key=latest_save_activity)
        logger.warning(
            "Multiple JC3 save profiles found; snapshotting the most recently "
            "modified profile %s. Use --save-directory to select another profile.",
            profile.name,
        )
        return profile

    async def snapshot_save(self, label: str) -> Path | None:
        safe_label = re.sub(r"[^A-Za-z0-9._-]+", "_", label.strip()).strip("._-")
        if not safe_label:
            logger.error("Save snapshot label must contain letters or numbers.")
            return None

        try:
            source = self._active_save_directory()
            snapshot_root = Path.cwd() / "save_snapshots"
            resolved_source = source.resolve()
            resolved_snapshot_root = snapshot_root.resolve()
            if (
                resolved_snapshot_root == resolved_source
                or resolved_source in resolved_snapshot_root.parents
            ):
                raise ValueError(
                    "save_snapshots cannot be created inside the source save directory."
                )

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            destination = snapshot_root / f"{timestamp}_{safe_label}"
            await asyncio.to_thread(
                snapshot_root.mkdir, parents=True, exist_ok=True
            )
            await asyncio.to_thread(
                shutil.copytree, source, destination, symlinks=True
            )
        except (OSError, RuntimeError, ValueError) as error:
            logger.error("Could not snapshot JC3 saves: %s", error)
            return None

        logger.info(
            "JC3 save snapshot copied from %s to %s.",
            source,
            destination,
        )
        return destination

    async def _send_item_grant(
        self,
        item_name: str,
        item_code: int,
        delivery_index: int,
    ) -> None:
        item_id = game_item_id(item_name)
        seed_name = self.server_seed_name
        slot_name = self.auth or self.username
        if not isinstance(seed_name, str) or not seed_name:
            raise RuntimeError("AP room seed name is not available for item delivery.")
        if not isinstance(slot_name, str) or not slot_name:
            raise RuntimeError("AP slot name is not available for item delivery.")

        while not self.exit_event.is_set():
            if not self.game_connected.is_set():
                try:
                    await asyncio.wait_for(
                        self.game_connected.wait(), timeout=1
                    )
                except asyncio.TimeoutError:
                    continue

            future: asyncio.Future[dict[str, Any]] = (
                asyncio.get_running_loop().create_future()
            )
            self.pending_grant_ack = future
            self.pending_grant_identity = (
                seed_name,
                slot_name,
                delivery_index,
            )
            try:
                await self._write_game_message(
                    {
                        "cmd": "grant_item",
                        "seed_name": seed_name,
                        "slot_name": slot_name,
                        "delivery_index": delivery_index,
                        "item_code": item_code,
                        "item_id": item_id,
                        "count": 1,
                    }
                )
                response = await asyncio.wait_for(
                    future, timeout=GAME_ACK_TIMEOUT_SECONDS
                )
                if response.get("event") != "grant_applied":
                    if (
                        response.get("event") == "grant_failed"
                        and response.get("retryable") is True
                    ):
                        logger.info(
                            "JC3 deferred item %s (%s); retrying shortly.",
                            item_name,
                            response.get("reason", "not_ready"),
                        )
                        await asyncio.sleep(1)
                        continue
                    if response.get("error") == "grant_queue_full":
                        logger.warning("JC3 item queue is full; retrying %s.", item_name)
                        await asyncio.sleep(1)
                        continue
                    raise RuntimeError(
                        f"JC3 bridge rejected delivery {delivery_index}: {response!r}"
                    )
                if (
                    response.get("seed_name") != seed_name
                    or response.get("slot_name") != slot_name
                    or response.get("delivery_index") != delivery_index
                    or response.get("item_id") != item_id
                    or response.get("count") != 1
                ):
                    raise RuntimeError(
                        f"JC3 bridge acknowledged the wrong delivery: {response!r}"
                    )
                received = self.received_items.get(delivery_index)
                if received is not None:
                    received["status"] = "applied"
                return
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError, asyncio.TimeoutError) as error:
                logger.warning("Could not queue AP item %s: %s", item_name, error)
                if self.game_writer is not None:
                    self.game_writer.close()
                await asyncio.sleep(0)
            finally:
                if self.pending_grant_ack is future:
                    self.pending_grant_ack = None
                    self.pending_grant_identity = None

    async def _drain_received_items(self) -> None:
        while not self.exit_event.is_set():
            item = self.pending_items.pop(self.next_item_index, None)
            if item is None:
                return

            item_code = getattr(item, "item", None)
            if item_code is None and isinstance(item, Sequence) and item:
                item_code = item[0]
            item_name = ITEM_ID_TO_NAME.get(item_code)
            if item_name is None:
                logger.error(
                    "Cannot deliver unknown Just Cause 3 AP item ID %r at index %d; "
                    "later deliveries are blocked to preserve AP item order.",
                    item_code,
                    self.next_item_index,
                )
                return

            if self.cached_game_status is None and self.game_connected.is_set():
                await self.query_game_status()
            if (
                self.cached_game_status is not None
                and self.cached_game_status.get("grant_callback_registered") is False
            ):
                logger.info(
                    "Manual-mode AP item: %s (item_id=%s, delivery_index=%d); "
                    "no game grant callback is registered.",
                    item_name,
                    item_code,
                    self.next_item_index,
                )
                received = self.received_items.get(self.next_item_index)
                if received is not None:
                    received["status"] = "manual"
                self.next_item_index += 1
                continue

            try:
                await self._send_item_grant(
                    item_name,
                    item_code,
                    self.next_item_index,
                )
            except RuntimeError as error:
                self.pending_items[self.next_item_index] = item
                logger.error("%s", error)
                return
            if self.exit_event.is_set():
                return
            logger.info("Queued AP item: %s", item_name)
            self.next_item_index += 1

    async def _flush_locations(self) -> None:
        if not self.ap_authenticated or self.slot is None:
            return
        for location_id in tuple(self.pending_location_ids):
            if location_id in self.locations_checked or location_id in self.pending_locations:
                self.pending_location_ids.discard(location_id)
                self.reported_location_ids.add(location_id)
                continue
            sent = await self.check_locations({location_id})
            if sent or location_id not in self.missing_locations:
                self.pending_location_ids.discard(location_id)
                self.reported_location_ids.add(location_id)
        await self.send_goal_if_complete()

    async def send_goal_if_complete(self) -> None:
        if self.goal_sent or not self.ap_authenticated:
            return

        checked = self.locations_checked & LOCATION_IDS
        goal = self.goal_option
        if goal in (0, "story_missions"):
            required = {
                location_id
                for location_id, region in LOCATION_ID_TO_REGION.items()
                if candidate_location_data[
                    LOCATION_ID_TO_NAME[location_id]
                ]["category"] == "story_mission"
            }
            reached = bool(required) and required <= checked
        elif goal in (2, "full_liberation"):
            reached = checked == LOCATION_IDS
        else:
            required_count = max(
                1,
                (len(LOCATION_IDS) * self.liberation_goal + 99) // 100,
            )
            reached = len(checked) >= required_count

        if reached:
            self.goal_sent = True
            self.send_msgs([{
                "cmd": "StatusUpdate",
                "status": ClientStatus.CLIENT_GOAL,
            }])

    async def process_telemetry_payload(self, payload: Any) -> set[int]:
        if not isinstance(payload, (dict, list)):
            return set()

        found: set[int] = set()

        def add_value(value: Any) -> None:
            if isinstance(value, bool):
                return
            if isinstance(value, int):
                if value in LOCATION_IDS:
                    found.add(value)
                return
            if isinstance(value, str):
                location_id = resolve_location_id(value)
                if location_id is not None:
                    found.add(location_id)
                else:
                    location_id = LOCATION_ASSET_KEYS.get(value.strip().casefold())
                    if location_id is not None:
                        found.add(location_id)

        def walk(value: Any) -> None:
            if isinstance(value, list):
                for item in value:
                    walk(item)
                return
            if not isinstance(value, dict):
                add_value(value)
                return
            for key, child in value.items():
                normalized_key = str(key).casefold()
                if normalized_key in {
                    "location_id",
                    "location",
                    "location_name",
                    "name",
                    "key",
                    "game_key",
                    "asset_id",
                    "completed",
                    "locations",
                    "checks",
                }:
                    if normalized_key == "completed":
                        if child is True:
                            continue
                    else:
                        if isinstance(child, list):
                            for item in child:
                                add_value(item)
                        else:
                            add_value(child)
                if isinstance(child, (dict, list)):
                    walk(child)

        walk(payload)
        if found:
            await self.check_locations(found)
        return found

    async def process_settlement_save_file(
        self,
        save_path: Path,
        mapping: dict[str, Any],
    ) -> set[int]:
        found = settlement_save_location_checks(save_path.read_bytes(), mapping)
        if found:
            self.pending_location_ids.update(found)
            await self._flush_locations()
        return found

    async def telemetry_watcher_loop(self) -> None:
        seen: dict[Path, tuple[int, int]] = {}
        mapping_signature: tuple[int, int] | None = None
        settlement_map: dict[str, Any] | None = None
        while not self.exit_event.is_set():
            if self.settlement_map_file is not None:
                try:
                    map_stat = self.settlement_map_file.stat()
                    current_map_signature = (map_stat.st_mtime_ns, map_stat.st_size)
                except OSError:
                    current_map_signature = None

                if current_map_signature != mapping_signature:
                    mapping_signature = current_map_signature
                    settlement_map = None
                    for save_path in tuple(seen):
                        if save_path.name == SETTLEMENT_SAVE_LAYOUT["file"]:
                            del seen[save_path]
                    if current_map_signature is not None:
                        try:
                            settlement_map = load_settlement_save_map(
                                self.settlement_map_file
                            )
                            logger.info(
                                "Loaded JC3 settlement save map: %s (%d locations)",
                                self.settlement_map_file,
                                len(settlement_map["locations"]),
                            )
                        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                            logger.error(
                                "Could not load JC3 settlement save map %s: %s",
                                self.settlement_map_file,
                                error,
                            )

            mapped_save_filename = (
                settlement_map["layout"]["file"]
                if settlement_map is not None
                else None
            )
            candidates: list[Path] = []
            if self.telemetry_file is not None:
                candidates.append(self.telemetry_file)
            if self.save_directory is not None:
                try:
                    candidates.extend(
                        path
                        for path in self.save_directory.rglob("*")
                        if path.is_file()
                        and path.suffix.casefold() in {".json", ".txt"}
                    )
                except (OSError, PermissionError):
                    pass
            if (
                settlement_map is not None
                and settlement_save_directory is not None
            ):
                try:
                    candidates.extend(
                        path
                        for path in settlement_save_directory.rglob("*")
                        if path.is_file() and path.name == mapped_save_filename
                    )
                except (OSError, PermissionError):
                    pass

            for path in candidates:
                try:
                    stat = path.stat()
                    signature = (stat.st_mtime_ns, stat.st_size)
                    if seen.get(path) == signature:
                        continue
                    seen[path] = signature
                    if (
                        settlement_map is not None
                        and path.name == mapped_save_filename
                    ):
                        try:
                            found = await self.process_settlement_save_file(
                                path, settlement_map
                            )
                            if found:
                                logger.info(
                                    "Detected %d mapped JC3 save location(s): %s",
                                    len(found),
                                    ", ".join(
                                        LOCATION_ID_TO_NAME[location_id]
                                        for location_id in sorted(found)
                                    ),
                                )
                        except (OSError, ValueError) as error:
                            logger.error(
                                "Could not read mapped JC3 save file %s: %s",
                                path,
                                error,
                            )
                        continue
                    text = path.read_text(encoding="utf-8")
                    payload = json.loads(text)
                    await self.process_telemetry_payload(payload)
                except (FileNotFoundError, PermissionError, OSError, UnicodeDecodeError):
                    continue
                except json.JSONDecodeError:
                    continue
            try:
                await asyncio.wait_for(self.exit_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def shutdown(self) -> None:
        if self.telemetry_task is not None:
            self.telemetry_task.cancel()
            try:
                await self.telemetry_task
            except asyncio.CancelledError:
                pass
        if self.game_task is not None:
            self.game_task.cancel()
            try:
                await self.game_task
            except asyncio.CancelledError:
                pass
        if self.grant_task is not None:
            self.grant_task.cancel()
            try:
                await self.grant_task
            except asyncio.CancelledError:
                pass
        writer = self.game_writer
        self.game_writer = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        await super().shutdown()


def launch(*launch_args: str) -> None:
    parser = get_base_parser(description="Just Cause 3 Archipelago Client")
    parser.add_argument("--name", default=None, help="Slot Name to connect as.")

    def parse_game_port(value: str) -> int:
        try:
            port = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError("game port must be an integer") from error
        if not 1 <= port <= 65535:
            raise argparse.ArgumentTypeError("game port must be between 1 and 65535")
        return port

    parser.add_argument(
        "--game-port",
        type=parse_game_port,
        default=os.environ.get("JC3AP_PORT", GAME_PORT),
        help="JC3 IPC server port (default: 54321).",
    )
    parser.add_argument(
        "--telemetry-file",
        default=os.environ.get("JC3AP_EVENTS_FILE", "jc3_ap_events.json"),
        help="Optional read-only JC3 JSON event drop file.",
    )
    parser.add_argument(
        "--save-directory",
        default=os.environ.get("JC3AP_SAVE_DIR"),
        help="Optional read-only JC3 save/telemetry directory.",
    )
    parser.add_argument(
        "--settlement-map",
        default=os.environ.get("JC3AP_SETTLEMENT_MAP"),
        help=(
            "Optional location map produced by diff_jc3_saves.py "
            "--learn-location."
        ),
    )
    args = parser.parse_args(launch_args)

    async def main() -> None:
        context = JustCause3Context(
            args.connect,
            args.password,
            GAME_HOST,
            args.game_port,
            telemetry_file=args.telemetry_file,
            save_directory=args.save_directory,
            settlement_map_file=args.settlement_map,
        )
        context.auth = args.name
        context.server_task = asyncio.create_task(
            server_loop(context), name="AP server loop"
        )
        context.game_task = asyncio.create_task(
            context.game_bridge_loop(), name="JC3 bridge loop"
        )
        context.telemetry_task = asyncio.create_task(
            context.telemetry_watcher_loop(), name="JC3 telemetry watcher"
        )
        if gui_enabled and not getattr(args, "nogui", False):
            context.run_gui()
        context.run_cli()
        await context.exit_event.wait()
        context.server_address = None
        await context.shutdown()

    asyncio.run(main())


if __name__ == "__main__":
    launch(*sys.argv[1:])
