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

    def test_a_whole_backup_still_covers_the_whole_grid(self):
        # The count must come from the payload, not from a hardcoded 45.
        self.set_grid(2, 5)
        backup = self.http.get("/backup_settings").get_json()
        backup["offsets"] = {str(i): 2832 for i in range(10)}
        body = self.http.post("/restore_settings", json=backup).get_json()
        self.assertEqual(body["modules_updated"], 10)

    def test_restoring_some_modules_leaves_the_rest_alone(self):
        # Rewriting a module's EEPROM is not free and is not safe: it happens
        # while the motors are drawing. Restoring three modules used to push
        # all forty-five, which is both slow and gratuitous wear.
        body = self.http.post("/restore_settings",
                              json={"offsets": {"2": 2800, "7": 2900}}).get_json()
        self.assertEqual(body["modules_updated"], 2)

    def test_restoring_nothing_writes_nothing(self):
        body = self.http.post("/restore_settings", json={"offsets": {}}).get_json()
        self.assertEqual(body["modules_updated"], 0)
        self.assertFalse(body["hardware_updated"])

    def test_a_malformed_backup_is_rejected_not_a_500(self):
        for payload in ({"offsets": "not-a-dict"},
                        {"calibrations": [1, 2, 3]},
                        {"tuned_chars": "nope"}):
            with self.subTest(payload=payload):
                response = self.http.post("/restore_settings", json=payload)
                self.assertEqual(response.status_code, 400)

    def test_an_empty_body_is_rejected(self):
        self.assertEqual(self.http.post("/restore_settings", json={}).status_code, 400)


class ModuleAuditTests(SplitflapTestCase):
    """Comparing module EEPROM against settings.json, without writing either."""

    def setUp(self):
        super().setUp()
        app.app.config["TESTING"] = False
        self.http = app.app.test_client()
        self.set_grid(3, 15)
        settings['calibrations']['0'] = 4096
        settings['offsets']['0'] = 2832
        settings['tuned_chars']['0'] = {'3': 120}
        state.ser = object()          # pretend a module is attached
        self.readings = {}
        self.patch_everywhere("read_hardware_data", lambda i: self.readings.get(i))

    def audit(self, ids=(0,)):
        return self.http.post("/module_audit", json={"ids": list(ids)}).get_json()

    def test_a_module_that_agrees_is_ok(self):
        self.readings[0] = {"offset": 2832, "calibration": 4096, "tuned": {"3": 120}}
        self.assertEqual(self.audit()["modules"][0]["status"], "ok")

    def test_a_differing_offset_is_reported(self):
        self.readings[0] = {"offset": 2900, "calibration": 4096, "tuned": {"3": 120}}
        entry = self.audit()["modules"][0]
        self.assertEqual(entry["status"], "diverged")
        self.assertEqual(entry["diverged"]["offset"], {"module": 2900, "ours": 2832})

    def test_an_out_of_range_step_is_reported_as_suspect(self):
        self.readings[0] = {"offset": 2832, "calibration": 4096,
                            "tuned": {"3": 120, "9": 60000}}
        entry = self.audit()["modules"][0]
        self.assertEqual(entry["rejected"]["tuned"], {"9": 60000})

    def test_an_erased_calibration_is_reported_as_suspect(self):
        self.readings[0] = {"offset": 2832, "calibration": 65535, "tuned": {"3": 120}}
        entry = self.audit()["modules"][0]
        self.assertEqual(entry["status"], "suspect")
        self.assertEqual(entry["rejected"]["calibration"], 65535)

    def test_a_silent_module_is_reported(self):
        entry = self.audit()["modules"][0]
        self.assertEqual(entry["status"], "no_response")

    def test_every_entry_has_the_same_shape(self):
        self.readings[1] = {"offset": 2832, "calibration": 4096, "tuned": {}}
        for entry in self.audit(ids=(0, 1))["modules"]:
            with self.subTest(module=entry["id"]):
                self.assertEqual(set(entry), {"id", "status", "rejected", "diverged"})

    def test_the_audit_writes_nothing(self):
        self.readings[0] = {"offset": 2900, "calibration": 4096, "tuned": {"9": 60000}}
        self.audit()
        self.assertEqual(settings['offsets']['0'], 2832)
        self.assertEqual(settings['tuned_chars']['0'], {'3': 120})
        self.assertEqual(self.sent, [], "the audit talked to the module bus")

    def test_it_refuses_when_no_hardware_is_attached(self):
        state.ser = None
        self.assertEqual(
            self.http.post("/module_audit", json={"ids": [0]}).status_code, 409)

    def test_it_refuses_a_bad_module_list(self):
        for bad in ("all", [999], [None], 5):
            with self.subTest(ids=bad):
                response = self.http.post("/module_audit", json={"ids": bad})
                self.assertEqual(response.status_code, 400)


class TunedStepRangeTests(SplitflapTestCase):
    def setUp(self):
        super().setUp()
        app.app.config["TESTING"] = False
        self.http = app.app.test_client()
        self.set_grid(3, 15)
        settings['calibrations']['0'] = 4096

    def test_a_step_beyond_one_revolution_is_refused(self):
        # auto_tune already clamps to this; custom_tune stored anything.
        response = self.http.post("/custom_tune", json={
            "action": "save", "id": 0, "index": 3, "step": 65535})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("3", settings['tuned_chars'].get('0', {}))

    def test_the_last_valid_step_is_accepted(self):
        response = self.http.post("/custom_tune", json={
            "action": "save", "id": 0, "index": 3, "step": 4095})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(settings['tuned_chars']['0']['3'], 4095)


