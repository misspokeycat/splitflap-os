"""Per-module tuning.

The settings defaults populate offsets, calibrations and tuned_chars for 45
modules, because that is the display this project was written for. The grid is
configurable up to 20x50, so any code that indexes those dicts by module id
has to cope with a module the defaults never created an entry for.
"""

import unittest

from support import SplitflapTestCase, app, state

from splitflap.grid import get_module_count
from splitflap.settings import settings


class LargeGridTuningTests(SplitflapTestCase):
    def setUp(self):
        super().setUp()
        app.app.config["TESTING"] = False
        self.http = app.app.test_client()
        self.set_grid(5, 15)          # 75 modules; defaults only cover 0-44
        self.high = 60

    def test_the_grid_really_is_bigger_than_the_defaults(self):
        self.assertGreater(get_module_count(), 45)
        self.assertNotIn(str(self.high), settings['tuned_chars'])

    def test_tuning_a_character_on_a_high_module(self):
        response = self.http.post("/custom_tune", json={
            "action": "save", "id": self.high, "index": 3, "step": 1234,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(settings['tuned_chars'][str(self.high)]["3"], 1234)

    def test_erasing_one_character_on_a_high_module(self):
        response = self.http.post("/custom_tune", json={
            "action": "erase", "id": self.high, "index": 3,
        })
        self.assertEqual(response.status_code, 200)

    def test_erasing_every_character_on_a_high_module(self):
        response = self.http.post("/custom_tune", json={
            "action": "erase", "id": self.high,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(settings['tuned_chars'][str(self.high)], {})

    def test_adjusting_the_offset_of_a_high_module(self):
        response = self.http.post("/settings", json={
            "action": "adjust", "id": self.high, "delta": 10,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["new_offset"], 2842)

    def test_homing_a_high_module(self):
        response = self.http.post("/settings", json={"action": "home_one", "id": self.high})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(state.current_indices[self.high], 0)


class ModuleIdValidationTests(SplitflapTestCase):
    """Module ids arrive in request bodies and are used both to index the
    display buffers and to build serial commands. An id outside the grid
    raised IndexError — after already putting a malformed frame like "m999h"
    on the wire."""

    def setUp(self):
        super().setUp()
        app.app.config["TESTING"] = False
        self.http = app.app.test_client()
        self.set_grid(3, 15)          # modules 0-44

    def test_home_one_rejects_a_module_outside_the_grid(self):
        response = self.http.post("/settings", json={"action": "home_one", "id": 999})
        self.assertEqual(response.status_code, 400)

    def test_home_one_sends_nothing_for_a_bad_module(self):
        self.http.post("/settings", json={"action": "home_one", "id": 999})
        self.assertEqual(self.sent, [], "a command was sent for a module that does not exist")

    def test_offset_adjust_rejects_a_module_outside_the_grid(self):
        response = self.http.post("/settings", json={"action": "adjust", "id": 999, "delta": 5})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("999", settings['offsets'])

    def test_custom_tune_rejects_a_module_outside_the_grid(self):
        response = self.http.post("/custom_tune", json={
            "action": "goto", "id": 999, "step": 10, "index": 1})
        self.assertEqual(response.status_code, 400)

    def test_custom_tune_rejects_a_non_numeric_step(self):
        response = self.http.post("/custom_tune", json={
            "action": "goto", "id": 0, "step": "abc", "index": 1})
        self.assertEqual(response.status_code, 400)

    def test_a_negative_module_id_is_rejected(self):
        # Negative indices are valid Python but address the wrong module.
        response = self.http.post("/custom_tune", json={
            "action": "goto", "id": -1, "step": 10, "index": 1})
        self.assertEqual(response.status_code, 400)

    def test_the_last_real_module_is_still_accepted(self):
        response = self.http.post("/settings", json={"action": "home_one", "id": 44})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.sent, ["m44h"])


class RestoreTests(SplitflapTestCase):
    def setUp(self):
        super().setUp()
        app.app.config["TESTING"] = False
        self.http = app.app.test_client()
        self.set_grid(3, 15)

    def test_a_backup_round_trips(self):
        settings['offsets']['0'] = 1111
        backup = self.http.get("/backup_settings").get_json()
        settings['offsets']['0'] = 2222
        self.http.post("/restore_settings", json=backup)
        self.assertEqual(settings['offsets']['0'], 1111)

    def test_the_module_count_reported_is_the_real_one(self):
        self.set_grid(2, 5)
        body = self.http.post("/restore_settings", json={"offsets": {}}).get_json()
        self.assertEqual(body["modules_updated"], 10)

    def test_a_malformed_backup_is_rejected_not_a_500(self):
        for payload in ({"offsets": "not-a-dict"},
                        {"calibrations": [1, 2, 3]},
                        {"tuned_chars": "nope"}):
            with self.subTest(payload=payload):
                response = self.http.post("/restore_settings", json=payload)
                self.assertEqual(response.status_code, 400)

    def test_an_empty_body_is_rejected(self):
        self.assertEqual(self.http.post("/restore_settings", json={}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
