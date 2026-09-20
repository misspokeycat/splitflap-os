"""Reading and writing settings.json on hardware that loses power.

settings.json is every offset, calibration and tuned character for all 45
modules. It is gitignored, so unlike everything else on the Pi it exists in
exactly one place. It was written by truncating the live file and writing
into it, which on a display full of stepper motors — the same brownouts that
cost the modules their EEPROM — is a way to end up with half of it.
"""

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from support import SplitflapTestCase  # noqa: F401  (sets up sys.path)

from splitflap import settings as settings_module

REAL = {"calibrations": {"0": 4096}, "offsets": {"0": 2832}}
OLDER = {"calibrations": {"0": 4095}, "offsets": {"0": 2800}}


class SettingsFileTestCase(unittest.TestCase):
    """Points the module's paths at a directory of its own."""

    def setUp(self):
        directory = tempfile.mkdtemp(prefix="splitflap-io-")
        self.dir = pathlib.Path(directory)
        self.config = self.dir / "settings.json"
        self.backup = self.dir / "settings.json.bak"
        for name, value in (("CONFIG_PATH", str(self.config)),
                            ("BACKUP_PATH", str(self.backup))):
            patch = mock.patch.object(settings_module, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, path, text):
        path.write_text(text, encoding="utf-8")

    def contents(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def strays(self):
        return [p.name for p in self.dir.iterdir()
                if p.name not in ("settings.json", "settings.json.bak")]


class SaveTests(SettingsFileTestCase):
    def test_a_saved_file_reads_back(self):
        settings_module.save_settings(REAL)

        self.assertEqual(self.contents(self.config), REAL)

    def test_the_copy_it_displaces_is_kept(self):
        settings_module.save_settings(OLDER)
        settings_module.save_settings(REAL)

        self.assertEqual(self.contents(self.config), REAL)
        self.assertEqual(self.contents(self.backup), OLDER)

    def test_a_write_that_dies_partway_leaves_the_live_file_alone(self):
        # The whole point. Before this, the live file was already truncated by
        # the time anything could go wrong with the data going into it.
        settings_module.save_settings(REAL)

        with mock.patch.object(settings_module.json, "dump",
                               side_effect=OSError("no space left on device")):
            with self.assertRaises(OSError):
                settings_module.save_settings({"calibrations": {"0": 1}})

        self.assertEqual(self.contents(self.config), REAL)

    def test_a_failed_write_does_not_litter_the_directory(self):
        # One stray per failed save, on the filesystem that is already unhappy.
        with mock.patch.object(settings_module.json, "dump",
                               side_effect=OSError("disk error")):
            for _ in range(3):
                with self.assertRaises(OSError):
                    settings_module.save_settings(REAL)

        self.assertEqual(self.strays(), [])

    def test_a_successful_write_does_not_litter_either(self):
        settings_module.save_settings(REAL)
        settings_module.save_settings(OLDER)

        self.assertEqual(self.strays(), [])

    def test_the_first_ever_save_needs_no_file_to_displace(self):
        settings_module.save_settings(REAL)

        self.assertEqual(self.contents(self.config), REAL)
        self.assertFalse(self.backup.exists())


class LoadTests(SettingsFileTestCase):
    def test_stored_settings_win_over_the_defaults(self):
        settings_module.save_settings({"sim_rows": 5, "zip_code": "02139"})

        loaded = settings_module.load_settings()

        self.assertEqual(loaded["sim_rows"], 5)
        self.assertEqual(loaded["zip_code"], "02139")
        self.assertIn("char_map", loaded)       # defaults still fill the rest

    def test_a_truncated_file_falls_back_to_the_previous_copy(self):
        settings_module.save_settings(OLDER)
        settings_module.save_settings(REAL)
        self.write(self.config, '{"calibrations": {"0": 40')   # power cut

        with self.assertLogs(level="ERROR"):
            loaded = settings_module.load_settings()

        self.assertEqual(loaded["calibrations"], OLDER["calibrations"])

    def test_an_empty_file_is_not_read_as_having_no_settings(self):
        # An empty settings.json used to mean "defaults", and the next save
        # made those defaults permanent — 45 modules' calibration gone.
        settings_module.save_settings(REAL)
        settings_module.save_settings(REAL)
        self.write(self.config, "")

        with self.assertLogs(level="ERROR"):
            loaded = settings_module.load_settings()

        self.assertEqual(loaded["calibrations"], REAL["calibrations"])

    def test_defaults_are_only_used_when_there_is_nothing_to_recover(self):
        loaded = settings_module.load_settings()

        self.assertEqual(loaded["calibrations"]["0"], 4096)
        self.assertEqual(loaded["offsets"]["0"], 2832)

    def test_a_file_holding_something_that_is_not_settings_is_refused(self):
        self.write(self.config, '["not", "a", "settings", "object"]')

        loaded = settings_module.load_settings()

        self.assertEqual(loaded["calibrations"]["0"], 4096)

    def test_the_early_read_recovers_from_the_backup_too(self):
        # read_config_file resolves the serial port at boot, before
        # load_settings has run.
        settings_module.save_settings({"serial_port": "/dev/ttyUSB3"})
        settings_module.save_settings({"serial_port": "/dev/ttyUSB3"})
        self.write(self.config, "{oh no")

        with self.assertLogs(level="ERROR"):
            self.assertEqual(
                settings_module.read_config_file()["serial_port"], "/dev/ttyUSB3")

    def test_the_early_read_is_empty_when_nothing_is_stored(self):
        self.assertEqual(settings_module.read_config_file(), {})


class DurabilityTests(SettingsFileTestCase):
    def test_the_bytes_are_forced_to_disk_before_the_rename(self):
        # Without the fsync the rename can land while the contents are still
        # only in the page cache, which is the corruption this is meant to
        # prevent, just with extra steps.
        order = []
        real_fsync = os.fsync
        real_replace = os.replace

        with mock.patch.object(settings_module.os, "fsync",
                               side_effect=lambda fd: (order.append("fsync"),
                                                       real_fsync(fd))[1]), \
             mock.patch.object(settings_module.os, "replace",
                               side_effect=lambda a, b: (order.append("replace"),
                                                         real_replace(a, b))[1]):
            settings_module.save_settings(REAL)

        self.assertEqual(order[0], "fsync")
        self.assertIn("replace", order)

    def test_a_platform_without_directory_fsync_still_saves(self):
        # Directories cannot be opened for reading on Windows, where this
        # suite runs; that must not turn into a failed save.
        real_open = os.open

        def refuse_directories(path, *args, **kwargs):
            if os.path.isdir(path):
                raise OSError("not supported")
            return real_open(path, *args, **kwargs)

        with mock.patch.object(settings_module.os, "open",
                               side_effect=refuse_directories):
            settings_module.save_settings(REAL)

        self.assertEqual(self.contents(self.config), REAL)


if __name__ == "__main__":
    unittest.main()
