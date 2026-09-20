"""Installing and uninstalling apps.

The install route takes an app id from the request and builds a filesystem
path out of it, then removes that path again if the download fails. Anything
other than a plain app id has to be refused before either of those happen.
"""

import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from support import SplitflapTestCase, app, state

from splitflap.plugins import _plugin_registry, is_valid_app_id
from splitflap.settings import settings


class AppIdValidationTests(unittest.TestCase):
    def test_accepts_the_ids_the_shipped_apps_use(self):
        for app_id in ("time", "world_clock", "word-clock", "anim_checker",
                       "planes_overhead", "word-of-the-day", "on-this-day"):
            with self.subTest(app_id=app_id):
                self.assertTrue(is_valid_app_id(app_id))

    def test_rejects_path_separators(self):
        for app_id in ("../etc", "a/b", "a\\b", "/abs", "./rel"):
            with self.subTest(app_id=app_id):
                self.assertFalse(is_valid_app_id(app_id))

    def test_rejects_traversal_and_dot_names(self):
        for app_id in ("..", ".", "...", "..%2f.."):
            with self.subTest(app_id=app_id):
                self.assertFalse(is_valid_app_id(app_id))

    def test_rejects_empty_and_whitespace(self):
        for app_id in ("", "   ", "a b", "a\tb", "a\nb"):
            with self.subTest(app_id=app_id):
                self.assertFalse(is_valid_app_id(app_id))

    def test_rejects_null_bytes_and_leading_dashes(self):
        self.assertFalse(is_valid_app_id("a\x00b"))
        self.assertFalse(is_valid_app_id("-rf"))


class InstallPathTests(SplitflapTestCase):
    """The install route runs as root on the Pi and the web UI has no
    authentication, so an id that escapes the apps directory lets any request
    on the LAN create directories anywhere on the filesystem, and write the
    three fetched filenames into them.

    An id naming a directory that already exists skips the download block
    entirely, so pre-existing directories are never removed — only ones the
    request itself just created."""

    def setUp(self):
        super().setUp()
        app.app.config["TESTING"] = False
        self.http = app.app.test_client()

        self.root = pathlib.Path(tempfile.mkdtemp(prefix="splitflap-install-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.apps = self.root / "apps"
        self.apps.mkdir()
        self.bystander = self.root / "bystander"
        self.bystander.mkdir()
        (self.bystander / "keep-me.txt").write_text("important")

        self.patch_everywhere("APPS_PATH", str(self.apps))
        # No network in tests: every download attempt fails, which is the path
        # that reaches shutil.rmtree.
        patcher = mock.patch("urllib.request.urlopen", side_effect=OSError("offline"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def install(self, app_id):
        return self.http.post("/app_library/install", json={"id": app_id})

    def test_an_id_that_escapes_the_apps_directory_is_refused(self):
        response = self.install("../bystander")
        self.assertEqual(response.status_code, 400)

    def test_no_directory_is_created_outside_apps(self):
        self.install("../made-up/nested")
        self.assertFalse((self.root / "made-up").exists(),
                         "a directory was created outside apps/")
        self.assertEqual(list(self.apps.iterdir()), [])

    def test_an_absolute_id_is_refused(self):
        self.assertEqual(self.install(str(self.bystander)).status_code, 400)
        self.assertTrue(self.bystander.exists())

    def test_a_refused_id_is_not_recorded_as_installed(self):
        self.install("../bystander")
        self.assertNotIn("../bystander", settings.get('installed_apps', []))

    def test_a_failed_download_cleans_up_its_own_directory(self):
        self.install("brand-new-app")
        self.assertFalse((self.apps / "brand-new-app").exists())

    def test_uninstall_refuses_a_bad_id(self):
        response = self.http.post("/app_library/uninstall", json={"id": "../bystander"})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
