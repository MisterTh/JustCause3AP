from dataclasses import dataclass
from typing import Dict, Optional

from BaseClasses import Item, ItemClassification

from .archi_defs import item_data_table as candidate_item_data
from .archi_defs import item_name_to_id as candidate_item_name_to_id


ITEM_ID_BASE = 0x4A430000


class JustCause3Item(Item):
    game = "Just Cause 3"


@dataclass(frozen=True)
class ItemData:
    code: Optional[int]
    classification: ItemClassification


item_name_to_id: Dict[str, int] = {
    name: ITEM_ID_BASE + code
    for name, code in candidate_item_name_to_id.items()
}

item_table: Dict[str, ItemData] = {
    name: ItemData(
        item_name_to_id[name],
        ItemClassification.useful,
    )
    for name in candidate_item_data
}

filler_item_name = "Rebel Supply Drop"
filler_item_code = max(candidate_item_name_to_id.values(), default=0) + 1
item_name_to_id[filler_item_name] = ITEM_ID_BASE + filler_item_code
item_table[filler_item_name] = ItemData(
    ITEM_ID_BASE + filler_item_code,
    ItemClassification.filler,
)

progression_items = {
    "Wingsuit Progression": ("wingsuit", 89),
    "Tether Progression": ("tether", 90),
    "FOW Hazard Disabler": ("hazard_disabler", 91),
}
for _name, (_category, _code) in progression_items.items():
    item_name_to_id[_name] = ITEM_ID_BASE + _code
    item_table[_name] = ItemData(
        ITEM_ID_BASE + _code,
        ItemClassification.progression,
    )

item_table["Just Cause 3 Victory"] = ItemData(
    None,
    ItemClassification.progression,
)


def build_item_name_to_id_table() -> Dict[str, int]:
    return dict(item_name_to_id)