class ApplyTuningTests(SplitflapTestCase):
    """Writing a handful of corrections without rewriting the display.

    The bus is 9600 baud and every command carries a fixed delay, so what
    matters here is not that the right values arrive but that nothing else
    does. Restoring a backup writes each module's whole tuning back; a camera
    pass correcting fifty-seven positions must write fifty-seven commands.
    """

    def setUp(self):
        super().setUp()
        app.app.config["TESTING"] = False
        self.http = app.app.test_client()
        self.set_grid(3, 15)

    def apply(self, tuned):
        return self.http.post("/apply_tuning", json={"tuned": tuned})

    def test_one_command_per_correction_and_nothing_else(self):
        body = self.apply({"3": {"10": 640, "11": 704}, "7": {"2": 128}}).get_json()
        self.assertEqual(body["writes"], 3)
        self.assertEqual(body["modules"], 2)
        self.assertEqual(self.sent, ["m03w10:640", "m03w11:704", "m07w2:128"])

    def test_the_values_are_stored(self):
        self.apply({"3": {"10": 640}})
        self.assertEqual(settings['tuned_chars']['3']['10'], 640)

    def test_tuning_already_stored_is_left_in_place(self):
        # The correction is a delta. Positions that were already right are
        # neither rewritten to EEPROM nor dropped from settings.
        settings['tuned_chars']['3'] = {"5": 320}
        self.apply({"3": {"10": 640}})
        self.assertEqual(settings['tuned_chars']['3'], {"5": 320, "10": 640})
        self.assertEqual(self.sent, ["m03w10:640"])

    def test_a_correction_for_a_module_that_does_not_exist(self):
        self.assertEqual(self.apply({"99": {"1": 10}}).status_code, 400)
        self.assertEqual(self.sent, [])

    def test_a_step_outside_one_revolution_is_rejected(self):
        settings['calibrations']['3'] = 4096
        for step in (-1, 4096, 65535, "banana"):
            with self.subTest(step=step):
                self.assertEqual(self.apply({"3": {"10": step}}).status_code, 400)
        self.assertEqual(self.sent, [])

    def test_an_index_past_the_end_of_the_reel_is_rejected(self):
        self.assertEqual(self.apply({"3": {"64": 100}}).status_code, 400)
        self.assertEqual(self.sent, [])

    def test_nothing_is_written_when_any_correction_is_bad(self):
        # A half-applied set is worse than a rejected one: the display ends
        # up in a state nothing recorded.
        response = self.apply({"3": {"10": 640}, "4": {"10": 99999}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.sent, [])
        self.assertNotIn("10", settings['tuned_chars'].get('3', {}))

    def test_the_payload_has_to_be_the_right_shape(self):
        for payload in ({}, {"tuned": "no"}, {"tuned": {"3": "no"}}, {"tuned": {"3": [1, 2]}}):
            with self.subTest(payload=payload):
                self.assertEqual(
                    self.http.post("/apply_tuning", json=payload).status_code, 400)

    def test_an_empty_correction_set_is_not_an_error(self):
        body = self.apply({}).get_json()
        self.assertEqual(body["writes"], 0)
        self.assertEqual(self.sent, [])

    def test_a_flap_cannot_be_written_past_its_neighbour(self):
        # Flap 11 sits at 704 on an untuned reel. Putting flap 10 beyond it
        # describes a reel that turns both ways.
        response = self.apply({"3": {"10": 705}})
        self.assertEqual(response.status_code, 409)
        body = response.get_json()
        self.assertEqual(body["module"], 3)
        self.assertIn("cannot move past its neighbour", body["error"])
        self.assertEqual(self.sent, [])
        self.assertNotIn("10", settings['tuned_chars'].get('3', {}))

    def test_a_whole_flap_of_error_is_refused(self):
        # What a misidentified flap produces, and what got written twice.
        self.assertEqual(self.apply({"3": {"10": 640 + 64}}).status_code, 409)
        self.assertEqual(self.sent, [])

    def test_compensating_for_real_slop_is_still_allowed(self):
        self.assertEqual(self.apply({"3": {"10": 640 + 20}}).status_code, 200)
        self.assertEqual(settings['tuned_chars']['3']['10'], 660)

    def test_a_module_already_out_of_order_stays_fixable(self):
        # Refusing only what a write introduces. A module carrying a bad
        # sequence from before must not be locked out of being corrected.
        settings['tuned_chars']['3'] = {"10": 705}
        response = self.apply({"3": {"10": 645}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(settings['tuned_chars']['3']['10'], 645)

    def test_the_check_looks_at_the_whole_batch_together(self):
        # Two writes that are each fine against what is stored but wrong
        # against each other.
        response = self.apply({"3": {"10": 640, "11": 641}})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.sent, [])

    def test_nothing_reaches_the_bus_when_the_server_holds_positions(self):
        # The next page sends the step itself, so writing EEPROM as well
        # would be wear on the one store here that loses writes.
        settings['position_source'] = 'server'
        body = self.apply({"3": {"10": 640}}).get_json()
        self.assertEqual(self.sent, [])
        self.assertEqual(body["writes"], 1)
        self.assertFalse(body["hardware_updated"])
        self.assertEqual(settings['tuned_chars']['3']['10'], 640)

    def test_a_high_module_on_a_large_grid(self):
        # Defaults only cover modules 0-44; the calibration lookup must fall
        # back rather than reject a module the grid really has.
        self.set_grid(5, 15)
        self.assertEqual(self.apply({"60": {"10": 640}}).status_code, 200)
        self.assertEqual(self.sent, ["m60w10:640"])


if __name__ == "__main__":
    unittest.main()