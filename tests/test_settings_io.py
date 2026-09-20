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


class BindAddressTests(SettingsFileTestCase):
    """Where the web server listens, for putting it behind a reverse proxy.

    The port used to be written into the systemd unit, so terminating TLS in
    front of it meant editing a file the installer rewrites. It is resolved
    the same way as the serial port now: environment first, then
    settings.json, then what it has always done.
    """

    def setUp(self):
        super().setUp()
        for name in ("SPLITFLAP_HOST", "SPLITFLAP_PORT"):
            patch = mock.patch.dict(os.environ, {}, clear=False)
            patch.start()
            self.addCleanup(patch.stop)
            os.environ.pop(name, None)

    def write_settings(self, data):
        self.write(self.config, json.dumps(data))

    def test_the_default_is_what_it_has_always_been(self):
        self.write_settings(REAL)
        self.assertEqual(settings_module.get_bind_host(), "0.0.0.0")
        self.assertEqual(settings_module.get_bind_port(), 80)

    def test_settings_json_moves_the_server(self):
        self.write_settings(dict(REAL, bind_host="127.0.0.1", bind_port=8080))
        self.assertEqual(settings_module.get_bind_host(), "127.0.0.1")
        self.assertEqual(settings_module.get_bind_port(), 8080)

    def test_a_port_written_as_a_string_still_works(self):
        # Hand-edited JSON, and the settings UI posts strings.
        self.write_settings(dict(REAL, bind_port="8443"))
        self.assertEqual(settings_module.get_bind_port(), 8443)

    def test_the_environment_wins(self):
        self.write_settings(dict(REAL, bind_host="127.0.0.1", bind_port=8080))
        with mock.patch.dict(os.environ, {"SPLITFLAP_HOST": "0.0.0.0",
                                          "SPLITFLAP_PORT": "9000"}):
            self.assertEqual(settings_module.get_bind_host(), "0.0.0.0")
            self.assertEqual(settings_module.get_bind_port(), 9000)

    def test_an_empty_setting_is_not_a_setting(self):
        self.write_settings(dict(REAL, bind_host="", bind_port=""))
        self.assertEqual(settings_module.get_bind_host(), "0.0.0.0")
        self.assertEqual(settings_module.get_bind_port(), 80)

    def test_an_unusable_port_falls_back_rather_than_failing_to_start(self):
        # app.run() would raise on any of these, and systemd would restart
        # into the same failure — leaving no way to reach the display at all.
        for bad in ("http", "0", "-1", "70000", "80.5"):
            with self.subTest(port=bad):
                self.write_settings(dict(REAL, bind_port=bad))
                self.assertEqual(settings_module.get_bind_port(), 80)

    def test_an_unusable_port_in_the_environment_falls_back_too(self):
        self.write_settings(REAL)
        with mock.patch.dict(os.environ, {"SPLITFLAP_PORT": "not-a-port"}):
            self.assertEqual(settings_module.get_bind_port(), 80)

    def test_an_already_loaded_settings_dict_is_not_re_read(self):
        # open_connection threads its dict through the resolvers for this
        # reason; these have to accept the same thing.
        self.write_settings(dict(REAL, bind_port=8080))
        data = {"bind_host": "10.0.0.5", "bind_port": 5000}
        self.assertEqual(settings_module.get_bind_host(data), "10.0.0.5")
        self.assertEqual(settings_module.get_bind_port(data), 5000)


if __name__ == "__main__":
    unittest.main()
