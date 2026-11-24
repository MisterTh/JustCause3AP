import argparse
import asyncio
import ctypes
import importlib
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType

HOST, PORT = "127.0.0.1", 54321
SEED_NAME = "jc3-test-seed"
SLOT_NAME = "TestPlayer"


def read_json_line(reader):
    line = reader.readline()
    if not line:
        raise AssertionError("Bridge disconnected before sending a response.")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise AssertionError(f"Expected a JSON object, received {value!r}")
    return value


def send_json_line(sock, message):
    sock.sendall((json.dumps(message) + "\n").encode("utf-8"))


def test_live_bridge(host, port):
    client = load_client_module()
    with socket.create_connection((host, port), timeout=3) as sock:
        sock.settimeout(3)
        reader = sock.makefile("r", encoding="utf-8")
        send_json_line(sock, {"cmd": "ping"})
        pong = read_json_line(reader)
        assert pong == {
            "event": "pong",
            "game": "JC3",
            "base_addr": "unavailable",
        }
        send_json_line(sock, {"cmd": "read_u32", "offsets": [0x123456, 0x10]})
        assert read_json_line(reader)["error"] == "memory_commands_disabled"
        send_json_line(sock, {"cmd": "get_status"})
        status = read_json_line(reader)
        assert status["event"] == "status", status
        assert status["backend_mode"] == "live", status
        assert isinstance(status["in_world"], bool), status
        assert status["registered_catalog_handlers"] == 395, status
        assert isinstance(status.get("build_timestamp"), str), status

        requests = (
            {
                "cmd": "set_session",
                "session_id": f"{SEED_NAME}/{SLOT_NAME}",
                "seed": SEED_NAME,
                "slot": SLOT_NAME,
                "id": 101,
            },
            {"cmd": "set_in_world", "in_world": True, "id": 102},
            {"cmd": "get_status", "id": 103},
        )
        for request in requests:
            sock.sendall(client.encode_game_message(request))
            response = read_json_line(reader)
            assert response.get("id") == request["id"], response
            assert response.get("event") != "error", response
            if request["cmd"] == "set_session":
                assert response["event"] == "session_updated", response
            elif request["cmd"] == "set_in_world":
                assert response == {
                    "event": "in_world_updated",
                    "in_world": True,
                    "id": request["id"],
                }, response
            else:
                assert response["session_id"] == f"{SEED_NAME}/{SLOT_NAME}", response
                assert response["in_world"] is True, response
                assert response.get("build_timestamp"), response

        sock.sendall(
            client.encode_game_message(
                {"cmd": "set_in_world", "in_world": False, "id": 104}
            )
        )
        reset = read_json_line(reader)
        assert reset["id"] == 104 and reset["in_world"] is False, reset

        sock.sendall(
            client.encode_game_message({"cmd": "invalid_test", "id": 105})
        )
        error = read_json_line(reader)
        assert error["event"] == "error" and error["id"] == 105, error
        assert error.get("message"), error

        send_json_line(
            sock,
            {
                "cmd": "write_u32",
                "offsets": [0x123456, 0x10],
                "value": 1,
            },
        )
        assert read_json_line(reader)["error"] == "memory_commands_disabled"

    print("Live JC3 bridge protocol checks passed.")


