from .bases import JustCause3TestBase


class TestJustCause3Default(JustCause3TestBase):
    options = {
        "goal": "low_liberation",
        "liberation_goal": 25,
        "region_progression": True,
        "fow_hazard_progression": True,
    }


class TestJustCause3LowLiberation(JustCause3TestBase):
    options = {
        "goal": "low_liberation",
        "liberation_goal": 10,
        "region_progression": True,
        "fow_hazard_progression": True,
    }


class TestJustCause3FullLiberation(JustCause3TestBase):
    options = {
        "goal": "full_liberation",
        "liberation_goal": 100,
        "region_progression": True,
        "fow_hazard_progression": True,
    }


class TestJustCause3RegionProgression(JustCause3TestBase):
    options = {
        "goal": "low_liberation",
        "liberation_goal": 25,
        "region_progression": True,
        "fow_hazard_progression": True,
    }

    def test_insula_fonte_requires_wingsuit(self) -> None:
        self.assertAccessDependency(
            ["Rebel Shrine: 40 37.14 E 5 39.54"],
            [["Wingsuit Progression"]],
        )

    def test_insula_dracon_requires_tether(self) -> None:
        self.assertAccessDependency(
            ["Rebel Shrine: 40 40.95 E 5 35.36"],
            [["Tether Progression"]],
        )


class TestJustCause3FOWProgression(JustCause3TestBase):
    options = {
        "goal": "low_liberation",
        "liberation_goal": 25,
        "region_progression": True,
        "fow_hazard_progression": True,
    }

    def test_hazard_region_requires_disabler(self) -> None:
        self.assertAccessDependency(
            ["Rebel Shrine: 40 45.91 E 5 44.17"],
            [["FOW Hazard Disabler"]],
        )
