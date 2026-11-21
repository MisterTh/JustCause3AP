from typing import TYPE_CHECKING, Dict

from BaseClasses import Location, Region

from .archi_defs import loc_data_table as candidate_location_data
from .archi_defs import loc_name_to_id as candidate_location_name_to_id

if TYPE_CHECKING:
    from . import JustCause3World


LOCATION_ID_BASE = 0x4A430100


class JustCause3Location(Location):
    game = "Just Cause 3"


location_name_to_id: Dict[str, int] = {
    name: LOCATION_ID_BASE + code
    for name, code in candidate_location_name_to_id.items()
}
location_table: Dict[str, int] = dict(location_name_to_id)


def build_location_name_to_id_table() -> Dict[str, int]:
    return dict(location_table)


def create_locations(
    world: "JustCause3World",
    regions_by_name: Dict[str, Region],
    fallback_region: Region,
    end_region: Region,
) -> None:
    for name, data in candidate_location_data.items():
        region = regions_by_name.get(data["region"], fallback_region)
        region.locations.append(
            JustCause3Location(
                world.player,
                name,
                location_table[name],
                region,
            )
        )

    victory = Location(world.player, "Victory", None, end_region)
    victory.place_locked_item(world.create_item("Just Cause 3 Victory"))
    end_region.locations.append(victory)
