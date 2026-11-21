def set_rules(world) -> None:
    world.multiworld.completion_condition[world.player] = (
        lambda state: state.has("Just Cause 3 Victory", world.player)
    )
