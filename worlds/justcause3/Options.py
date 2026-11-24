from dataclasses import dataclass

from Options import Choice, PerGameCommonOptions, Range, Toggle


class Goal(Choice):
    """Choose the configured Archipelago victory condition."""

    display_name = "Goal"
    option_story_missions = 0
    option_low_liberation = 1
    option_full_liberation = 2
    default = option_low_liberation


class LiberationGoal(Range):
    """Percentage of campaign locations required for low-liberation goals."""

    display_name = "Liberation Goal"
    range_start = 1
    range_end = 100
    default = 25


class RegionProgression(Toggle):
    """Require traversal progression items for Medici's island regions."""

    display_name = "Region Progression"
    default = True


class FOWHazardProgression(Toggle):
    """Require the FOW hazard disabler for hazardous region locations."""

    display_name = "FOW Hazard Progression"
    default = True


@dataclass
class JustCause3Options(PerGameCommonOptions):
    goal: Goal
    liberation_goal: LiberationGoal
    region_progression: RegionProgression
    fow_hazard_progression: FOWHazardProgression
