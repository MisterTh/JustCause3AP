"""Generate Archipelago tables from the curated Just Cause 3 wiki lists."""

import argparse
import importlib.util
from pprint import pformat
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WIKI_DATA_PATH = ROOT / "worlds" / "justcause3" / "wiki_checks.py"
DEFAULT_OUTPUT = ROOT / "worlds" / "justcause3" / "archi_defs.py"
DEFAULT_REPORT = ROOT / "CHECKS.md"


def load_wiki_data():
    spec = importlib.util.spec_from_file_location("jc3_wiki_checks", WIKI_DATA_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load curated data from {WIKI_DATA_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_data_table(
    candidates: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    names = [candidate["display_name"] for candidate in candidates]
    duplicate_names = {name for name in names if names.count(name) > 1}

    table: dict[str, dict[str, Any]] = {}
    for code, candidate in enumerate(candidates, start=1):
        name = candidate["display_name"]
        if name in duplicate_names:
            name = f"{name} [{candidate['id']}]"
        data = {
            "code": code,
            "asset_id": candidate["id"],
            "category": candidate["category"],
            "region": candidate["regions"][0],
            "sources": candidate["sources"],
        }
        if "unlock_method" in candidate:
            data["unlock_method"] = candidate["unlock_method"]
        table[name] = data
    return table


def build_catalog() -> dict[str, Any]:
    wiki_data = load_wiki_data()
    items = wiki_data.build_item_candidates()
    locations = wiki_data.build_location_candidates()
    missions = [
        candidate
        for candidate in locations
        if candidate["category"] in {"story_mission", "dlc_mission"}
    ]

    return {
        "schema_version": 2,
        "source": "Curated Just Cause Wiki check lists",
        "sources": wiki_data.SOURCES,
        "notes": [
            "This catalog replaces guesses from generic asset names with explicitly listed candidate checks.",
            "Names and categories are transcribed from the cited community wiki pages; verify in-game before finalizing logic.",
            "The 78 military entries exclude Guardia Massos V because the wiki says it remains permanently restricted.",
            "Rebel Coves and random encounters are excluded because they do not count as ordinary liberation/100% checks.",
            "Rebel Drop vehicles marked as garage unlocks enter the list after being brought to a garage; they are not ambient vehicle spawns.",
            "DLC checks are listed separately by category and may be removed if the world targets base game only.",
        ],
        "excluded": {
            "military_bases": list(wiki_data.EXCLUDED_MILITARY_BASES),
            "rebel_coves": list(wiki_data.EXCLUDED_REBEL_COVES),
            "random_encounters": "Recurring encounters are not fixed 100% completion checks.",
        },
        "items": items,
        "locations": locations,
        "missions": missions,
        "mission_sources": [
            {"path": "wiki", "format": "curated list", "source": wiki_data.SOURCES["story_missions"]},
            {"path": "wiki", "format": "curated list", "source": wiki_data.SOURCES["dlc_missions"]},
        ],
    }


def render_python_module(catalog: dict[str, Any]) -> str:
    item_data_table = make_data_table(catalog["items"])
    loc_data_table = make_data_table(catalog["locations"])
    mission_data_table = {
        name: data
        for name, data in loc_data_table.items()
        if data["category"] in {"story_mission", "dlc_mission"}
    }

    return (
        '"""Generated Just Cause 3 check data; regenerate with scripts/get_checks.py."""\n\n'
        f"GAME_DATA = {pformat(catalog, width=100, sort_dicts=False)}\n\n"
        f"item_data_table = {pformat(item_data_table, width=100, sort_dicts=False)}\n\n"
        f"loc_data_table = {pformat(loc_data_table, width=100, sort_dicts=False)}\n\n"
        f"mission_data_table = {pformat(mission_data_table, width=100, sort_dicts=False)}\n\n"
        "item_name_to_id = {name: data['code'] for name, data in item_data_table.items()}\n"
        "loc_name_to_id = {name: data['code'] for name, data in loc_data_table.items()}\n"
        "item_id_to_name = {code: name for name, code in item_name_to_id.items()}\n"
        "loc_id_to_name = {code: name for name, code in loc_name_to_id.items()}\n\n"
        'ITEMS = GAME_DATA["items"]\n'
        'LOCATIONS = GAME_DATA["locations"]\n'
        'MISSIONS = GAME_DATA["missions"]\n'
        'MISSION_SOURCES = GAME_DATA["mission_sources"]\n'
    )


def render_markdown(catalog: dict[str, Any]) -> str:
    wiki_data = load_wiki_data()
    lines = [
        "# Just Cause 3 candidate checks and unlocks",
        "",
        "These names are transcribed from the cited community-wiki lists. "
        "Verify them in-game before treating them as final logic.",
        "",
        "## Check candidates",
        "",
    ]

    location_labels = {
        "military_base": "Liberatable military bases and outposts",
        "settlement": "Settlements and villages",
        "di_ravello_tape": "Di Ravello tapes",
        "eden_callaghan_tape": "Eden Callaghan tapes (DLC)",
        "rebel_shrine": "Rebel shrines",
        "ancient_tomb": "Ancient tombs",
        "story_mission": "Base-game story missions",
        "dlc_mission": "DLC missions",
    }
    for category, label in location_labels.items():
        candidates = [
            item for item in catalog["locations"] if item["category"] == category
        ]
        lines.extend([f"### {label} ({len(candidates)})", ""])
        for candidate in candidates:
            region = candidate["regions"][0]
            lines.append(f"- **{region}:** {candidate['name']}")
        lines.append("")

    lines.extend(["## Rebel Drop unlock candidates", ""])
    item_labels = {
        "weapon": "Weapons",
        "vehicle": "Vehicles",
    }
    method_labels = {
        "base": "Base rewards",
        "liberation": "Liberation rewards",
        "mission": "Mission rewards",
        "vintage_parts": "Vintage Parts rewards",
        "ancient_tombs": "Ancient Tomb reward",
        "di_ravello_tapes": "Di Ravello tape reward",
        "daredevil_jumps": "Daredevil Jump rewards",
        "garage_unlock": "Added to Rebel Drop after bringing to a garage",
    }
    for kind, kind_label in item_labels.items():
        lines.extend([f"### {kind_label}", ""])
        candidates = [item for item in catalog["items"] if item["category"] == kind]
        methods = dict.fromkeys(item["unlock_method"] for item in candidates)
        for method in methods:
            rewards = [
                item["name"]
                for item in candidates
                if item["unlock_method"] == method
            ]
            lines.append(f"- **{method_labels[method]}:** " + "; ".join(rewards))
        lines.append("")

    lines.extend(["## Exclusions and notes", ""])
    lines.append(
        "- **Permanently restricted military entry:** "
        + "; ".join(wiki_data.EXCLUDED_MILITARY_BASES)
        + "."
    )
    lines.append(
        "- **Rebel Coves excluded as liberation checks:** "
        + "; ".join(wiki_data.EXCLUDED_REBEL_COVES)
        + "."
    )
    lines.append(
        "- Recurring random encounters and ambient vehicle spawns are not listed "
        "as fixed checks."
    )
    lines.append(
        "- Rebel Drop garage unlocks are items/unlocks, not checks for every "
        "overworld vehicle instance."
    )
    lines.append(
        "- Tapes and coordinates identify collectible entries; shrine and tomb "
        "coordinates follow the wiki's listed format."
    )

    lines.extend(["", "## Sources", ""])
    for source_name, source_url in wiki_data.SOURCES.items():
        label = source_name.replace("_", " ").title()
        lines.append(f"- [{label}]({source_url})")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Just Cause 3 Archipelago tables from curated wiki lists."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Python module output path (defaults to worlds/justcause3/archi_defs.py)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help="Markdown list output path (defaults to CHECKS.md)",
    )
    args = parser.parse_args()

    catalog = build_catalog()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_python_module(catalog), encoding="utf-8")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_markdown(catalog), encoding="utf-8")
    print(f"Wrote curated definitions: {args.output}")
    print(f"Wrote readable check list: {args.report}")
    print(
        f"Candidates: {len(catalog['items'])} items, "
        f"{len(catalog['locations'])} checks "
        f"({len(catalog['missions'])} missions)"
    )


if __name__ == "__main__":
    main()