def load_client_module():
    project_root = Path(__file__).resolve().parents[1]
    worlds = ModuleType("worlds")
    worlds.__path__ = [str(project_root / "worlds")]
    justcause3 = ModuleType("worlds.justcause3")
    justcause3.__path__ = [str(project_root / "worlds" / "justcause3")]
    sys.modules["worlds"] = worlds
    sys.modules["worlds.justcause3"] = justcause3

    base_classes = ModuleType("BaseClasses")
    base_classes.Item = type("Item", (), {})
    base_classes.Location = type("Location", (), {})
    base_classes.Region = type("Region", (), {})
    base_classes.ItemClassification = type(
        "ItemClassification",
        (),
        {"useful": 1, "filler": 2, "progression": 4},
    )
    sys.modules["BaseClasses"] = base_classes

    common_client = ModuleType("CommonClient")

    class MockCommandProcessor:
        def __init__(self, ctx):
            self.ctx = ctx
            self.messages = []

        def output(self, message):
            self.messages.append(message)

    class MockCommonContext:
        def __init__(self, server_address=None, password=None):
            self.server_address = server_address
            self.password = password
            self.auth = SLOT_NAME
            self.username = SLOT_NAME
            self.tags = []
            self.want_slot_data = False
            self.input_responses = []
            self.sent_packets = []
            self.base_packages = []
            self.server_seed_name = SEED_NAME
            self.exit_event = asyncio.Event()
            self.server = None
            self.slot = None
            self.team = None
            self.locations_checked = set()
            self.checked_locations = set()
            self.missing_locations = set()
            self.pending_locations = set()
            self.server_task = None
            self.keep_alive_task = None
            self.ui_task = None
            self.input_task = None

        def on_package(self, cmd, args):
            self.base_packages.append((cmd, args))

        async def console_input(self):
            return self.input_responses.pop(0)

        async def server_auth(self, password_requested=False):
            if password_requested and not self.password:
                self.password = await self.console_input()
                return self.password

        async def get_username(self):
            if not self.auth:
                self.auth = self.username
                if not self.auth:
                    self.auth = await self.console_input()

        async def send_connect(self, **kwargs):
            packet = {
                "cmd": "Connect",
                "password": self.password,
                "name": self.auth,
                "game": self.game,
                "items_handling": self.items_handling,
                "slot_data": self.want_slot_data,
            }
            packet.update(kwargs)
            await self.send_msgs([packet])

        async def send_msgs(self, messages):
            self.sent_packets.extend(messages)

        async def check_locations(self, locations):
            locations = set(locations) & (
                self.missing_locations - self.pending_locations
            )
            if locations:
                self.pending_locations.update(locations)
                await self.send_msgs(
                    [{"cmd": "LocationChecks", "locations": tuple(locations)}]
                )
            return locations

        async def shutdown(self):
            self.exit_event.set()

        def run_gui(self):
            pass

        def run_cli(self):
            pass

    common_client.CommonContext = MockCommonContext
    common_client.ClientCommandProcessor = MockCommandProcessor
    common_client.gui_enabled = False
    common_client.get_base_parser = lambda description=None: argparse.ArgumentParser(
        description=description
    )
    common_client.logger = logging.getLogger("test_bridge")
    common_client.server_loop = None
    sys.modules["CommonClient"] = common_client
    multi_server = ModuleType("MultiServer")
    multi_server.mark_raw = lambda function: function
    sys.modules["MultiServer"] = multi_server
    utils = ModuleType("Utils")
    utils.async_start = lambda coroutine, name=None: asyncio.create_task(
        coroutine, name=name
    )
    sys.modules["Utils"] = utils

    return importlib.import_module("worlds.justcause3.Client")


async def test_authentication_flow():
    client = load_client_module()
    context = client.JustCause3Context("ws://localhost:38281", None)
    assert context.game == "Just Cause 3"
    assert context.items_handling == 0b111
    await context.server_auth()
    assert context.sent_packets == [
        {
            "cmd": "Connect",
            "password": None,
            "name": SLOT_NAME,
            "game": "Just Cause 3",
            "items_handling": 0b111,
            "slot_data": False,
        }
    ], context.sent_packets

    password_context = client.JustCause3Context(
        "ws://localhost:38281", None
    )
    password_context.auth = None
    password_context.username = None
    password_context.input_responses = ["server-password", SLOT_NAME]
    await password_context.server_auth(password_requested=True)
    assert password_context.password == "server-password"
    assert password_context.sent_packets[0]["name"] == SLOT_NAME
    assert password_context.sent_packets[0]["password"] == "server-password"
    assert password_context.sent_packets[0]["game"] == "Just Cause 3"

    sync_calls = []

    async def record_session_sync():
        sync_calls.append(True)

    context.game_connected.set()
    context._sync_game_session = record_session_sync
    context.on_package("RoomInfo", {"seed_name": SEED_NAME})
    context.on_package("Connected", {"slot_data": {}})
    await asyncio.sleep(0)
    assert context.base_packages == [
        ("RoomInfo", {"seed_name": SEED_NAME}),
        ("Connected", {"slot_data": {}}),
    ]
    assert context.ap_authenticated
    assert sync_calls == [True]
    print("AP authentication and Connect packet passed.")


