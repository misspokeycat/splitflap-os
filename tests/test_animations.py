"""Invariants for the module send orders.

Every style is a permutation of the grid: each module addressed exactly once.
A style that drops or repeats an index leaves modules stuck on the previous
frame, which is invisible in a unit test of any single style but obvious as
an invariant across all of them.
"""

import unittest

from support import app  # noqa: F401  (imported first: it sets up sys.path)

from splitflap.animations import get_animation_order
from splitflap.settings import settings

STYLES = [
    'ltr', 'rtl', 'center_out', 'outside_in', 'spiral', 'diagonal',
    'anti_diagonal', 'random', 'rain', 'reverse_rain', 'columns',
    'columns_rtl', 'alternating',
]

GRIDS = [(3, 15), (1, 10), (10, 1), (4, 20), (2, 2), (5, 7)]


class AnimationOrderTests(unittest.TestCase):
    def test_every_style_visits_every_module_exactly_once(self):
        for style in STYLES:
            for rows, cols in GRIDS:
                with self.subTest(style=style, grid=f"{rows}x{cols}"):
                    order = get_animation_order(style, rows, cols)
                    self.assertEqual(
                        sorted(order), list(range(rows * cols)),
                        f"{style} on {rows}x{cols} is not a permutation",
                    )

    def test_unknown_style_falls_back_to_left_to_right(self):
        self.assertEqual(get_animation_order('nonsense', 2, 3), [0, 1, 2, 3, 4, 5])

    def test_rtl_is_ltr_reversed(self):
        self.assertEqual(
            get_animation_order('rtl', 3, 15),
            list(reversed(get_animation_order('ltr', 3, 15))),
        )

    def test_outside_in_is_center_out_reversed(self):
        self.assertEqual(
            get_animation_order('outside_in', 3, 15),
            list(reversed(get_animation_order('center_out', 3, 15))),
        )

    def test_center_out_starts_at_the_middle_column(self):
        order = get_animation_order('center_out', 1, 5)
        self.assertEqual(order[0], 2)

    def test_columns_walks_down_before_across(self):
        self.assertEqual(get_animation_order('columns', 2, 3), [0, 3, 1, 4, 2, 5])

    def test_rain_walks_across_before_down(self):
        self.assertEqual(get_animation_order('rain', 2, 3), [0, 1, 2, 3, 4, 5])

    def test_order_defaults_to_the_configured_grid(self):
        settings['sim_rows'], settings['sim_cols'] = 3, 15
        self.assertEqual(len(get_animation_order('spiral')), 45)


if __name__ == "__main__":
    unittest.main()
