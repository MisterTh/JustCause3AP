import contextlib
import hashlib
import io
import json

from scripts.diff_jc3_saves import (
    changed_ranges,
    compare_directories,
    correlate_directories,
    learn_location_record,
    main,
    summarize_directories,
)


def test_changed_ranges_includes_adjacent_changes_and_file_growth():
    assert changed_ranges(b"\x00\x01\x02\x03", b"\x00\xFF\xFE\x03\x04") == [
        (1, 3),
        (4, 5),
    ]


def test_compare_directories_reports_hashes_words_strings_and_file_changes(tmp_path):
    before = tmp_path / "before"
    after = tmp_path / "after"
    before.mkdir()
    after.mkdir()
    before_data = b"FLAG=0;TAIL"
    after_data = b"FLAG=1;TAIL+"
    (before / "profile.sav").write_bytes(before_data)
    (after / "profile.sav").write_bytes(after_data)
    (before / "removed.sav").write_bytes(b"old")
    (after / "added.sav").write_bytes(b"new")

    output = io.StringIO()
    counts = compare_directories(before, after, output)
    report = output.getvalue()

    assert counts == (1, 1, 1)
    assert "size: 11 -> 12 bytes (delta +1)" in report
    assert hashlib.sha256(before_data).hexdigest() in report
    assert hashlib.sha256(after_data).hexdigest() in report
    assert "u32/hash@0x00000004:" in report
    assert "u64/hash@0x00000000:" in report
    assert "'FLAG=0;TAIL'" in report
    assert "'FLAG=1;TAIL+'" in report
    assert "ADDED added.sav" in report
    assert "REMOVED removed.sav" in report


def test_summary_omits_header_bytes_and_variable_length_candidates(tmp_path):
    before = tmp_path / "before"
    after = tmp_path / "after"
    before.mkdir()
    after.mkdir()
    (before / "fixed.sav").write_bytes(b"\x00" * 8)
    (after / "fixed.sav").write_bytes(b"\x01\x00\x00\x00\x00\x02\x00\x00")
    (before / "array.sav").write_bytes(b"abc")
    (after / "array.sav").write_bytes(b"abcd")

    output = io.StringIO()
    summarize_directories(before, after, output)
    report = output.getvalue()
    overview, candidates = report.split("Fixed-offset candidates", maxsplit=1)

    assert "MODIFIED fixed.sav" in overview
    assert "MODIFIED array.sav" in overview
    assert "offset=0x00000005" in candidates
    assert "xor_mask=0x02" in candidates
    assert "offset=0x00000000" not in candidates
    assert "array.sav" not in candidates


def test_correlate_finds_settlement_record_flag_and_removed_record(tmp_path):
    before = tmp_path / "before"
    after = tmp_path / "after"
    before.mkdir()
    after.mkdir()
    header = bytes(0x62)
    records_before = bytes.fromhex("01 02 03 04 01 05 06 07 08 00")
    records_after = bytes.fromhex("01 02 03 04 03")
    (before / "settlement_achievement_status").write_bytes(
        header + records_before
    )
    (after / "settlement_achievement_status").write_bytes(
        header + records_after
    )

    output = io.StringIO()
    correlate_directories(before, after, output)
    report = output.getvalue()

    assert "record_key=0x04030201" in report
    assert "state_offset=0x00000066" in report
    assert "xor_mask=0x02" in report
    assert "record_offset=0x00000067" in report
    assert "record removed" in report


def test_correlate_tracks_unlocked_resupply_record_changes(tmp_path):
    before = tmp_path / "before"
    after = tmp_path / "after"
    before.mkdir()
    after.mkdir()
    header = bytes(0x68)
    record_one_before = bytes.fromhex("01 02 03 04 05 06 07 08")
    record_one_after = bytes.fromhex("03 02 03 04 05 06 07 08")
    record_two = bytes.fromhex("11 12 13 14 15 16 17 18")
    trailer = b"\x00"
    (before / "unlocked_resupply_points").write_bytes(
        header + record_one_before + record_two + trailer
    )
    (after / "unlocked_resupply_points").write_bytes(
        header + record_one_after + trailer
    )

    output = io.StringIO()
    correlate_directories(before, after, output)
    report = output.getvalue()

    assert "record_key=0x0807060504" in report
    assert "state_offset=0x00000068" in report
    assert "xor_mask=0x02" in report
    assert "record removed" in report
    assert "record_offset=0x00000070" in report
    assert "record_key=0x1817161514" in report


