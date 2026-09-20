"""Text preparation shared by the senders.

Every sender normalises the same way before it touches the modules: uppercase,
map colour emoji to their flap codes, alias the configured currency symbol
onto the physical $ flap, substitute the " flap's firmware code, then pad or
truncate to the grid. Drift between them shows up as a character rendering
correctly under one transition style and blank under another.
"""

import os
import unittest
from unittest import mock

from support import SplitflapTestCase, state

from splitflap import display
from splitflap.settings import get_module_char_map, get_position_source, settings


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



class PositionSourceTests(SplitflapTestCase):
    """Who decides where a flap stops.

    Module EEPROM is written while the motors are drawing and this display
    has lost tuning to that more than once. settings.json is written
    atomically, keeps a backup, and is one file rather than forty-five chips.
    So the server can hold the positions instead and send the motor step,
    leaving the module nothing to remember.
    """

    def setUp(self):
        super().setUp()
        self.set_grid(3, 15)
        settings['calibrations']['0'] = 4096
        settings['calibrations']['1'] = 4096
        settings['tuned_chars']['0'] = {}
        settings['tuned_chars']['1'] = {}

    def test_by_default_the_module_is_asked_for_a_character(self):
        settings.pop('position_source', None)
        self.assertEqual(display.module_frame(0, 'A'), "m00-A")

    def test_an_unknown_setting_falls_back_to_the_module(self):
        # The default has to be the behaviour that does not depend on this
        # setting being understood.
        for value in ('', 'moduler', 'yes', None, 'MODULE'):
            with self.subTest(value=value):
                settings['position_source'] = value
                self.assertEqual(display.module_frame(0, 'A'), "m00-A")

    def test_the_server_sends_the_step_instead(self):
        settings['position_source'] = 'server'
        # "A" is flap 1 of 64 on a 4096 step reel.
        self.assertEqual(display.module_frame(0, 'A'), "m00g64")

    def test_the_step_sent_is_the_tuned_one_when_there_is_one(self):
        settings['position_source'] = 'server'
        settings['tuned_chars']['0'] = {'1': 77}
        self.assertEqual(display.module_frame(0, 'A'), "m00g77")
        self.assertEqual(display.module_frame(1, 'A'), "m01g64")   # untuned

    def test_the_step_stays_inside_one_revolution(self):
        # Outside it the module has nowhere to go and firmware would be
        # right to ignore the frame.
        settings['position_source'] = 'server'
        settings['tuned_chars']['0'] = {'1': 99999}
        self.assertEqual(display.module_frame(0, 'A'), "m00g4095")
        settings['tuned_chars']['0'] = {'1': -5}
        self.assertEqual(display.module_frame(0, 'A'), "m00g0")

    def test_the_step_matches_what_tuning_status_calls_active(self):
        # Two places compute this; they have to agree, or the camera tuner
        # corrects against a position the display never sends.
        settings['position_source'] = 'server'
        settings['tuned_chars']['0'] = {'5': 321}
        cal = int(settings['calibrations']['0'])
        for idx in (0, 1, 5, 30, 63):
            with self.subTest(idx=idx):
                tuned = settings['tuned_chars']['0'].get(str(idx))
                active = int(tuned) if tuned is not None else (idx * cal) // 64
                char = get_module_char_map(0)[idx]
                self.assertEqual(display.module_step(0, char), active)

    def test_a_character_the_reel_does_not_have_falls_to_the_first_flap(self):
        settings['position_source'] = 'server'
        self.assertEqual(display.module_frame(0, '¥'), "m00g0")

    def test_the_environment_overrides_the_file(self):
        settings['position_source'] = 'module'
        with mock.patch.dict(os.environ, {"SPLITFLAP_POSITION_SOURCE": "server"}):
            self.assertEqual(get_position_source(), "server")


class SenderFrameTests(SplitflapTestCase):
    """The senders emit the frame the setting asks for.

    module_frame being right is not the same as the senders using it: there
    are four of them, they write to the port directly rather than through
    send_raw, and a helper that only one of them calls is the bug this
    project has already shipped twice.
    """

    class FakePort:
        def __init__(self):
            self.frames = []

        def write(self, payload):
            self.frames.append(payload.decode('cp1252').strip())

        def flush(self):
            pass

    def setUp(self):
        super().setUp()
        self.set_grid(1, 3)
        self.port = self.FakePort()
        state.ser = self.port
        state.sim_mode = False
        for m in range(3):
            settings['calibrations'][str(m)] = 4096
            settings['tuned_chars'][str(m)] = {}

    def senders(self):
        return [
            ("send_to_display", lambda t: display.send_to_display(t, raw=True)),
            ("send_to_display_sync", display.send_to_display_sync),
            ("send_to_display_slot", display.send_to_display_slot),
        ]

    def test_every_sender_asks_the_module_by_default(self):
        settings['position_source'] = 'module'
        for name, send in self.senders():
            with self.subTest(sender=name):
                self.port.frames.clear()
                send("ABC")
                self.assertTrue(self.port.frames, f"{name} sent nothing")
                self.assertTrue(all('-' in f for f in self.port.frames),
                                f"{name} sent {self.port.frames}")

    def test_every_sender_sends_steps_when_the_server_holds_them(self):
        settings['position_source'] = 'server'
        for name, send in self.senders():
            with self.subTest(sender=name):
                self.port.frames.clear()
                send("ABC")
                self.assertTrue(self.port.frames, f"{name} sent nothing")
                for frame in self.port.frames:
                    self.assertRegex(frame, r"^m\d{2}g\d+$",
                                     f"{name} sent a character frame: {frame}")

    def test_the_steps_sent_are_the_tuned_ones(self):
        settings['position_source'] = 'server'
        settings['tuned_chars']['1'] = {'2': 150}      # module 1 showing "B"
        display.send_to_display("ABC", raw=True)
        self.assertEqual(self.port.frames, ["m00g64", "m01g150", "m02g192"])

    def test_switching_back_needs_no_restart(self):
        settings['position_source'] = 'server'
        display.send_to_display("ABC", raw=True)
        settings['position_source'] = 'module'
        self.port.frames.clear()
        display.send_to_display("ABC", raw=True)
        self.assertEqual(self.port.frames, ["m00-A", "m01-B", "m02-C"])
if __name__ == "__main__":
    unittest.main()
