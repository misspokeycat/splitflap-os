"""Remembering which physical module is which.

A module's ID lives in the same EEPROM as its calibration, and this hardware
forgets. The chip serial does not — it is burned into the microcontroller — so
the pairing between the two is kept here and handed back to a module that
turns up on the bus asking who it is.
"""

import unittest
from unittest import mock

from support import SERVER_DIR, SplitflapTestCase

from splitflap.module_registry import ModuleRegistry
from splitflap.settings import settings
from splitflap.transport import restore_module_settings, universal_firmware

SERIAL = "A3F24C0018E7D29B3F01"
OTHER = "B10055FFA3C2918D7E44"


class RegistryTests(SplitflapTestCase):
    def setUp(self):
        super().setUp()
        settings["module_registry"] = {}
        self.restored = []
        self.registry = ModuleRegistry(restore=self.restored.append)

    def stored(self):
        return settings["module_registry"]

    def test_a_pairing_is_remembered_and_read_back(self):
        self.assertTrue(self.registry.remember(SERIAL, 7, firmware="29"))

        self.assertEqual(self.registry.known_id(SERIAL), 7)
        self.assertEqual(self.stored()[SERIAL]["firmware"], "29")

    def test_a_serial_is_matched_however_it_is_written(self):
        self.registry.remember(SERIAL.lower(), 7)

        self.assertEqual(self.registry.known_id(SERIAL), 7)
        self.assertEqual(self.registry.known_id("  " + SERIAL.lower() + "  "), 7)

    def test_a_module_we_have_never_seen_has_no_id(self):
        self.assertIsNone(self.registry.known_id(OTHER))
        self.assertIsNone(self.registry.known_id(""))
        self.assertIsNone(self.registry.known_id(None))

    def test_an_id_belongs_to_one_module(self):
        # Otherwise both would try to recover into the same place in the
        # display, and one of them would be re-provisioning the other.
        self.registry.remember(SERIAL, 7)
        self.registry.remember(OTHER, 7)

        self.assertEqual(self.registry.known_id(OTHER), 7)
        self.assertIsNone(self.registry.known_id(SERIAL))

    def test_a_module_that_moves_takes_its_history_with_it(self):
        self.registry.remember(SERIAL, 7)
        self.registry.on_recovered(SERIAL, 7)
        self.registry.remember(SERIAL, 12)

        self.assertEqual(self.registry.known_id(SERIAL), 12)
        self.assertEqual(self.registry.recoveries(SERIAL), 1)

    def test_a_nonsense_pairing_is_not_stored(self):
        self.assertFalse(self.registry.remember("", 7))
        self.assertFalse(self.registry.remember(SERIAL, 255))
        self.assertFalse(self.registry.remember(SERIAL, "not a number"))
        self.assertEqual(self.stored(), {})

    def test_a_pairing_stored_as_nonsense_is_ignored_rather_than_believed(self):
        # settings.json is a file on a Pi's SD card, and people edit it.
        settings["module_registry"] = {SERIAL: {"id": "twelve"}, OTHER: {"id": 900}}

        self.assertIsNone(self.registry.known_id(SERIAL))
        self.assertIsNone(self.registry.known_id(OTHER))

    def test_seeing_a_module_again_is_not_worth_a_disk_write(self):
        # Every acknowledgement and version reply on the bus comes through
        # here, and a scan produces one per module.
        self.registry.remember(SERIAL, 7, firmware="29")
        with mock.patch("splitflap.module_registry.save_settings") as save:
            self.assertFalse(self.registry.remember(SERIAL, 7, firmware="29"))
            save.assert_not_called()

        self.assertGreater(self.stored()[SERIAL]["last_seen"], 0)

    def test_forgetting_by_serial_or_by_id(self):
        self.registry.remember(SERIAL, 7)
        self.registry.remember(OTHER, 8)

        self.assertEqual(self.registry.forget(serial=SERIAL), [SERIAL])
        self.assertEqual(self.registry.forget(module_id=8), [OTHER])
        self.assertEqual(self.stored(), {})

    def test_forgetting_something_unknown_changes_nothing(self):
        self.registry.remember(SERIAL, 7)

        self.assertEqual(self.registry.forget(serial=OTHER), [])
        self.assertEqual(self.registry.forget(module_id=None), [])
        self.assertEqual(self.registry.known_id(SERIAL), 7)

    def test_forgetting_everything(self):
        self.registry.remember(SERIAL, 7)
        self.registry.remember(OTHER, 8)

        self.assertEqual(self.registry.forget_all(), sorted([SERIAL, OTHER]))
        self.assertEqual(self.stored(), {})

    def test_a_recovered_module_gets_its_settings_back_and_is_counted(self):
        # The ID is only the address. A module whose EEPROM dropped it has
        # almost certainly dropped its calibration and tuning too.
        self.registry.remember(SERIAL, 7)
        self.registry.on_recovered(SERIAL, 7)
        self.registry.on_recovered(SERIAL, 7)

        self.assertEqual(self.restored, [7, 7])
        self.assertEqual(self.registry.recoveries(SERIAL), 2)
        self.assertGreater(self.stored()[SERIAL]["last_recovery_at"], 0)

    def test_a_module_that_comes_back_unrecognised_is_still_recorded(self):
        self.registry.on_recovered(SERIAL, 7)

        self.assertEqual(self.registry.known_id(SERIAL), 7)
        self.assertEqual(self.registry.recoveries(SERIAL), 1)

    def test_a_failed_restore_does_not_undo_the_recovery(self):
        # The ID is back either way, and that is the part that unblocks the
        # display; a serial write that fails must not cost us the record.
        def explode(module_id):
            raise RuntimeError("the port went away")

        registry = ModuleRegistry(restore=explode)
        with self.assertLogs(level="ERROR"):
            registry.on_recovered(SERIAL, 7)

        self.assertEqual(registry.recoveries(SERIAL), 1)

    def test_recovery_follows_the_setting(self):
        self.assertTrue(self.registry.enabled())
        settings["auto_reprovision"] = False
        self.assertFalse(self.registry.enabled())