async def test_save_snapshot():
    client = load_client_module()
    with tempfile.TemporaryDirectory() as temporary_directory:
        working_directory = Path(temporary_directory)
        save_root = (
            working_directory
            / "Documents"
            / "Square Enix"
            / "Just Cause 3"
            / "Saves"
        )
        profile = save_root / "76561198000000000"
        profile.mkdir(parents=True)
        save_file = profile / "savegame.sav"
        save_file.write_bytes(b"snapshot source")

        previous_directory = Path.cwd()
        try:
            os.chdir(working_directory)
            context = client.JustCause3Context(
                None, None, save_directory=save_root
            )
            destination = await context.snapshot_save("before settlement")
            assert destination is not None
            assert destination.parent == working_directory / "save_snapshots"
            assert destination.name.endswith("_before_settlement")
            assert (
                destination / "savegame.sav"
            ).read_bytes() == b"snapshot source"
            assert save_file.read_bytes() == b"snapshot source"
            await context.shutdown()
        finally:
            os.chdir(previous_directory)
    print("Read-only JC3 save snapshot passed.")


def test_settlement_save_mapping():
    client = load_client_module()
    location_name = "Settlement: Agro"
    location_id = client.location_table[location_name]
    mapping = {
        "schema_version": 1,
        "layout": client.SETTLEMENT_SAVE_LAYOUT,
        "locations": {
            location_name: {
                "record_key": "0x04030201",
                "state_mask": "0x03",
                "expected_state": "0x03",
            }
        },
    }
    save_prefix = bytes(0x62)
    record_key = bytes.fromhex("01 02 03 04")

    checks = client.settlement_save_location_checks(
        save_prefix + record_key + b"\x03",
        mapping,
    )
    assert checks == {location_id}, checks
    assert client.settlement_save_location_checks(
        save_prefix + record_key + b"\x00",
        mapping,
    ) == set()
    mapping["locations"][location_name] = {
        "record_key": "0x04030201",
        "match": "present",
        "state_mask": "0x00",
        "expected_state": "0x00",
    }
    assert client.settlement_save_location_checks(
        save_prefix + record_key + b"\x00",
        mapping,
    ) == {location_id}
    with tempfile.TemporaryDirectory(prefix="jc3ap-save-map-") as directory:
        mapping_path = Path(directory) / "settlement-map.json"
        mapping_path.write_text(json.dumps(mapping), encoding="utf-8")
        loaded_mapping = client.load_settlement_save_map(mapping_path)
        assert loaded_mapping == mapping
    print("JC3 settlement save mapping checks passed.")


async def test_settlement_save_check_submission():
    client = load_client_module()
    location_name = "Settlement: Agro"
    location_id = client.location_table[location_name]
    context = client.JustCause3Context(None, None)
    context.ap_authenticated = True
    context.slot = 1
    context.locations_checked = set()
    context.pending_locations = set()
    context.missing_locations = {location_id}
    sent_checks = []

    async def record_checks(location_ids):
        sent_checks.append(set(location_ids))
        return True

    context.check_locations = record_checks
    mapping = {
        "schema_version": 1,
        "layout": client.SETTLEMENT_SAVE_LAYOUT,
        "locations": {
            location_name: {
                "record_key": "0x04030201",
                "match": "state",
                "state_mask": "0x03",
                "expected_state": "0x03",
            }
        },
    }

    with tempfile.TemporaryDirectory(prefix="jc3ap-save-check-") as directory:
        save_path = Path(directory) / "settlement_achievement_status"
        save_path.write_bytes(bytes(0x62) + bytes.fromhex("01 02 03 04 03"))
        await context.process_settlement_save_file(save_path, mapping)

    assert sent_checks == [{location_id}], sent_checks
    assert location_id in context.reported_location_ids
    await context.shutdown()
    print("JC3 save-derived LocationChecks submission passed.")


