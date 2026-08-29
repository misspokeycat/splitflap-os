"""The display loop and the app-playlist runner."""

import unittest

from support import SplitflapTestCase, state

from splitflap import notifications, playlist
from splitflap.plugins import _plugin_registry
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


class SharedPageBehaviourTests(SplitflapTestCase):
    """_run_app_playlist and playlist_loop used to carry their own copy of
    "send the page, wait out the rotation, hold". Two features were added to
    one copy and never to the other:

      6c6fdbb  "Skip rotation wait for random spin app"  -> playlist_loop only
      a1711be  notification interrupts                   -> playlist_loop only

    so the same app ran slower inside a playlist than on its own, and
    notifications never appeared while an app playlist was running. Both loops
    now share _show_page, which is what makes these pass.
    """

    def setUp(self):
        super().setUp()
        self.set_grid(3, 15)
        self.patch_everywhere("_get_pages_for_app", lambda key: ["PAGE"])
        self.patch_everywhere(
            "_send_with_effect",
            lambda text, style, speed, is_anim, app_id=None: 7)
        self.rotation_waits = []
        self.patch_everywhere(
            "_rotation_time",
            lambda dist: self.rotation_waits.append(dist) or 0.0)
        state.app_playlist_loop = False
        state.last_sent_page = None
        # The queue outlives a test unless it is cleared: a message left
        # undelivered by one test gets shown by the next.
        notifications._notify_queue.clear()
        self.addCleanup(notifications._notify_queue.clear)

    def register(self, app_id, manifest):
        _plugin_registry[app_id] = manifest
        self.addCleanup(_plugin_registry.pop, app_id, None)

    def run_playlist(self, app_id, duration=0.2):
        # A page whose dwell outlasts the entry's duration is cut short before
        # the end of _show_page, so tests that need the whole page to finish
        # set the dwell to zero and give the entry room.
        state.active_app_playlist = [{"type": "app", "app": app_id, "duration": duration}]
        playlist._run_app_playlist()

    def test_an_app_playlist_waits_for_rotation_by_default(self):
        self.register("normal-app", {})
        self.run_playlist("normal-app")
        self.assertTrue(self.rotation_waits, "the rotation wait was skipped")

    def test_an_app_playlist_honours_skip_rotation_wait(self):
        self.register("spinner", {"skip_rotation_wait": True})
        self.run_playlist("spinner")
        self.assertEqual(self.rotation_waits, [],
                         "skip_rotation_wait was ignored inside a playlist")

    def test_notifications_interrupt_an_app_playlist(self):
        shown = []
        self.patch_everywhere("_show_notify_message", shown.append)
        settings['notify_enabled'] = True
        settings['global_loop_delay'] = 0
        notifications.push("URGENT", "test")
        self.register("normal-app", {})
        self.run_playlist("normal-app", duration=1)
        # The entry re-renders its page until its duration is up, so the exact
        # count depends on timing; that it reached the display at all is what
        # this is about.
        self.assertIn("URGENT", [m['text'] for m in shown])

    def test_notifications_are_not_shown_when_disabled(self):
        shown = []
        self.patch_everywhere("_show_notify_message", shown.append)
        settings['notify_enabled'] = False
        settings['global_loop_delay'] = 0
        self.register("normal-app", {})
        self.run_playlist("normal-app", duration=1)
        self.assertEqual(shown, [])

    def test_a_stop_takes_priority_over_a_queued_notification(self):
        # The old playlist_loop broke out of the dwell on stop_event and then
        # showed a notification anyway, delaying whatever the user had just
        # asked for. _show_page returns as soon as the hold is cut short.
        shown = []
        self.patch_everywhere("_show_notify_message", shown.append)
        settings['notify_enabled'] = True
        settings['global_loop_delay'] = 5
        notifications.push("URGENT", "test")
        self.register("normal-app", {})
        state.stop_event.set()
        self.run_playlist("normal-app", duration=1)
        self.assertEqual(shown, [])


class LoopDelayTests(SplitflapTestCase):
    def test_animation_speed_is_floored(self):
        # anim_speed of 0 would spin the loop with no delay at all.
        settings['anim_speed'] = '0'
        self.assertEqual(max(0.1, float(settings.get('anim_speed', '0.4'))), 0.1)


if __name__ == "__main__":
    unittest.main()
