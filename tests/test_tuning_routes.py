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