def test_learn_location_writes_reusable_key_and_state_mask(tmp_path):
    before = tmp_path / "before"
    after = tmp_path / "after"
    before.mkdir()
    after.mkdir()
    record_key = bytes.fromhex("57 9E FE 3D")
    header = bytes(0x62)
    (before / "settlement_achievement_status").write_bytes(
        header + record_key + b"\x00"
    )
    (after / "settlement_achievement_status").write_bytes(
        header + record_key + b"\x03"
    )
    mapping_path = tmp_path / "settlement-map.json"

    learned_key, mask, offset = learn_location_record(
        before, after, "Military Base: Griphon", mapping_path
    )

    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert learned_key == 0x3DFE9E57
    assert mask == 0x03
    assert offset == 0x66
    assert mapping["schema_version"] == 1
    entry = mapping["locations"]["Military Base: Griphon"]
    assert entry["record_key"] == "0x3DFE9E57"
    assert entry["state_mask"] == "0x03"
    assert entry["expected_state"] == "0x03"
    assert entry["observed_before"] == "0x00"
    assert entry["observed_after"] == "0x03"
    assert entry["sample_state_offset"] == "0x00000066"


def test_learn_location_rejects_multiple_changed_records(tmp_path):
    before = tmp_path / "before"
    after = tmp_path / "after"
    before.mkdir()
    after.mkdir()
    header = bytes(0x62)
    (before / "settlement_achievement_status").write_bytes(
        header + bytes.fromhex("01 02 03 04 00 05 06 07 08 00")
    )
    (after / "settlement_achievement_status").write_bytes(
        header + bytes.fromhex("01 02 03 04 01 05 06 07 08 03")
    )
    mapping_path = tmp_path / "settlement-map.json"

    try:
        learn_location_record(
            before, after, "Settlement: Example", mapping_path
        )
    except ValueError as error:
        assert "found 2" in str(error)
    else:
        raise AssertionError("ambiguous multi-record transitions must be rejected")
    assert not mapping_path.exists()


def test_learn_location_records_new_key_as_presence_match(tmp_path):
    before = tmp_path / "before"
    after = tmp_path / "after"
    before.mkdir()
    after.mkdir()
    (before / "settlement_achievement_status").write_bytes(bytes(0x62))
    (after / "settlement_achievement_status").write_bytes(
        bytes(0x62) + bytes.fromhex("57 9E FE 3D 00")
    )
    mapping_path = tmp_path / "settlement-map.json"

    learn_location_record(
        before, after, "Military Base: Griphon", mapping_path
    )

    entry = json.loads(mapping_path.read_text(encoding="utf-8"))["locations"][
        "Military Base: Griphon"
    ]
    assert entry["record_key"] == "0x3DFE9E57"
    assert entry["match"] == "present"
    assert entry["state_mask"] == "0x00"


def test_learn_location_accumulates_entries(tmp_path):
    mapping_path = tmp_path / "settlement-map.json"
    header = bytes(0x62)

    for label, location_name, key_bytes in (
        ("agro", "Settlement: Agro", bytes.fromhex("01 02 03 04")),
        ("nacre", "Settlement: Nacre", bytes.fromhex("11 12 13 14")),
    ):
        before = tmp_path / f"{label}-before"
        after = tmp_path / f"{label}-after"
        before.mkdir()
        after.mkdir()
        (before / "settlement_achievement_status").write_bytes(
            header + key_bytes + b"\x00"
        )
        (after / "settlement_achievement_status").write_bytes(
            header + key_bytes + b"\x03"
        )
        learn_location_record(before, after, location_name, mapping_path)

    locations = json.loads(mapping_path.read_text(encoding="utf-8"))["locations"]
    assert set(locations) == {"Settlement: Agro", "Settlement: Nacre"}


def test_learn_location_cli_writes_and_reports_mapping(tmp_path):
    before = tmp_path / "before"
    after = tmp_path / "after"
    before.mkdir()
    after.mkdir()
    header = bytes(0x62)
    record_key = bytes.fromhex("57 9E FE 3D")
    (before / "settlement_achievement_status").write_bytes(
        header + record_key + b"\x00"
    )
    (after / "settlement_achievement_status").write_bytes(
        header + record_key + b"\x03"
    )
    mapping_path = tmp_path / "settlement-map.json"
    output = io.StringIO()

    with contextlib.redirect_stdout(output):
        result = main([
            "--before", str(before),
            "--after", str(after),
            "--learn-location", "Military Base: Griphon",
            "--mapping", str(mapping_path),
        ])

    assert result == 0
    assert "record_key=0x3DFE9E57" in output.getvalue()
    assert "match=state" in output.getvalue()
    assert mapping_path.is_file()
