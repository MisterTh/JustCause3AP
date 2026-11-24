"""Compare two Just Cause 3 save snapshots at the byte level."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

HEADER_CHECKSUM_BYTES = 4
SETTLEMENT_STATUS_FILENAME = "settlement_achievement_status"
SETTLEMENT_RECORD_START = 0x62
SETTLEMENT_RECORD_SIZE = 5
SETTLEMENT_KEY_SIZE = 4
SETTLEMENT_STATE_INDEX = 4
RESUPPLY_RECORD_START = 0x68
RESUPPLY_RECORD_SIZE = 8
RESUPPLY_TRAILING_BYTES = 1
LOCATION_MAP_SCHEMA_VERSION = 1


def changed_ranges(before: bytes, after: bytes) -> list[tuple[int, int]]:
    """Return half-open ranges of differing offsets, including length changes."""
    ranges: list[tuple[int, int]] = []
    range_start: int | None = None

    for offset in range(max(len(before), len(after))):
        old_byte = before[offset] if offset < len(before) else None
        new_byte = after[offset] if offset < len(after) else None
        if old_byte != new_byte:
            if range_start is None:
                range_start = offset
        elif range_start is not None:
            ranges.append((range_start, offset))
            range_start = None

    if range_start is not None:
        ranges.append((range_start, max(len(before), len(after))))
    return ranges


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _format_bytes(data: bytes, start: int, end: int) -> str:
    return " ".join(
        f"{data[offset]:02X}" if offset < len(data) else "--"
        for offset in range(start, end)
    )


def _format_word(data: bytes, offset: int, width: int) -> str:
    word = data[offset : offset + width]
    if len(word) != width:
        return "n/a"
    value = int.from_bytes(word, byteorder="little", signed=False)
    return f"0x{value:0{width * 2}X} ({value})"


def _changed_word_offsets(start: int, end: int, alignment: int) -> range:
    first = start - start % alignment
    return range(first, end, alignment)


def _printable_strings(data: bytes, start: int, end: int) -> list[tuple[int, str]]:
    return [
        (start + match.start(), match.group().decode("ascii"))
        for match in re.finditer(rb"[\x20-\x7e]{4,}", data[start:end])
    ]


def _format_strings(strings: list[tuple[int, str]]) -> str:
    if not strings:
        return "none"
    return "; ".join(f"0x{offset:08X} {value!r}" for offset, value in strings)


def _file_map(directory: Path) -> dict[str, Path]:
    return {
        path.relative_to(directory).as_posix(): path
        for path in directory.rglob("*")
        if path.is_file()
    }


def _file_differences(
    before_directory: Path,
    after_directory: Path,
):
    before_files = _file_map(before_directory)
    after_files = _file_map(after_directory)
    for relative_name in sorted(before_files.keys() | after_files.keys()):
        before_path = before_files.get(relative_name)
        after_path = after_files.get(relative_name)
        before = before_path.read_bytes() if before_path is not None else None
        after = after_path.read_bytes() if after_path is not None else None
        if before != after:
            yield relative_name, before, after


def _print_bit_changes(
    relative_name: str,
    before: bytes,
    after: bytes,
    output: TextIO,
) -> int:
    changed = 0
    for offset, (old_byte, new_byte) in enumerate(zip(before, after)):
        if old_byte == new_byte or offset < HEADER_CHECKSUM_BYTES:
            continue
        mask = old_byte ^ new_byte
        bits = ",".join(str(bit) for bit in range(8) if mask & (1 << bit))
        print(
            f"  candidate {relative_name} offset=0x{offset:08X} "
            f"old=0x{old_byte:02X} new=0x{new_byte:02X} "
            f"xor_mask=0x{mask:02X} bits=[{bits}]",
            file=output,
        )
        changed += 1
    return changed


def summarize_directories(
    before_directory: Path,
    after_directory: Path,
    output: TextIO | None = None,
) -> None:
    """Print file-level changes and fixed-offset candidates only."""
    output = output or sys.stdout
    differences = list(_file_differences(before_directory, after_directory))
    print("File overview:", file=output)
    for relative_name, before, after in differences:
        if before is None:
            print(f"  ADDED {relative_name}: {len(after)} bytes", file=output)
        elif after is None:
            print(f"  REMOVED {relative_name}: {len(before)} bytes", file=output)
        else:
            delta = len(after) - len(before)
            print(
                f"  MODIFIED {relative_name}: {len(before)} -> {len(after)} "
                f"bytes (delta {delta:+d}, "
                f"{len(changed_ranges(before, after))} changed range(s))",
                file=output,
            )

    print(
        "Fixed-offset candidates (same-size files; first four header bytes "
        "omitted as probable checksums):",
        file=output,
    )
    candidate_count = 0
    for relative_name, before, after in differences:
        if before is None or after is None or len(before) != len(after):
            continue
        candidate_count += _print_bit_changes(
            relative_name, before, after, output
        )
    if not candidate_count:
        print("  none", file=output)


def _settlement_records(data: bytes) -> list[tuple[int, bytes, int]] | None:
    payload_size = len(data) - SETTLEMENT_RECORD_START
    if payload_size < 0 or payload_size % SETTLEMENT_RECORD_SIZE:
        return None

    records = []
    for offset in range(
        SETTLEMENT_RECORD_START, len(data), SETTLEMENT_RECORD_SIZE
    ):
        record = data[offset : offset + SETTLEMENT_RECORD_SIZE]
        records.append((offset, record[:4], record[4]))
    return records


def _find_snapshot_file(directory: Path, filename: str) -> Path:
    direct_path = directory / filename
    if direct_path.is_file():
        return direct_path

    matches = list(directory.rglob(filename))
    if len(matches) != 1:
        raise ValueError(
            f"expected one {filename!r} in {directory}, found {len(matches)}"
        )
    return matches[0]


def _empty_location_map() -> dict[str, object]:
    return {
        "schema_version": LOCATION_MAP_SCHEMA_VERSION,
        "layout": {
            "file": SETTLEMENT_STATUS_FILENAME,
            "record_start": SETTLEMENT_RECORD_START,
            "record_size": SETTLEMENT_RECORD_SIZE,
            "key_size": SETTLEMENT_KEY_SIZE,
            "key_byteorder": "little",
            "state_index": SETTLEMENT_STATE_INDEX,
        },
        "locations": {},
    }


def _load_location_map(path: Path) -> dict[str, object]:
    if not path.exists():
        return _empty_location_map()

    try:
        mapping = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read location map {path}: {error}") from error
    expected = _empty_location_map()
    if (
        not isinstance(mapping, dict)
        or mapping.get("schema_version") != LOCATION_MAP_SCHEMA_VERSION
        or mapping.get("layout") != expected["layout"]
        or not isinstance(mapping.get("locations"), dict)
    ):
        raise ValueError(
            f"unsupported or malformed location map {path}; "
            "expected schema version 1 and the known settlement record layout"
        )
    return mapping


def learn_location_record(
    before_directory: Path,
    after_directory: Path,
    location_name: str,
    mapping_path: Path,
) -> tuple[int, int, int]:
    """Learn one location's stable record key from a controlled save pair."""
    location_name = location_name.strip()
    if not location_name:
        raise ValueError("location name must not be empty")

    before_path = _find_snapshot_file(
        before_directory, SETTLEMENT_STATUS_FILENAME
    )
    after_path = _find_snapshot_file(
        after_directory, SETTLEMENT_STATUS_FILENAME
    )
    before_records = _settlement_records(before_path.read_bytes())
    after_records = _settlement_records(after_path.read_bytes())
    if before_records is None or after_records is None:
        raise ValueError(
            f"{SETTLEMENT_STATUS_FILENAME} does not match the observed "
            "5-byte record layout from offset 0x62"
        )

    before_by_key = {key: (offset, state) for offset, key, state in before_records}
    after_by_key = {key: (offset, state) for offset, key, state in after_records}
    if len(before_by_key) != len(before_records) or len(after_by_key) != len(after_records):
        raise ValueError("duplicate settlement record keys make this pair ambiguous")

    changed = [
        (key, before_by_key[key], after_by_key[key])
        for key in before_by_key.keys() & after_by_key.keys()
        if before_by_key[key][1] != after_by_key[key][1]
    ]
    added = after_by_key.keys() - before_by_key.keys()
    if len(changed) == 1:
        key, (before_offset, before_state), (after_offset, after_state) = changed[0]
        state_mask = before_state ^ after_state
        match = "state"
    elif not changed and len(added) == 1:
        key = next(iter(added))
        before_offset = None
        before_state = None
        after_offset, after_state = after_by_key[key]
        state_mask = 0
        match = "present"
    else:
        raise ValueError(
            "learning requires exactly one changed existing record or one "
            f"new record; found {len(changed)} changed and {len(added)} added. "
            "Use snapshots taken immediately before and after liberating only "
            "this location."
        )

    mapping = _load_location_map(mapping_path)
    locations = mapping["locations"]
    assert isinstance(locations, dict)
    key_text = f"0x{int.from_bytes(key, 'little'):08X}"
    for existing_name, existing_entry in locations.items():
        if not isinstance(existing_entry, dict):
            raise ValueError(
                f"malformed entry for {existing_name!r} in {mapping_path}"
            )
        if (
            existing_name != location_name
            and existing_entry.get("record_key") == key_text
        ):
            raise ValueError(
                f"record key {key_text} is already assigned to "
                f"{existing_name!r}; refusing an ambiguous mapping"
            )
    existing = locations.get(location_name)
    if isinstance(existing, dict) and existing.get("record_key") != key_text:
        raise ValueError(
            f"{location_name!r} is already mapped to "
            f"{existing.get('record_key')}; refusing to replace it with {key_text}"
        )

    locations[location_name] = {
        "record_key": key_text,
        "match": match,
        "state_mask": f"0x{state_mask:02X}",
        "expected_state": f"0x{after_state:02X}",
        "observed_before": (
            f"0x{before_state:02X}" if before_state is not None else None
        ),
        "observed_after": f"0x{after_state:02X}",
        "sample_state_offset": f"0x{after_offset + SETTLEMENT_STATE_INDEX:08X}",
        "evidence": {
            "before_snapshot": before_directory.name,
            "after_snapshot": after_directory.name,
            "learned_at_utc": datetime.now(timezone.utc).isoformat(),
            "before_record_offset": (
                f"0x{before_offset:08X}" if before_offset is not None else None
            ),
            "after_record_offset": f"0x{after_offset:08X}",
        },
    }

    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=mapping_path.parent,
            prefix=f".{mapping_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(mapping, temporary_file, indent=2, sort_keys=True)
            temporary_file.write("\n")
        os.replace(temporary_path, mapping_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()

    return (
        int.from_bytes(key, "little"),
        state_mask,
        after_offset + SETTLEMENT_STATE_INDEX,
    )


def _resupply_records(data: bytes) -> list[tuple[int, bytes, bytes]] | None:
    record_end = len(data) - RESUPPLY_TRAILING_BYTES
    payload_size = record_end - RESUPPLY_RECORD_START
    if payload_size < 0 or payload_size % RESUPPLY_RECORD_SIZE:
        return None

    return [
        (
            offset,
            data[offset + 3 : offset + RESUPPLY_RECORD_SIZE],
            data[offset : offset + 3],
        )
        for offset in range(
            RESUPPLY_RECORD_START,
            record_end,
            RESUPPLY_RECORD_SIZE,
        )
    ]


def _correlate_settlement_records(
    before: bytes,
    after: bytes,
    output: TextIO,
) -> bool:
    before_records = _settlement_records(before)
    after_records = _settlement_records(after)
    if before_records is None or after_records is None:
        return False

    before_by_key: dict[bytes, tuple[int, int]] = {}
    after_by_key: dict[bytes, tuple[int, int]] = {}
    for offset, key, state_byte in before_records:
        if key in before_by_key:
            return False
        before_by_key[key] = (offset, state_byte)
    for offset, key, state_byte in after_records:
        if key in after_by_key:
            return False
        after_by_key[key] = (offset, state_byte)

    print(
        f"Settlement record correlation for settlement_achievement_status "
        f"(5-byte records from 0x{SETTLEMENT_RECORD_START:02X}):",
        file=output,
    )
    changes = 0
    for key in sorted(before_by_key.keys() & after_by_key.keys()):
        old_offset, old_state = before_by_key[key]
        new_offset, new_state = after_by_key[key]
        mask = old_state ^ new_state
        if not mask:
            continue
        bits = ",".join(str(bit) for bit in range(8) if mask & (1 << bit))
        print(
            f"  candidate settlement_achievement_status "
            f"record_key=0x{int.from_bytes(key, 'little'):08X} "
            f"state_offset=0x{new_offset + 4:08X} "
            f"old=0x{old_state:02X} new=0x{new_state:02X} "
            f"xor_mask=0x{mask:02X} bits=[{bits}] "
            f"(record moved from 0x{old_offset:08X})",
            file=output,
        )
        changes += 1

    for key in sorted(before_by_key.keys() - after_by_key.keys()):
        offset, state_byte = before_by_key[key]
        print(
            f"  record removed: file=settlement_achievement_status "
            f"record_offset=0x{offset:08X} "
            f"record_key=0x{int.from_bytes(key, 'little'):08X} "
            f"state_byte=0x{state_byte:02X}",
            file=output,
        )
        changes += 1
    for key in sorted(after_by_key.keys() - before_by_key.keys()):
        offset, state_byte = after_by_key[key]
        print(
            f"  record added: file=settlement_achievement_status "
            f"record_offset=0x{offset:08X} "
            f"record_key=0x{int.from_bytes(key, 'little'):08X} "
            f"state_byte=0x{state_byte:02X}",
            file=output,
        )
        changes += 1

    if not changes:
        print("  no record-level changes", file=output)
    return True


def _correlate_resupply_records(
    before: bytes,
    after: bytes,
    output: TextIO,
) -> bool:
    before_records = _resupply_records(before)
    after_records = _resupply_records(after)
    if before_records is None or after_records is None:
        return False

    before_by_key = {
        key: (offset, state) for offset, key, state in before_records
    }
    after_by_key = {
        key: (offset, state) for offset, key, state in after_records
    }
    if len(before_by_key) != len(before_records) or len(after_by_key) != len(after_records):
        return False

    print(
        "Resupply-point record correlation for unlocked_resupply_points "
        f"({RESUPPLY_RECORD_SIZE}-byte records from "
        f"0x{RESUPPLY_RECORD_START:02X}; 3-byte state + 5-byte key):",
        file=output,
    )
    changes = 0
    for key in sorted(before_by_key.keys() & after_by_key.keys()):
        old_offset, old_state = before_by_key[key]
        new_offset, new_state = after_by_key[key]
        for byte_index, (old_byte, new_byte) in enumerate(
            zip(old_state, new_state)
        ):
            mask = old_byte ^ new_byte
            if not mask:
                continue
            bits = ",".join(
                str(bit) for bit in range(8) if mask & (1 << bit)
            )
            print(
                f"  candidate unlocked_resupply_points "
                f"record_key=0x{int.from_bytes(key, 'little'):010X} "
                f"state_offset=0x{new_offset + byte_index:08X} "
                f"old=0x{old_byte:02X} new=0x{new_byte:02X} "
                f"xor_mask=0x{mask:02X} bits=[{bits}] "
                f"(record moved from 0x{old_offset:08X})",
                file=output,
            )
            changes += 1

    for key in sorted(before_by_key.keys() - after_by_key.keys()):
        offset, state = before_by_key[key]
        print(
            f"  record removed: file=unlocked_resupply_points "
            f"record_offset=0x{offset:08X} "
            f"record_key=0x{int.from_bytes(key, 'little'):010X} "
            f"state={state.hex(' ').upper()}",
            file=output,
        )
        changes += 1
    for key in sorted(after_by_key.keys() - before_by_key.keys()):
        offset, state = after_by_key[key]
        print(
            f"  record added: file=unlocked_resupply_points "
            f"record_offset=0x{offset:08X} "
            f"record_key=0x{int.from_bytes(key, 'little'):010X} "
            f"state={state.hex(' ').upper()}",
            file=output,
        )
        changes += 1

    if not changes:
        print("  no record-level changes", file=output)
    return True


def correlate_directories(
    before_directory: Path,
    after_directory: Path,
    output: TextIO | None = None,
) -> None:
    """Report stable byte masks and correlate settlement records across saves."""
    output = output or sys.stdout
    print("Single-change correlation candidates:", file=output)
    for relative_name, before, after in _file_differences(
        before_directory, after_directory
    ):
        if before is None or after is None:
            print(f"  file added/removed: {relative_name}", file=output)
            continue
        if len(before) != len(after):
            file_name = Path(relative_name).name
            if file_name == "settlement_achievement_status":
                if _correlate_settlement_records(before, after, output):
                    continue
            elif file_name == "unlocked_resupply_points":
                if _correlate_resupply_records(before, after, output):
                    continue
            print(
                f"  skipped shifted/variable-length file: {relative_name} "
                f"({len(before)} -> {len(after)} bytes)",
                file=output,
            )
            continue
        _print_bit_changes(relative_name, before, after, output)


def _describe_change(
    relative_name: str,
    before: bytes,
    after: bytes,
    output: TextIO,
) -> None:
    size_delta = len(after) - len(before)
    print(f"MODIFIED {relative_name}", file=output)
    print(
        f"  size: {len(before)} -> {len(after)} bytes "
        f"(delta {size_delta:+d})",
        file=output,
    )
    print(f"  SHA-256 before: {_sha256(before)}", file=output)
    print(f"  SHA-256 after:  {_sha256(after)}", file=output)

    for start, end in changed_ranges(before, after):
        print(
            f"  changed offsets [0x{start:08X}, 0x{end:08X}) "
            f"({end - start} byte(s))",
            file=output,
        )
        print(f"    old: {_format_bytes(before, start, end)}", file=output)
        print(f"    new: {_format_bytes(after, start, end)}", file=output)

        for width, alignment, label in (
            (4, 4, "u32/hash"),
            (8, 8, "u64/hash"),
        ):
            for offset in _changed_word_offsets(start, end, alignment):
                old_word = _format_word(before, offset, width)
                new_word = _format_word(after, offset, width)
                print(
                    f"    {label}@0x{offset:08X}: "
                    f"{old_word} -> {new_word}",
                    file=output,
                )

        context_start = max(0, start - 32)
        context_end = min(max(len(before), len(after)), end + 32)
        old_strings = _printable_strings(
            before, context_start, min(context_end, len(before))
        )
        new_strings = _printable_strings(
            after, context_start, min(context_end, len(after))
        )
        print(
            f"    ASCII before: {_format_strings(old_strings)}",
            file=output,
        )
        print(
            f"    ASCII after:  {_format_strings(new_strings)}",
            file=output,
        )


def compare_directories(
    before_directory: Path,
    after_directory: Path,
    output: TextIO | None = None,
) -> tuple[int, int, int]:
    """Print file and byte differences; return modified, added, removed counts."""
    output = output or sys.stdout
    modified = added = removed = 0
    for relative_name, before_data, after_data in _file_differences(
        before_directory, after_directory
    ):
        if before_data is None:
            data = after_data
            print(
                f"ADDED {relative_name} ({len(data)} bytes, "
                f"SHA-256 {_sha256(data)})",
                file=output,
            )
            added += 1
        elif after_data is None:
            data = before_data
            print(
                f"REMOVED {relative_name} ({len(data)} bytes, "
                f"SHA-256 {_sha256(data)})",
                file=output,
            )
            removed += 1
        else:
            _describe_change(
                relative_name, before_data, after_data, output
            )
            modified += 1

    print(
        f"Summary: {modified} modified, {added} added, {removed} removed.",
        file=output,
    )
    return modified, added, removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare two Just Cause 3 save snapshots."
    )
    parser.add_argument("--before", type=Path)
    parser.add_argument("--after", type=Path)
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Show file overview and same-size fixed-offset candidates only.",
    )
    parser.add_argument(
        "--correlate",
        nargs=2,
        metavar=("BEFORE_SNAPSHOT", "AFTER_SNAPSHOT"),
        help="Find byte masks and correlate settlement records between snapshots.",
    )
    parser.add_argument(
        "--learn-location",
        metavar="LOCATION_NAME",
        help=(
            "Learn one changed or newly added settlement record for this AP "
            "location and add it to a reusable JSON mapping."
        ),
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        help="JSON mapping file to create or update with --learn-location.",
    )
    args = parser.parse_args(argv)

    if args.correlate:
        if (
            args.before is not None
            or args.after is not None
            or args.summary
            or args.learn_location
            or args.mapping
        ):
            parser.error(
                "--correlate cannot be combined with --before/--after/--summary/"
                "--learn-location/--mapping"
            )
        before_directory, after_directory = map(Path, args.correlate)
    else:
        if args.before is None or args.after is None:
            parser.error(
                "provide both --before and --after, or use --correlate"
            )
        if args.learn_location and args.summary:
            parser.error("--learn-location cannot be combined with --summary")
        if args.mapping and not args.learn_location:
            parser.error("--mapping requires --learn-location")
        if args.learn_location and not args.mapping:
            parser.error("--learn-location requires --mapping")
        before_directory, after_directory = args.before, args.after

    if not before_directory.is_dir():
        parser.error(f"before snapshot is not a directory: {before_directory}")
    if not after_directory.is_dir():
        parser.error(f"after snapshot is not a directory: {after_directory}")

    try:
        if args.correlate:
            correlate_directories(before_directory, after_directory)
        elif args.learn_location:
            record_key, state_mask, state_offset = learn_location_record(
                before_directory,
                after_directory,
                args.learn_location,
                args.mapping,
            )
            print(f"Learned {args.learn_location!r}:")
            print(f"  record_key=0x{record_key:08X}")
            print(f"  state_mask=0x{state_mask:02X}")
            print(f"  sample_state_offset=0x{state_offset:08X}")
            learned_entry = _load_location_map(args.mapping)["locations"][
                args.learn_location.strip()
            ]
            print(f"  match={learned_entry['match']}")
            print(f"  mapping={args.mapping}")
        elif args.summary:
            summarize_directories(before_directory, after_directory)
        else:
            compare_directories(before_directory, after_directory)
    except (OSError, ValueError) as error:
        parser.exit(2, f"error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
