"""Text preparation shared by the senders.

Every sender normalises the same way before it touches the modules: uppercase,
map colour emoji to their flap codes, alias the configured currency symbol
onto the physical $ flap, substitute the " flap's firmware code, then pad or
truncate to the grid. Drift between them shows up as a character rendering
correctly under one transition style and blank under another.
"""

import unittest
from unittest import mock

from support import SplitflapTestCase, state

from splitflap import display
from splitflap.settings import settings


class TextPreparationTests(SplitflapTestCase):
    def setUp(self):
        super().setUp()
        self.set_grid(1, 10)
        # The senders stagger their writes with sleeps proportional to how far
        # each flap must travel; none of that is under test here.
        patcher = mock.patch("time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def rendered(self):
        return state.current_display_string

    # ── the property that must hold for every sender ────────────────
    def senders(self):
        return {
            "send_to_display": lambda text: display.send_to_display(text),
            "send_to_display_sync": display.send_to_display_sync,
            "send_to_display_slot": display.send_to_display_slot,
        }

    def test_every_sender_uppercases(self):
        for name, send in self.senders().items():
            with self.subTest(sender=name):
                send("hi")
                self.assertEqual(self.rendered(), "HI".ljust(10))

    def test_every_sender_pads_to_the_grid(self):
        for name, send in self.senders().items():
            with self.subTest(sender=name):
                send("AB")
                self.assertEqual(len(self.rendered()), 10)

    def test_every_sender_truncates_to_the_grid(self):
        for name, send in self.senders().items():
            with self.subTest(sender=name):
                send("ABCDEFGHIJKLMNOP")
                self.assertEqual(self.rendered(), "ABCDEFGHIJ")

    def test_every_sender_maps_colour_emoji_to_flap_codes(self):
        for name, send in self.senders().items():
            with self.subTest(sender=name):
                send("\U0001f7e5\U0001f7e6")
                self.assertEqual(self.rendered()[:2], "rb")

    def test_every_sender_aliases_the_configured_currency_symbol(self):
        # The physical flap is '$'. A user whose currency is set to something
        # else expects that character to land on it — under any transition.
        settings['currency_symbol'] = '£'
        for name, send in self.senders().items():
            with self.subTest(sender=name):
                send("£9")
                self.assertEqual(self.rendered()[:2], "$9")

    def test_currency_alias_is_a_no_op_when_the_symbol_is_already_dollar(self):
        settings['currency_symbol'] = '$'
        for name, send in self.senders().items():
            with self.subTest(sender=name):
                send("$9")
                self.assertEqual(self.rendered()[:2], "$9")

    def test_empty_text_is_ignored(self):
        for name, send in self.senders().items():
            with self.subTest(sender=name):
                self.assertEqual(send(""), 0)

    def test_raw_pages_keep_their_lowercase_colour_codes(self):
        # Animations emit colour codes directly and must not be uppercased.
        display.send_to_display("rgb", raw=True)
        self.assertEqual(self.rendered()[:3], "rgb")


if __name__ == "__main__":
    unittest.main()