class RestoreModuleSettingsTests(SplitflapTestCase):
    """One module's worth of what /restore_settings does to the whole grid."""

    def setUp(self):
        super().setUp()
        settings["offsets"]["3"] = 2900
        settings["calibrations"]["3"] = 4100
        settings["tuned_chars"]["3"] = {"0": 12, "5": 300}

    def test_offset_calibration_and_tuning_all_go_back(self):
        restore_module_settings(3)

        self.assertEqual(self.sent, [
            "m03o2900", "m03t4100", "m03e", "m03w0:12", "m03w5:300",
        ])

    def test_the_erased_sentinel_is_not_written_back_as_a_position(self):
        # 65535 is what an unwritten cell reads as, and `w<index>:65535` is
        # the erase command — writing it back would be writing nothing.
        settings["tuned_chars"]["3"] = {"0": 65535, "1": 40}

        restore_module_settings(3)

        self.assertEqual(self.sent[-1], "m03w1:40")
        self.assertNotIn("m03w0:65535", self.sent)

    def test_a_module_we_hold_nothing_for_gets_the_defaults(self):
        restore_module_settings(44)

        self.assertEqual(self.sent, ["m44o2832", "m44t4096", "m44e"])


class WiringTests(SplitflapTestCase):
    def test_the_firmware_manager_was_given_a_registry(self):
        # Without one it observes the bus and recovers nothing.
        self.assertIsInstance(universal_firmware._registry, ModuleRegistry)

    def test_the_watch_starts_at_boot_rather_than_on_a_page_view(self):
        # The reader used to start on the first request from the calibration
        # page, which is the page nobody has open when a module drops off.
        source = (SERVER_DIR / "splitflap" / "startup.py").read_text(encoding="utf-8")
        self.assertIn("universal_firmware.ensure_started()", source)


if __name__ == "__main__":
    unittest.main()
