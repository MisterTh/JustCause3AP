from .archi_defs import loc_data_table
from worlds.generic.Rules import set_rule


def set_rules(world) -> None:
    player = world.player
    goal = int(world.options.goal)

    if goal == 0:
        required_locations = [
            name
            for name, data in loc_data_table.items()
            if data["category"] == "story_mission"
        ]
    elif goal == 2:
        required_locations = list(loc_data_table)
    else:
        required_count = max(
            1,
            (len(loc_data_table) * int(world.options.liberation_goal) + 99) // 100,
        )
        required_locations = list(loc_data_table)[:required_count]

    victory_location = world.get_location("Victory")
    set_rule(victory_location, lambda state: (
        all(
            state.can_reach_location(name, player)
            for name in required_locations
        )
    ))

    world.multiworld.completion_condition[player] = lambda state: (
        state.can_reach_location("Victory", player)
        and state.has("Just Cause 3 Victory", player)
    )
