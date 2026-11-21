from typing import Optional

from BaseClasses import ItemClassification
from worlds.AutoWorld import World

from .Items import (
    JustCause3Item,
    build_item_name_to_id_table,
    filler_item_name,
    item_table,
)
from .Locations import (
    candidate_location_data,
    build_location_name_to_id_table,
)
from .Options import JustCause3Options
from .Regions import create_regions
from .Rules import set_rules


class JustCause3World(World):
    game = "Just Cause 3"
    options_dataclass = JustCause3Options
    options: JustCause3Options

    item_name_to_id = build_item_name_to_id_table()
    location_name_to_id = build_location_name_to_id_table()

    def create_regions(self) -> None:
        create_regions(self)

    def create_items(self) -> None:
        item_pool = [
            self.create_item(name)
            for name in item_table
            if name not in {filler_item_name, "Just Cause 3 Victory"}
        ]
        filler_count = len(candidate_location_data) - len(item_pool)
        if filler_count < 0:
            raise ValueError(
                "The Just Cause 3 item candidates outnumber its location checks."
            )
        item_pool.extend(
            self.create_item(filler_item_name) for _ in range(filler_count)
        )
        self.multiworld.itempool.extend(item_pool)

    def set_rules(self) -> None:
        set_rules(self)

    def create_item(
        self,
        name: str,
        classification_override: Optional[ItemClassification] = None,
    ) -> JustCause3Item:
        data = item_table[name]
        classification = (
            classification_override
            if classification_override is not None
            else data.classification
        )
        code = data.code
        return JustCause3Item(name, classification, code, self.player)

    def get_filler_item_name(self) -> str:
        return filler_item_name
