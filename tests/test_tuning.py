import pathlib
import sys
import unittest


SERVER_DIR = pathlib.Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER_DIR))

from tuning import (  # noqa: E402
    build_tuning_adjust_commands,
    effective_steps,
    step_sequence_problems,
)


class TuningCommandTests(unittest.TestCase):
    def test_adjustment_is_saved_and_immediately_previewed(self):
        self.assertEqual(
            build_tuning_adjust_commands(3, 51, 2618, 4096),
            ("m03w51:2618", "m03g2618"),
        )

    def test_preview_uses_absolute_step_position_not_character_index(self):
        _, preview_command = build_tuning_adjust_commands(3, 51, 2618, 4096)
        self.assertEqual(preview_command, "m03g2618")
        self.assertNotEqual(preview_command, "m03g51")

    def test_allows_last_valid_step_for_calibration(self):
        self.assertEqual(
            build_tuning_adjust_commands(3, 51, 4095, 4096),
            ("m03w51:4095", "m03g4095"),
        )

    def test_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            build_tuning_adjust_commands(-1, 10, 500, 4096)
        with self.assertRaises(ValueError):
            build_tuning_adjust_commands(1, 64, 500, 4096)
        with self.assertRaises(ValueError):
            build_tuning_adjust_commands(1, 10, 500, 0)
        with self.assertRaises(ValueError):
            build_tuning_adjust_commands(1, 10, -1, 4096)
        with self.assertRaises(ValueError):
            build_tuning_adjust_commands(1, 10, 4096, 4096)


if __name__ == "__main__":
    unittest.main()


class StepSequenceTests(unittest.TestCase):
    """A reel turns one way.

    Flap i+1 is one flap further round than flap i, so a module's positions
    have to climb and come back to the start after exactly one revolution.
    Nothing mechanical moves a flap past its neighbour, which makes this the
    one check that can tell a correction from a misread without a camera.

    It is about order, not distance. How far apart two flaps sit says only
    that the spacing is uneven, and uneven spacing is what a correction in
    progress looks like.
    """

    CAL, FLAPS = 4096, 64

    def steps(self, **tuned):
        return effective_steps({str(k): v for k, v in tuned.items()}, self.CAL, self.FLAPS)

    def problems(self, steps):
        return [p["index"] for p in step_sequence_problems(steps, self.CAL)]

    def test_an_untuned_reel_is_already_in_order(self):
        self.assertEqual(self.problems(self.steps()), [])

    def test_the_wrap_from_the_last_flap_to_the_first_counts(self):
        # The seam is read like any other step from one flap to the next;
        # skipping it would let the worst error of all through unnoticed.
        steps = self.steps()
        self.assertEqual(len(steps), self.FLAPS)
        steps[self.FLAPS - 1] = steps[0] + 10          # last flap past the first
        self.assertIn(self.FLAPS - 1, self.problems(steps))

    def test_compensating_for_real_slop_is_allowed(self):
        for nudge in (-20, -5, 5, 20):
            with self.subTest(nudge=nudge):
                self.assertEqual(self.problems(self.steps(**{"10": 640 + nudge})), [])

    def test_a_flap_out_by_a_whole_position_is_caught(self):
        # The error the camera tuner writes when it misidentifies a flap.
        for nudge in (-64, 64, 128):
            with self.subTest(nudge=nudge):
                self.assertNotEqual(self.problems(self.steps(**{"10": 640 + nudge})), [])

    def test_repeated_nudges_at_one_flap_are_allowed(self):
        # A correction is made by nudging, not by jumping, so one flap may
        # legitimately drift a couple of nudges away from nominal before it
        # lands on the right character.
        for nudge in (-50, -25, 25, 50):
            with self.subTest(nudge=nudge):
                self.assertEqual(self.problems(self.steps(**{"10": 640 + nudge})), [])

    def test_nudging_past_a_neighbour_is_caught(self):
        # Flap 9 sits at 576 and flap 11 at 704, so these land the other side
        # of one of them.
        for nudge in (-75, 75):
            with self.subTest(nudge=nudge):
                self.assertNotEqual(self.problems(self.steps(**{"10": 640 + nudge})), [])

    def test_a_correction_bigger_than_a_nudge_is_allowed_while_it_stays_in_order(self):
        # Positions are corrected in ascending order, so at the moment a flap
        # is written its lower neighbours are already corrected and its higher
        # ones are not. Judging that gap against nominal spacing refused every
        # module needing more than about fifty steps, at every flap, while the
        # sequence was in order the whole time.
        for correction in (25, 50, 55, 63):
            with self.subTest(correction=correction):
                part_way = {str(i): (i * self.CAL) // self.FLAPS - correction
                            for i in range(1, 11)}
                self.assertEqual(self.problems(self.steps(**part_way)), [])

    def test_the_seam_may_fall_anywhere(self):
        # The one place the steps drop is the reel closing its loop, and a
        # module whose home offset sits mid-flap puts it somewhere other than
        # between the last flap and the first. That is not a fault.
        steps = self.steps()
        steps[0] = self.CAL - 25                       # flap 0 nudged back past zero
        self.assertEqual(self.problems(steps), [])

    def test_a_second_place_the_steps_drop_is_the_fault(self):
        steps = self.steps()
        steps[0] = self.CAL - 25
        steps[30] = steps[29] - 1                      # and now flap 30 is behind flap 29
        self.assertNotEqual(self.problems(steps), [])

    def test_a_flap_placed_past_its_neighbour_is_caught(self):
        self.assertNotEqual(self.problems(self.steps(**{"10": 705})), [])

    def test_two_flaps_on_the_same_step_is_caught(self):
        self.assertNotEqual(self.problems(self.steps(**{"10": 704})), [])

    def test_a_whole_module_shifted_together_is_fine(self):
        # A home offset moves every flap by the same amount and disturbs no
        # order at all, so it must not look like a sequencing fault.
        shifted = [(s + 30) % self.CAL for s in self.steps()]
        self.assertEqual(self.problems(shifted), [])

    def test_the_report_names_the_flaps_and_where_they_would_sit(self):
        found = step_sequence_problems(self.steps(**{"10": 705}), self.CAL)
        at_ten = [p for p in found if p["index"] == 10]
        self.assertTrue(at_ten)
        self.assertEqual(at_ten[0]["next"], 11)
        self.assertEqual(at_ten[0]["step"], 705)
        self.assertEqual(at_ten[0]["next_step"], 704)

    def test_nonsense_inputs_do_not_raise(self):
        for steps, cal in (([], 4096), ([10], 4096), ([1, 2, 3], 0), ([1, 2, 3], -1)):
            with self.subTest(steps=steps, cal=cal):
                self.assertEqual(step_sequence_problems(steps, cal), [])

    def test_effective_steps_prefers_the_tuned_value(self):
        steps = effective_steps({"3": 999}, self.CAL, self.FLAPS)
        self.assertEqual(steps[3], 999)
        self.assertEqual(steps[4], (4 * self.CAL) // self.FLAPS)

    def test_effective_steps_covers_every_flap(self):
        self.assertEqual(len(effective_steps({}, self.CAL, 40)), 40)
