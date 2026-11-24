"""A truck wheel's bump is measured over its axle's travel, as the game's own vibration measures it."""

from __future__ import annotations

import unittest

from effects import DT, TRAVEL_VANILLA, Gains, Synth, effective_gains
from tests.test_effects import frame


def knock(wheel: int, speed: float = 0.6, **gains) -> float:
    """How strong the knock is that one truck wheel stepping at speed m/s for a frame makes; 0 for none."""
    s = Synth(effective_gains(Gains(**gains)))
    depths = [0.02] * 6
    s.render(frame(deflection=list(depths)))
    depths[wheel] += speed * DT
    s.render(frame(deflection=list(depths)))
    depths[wheel] -= 0.0001  # it stays there: a step, not a spike
    s.render(frame(deflection=list(depths)))
    return max((burst.level for effect, _, burst in s.bursts if effect == "bumps"), default=0.0)


class TravelTest(unittest.TestCase):
    def test_a_vanilla_truck_knocks_as_it_did_in_plain_metres_a_second(self) -> None:
        self.assertEqual((Gains().travel_front, Gains().travel_rear), TRAVEL_VANILLA)
        self.assertGreater(knock(2), 0.0)
        # each axle is set against its own vanilla travel: the same step, the same knock
        self.assertEqual(knock(0), knock(2))
        self.assertEqual(knock(2), knock(4))

    def test_the_same_step_counts_by_how_much_room_the_axle_has(self) -> None:
        # twice the travel is half the movement: 0.6 m/s there is 0.3 m/s on a vanilla axle
        self.assertAlmostEqual(knock(0, speed=0.6, travel_front=0.2), knock(0, speed=0.3))
        self.assertAlmostEqual(knock(2, speed=0.6, travel_rear=0.176), knock(2, speed=0.3))

    def test_a_physics_mods_longer_travel_softens_that_axle_only(self) -> None:
        vanilla, modded = knock(0), knock(0, travel_front=0.18)  # the developer's chassis mod
        self.assertGreater(modded, 0.0)
        self.assertLess(modded, 0.5 * vanilla)
        self.assertEqual(knock(2, travel_front=0.18), knock(2))
        # long enough, and the step is under the threshold
        self.assertEqual(knock(0, travel_front=0.3), 0.0)

    def test_a_shorter_travel_makes_more_of_it(self) -> None:
        self.assertGreater(knock(2, speed=0.3, travel_rear=0.05), knock(2, speed=0.3))

    def test_a_travel_of_nothing_is_read_as_the_vanilla_one(self) -> None:
        self.assertEqual(knock(2, travel_rear=0.0), knock(2))


if __name__ == "__main__":
    unittest.main()
