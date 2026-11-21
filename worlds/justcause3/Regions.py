from typing import TYPE_CHECKING

from BaseClasses import Region

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
        medici.connect(region, f"Travel to {region_name}")

    multiworld.regions += [menu, medici, *regions_by_name.values(), end]
    create_locations(world, regions_by_name, medici, end)
