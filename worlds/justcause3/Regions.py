from typing import TYPE_CHECKING

from BaseClasses import Region
from worlds.generic.Rules import set_rule

from .Locations import candidate_location_data, create_locations

if TYPE_CHECKING:
    from . import JustCause3World


def create_regions(world: "JustCause3World") -> None:
    multiworld = world.multiworld
    player = world.player

    menu = Region("Menu", player, multiworld)
    medici = Region("Medici", player, multiworld)
    end = Region("End", player, multiworld)

    menu.connect(medici, "Start")
    medici.connect(end, "Final Mission")

    regions_by_name: dict[str, Region] = {}
    for region_name in sorted(
        {data["region"] for data in candidate_location_data.values()}
        - {"Medici"}
    ):
        region = Region(region_name, player, multiworld)
        regions_by_name[region_name] = region
        rule = None
        if world.options.region_progression:
            if region_name == "Insula Fonte":
                rule = lambda state: state.has("Wingsuit Progression", player)
            elif region_name == "Insula Dracon":
                rule = lambda state: state.has("Tether Progression", player)
            elif region_name == "Insula Striate":
                rule = (
                    lambda state: state.has("FOW Hazard Disabler", player)
                    if world.options.fow_hazard_progression
                    else None
                )
            elif region_name == "Scolio":
                rule = lambda state: (
                    state.has("Wingsuit Progression", player)
                    and state.has("Tether Progression", player)
                    and (
                        not world.options.fow_hazard_progression
                        or state.has("FOW Hazard Disabler", player)
                    )
                )
        entrance = medici.connect(
            region,
            f"Travel to {region_name}",
        )
        if rule is not None:
            set_rule(entrance, rule)

    multiworld.regions += [menu, medici, *regions_by_name.values(), end]
    create_locations(world, regions_by_name, medici, end)
