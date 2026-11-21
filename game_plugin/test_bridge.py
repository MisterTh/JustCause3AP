import argparse
import asyncio
import importlib
import json
import logging
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
    with socket.create_connection((host, port), timeout=3) as sock:
        sock.settimeout(3)
        reader = sock.makefile("r", encoding="utf-8")
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
        assert status["registered_catalog_handlers"] == 392, status
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
            del cmd, args

        async def send_msgs(self, messages):
            del messages

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


async def test_manual_tracker_commands():
    client = load_client_module()
    location_name = next(iter(client.location_table))
    location_id = client.location_table[location_name]
    context = client.JustCause3Context(None, None)
    context.server = object()
    context.slot = 1
    context.missing_locations = {location_id}
    context.game_connected.set()
    sent_packets = []

    async def capture_packets(messages):
        sent_packets.extend(messages)

    context.send_msgs = capture_packets

    async def return_status(message):
        assert message == {"cmd": "get_status"}
        context.pending_status.set_result(
            {
                "event": "status",
                "backend_mode": "live",
                "in_world": True,
                "registered_catalog_handlers": 392,
            }
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


async def test_mock_round_trip():
    client = load_client_module()
    location_id = next(iter(client.LOCATION_IDS))
    item_code, item_name = next(iter(client.ITEM_ID_TO_NAME.items()))
    expected_item_id = client.game_item_id(item_name)
    state = {"next_delivery_index": 0, "apply_count": 0}
    received_grants = []
    connection_count = 0
    location_sent = False

    async def mock_plugin(reader, writer):
        nonlocal connection_count, location_sent
        connection_count += 1
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
        context.on_package(
            "ReceivedItems",
            {"index": 0, "items": [[item_code, 0, 2, 0]]},
        )
        return context

    contexts = []
    try:
        first = await make_context()
        contexts.append(first)
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
        print("Mock AP round-trip and duplicate-delivery recovery passed.")
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
            assert status["registered_catalog_handlers"] == 392, status
            assert status["item_catalog_entries"] == 88, status
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

                process.stdin.write("world\n")
                process.stdin.flush()
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
        finally:
            assert stop_native_host(process) == 1

    print("Native live-backend world gating passed.")


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
    args = parser.parse_args()
    if args.mock:
        asyncio.run(test_mock_round_trip())
        asyncio.run(test_manual_tracker_commands())
    elif args.native_host:
        executable = args.native_host.resolve()
        test_native_idempotency(executable)
        test_native_world_gate(executable)
    else:
        test_live_bridge(args.host, args.port)


if __name__ == "__main__":
    main()
