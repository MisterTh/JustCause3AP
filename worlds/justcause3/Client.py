from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
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


def resolve_location_id(value: str) -> int | None:
    query = value.strip()
    try:
        location_id = int(query, 0)
    except ValueError:
        location_id = LOCATION_ID_TO_NAME_CASEFOLD.get(query.casefold())
        if location_id is None:
            return None
    return location_id if location_id in LOCATION_IDS else None


class JustCause3CommandProcessor(ClientCommandProcessor):
    def _cmd_status(self) -> bool:
        """Show the live JC3 bridge status, if the bridge is connected."""
        async_start(self.ctx.query_game_status(), name="JC3 bridge status")
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


def game_item_id(item_name: str) -> str:
    """Convert the AP item display name to the C++ bridge's safe identifier."""
    identifier = re.sub(r"[^a-zA-Z0-9_-]+", "_", item_name).strip("_")
    if not identifier:
        raise ValueError(f"Item name has no bridge-safe characters: {item_name!r}")
    return identifier.lower()


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
        self.pending_status: asyncio.Future[dict[str, Any]] | None = None
        self.status_query_lock = asyncio.Lock()
        self.grant_task: asyncio.Task[None] | None = None
        self.game_task: asyncio.Task[None] | None = None
        self.next_item_index = 0
        self.pending_items: dict[int, Any] = {}
        self.pending_location_ids: set[int] = set()
        self.reported_location_ids: set[int] = set()
        self.ap_session_identity: tuple[Any, Any, Any, Any] | None = None

    def on_package(self, cmd: str, args: dict[str, Any]) -> None:
        super().on_package(cmd, args)

        if cmd == "Connected":
            identity = (
                self.server_address,
                self.server_seed_name,
                self.team,
                self.slot,
            )
            if self.ap_session_identity is not None and identity != self.ap_session_identity:
                self.next_item_index = 0
                self.pending_items.clear()
                self.pending_location_ids.clear()
                self.reported_location_ids.clear()
            self.ap_session_identity = identity
            asyncio.create_task(self._flush_locations(), name="JC3 location sync")
        elif cmd == "RoomUpdate":
            asyncio.create_task(self._flush_locations(), name="JC3 location sync")
        elif cmd == "ReceivedItems":
            start_index = args.get("index")
            items = args.get("items")
            if not isinstance(start_index, int) or start_index < 0 or not isinstance(items, list):
                logger.error("Ignoring malformed ReceivedItems packet for Just Cause 3.")
                return

            for offset, item in enumerate(items):
                absolute_index = start_index + offset
                if absolute_index >= self.next_item_index:
                    self.pending_items.setdefault(absolute_index, item)

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
            writer.write(
                (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
            )
            await writer.drain()

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
        if event == "status":
            future = self.pending_status
            if future is not None and not future.done():
                future.set_result(message)
            else:
                logger.info("JC3 bridge status: %s", message)
            return False
        if event == "pong":
            if message.get("game") != "JC3":
                logger.warning("Connected to an unexpected game bridge.")
            return True
        if event == "grant_queued":
            return False
        if event in {"grant_applied", "grant_failed", "error"}:
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
                and (event == "error" or response_identity == identity)
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
            future = asyncio.get_running_loop().create_future()
            self.pending_status = future
            try:
                await self._write_game_message({"cmd": "get_status"})
                status = await asyncio.wait_for(future, timeout=5)
                logger.info(
                    "JC3 bridge status: mode=%s in_world=%s session=%s "
                    "delivery_index=%s handlers=%s",
                    status.get("backend_mode", "unknown"),
                    status.get("in_world", "unknown"),
                    status.get("active_session_id"),
                    status.get("delivery_index"),
                    status.get("registered_catalog_handlers", "unknown"),
                )
                return status
            except (ConnectionError, OSError, asyncio.TimeoutError) as error:
                logger.warning("Could not query JC3 bridge status: %s", error)
                return None
            finally:
                if self.pending_status is future:
                    self.pending_status = None

    async def manual_check(self, location_id: int) -> set[int]:
        if self.server is None or self.slot is None:
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
        if self.slot is None or self.server is None:
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

    async def shutdown(self) -> None:
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


def launch() -> None:
    parser = get_base_parser(description="Just Cause 3 Archipelago Client")

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
    args = parser.parse_args()

    async def main() -> None:
        context = JustCause3Context(
            args.connect, args.password, GAME_HOST, args.game_port
        )
        context.server_task = asyncio.create_task(
            server_loop(context), name="AP server loop"
        )
        context.game_task = asyncio.create_task(
            context.game_bridge_loop(), name="JC3 bridge loop"
        )
        if gui_enabled and not getattr(args, "nogui", False):
            context.run_gui()
        context.run_cli()
        await context.exit_event.wait()
        context.server_address = None
        await context.shutdown()

    asyncio.run(main())


if __name__ == "__main__":
    launch()
