"""Setting the display over HTTP.

/update_playlist has always taken `pages`: raw strings written to the modules
as-is, which means the caller has to lay the grid out itself. That is fine for
the web UI, which knows the geometry, and awkward for everything else — a
curl one-liner had to pad and centre by hand, while the MQTT text entity got
"HELLO|WORLD" for free.

`text` is the same input the MQTT entity takes, so both interfaces now speak
the form a person can type.
"""

import unittest

from support import SplitflapTestCase, app, state

from splitflap.grid import get_module_count
from splitflap.settings import settings


class TextFieldTests(SplitflapTestCase):
    def setUp(self):
        super().setUp()
        app.app.config["TESTING"] = False
        self.http = app.app.test_client()
        self.set_grid(3, 15)

    def post(self, payload):
        return self.http.post("/update_playlist", json=payload)

    def page(self):
        return state.current_playlist[0]

    def rows(self):
        cols = 15
        return [self.page()[i * cols:(i + 1) * cols] for i in range(3)]

    def test_a_single_line_is_centred(self):
        self.post({"text": "HELLO"})
        self.assertEqual(self.rows()[0], "     HELLO     ")

    def test_a_pipe_starts_a_new_line(self):
        self.post({"text": "HELLO|WORLD"})
        self.assertEqual(self.rows()[:2], ["     HELLO     ", "     WORLD     "])

    def test_the_page_fills_the_grid(self):
        self.post({"text": "HI"})
        self.assertEqual(len(self.page()), get_module_count())

    def test_centring_can_be_turned_off(self):
        self.post({"text": "HELLO|WORLD", "center": False})
        self.assertEqual(self.rows()[:2], ["HELLO          ", "WORLD          "])

    def test_several_pages_rotate(self):
        self.post({"text": ["ONE", "TWO"]})
        self.assertEqual(len(state.current_playlist), 2)
        self.assertTrue(all(len(p) == get_module_count() for p in state.current_playlist))

    def test_lines_beyond_the_grid_are_dropped(self):
        self.post({"text": "A|B|C|D|E"})
        self.assertEqual(len(self.page()), get_module_count())

    def test_setting_text_stops_a_running_app(self):
        state.active_app = "time"
        state.active_app_playlist = ["time"]
        self.post({"text": "TAKEOVER"})
        self.assertIsNone(state.active_app)
        self.assertIsNone(state.active_app_playlist)
        self.assertTrue(state.stop_event.is_set())

    def test_delay_is_honoured(self):
        self.post({"text": "HELLO", "delay": 42})
        self.assertEqual(state.loop_delay, 42)

    def test_the_response_reports_what_was_queued(self):
        body = self.post({"text": "HELLO|WORLD"}).get_json()
        self.assertEqual(body["pages"], 1)
        self.assertEqual(body["status"], "success")


class BackwardCompatibilityTests(SplitflapTestCase):
    """The web UI sends `pages` and must keep working untouched."""

    def setUp(self):
        super().setUp()
        app.app.config["TESTING"] = False
        self.http = app.app.test_client()
        self.set_grid(3, 15)

    def test_raw_pages_are_passed_through_unchanged(self):
        raw = "X" * 45
        self.http.post("/update_playlist", json={"pages": [raw], "delay": 5})
        self.assertEqual(state.current_playlist, [raw])

    def test_dict_pages_still_work(self):
        page = {"text": "HELLO", "delay": 10, "style": "spiral", "speed": 5}
        self.http.post("/update_playlist", json={"pages": [page]})
        self.assertEqual(state.current_playlist, [page])

    def test_an_empty_body_clears_the_playlist(self):
        self.http.post("/update_playlist", json={"pages": ["HI"]})
        self.http.post("/update_playlist", json={})
        self.assertEqual(state.current_playlist, [])

    def test_pages_wins_if_both_are_given(self):
        self.http.post("/update_playlist", json={"pages": ["RAW"], "text": "FORMATTED"})
        self.assertEqual(state.current_playlist, ["RAW"])


class ValidationTests(SplitflapTestCase):
    def setUp(self):
        super().setUp()
        app.app.config["TESTING"] = False
        self.http = app.app.test_client()

    def test_a_non_string_text_is_rejected(self):
        for bad in (5, {"a": 1}, [1, 2], [None]):
            with self.subTest(text=bad):
                response = self.http.post("/update_playlist", json={"text": bad})
                self.assertEqual(response.status_code, 400)

    def test_a_non_list_pages_is_rejected(self):
        response = self.http.post("/update_playlist", json={"pages": "not-a-list"})
        self.assertEqual(response.status_code, 400)

    def test_a_bad_delay_is_rejected(self):
        response = self.http.post("/update_playlist", json={"text": "HI", "delay": "soon"})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
