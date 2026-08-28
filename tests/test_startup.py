"""What must be true once the server module has finished importing.

These cover the import-time wiring that the module breakup can silently
break, and that no request-level test would notice.
"""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

from support import SERVER_DIR, app  # noqa: F401  (sets up sys.path)

from splitflap.plugins import _plugin_registry
from splitflap.state import state


class LoggingSetupTests(unittest.TestCase):
    """Several modules log while being imported — transport.py reports the
    serial connection as it opens it. The first logging call installs a
    default WARNING handler, which makes a later basicConfig() a no-op and
    drops every INFO line the service writes to journalctl. Configuring it
    in splitflap/__init__ is what keeps that from happening.

    This has to run in a subprocess: the test runner installs its own root
    handler, so asserting against logging inside this process would only
    describe pytest's configuration.
    """

    def import_server(self, probe="probe-line"):
        env = dict(os.environ)
        env["SPLITFLAP_NO_BACKGROUND_TASKS"] = "1"
        env["SPLITFLAP_CONFIG"] = os.path.join(
            tempfile.mkdtemp(prefix="splitflap-log-"), "settings.json")
        result = subprocess.run(
            [sys.executable, "-c",
             f"import logging, app; logging.info({probe!r})"],
            cwd=str(SERVER_DIR), env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return result.stderr

    def test_info_logging_survives_import(self):
        self.assertIn("probe-line", self.import_server())

    def test_log_lines_are_timestamped(self):
        for line in self.import_server().splitlines():
            if "probe-line" in line:
                self.assertRegex(line, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} - ")
                return
        self.fail("probe line never logged")

    def test_import_time_messages_are_not_lost(self):
        # The plugin loader logs one INFO line per app as it loads them.
        self.assertIn("Plugin loaded:", self.import_server())

    def test_logging_is_configured_in_the_package_init(self):
        init = pathlib.Path(__import__("splitflap").__file__)
        self.assertIn("logging.basicConfig", init.read_text(encoding="utf-8"))


class ImportTimeWiringTests(unittest.TestCase):
    def test_plugins_are_loaded(self):
        self.assertTrue(_plugin_registry, "no apps registered at import")
        self.assertIn("time", _plugin_registry)

    def test_a_transport_decision_was_made(self):
        # Either hardware is attached or the server fell back to simulation;
        # never both, and never neither.
        self.assertEqual(state.sim_mode, state.ser is None)

    def test_display_buffers_match_the_configured_grid(self):
        from splitflap.grid import get_module_count
        self.assertEqual(len(state.current_indices), get_module_count())
        self.assertEqual(len(state.current_display_string), get_module_count())

    def test_background_loops_are_skipped_under_the_test_gate(self):
        from splitflap.tasks import BACKGROUND_TASKS
        self.assertFalse(BACKGROUND_TASKS)


class SupervisedLoopTests(unittest.TestCase):
    """Each loop owns a subsystem and nothing restarts it. An unhandled
    exception used to end that subsystem for the life of the process — an
    escape from the display loop freezes the sign until the service is
    restarted."""

    def test_a_crashing_loop_is_restarted(self):
        from splitflap.tasks import supervise
        calls = []

        def flaky():
            calls.append(len(calls))
            if len(calls) < 3:
                raise RuntimeError("boom")
            raise SystemExit  # break out of the supervisor for the test

        runner = supervise(flaky, delay=0)
        with self.assertRaises(SystemExit):
            runner()
        self.assertEqual(len(calls), 3, "supervisor did not resume after crashes")

    def test_a_loop_that_returns_is_also_restarted(self):
        from splitflap.tasks import supervise
        calls = []

        def returns_early():
            calls.append(1)
            if len(calls) >= 2:
                raise SystemExit
            return  # a `while True` should never do this

        runner = supervise(returns_early, delay=0)
        with self.assertRaises(SystemExit):
            runner()
        self.assertEqual(len(calls), 2)

    def test_every_long_running_loop_is_supervised(self):
        for module in ("playlist", "scheduler", "triggers", "network"):
            with self.subTest(module=module):
                src = (SERVER_DIR / "splitflap" / f"{module}.py").read_text(encoding="utf-8")
                self.assertIn("start_supervised_loop(", src)


if __name__ == "__main__":
    unittest.main()
