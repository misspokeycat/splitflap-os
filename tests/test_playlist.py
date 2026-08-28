"""The display loop and the app-playlist runner."""

import unittest

from support import SplitflapTestCase, state

from splitflap import playlist
from splitflap.settings import settings


class AppPlaylistRunnerTests(SplitflapTestCase):
    """_run_app_playlist skips sending a page whose text is already on the
    display, then waits for that send's rotation to finish. When the very
    first page is skipped there is no send to wait for, and max_dist has
    never been assigned."""

    def setUp(self):
        super().setUp()
        self.set_grid(3, 15)
        # "plain-app" is deliberately not in the registry, so is_anim is False
        # and the unchanged-page skip is reachable. Animations always resend.
        self.patch_everywhere("_get_pages_for_app", lambda key: ["ALREADY SHOWING"])
        self.sends = []
        self.patch_everywhere(
            "_send_with_effect",
            lambda text, style, speed, is_anim, app_id=None: self.sends.append(text) or 7,
        )
        self.patch_everywhere("_rotation_time", lambda dist: 0.0)
        state.app_playlist_loop = False
        state.active_app_playlist = [
            {"type": "app", "app": "plain-app", "duration": 0.2},
        ]

    def test_runner_survives_a_first_page_that_is_already_displayed(self):
        # The display is already showing exactly what the first page renders,
        # so the runner skips the send — and then waits on its result.
        state.last_sent_page = "ALREADY SHOWING"
        playlist._run_app_playlist()
        self.assertEqual(self.sends, [], "page should have been skipped as unchanged")

    def test_runner_sends_a_first_page_that_differs(self):
        state.last_sent_page = "SOMETHING ELSE"
        playlist._run_app_playlist()
        self.assertEqual(self.sends, ["ALREADY SHOWING"])

    def test_runner_clears_the_active_app_when_done(self):
        state.last_sent_page = None
        playlist._run_app_playlist()
        self.assertIsNone(state.active_app)

    def test_compose_entries_are_sent(self):
        sent = []
        self.patch_everywhere(
            "send_to_display",
            lambda text, order=None, raw=False, step_delay_ms=15: sent.append(text) or 3,
        )
        self.patch_everywhere("_rotation_time", lambda dist: 0.0)
        state.active_app_playlist = [
            {"type": "compose", "text": "HELLO", "style": "ltr", "speed": 15, "duration": 0.1},
        ]
        playlist._run_app_playlist()
        self.assertEqual(sent, ["HELLO"])


class LoopDelayTests(SplitflapTestCase):
    def test_animation_speed_is_floored(self):
        # anim_speed of 0 would spin the loop with no delay at all.
        settings['anim_speed'] = '0'
        self.assertEqual(max(0.1, float(settings.get('anim_speed', '0.4'))), 0.1)


if __name__ == "__main__":
    unittest.main()
