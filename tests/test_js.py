"""Run the browser-side tests as part of the suite.

app.js is where the display's state meets a 2.5-second poll, which is a
reliable source of bugs that never reach Python — a value being typed gets
overwritten by a re-render, and nothing server-side notices. These run under
node against a minimal DOM stub, and are skipped where node is not installed
rather than failing.
"""

import pathlib
import shutil
import subprocess
import unittest

JS_DIR = pathlib.Path(__file__).resolve().parent / "js"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed")
class JavaScriptTests(unittest.TestCase):
    def run_suite(self, name):
        result = subprocess.run(
            [NODE, str(JS_DIR / name)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(
            result.returncode, 0,
            f"{name} failed:\n{result.stdout}\n{result.stderr}",
        )
        return result.stdout

    def test_provisioning_input_survives_the_poll(self):
        output = self.run_suite("test_provisioning.js")
        self.assertNotIn("FAIL", output)
        self.assertIn("PASS", output)

    def test_camera_tuning_maths(self):
        output = self.run_suite("test_camcal.js")
        self.assertNotIn("FAIL", output)
        self.assertIn("PASS", output)


if __name__ == "__main__":
    unittest.main()
