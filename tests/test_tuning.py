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
    """

    CAL, FLAPS = 4096, 64

    def steps(self, **tuned):
        return effective_steps({str(k): v for k, v in tuned.items()}, self.CAL, self.FLAPS)

    def problems(self, steps):
        return [p["index"] for p in step_sequence_problems(steps, self.CAL)]

    def test_an_untuned_reel_is_already_in_order(self):
        self.assertEqual(self.problems(self.steps()), [])

    def test_the_wrap_from_the_last_flap_to_the_first_counts(self):
        # The gap over the seam is a gap like any other; measuring it wrong
        # would let the worst error of all through unnoticed.
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

    def test_nudging_past_most_of_a_flap_is_caught(self):
        # By here the flap would be sitting on its neighbour, and what the
        # module actually has is a home offset rather than a bad position.
        for nudge in (-75, 75):
            with self.subTest(nudge=nudge):
                self.assertNotEqual(self.problems(self.steps(**{"10": 640 + nudge})), [])

    def test_a_flap_placed_past_its_neighbour_is_caught(self):
        self.assertNotEqual(self.problems(self.steps(**{"10": 705})), [])

    def test_two_flaps_on_the_same_step_is_caught(self):
        self.assertNotEqual(self.problems(self.steps(**{"10": 704})), [])

    def test_a_whole_module_shifted_together_is_fine(self):
        # A home offset moves every flap by the same amount and changes no
        # gap at all, so it must not look like a sequencing fault.
        shifted = [(s + 30) % self.CAL for s in self.steps()]
        self.assertEqual(self.problems(shifted), [])

    def test_the_report_says_which_gap_and_how_wide(self):
        found = step_sequence_problems(self.steps(**{"10": 705}), self.CAL)
        self.assertTrue(found)
        self.assertEqual(found[0]["next"], found[0]["index"] + 1)
        self.assertEqual(found[0]["nominal"], 64.0)
        self.assertIsInstance(found[0]["gap"], int)

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
