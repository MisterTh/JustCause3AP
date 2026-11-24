"""Generate or verify the C++ catalog from the APWorld source data."""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import runpy
import sys
from types import ModuleType
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "worlds" / "justcause3"
OUTPUT = ROOT / "game_plugin" / "jc3_catalog.hpp"
ITEM_CATEGORIES = {
    "vehicle",
    "weapon",
    "rebel_drop",
    "gear_mod",
    "fow_hazard",
    "hazard_disabler",
    "tether",
    "wingsuit",
}
LOCATION_CATEGORIES = {
    "ancient_tomb",
    "di_ravello_tape",
    "dlc_mission",
    "eden_callaghan_tape",
    "military_base",
    "rebel_shrine",
    "settlement",
    "story_mission",
}


def read_integer_constant(path: Path, name: str) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name
               for target in statement.targets):
            value = ast.literal_eval(statement.value)
            if not isinstance(value, int):
                raise ValueError(f"{name} in {path} is not an integer")
            return value
    raise ValueError(f"Could not find {name} in {path}")


def cpp_string(value: str) -> str:
    # Generated metadata is UTF-8 C++ source; JSON escaping handles quotes,
    # backslashes, and control characters without changing the display text.
    return json.dumps(value, ensure_ascii=False)


def row(ap_id: int, name: str, category: str, game_key: str) -> str:
    return (
        f"    CatalogEntry{{0x{ap_id:08X}u, {cpp_string(name)}, "
        f"{cpp_string(category)}, {cpp_string(game_key)}}},"
    )


def load_python_tables() -> tuple[dict[str, int], dict[str, int]]:
    worlds = ModuleType("worlds")
    worlds.__path__ = [str(PACKAGE.parent)]
    package = ModuleType("worlds.justcause3")
    package.__path__ = [str(PACKAGE)]
    base_classes = ModuleType("BaseClasses")
    base_classes.Item = type("Item", (), {})
    base_classes.Location = type("Location", (), {})
    base_classes.Region = type("Region", (), {})
    base_classes.ItemClassification = type(
        "ItemClassification",
        (),
        {"useful": 1, "filler": 2, "progression": 4},
    )
    sys.modules["worlds"] = worlds
    sys.modules["worlds.justcause3"] = package
    sys.modules["BaseClasses"] = base_classes
    items = importlib.import_module("worlds.justcause3.Items")
    locations = importlib.import_module("worlds.justcause3.Locations")
    return items.item_name_to_id, locations.location_table


