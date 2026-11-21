"""Verify the installed Just Cause 3 APWorld against Archipelago's data package."""

import argparse
import ast
from datetime import datetime
import json
from pathlib import Path
import sys
from zipfile import BadZipFile, ZipFile


GAME_NAME = "Just Cause 3"
DEFAULT_ARCHIVE = Path(
    r"C:\ProgramData\Archipelago\custom_worlds\justcause3.apworld"
)
DEFAULT_DATAPACKAGE = Path(
    r"C:\ProgramData\Archipelago\datapackage_export.json"
)
ITEM_ID_BASE = 0x4A430000
LOCATION_ID_BASE = 0x4A430100
FILLER_ITEM_NAME = "Rebel Supply Drop"


def literal_assignment(source: str, name: str):
    module = ast.parse(source)
    for statement in module.body:
        if isinstance(statement, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == name
                for target in statement.targets
            ):
                return ast.literal_eval(statement.value)
    raise ValueError(f"Could not find literal assignment for {name}")


def read_archive_tables(archive_path: Path):
    try:
        archive = ZipFile(archive_path)
    except (OSError, BadZipFile) as error:
        raise ValueError(f"Cannot open APWorld archive: {error}") from error

    with archive:
        bad_member = archive.testzip()
        if bad_member:
            raise ValueError(f"Archive CRC check failed for {bad_member}")

        names = archive.namelist()
        top_level = {name.split("/", 1)[0] for name in names}
        if top_level != {"justcause3"}:
            raise ValueError(
                "Expected one 'justcause3/' directory in archive; "
                f"found {sorted(top_level)}"
            )

        required = {
            "justcause3/__init__.py",
            "justcause3/archipelago.json",
            "justcause3/archi_defs.py",
            "justcause3/Items.py",
            "justcause3/Locations.py",
        }
        missing = required - set(names)
        if missing:
            raise ValueError(f"Archive is missing required files: {sorted(missing)}")

        manifest = json.loads(archive.read("justcause3/archipelago.json"))
        if manifest.get("game") != GAME_NAME:
            raise ValueError(
                f"Archive manifest game is {manifest.get('game')!r}, "
                f"not {GAME_NAME!r}"
            )

        definitions = archive.read("justcause3/archi_defs.py").decode("utf-8")
        for member in names:
            if member.endswith(".py"):
                compile(archive.read(member), member, "exec")

    item_data = literal_assignment(definitions, "item_data_table")
    location_data = literal_assignment(definitions, "loc_data_table")
    item_ids = {
        name: ITEM_ID_BASE + data["code"] for name, data in item_data.items()
    }
    item_ids[FILLER_ITEM_NAME] = (
        ITEM_ID_BASE + max((data["code"] for data in item_data.values()), default=0) + 1
    )
    location_ids = {
        name: LOCATION_ID_BASE + data["code"]
        for name, data in location_data.items()
    }
    return manifest, item_ids, location_ids


def compare_data_packages(
    expected: dict[str, int],
    actual: dict[str, int],
    label: str,
) -> list[str]:
    problems = []
    for name in sorted(expected.keys() - actual.keys()):
        problems.append(f"Missing {label}: {name}")
    for name in sorted(actual.keys() - expected.keys()):
        problems.append(f"Unexpected {label}: {name}")
    for name in sorted(expected.keys() & actual.keys()):
        if expected[name] != actual[name]:
            problems.append(
                f"ID mismatch for {label.lower()} {name}: "
                f"archive={expected[name]}, export={actual[name]}"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the installed Just Cause 3 APWorld and print its items."
    )
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--datapackage", type=Path, default=DEFAULT_DATAPACKAGE)
    args = parser.parse_args()

    if not args.archive.is_file():
        print(f"ERROR: Installed APWorld not found: {args.archive}")
        return 1
    if not args.datapackage.is_file():
        print(f"ERROR: Archipelago data package not found: {args.datapackage}")
        return 1

    try:
        manifest, expected_items, expected_locations = read_archive_tables(
            args.archive
        )
        archive_modified = args.archive.stat().st_mtime
        datapackage_modified = args.datapackage.stat().st_mtime
        export = json.loads(args.datapackage.read_text(encoding="utf-8"))
        game_export = export["games"][GAME_NAME]
        exported_items = game_export["item_name_to_id"]
        exported_locations = game_export["location_name_to_id"]
    except (BadZipFile, KeyError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        print(f"ERROR: Verification failed: {error}")
        return 1

    if datapackage_modified < archive_modified:
        print("ERROR: Datapackage export predates the installed APWorld.")
        print(
            f"  APWorld: {args.archive} "
            f"({datetime.fromtimestamp(archive_modified).astimezone().isoformat(timespec='seconds')})"
        )
        print(
            f"  Export:  {args.datapackage} "
            f"({datetime.fromtimestamp(datapackage_modified).astimezone().isoformat(timespec='seconds')})"
        )
        print(
            f"  Expected: {len(expected_items)} items, "
            f"{len(expected_locations)} locations"
        )
        print(
            f"  Exported: {len(exported_items)} items, "
            f"{len(exported_locations)} locations"
        )
        print(
            "Restarting the Launcher does not rewrite this file. After the "
            "restart finishes loading worlds, use its 'Export Datapackage' "
            "tool, then run this verifier again."
        )
        return 1

    problems = compare_data_packages(
        expected_items, exported_items, "item"
    ) + compare_data_packages(
        expected_locations, exported_locations, "location"
    )
    if problems:
        print(f"ERROR: Installed archive and {args.datapackage} do not match:")
        for problem in problems[:20]:
            print(f"  - {problem}")
        if len(problems) > 20:
            print(f"  ... and {len(problems) - 20} more discrepancies")
        return 1

    print(f"Verified installed APWorld: {args.archive}")
    print(f"Game: {manifest['game']} (world version {manifest.get('world_version', 'unknown')})")
    print(f"Archipelago data package: {args.datapackage}")
    print(
        f"Registered item names and IDs ({len(exported_items)}):"
    )
    for name, item_id in sorted(exported_items.items(), key=lambda entry: entry[1]):
        print(f"  [{item_id}] {name}")
    print(
        f"\nVerified {len(exported_locations)} exported locations "
        "against the installed archive."
    )
    print("The world is present in the data package; restart Archipelago after replacing it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