async def test_received_item_delivery_index():
    client = load_client_module()
    context = client.JustCause3Context(None, None)
    item_code = next(iter(client.ITEM_ID_TO_NAME))
    context.grant_task = asyncio.create_task(asyncio.sleep(60))
    records = []

    class CaptureHandler(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = CaptureHandler()
    previous_log_level = client.logger.level
    client.logger.setLevel(logging.INFO)
    client.logger.addHandler(handler)
    try:
        context.on_package(
            "ReceivedItems",
            {"index": 7, "items": [[item_code, 0, 2, 0]]},
        )
        processor = client.JustCause3CommandProcessor(context)
        context.output_received_items(processor)
        assert any("delivery_index=7" in message for message in records), records
        assert any("delivery_index=7" in message for message in processor.messages)
    finally:
        client.logger.removeHandler(handler)
        client.logger.setLevel(previous_log_level)
        await context.shutdown()
    print("AP item delivery index display passed.")


async def test_manual_tracker_commands():
    client = load_client_module()
    location_name = next(iter(client.location_table))
    location_id = client.location_table[location_name]
    context = client.JustCause3Context(None, None)
    context.server = object()
    context.slot = 1
    context.ap_authenticated = True
    context.missing_locations = {location_id}
    context.game_connected.set()
    sent_packets = []

    async def capture_packets(messages):
        sent_packets.extend(messages)

    context.send_msgs = capture_packets

    async def return_status(message):
        assert message["cmd"] == "get_status"
        await context._handle_game_line(
            (json.dumps({
                "event": "status",
                "backend_mode": "live",
                "in_world": True,
                "registered_catalog_handlers": 395,
                "build_timestamp": "mock build",
                "id": message["id"],
            }) + "\n").encode("utf-8")
        )

    context._write_game_message = return_status
    status = await context.query_game_status()
    assert status["backend_mode"] == "live"

    processor = client.JustCause3CommandProcessor(context)
    assert processor._cmd_list_missing("Medici")
    assert any(location_name in message for message in processor.messages)

    processor.messages.clear()
    assert processor._cmd_check(location_name)
    await asyncio.sleep(0)
    assert sent_packets == [
        {"cmd": "LocationChecks", "locations": (location_id,)}
    ], sent_packets
    context.pending_locations.clear()
    assert processor._cmd_check(hex(location_id))
    await asyncio.sleep(0)
    assert sent_packets[-1] == {
        "cmd": "LocationChecks",
        "locations": (location_id,),
    }
    assert not processor._cmd_list_missing("not a region")
    assert not processor._cmd_check("not a JC3 location")
    assert len(sent_packets) == 2
    await context.shutdown()
    print("Manual tracker commands passed.")


async def test_legacy_bridge_response_correlation():
    client = load_client_module()
    context = client.JustCause3Context(None, None)
    context.game_connected.set()

    async def emulate_legacy_bridge(message):
        if message["cmd"] == "get_status":
            response = {
                "event": "status",
                "backend_mode": "live",
                "in_world": False,
                "active_session_id": None,
            }
        else:
            response = {"event": "error", "error": "unknown_command"}
        await context._handle_game_line(
            (json.dumps(response) + "\n").encode("utf-8")
        )

    context._write_game_message = emulate_legacy_bridge
    status = await context.query_game_status()
    assert status is not None
    assert status["backend_mode"] == "live"
    assert not context.pending_bridge_requests
    assert not await context.set_game_world_state(True)
    assert not context.pending_bridge_requests
    await context.shutdown()
    print("Legacy bridge response correlation passed.")


async def test_mock_round_trip():
    client = load_client_module()
    location_id = next(iter(client.LOCATION_IDS))
    item_code, item_name = next(iter(client.ITEM_ID_TO_NAME.items()))
    expected_item_id = client.game_item_id(item_name)
    state = {
        "next_delivery_index": 0,
        "apply_count": 0,
        "grant_callback_registered": True,
    }
    received_grants = []
    connection_count = 0
    location_sent = False

    async def mock_plugin(reader, writer):
        nonlocal connection_count, location_sent
        connection_count += 1
        bridge_world_state = False
        try:
            while line := await reader.readline():
                request = json.loads(line)
                if request.get("cmd") == "ping":
                    response = {
                        "event": "pong",
                        "game": "JC3",
                        "base_addr": "unavailable",
                    }
                    writer.write((json.dumps(response) + "\n").encode("utf-8"))
                    await writer.drain()
                    continue
                if request.get("cmd") == "set_session":
                    assert request["seed"] == SEED_NAME
                    assert request["slot"] == SLOT_NAME
                    writer.write(
                        (json.dumps({
                            "event": "session_updated",
                            "session_id": request["session_id"],
                            "delivery_index": state["next_delivery_index"],
                            "id": request["id"],
                        }) + "\n").encode("utf-8")
                    )
                    await writer.drain()
                    continue
                if request.get("cmd") == "set_in_world":
                    bridge_world_state = request["in_world"]
                    writer.write(
                        (json.dumps({
                            "event": "in_world_updated",
                            "in_world": bridge_world_state,
                            "id": request["id"],
                        }) + "\n").encode("utf-8")
                    )
                    await writer.drain()
                    continue
                if request.get("cmd") == "get_status":
                    writer.write(
                        (json.dumps({
                            "event": "status",
                            "backend_mode": "live",
                            "in_world": bridge_world_state,
                            "grant_callback_registered": state[
                                "grant_callback_registered"
                            ],
                            "session_id": f"{SEED_NAME}/{SLOT_NAME}",
                            "pending_queue_size": 0,
                            "build_timestamp": "mock build",
                            "id": request["id"],
                        }) + "\n").encode("utf-8")
                    )
                    await writer.drain()
                    continue
                assert request["cmd"] == "grant_item"
                received_grants.append(request)
                assert request == {
                    "cmd": "grant_item",
                    "seed_name": SEED_NAME,
                    "slot_name": SLOT_NAME,
                    "delivery_index": 0,
                    "item_code": item_code,
                    "item_id": expected_item_id,
                    "count": 1,
                }

                if not state.get("deferred"):
                    state["deferred"] = True
                    writer.write(
                        (
                            json.dumps(
                                {
                                    "event": "grant_queued",
                                    "seed_name": SEED_NAME,
                                    "slot_name": SLOT_NAME,
                                    "delivery_index": 0,
                                    "item_id": expected_item_id,
                                    "count": 1,
                                }
                            )
                            + "\n"
                            + json.dumps(
                                {
                                    "event": "grant_failed",
                                    "seed_name": SEED_NAME,
                                    "slot_name": SLOT_NAME,
                                    "delivery_index": 0,
                                    "item_id": expected_item_id,
                                    "count": 1,
                                    "reason": "world_not_loaded",
                                    "retryable": True,
                                }
                            )
                            + "\n"
                        ).encode("utf-8")
                    )
                    await writer.drain()
                    continue

                if request["delivery_index"] < state["next_delivery_index"]:
                    response = {
                        "event": "grant_applied",
                        "seed_name": SEED_NAME,
                        "slot_name": SLOT_NAME,
                        "delivery_index": request["delivery_index"],
                        "item_id": expected_item_id,
                        "count": 1,
                    }
                    writer.write((json.dumps(response) + "\n").encode("utf-8"))
                    await writer.drain()
                    return

                assert request["delivery_index"] == state["next_delivery_index"]
                state["apply_count"] += 1
                state["next_delivery_index"] += 1
                queued = {
                    "event": "grant_queued",
                    "seed_name": SEED_NAME,
                    "slot_name": SLOT_NAME,
                    "delivery_index": 0,
                    "item_id": expected_item_id,
                    "count": 1,
                }
                applied = {
                    "event": "grant_applied",
                    "seed_name": SEED_NAME,
                    "slot_name": SLOT_NAME,
                    "delivery_index": 0,
                    "item_id": expected_item_id,
                    "count": 1,
                }
                writer.write((json.dumps(queued) + "\n").encode("utf-8"))
                writer.write((json.dumps(applied) + "\n").encode("utf-8"))
                if not location_sent:
                    writer.write(
                        (
                            json.dumps(
                                {
                                    "event": "location_checked",
                                    "location_id": location_id,
                                }
                            )
                            + "\n"
                        ).encode("utf-8")
                    )
                    location_sent = True
                await writer.drain()
                return
        finally:
            writer.close()
            await writer.wait_closed()

    port_probe = socket.socket()
    port_probe.bind(("127.0.0.1", 0))
    port = port_probe.getsockname()[1]
    port_probe.close()
    plugin_server = await asyncio.start_server(mock_plugin, "127.0.0.1", port)
    ap_packets = []

    async def make_context():
        context = client.JustCause3Context(None, None, "127.0.0.1", port)
        context.slot = 1
        context.server = object()
        context.missing_locations = set(client.LOCATION_IDS)

        async def capture_ap_packets(messages):
            ap_packets.extend(messages)

        context.send_msgs = capture_ap_packets
        context.game_task = asyncio.create_task(context.game_bridge_loop())
        context.on_package("Connected", {"slot_data": {}})
        context.on_package(
            "ReceivedItems",
            {"index": 0, "items": [[item_code, 0, 2, 0]]},
        )
        return context

    contexts = []
    try:
        first = await make_context()
        contexts.append(first)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not first.game_connected.is_set():
            await asyncio.sleep(0.01)
        assert await first.set_game_world_state(True)
        assert first.ap_authenticated
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and (
            state["apply_count"] != 1
            or first.next_item_index != 1
            or not ap_packets
        ):
            await asyncio.sleep(0.01)

        assert ap_packets == [
            {"cmd": "LocationChecks", "locations": (location_id,)}
        ], ap_packets

        await first.shutdown()
        second = await make_context()
        contexts.append(second)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and (
            second.next_item_index != 1 or connection_count < 2
        ):
            await asyncio.sleep(0.01)

        assert second.next_item_index == 1
        assert state["apply_count"] == 1
        assert len(received_grants) == 3
        manual = client.JustCause3Context(None, None)
        manual.cached_game_status = {"grant_callback_registered": False}
        manual.pending_items[0] = [item_code, 0, 2, 0]
        manual.received_items[0] = {
            "name": item_name,
            "item_code": item_code,
            "status": "queued",
        }
        await manual._drain_received_items()
        assert manual.next_item_index == 1
        assert manual.received_items[0]["status"] == "manual"
        assert len(received_grants) == 3
        print(
            "Mock AP round-trip, duplicate-delivery recovery, and "
            "manual item mode passed."
        )
    finally:
        for context in contexts:
            await context.shutdown()
        plugin_server.close()
        await plugin_server.wait_closed()


def start_native_host(executable, port, state_directory, mode="mock"):
    process = subprocess.Popen(
        [str(executable), str(port), str(state_directory), mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 10
    last_error = None
    while time.monotonic() < deadline and process.poll() is None:
        try:
            with socket.create_connection((HOST, port), timeout=0.2):
                return process
        except OSError as error:
            last_error = error
            time.sleep(0.05)
    output = process.communicate(timeout=2)[0]
    raise RuntimeError(f"Native bridge host did not start: {last_error}\n{output}")


def stop_native_host(process):
    if process.poll() is None:
        process.stdin.write("quit\n")
        process.stdin.flush()
    output = process.communicate(timeout=10)[0]
    if process.returncode != 0:
        raise AssertionError(f"Native test host failed:\n{output}")
    for line in output.splitlines():
        if line.startswith(("mock_apply_count=", "live_apply_count=")):
            return int(line.partition("=")[2])
    raise AssertionError(f"Native host omitted apply count:\n{output}")


def test_native_idempotency(executable):
    port_probe = socket.socket()
    port_probe.bind((HOST, 0))
    first_port = port_probe.getsockname()[1]
    port_probe.close()
    port_probe = socket.socket()
    port_probe.bind((HOST, 0))
    second_port = port_probe.getsockname()[1]
    port_probe.close()

    item_code = 0x4A430001
    request = {
        "cmd": "grant_item",
        "seed_name": SEED_NAME,
        "slot_name": SLOT_NAME,
        "delivery_index": 0,
        "item_code": item_code,
        "item_id": "uvk_13",
        "count": 1,
    }

    with tempfile.TemporaryDirectory(prefix="jc3ap-grants-") as directory:
        state_directory = Path(directory)
        first = start_native_host(executable, first_port, state_directory)
        with socket.create_connection((HOST, first_port), timeout=5) as sock:
            sock.settimeout(5)
            reader = sock.makefile("r", encoding="utf-8")
            send_json_line(sock, request)
            queued = read_json_line(reader)
            assert queued["event"] == "grant_queued", queued
            applied = read_json_line(reader)
            assert applied["event"] == "grant_applied", applied
            assert applied["delivery_index"] == 0
            assert applied["seed_name"] == SEED_NAME
            assert applied["slot_name"] == SLOT_NAME
            send_json_line(sock, {"cmd": "get_status"})
            status = read_json_line(reader)
            assert status["event"] == "status", status
            assert status["backend_mode"] == "mock", status
            assert status["in_world"] is True, status
            assert status["active_session_id"] == f"{SEED_NAME}/{SLOT_NAME}", status
            assert status["delivery_index"] == 1, status
            assert status["registered_catalog_handlers"] == 395, status
            assert status["item_catalog_entries"] == 91, status
            assert status["location_catalog_entries"] == 304, status

        assert stop_native_host(first) == 1

        restarted = start_native_host(executable, second_port, state_directory)
        with socket.create_connection((HOST, second_port), timeout=5) as sock:
            sock.settimeout(5)
            reader = sock.makefile("r", encoding="utf-8")
            send_json_line(sock, request)
            duplicate = read_json_line(reader)
            assert duplicate["event"] == "grant_applied", duplicate
            assert duplicate["delivery_index"] == 0
        assert stop_native_host(restarted) == 0

    print("Native persistent grant deduplication passed across restart.")


def test_native_world_gate(executable):
    client = load_client_module()
    port_probe = socket.socket()
    port_probe.bind((HOST, 0))
    port = port_probe.getsockname()[1]
    port_probe.close()
    request = {
        "cmd": "grant_item",
        "seed_name": SEED_NAME,
        "slot_name": SLOT_NAME,
        "delivery_index": 0,
        "item_code": 0x4A430001,
        "item_id": "uvk_13",
        "count": 1,
    }

    with tempfile.TemporaryDirectory(prefix="jc3ap-world-gate-") as directory:
        process = start_native_host(executable, port, Path(directory), "live")
        try:
            with socket.create_connection((HOST, port), timeout=5) as sock:
                sock.settimeout(5)
                reader = sock.makefile("r", encoding="utf-8")
                sock.sendall(
                    client.encode_game_message({
                        "cmd": "set_session",
                        "session_id": f"{SEED_NAME}/{SLOT_NAME}",
                        "seed": SEED_NAME,
                        "slot": SLOT_NAME,
                        "id": 501,
                    })
                )
                session = read_json_line(reader)
                assert session["event"] == "session_updated", session
                assert session["session_id"] == f"{SEED_NAME}/{SLOT_NAME}"
                assert session["id"] == 501

                sock.sendall(
                    client.encode_game_message({"cmd": "get_status", "id": 502})
                )
                status = read_json_line(reader)
                assert status["id"] == 502
                assert status["active_session_id"] == f"{SEED_NAME}/{SLOT_NAME}"
                assert status["in_world"] is False
                assert status.get("build_timestamp"), status

                sock.sendall(
                    client.encode_game_message(
                        {"cmd": "invalid_test", "id": 504}
                    )
                )
                error = read_json_line(reader)
                assert error["event"] == "error" and error["id"] == 504, error
                assert error.get("message"), error

                send_json_line(sock, request)
                assert read_json_line(reader)["event"] == "grant_queued"
                failed = read_json_line(reader)
                assert failed["event"] == "grant_failed", failed
                assert failed["reason"] == "world_not_loaded", failed
                assert failed["retryable"] is True, failed

                send_json_line(sock, {"cmd": "get_status"})
                status = read_json_line(reader)
                assert status["backend_mode"] == "live", status
                assert status["in_world"] is False, status

                sock.sendall(
                    client.encode_game_message(
                        {"cmd": "set_in_world", "in_world": True, "id": 503}
                    )
                )
                world_update = read_json_line(reader)
                assert world_update == {
                    "event": "in_world_updated",
                    "in_world": True,
                    "id": 503,
                }, world_update
                location = read_json_line(reader)
                assert location == {
                    "event": "location_checked",
                    "location_id": 0x4A430101,
                }, location
                applied = read_json_line(reader)
                assert applied["event"] == "grant_applied", applied
                time.sleep(0.15)
                send_json_line(sock, {"cmd": "get_status"})
                status = read_json_line(reader)
                assert status["event"] == "status", status
                assert status["in_world"] is True, status
        finally:
            assert stop_native_host(process) == 1

    print("Native live-backend world gating passed.")


def test_loaded_bridge_dll(loader_path, port, proxy=False):
    if os.name != "nt":
        raise RuntimeError("Loading the JC3 Windows DLLs requires Windows.")
    bridge_path = (
        loader_path.resolve().with_name("jc3_ap_plugin.dll")
        if proxy
        else loader_path.resolve()
    )
    if not bridge_path.is_file():
        raise FileNotFoundError(f"Bridge plugin not found: {bridge_path}")
    previous_port = os.environ.get("JC3AP_PORT")
    previous_state_dir = os.environ.get("JC3AP_STATE_DIR")
    with tempfile.TemporaryDirectory(prefix="jc3ap-plugin-test-") as directory:
        os.environ["JC3AP_PORT"] = str(port)
        os.environ["JC3AP_STATE_DIR"] = directory
        loader = None
        plugin = None
        try:
            loader = ctypes.WinDLL(str(loader_path.resolve()))
            if proxy:
                plugin = ctypes.WinDLL(str(bridge_path))
            else:
                plugin = loader
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection((HOST, port), timeout=0.25) as sock:
                        sock.settimeout(1)
                        with sock.makefile("r", encoding="utf-8") as reader:
                            send_json_line(sock, {"cmd": "ping"})
                            pong = read_json_line(reader)
                            if pong.get("event") == "pong":
                                break
                except OSError:
                    time.sleep(0.05)
            else:
                raise RuntimeError("jc3_ap_plugin.dll did not start its IPC server.")
            test_live_bridge(HOST, port)
        finally:
            try:
                if plugin is not None:
                    plugin.JC3AP_Shutdown.restype = ctypes.c_int
                    if plugin.JC3AP_Shutdown() != 1:
                        raise RuntimeError(
                            "jc3_ap_plugin.dll bridge did not shut down cleanly."
                        )
            finally:
                if previous_port is None:
                    os.environ.pop("JC3AP_PORT", None)
                else:
                    os.environ["JC3AP_PORT"] = previous_port
                if previous_state_dir is None:
                    os.environ.pop("JC3AP_STATE_DIR", None)
                else:
                    os.environ["JC3AP_STATE_DIR"] = previous_state_dir
    print(
        f"{loader_path.name} end-to-end protocol passed "
        f"(loaded {bridge_path.name})."
    )


def test_plugin_dll(plugin_path, port):
    test_loaded_bridge_dll(plugin_path, port)


def test_proxy_dll(proxy_path, port):
    test_loaded_bridge_dll(proxy_path, port, proxy=True)


def main():
    parser = argparse.ArgumentParser(
        description="Check the Just Cause 3 NDJSON bridge protocol."
    )
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Exercise AP delivery, retryable failures, and disconnect recovery.",
    )
    parser.add_argument(
        "--native-host",
        type=Path,
        help="Run C++ persistence, world-gating, and location-poll tests.",
    )
    parser.add_argument(
        "--plugin-dll",
        type=Path,
        help="Load the compiled jc3_ap_plugin.dll and test its IPC protocol.",
    )
    parser.add_argument(
        "--proxy-dll",
        type=Path,
        help="Load dinput8.dll and test its same-directory bridge plugin.",
    )
    args = parser.parse_args()
    if args.mock:
        asyncio.run(test_authentication_flow())
        asyncio.run(test_save_snapshot())
        test_settlement_save_mapping()
        asyncio.run(test_settlement_save_check_submission())
        asyncio.run(test_received_item_delivery_index())
        asyncio.run(test_mock_round_trip())
        asyncio.run(test_manual_tracker_commands())
        asyncio.run(test_legacy_bridge_response_correlation())
    elif args.native_host:
        executable = args.native_host.resolve()
        test_native_idempotency(executable)
        test_native_world_gate(executable)
    elif args.plugin_dll:
        test_plugin_dll(args.plugin_dll, args.port)
    elif args.proxy_dll:
        test_proxy_dll(args.proxy_dll, args.port)
    else:
        test_live_bridge(args.host, args.port)


if __name__ == "__main__":
    main()