def build_header() -> str:
    source = runpy.run_path(str(PACKAGE / "archi_defs.py"))
    game_data: dict[str, Any] = source["GAME_DATA"]
    item_data: dict[str, dict[str, Any]] = source["item_data_table"]
    location_data: dict[str, dict[str, Any]] = source["loc_data_table"]
    item_names: dict[str, int] = source["item_name_to_id"]
    location_names: dict[str, int] = source["loc_name_to_id"]

    item_base = read_integer_constant(PACKAGE / "Items.py", "ITEM_ID_BASE")
    location_base = read_integer_constant(
        PACKAGE / "Locations.py", "LOCATION_ID_BASE"
    )
    if item_base != 0x4A430000 or location_base != 0x4A430100:
        raise ValueError("Unexpected Just Cause 3 AP ID bases")
    python_items, python_locations = load_python_tables()
    expected_items = {
        name: item_base + code for name, code in item_names.items()
    }
    expected_items["Rebel Supply Drop"] = (
        item_base + max(item_names.values(), default=0) + 1
    )
    expected_locations = {
        name: location_base + code for name, code in location_names.items()
    }
    expected_items.update({
        "Wingsuit Progression": item_base + 89,
        "Tether Progression": item_base + 90,
        "FOW Hazard Disabler": item_base + 91,
    })
    if python_items != expected_items or python_locations != expected_locations:
        raise ValueError("Runtime Items.py/Locations.py IDs differ from source data")

    item_metadata = {entry["id"]: entry for entry in game_data["items"]}
    location_metadata = {entry["id"]: entry for entry in game_data["locations"]}
    if {record["asset_id"] for record in item_data.values()} != set(item_metadata):
        raise ValueError("Item metadata and AP lookup table identifiers differ")
    if {record["asset_id"] for record in location_data.values()} != set(location_metadata):
        raise ValueError("Location metadata and AP lookup table identifiers differ")

    items: list[tuple[int, str, str, str]] = []
    seen_item_codes: set[int] = set()
    for name, record in item_data.items():
        code = record["code"]
        if item_names.get(name) != code or code in seen_item_codes:
            raise ValueError(f"Inconsistent or duplicate item code: {name}")
        seen_item_codes.add(code)
        if record["category"] not in ITEM_CATEGORIES:
            raise ValueError(f"Unmapped item category: {record['category']}")
        metadata = item_metadata[record["asset_id"]]
        if (
            metadata["id"] != record["asset_id"]
            or metadata["category"] != record["category"]
        ):
            raise ValueError(f"Inconsistent item catalog key: {name}")
        items.append((
            item_base + code,
            name,
            record["category"],
            record["asset_id"],
        ))

    for name, code, category, game_key in (
        ("Wingsuit Progression", 89, "wingsuit", "progression:wingsuit"),
        ("Tether Progression", 90, "tether", "progression:tether"),
        ("FOW Hazard Disabler", 91, "hazard_disabler", "progression:fow_hazard"),
    ):
        if code in seen_item_codes:
            raise ValueError(f"Progression item code collides: {name}")
        seen_item_codes.add(code)
        items.append((
            item_base + code,
            name,
            category,
            game_key,
        ))

    filler_name = "Rebel Supply Drop"
    filler_code = max(item_names.values(), default=0) + 1
    if filler_name in item_names or filler_code in seen_item_codes:
        raise ValueError("Filler item ID collides with an AP item")
    items.append((
        item_base + filler_code,
        filler_name,
        "rebel_drop",
        "rebel_drop:Rebel Supply Drop",
    ))

    locations: list[tuple[int, str, str, str]] = []
    seen_location_codes: set[int] = set()
    for name, record in location_data.items():
        code = record["code"]
        if location_names.get(name) != code or code in seen_location_codes:
            raise ValueError(f"Inconsistent or duplicate location code: {name}")
        seen_location_codes.add(code)
        if record["category"] not in LOCATION_CATEGORIES:
            raise ValueError(f"Unmapped location category: {record['category']}")
        metadata = location_metadata[record["asset_id"]]
        if (
            metadata["id"] != record["asset_id"]
            or metadata["category"] != record["category"]
        ):
            raise ValueError(f"Inconsistent location catalog key: {name}")
        locations.append((
            location_base + code,
            name,
            record["category"],
            record["asset_id"],
        ))

    items.sort()
    locations.sort()
    lines = [
        "#pragma once",
        "",
        "#include <array>",
        "#include <cstdint>",
        "#include <string_view>",
        "",
        "namespace jc3ap {",
        "",
        "// Generated from worlds/justcause3/archi_defs.py.",
        "// game_key is curated metadata, not a verified engine hash or SDK handle.",
        "struct CatalogEntry {",
        "    std::uint32_t ap_id;",
        "    std::string_view name;",
        "    std::string_view category;",
        "    std::string_view game_key;",
        "};",
        "",
        f"inline constexpr std::array<CatalogEntry, {len(items)}> kItemCatalog{{{{",
        *(row(*entry) for entry in items),
        "}};",
        "",
        f"inline constexpr std::array<CatalogEntry, {len(locations)}> kLocationCatalog{{{{",
        *(row(*entry) for entry in locations),
        "}};",
        "",
        "}  // namespace jc3ap",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail if jc3_catalog.hpp is missing or out of date.",
    )
    args = parser.parse_args()
    generated = build_header()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != generated:
            print(f"Catalog is stale; run {Path(__file__).name} to regenerate it.")
            return 1
        print("JC3 catalog parity passed.")
        return 0

    OUTPUT.write_text(generated, encoding="utf-8", newline="\n")
    print(f"Generated {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
