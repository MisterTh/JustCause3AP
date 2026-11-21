from __future__ import annotations

from argparse import Namespace

import pytest
from BaseClasses import CollectionState, MultiWorld
from Fill import distribute_items_restrictive
from worlds import AutoWorldRegister
from worlds.AutoWorld import call_all


GAME_NAME = "Just Cause 3"
EXPECTED_CHECK_COUNT = 304
GENERATION_STEPS = (
    "generate_early",
    "create_regions",
    "create_items",
    "set_rules",
    "connect_entrances",
    "generate_basic",
    "pre_fill",
)


def generate_solo_world(world_type: type, seed: int) -> MultiWorld:
    multiworld = MultiWorld(1)
    multiworld.game = {1: world_type.game}
    multiworld.player_name = {1: "JC3 test"}
    multiworld.set_seed(seed)

    args = Namespace()
    for key, option_type in world_type.options_dataclass.type_hints.items():
        values = getattr(args, key, {})
        values[1] = option_type.from_any(option_type.default)
        setattr(args, key, values)
    multiworld.set_options(args)
    multiworld.state = CollectionState(multiworld)
    for step in GENERATION_STEPS:
        call_all(multiworld, step)
    return multiworld


@pytest.mark.parametrize("seed", [13, 37, 91])
def test_solo_world_generates_and_all_locations_are_reachable(seed: int) -> None:
    world_type = AutoWorldRegister.world_types[GAME_NAME]
    multiworld = generate_solo_world(world_type, seed)
    world = multiworld.worlds[1]

    checks = [
        location
        for location in multiworld.get_locations()
        if location.address is not None
    ]
    assert len(checks) == EXPECTED_CHECK_COUNT
    assert len(world.location_name_to_id) == EXPECTED_CHECK_COUNT
    assert len(world.item_name_to_id) == 88
    assert len(multiworld.itempool) == len(multiworld.get_unfilled_locations())

    state = CollectionState(multiworld)
    unreachable = [location.name for location in checks if not location.can_reach(state)]
    assert not unreachable, f"Locations unreachable without items: {unreachable}"

    distribute_items_restrictive(multiworld)
    call_all(multiworld, "post_fill")
    call_all(multiworld, "finalize_multiworld")
    assert len(multiworld.get_unfilled_locations()) == 0

    state = CollectionState(multiworld)
    remaining = list(multiworld.get_filled_locations())
    while remaining:
        sphere = [location for location in remaining if location.can_reach(state)]
        assert sphere, f"Generation created unreachable locations: {remaining}"
        for location in sphere:
            remaining.remove(location)
            if location.item is not None:
                state.collect(location.item, True, location)

    assert multiworld.can_beat_game(state)


def test_world_has_no_unimplemented_option_fields() -> None:
    world_type = AutoWorldRegister.world_types[GAME_NAME]
    assert not world_type.options_dataclass.__annotations__, (
        "If JC3 options are added, add named generation presets and logic tests "
        "for every option combination here."
    )
