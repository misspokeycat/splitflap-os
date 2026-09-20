"""Preserving a unit file's Environment= settings across an update.

install.sh rewrites /etc/systemd/system/splitflap.service on every run, so
anything configured there has to be carried over from the installed copy. It
was keyed on the literal "Environment" rather than on the variable name, which
made every variable collide: a unit holding both SPLITFLAP_CONFIG and a
hand-added variable came out of an update with only one of them. Losing
SPLITFLAP_CONFIG points the server at a fresh settings.json — the calibration
of 45 modules, gone from view on a machine where it exists nowhere else.

The function is extracted from install.sh and run on its own, the way the
browser tests pull a function out of app.js, so the rest of the installer does
not have to run. Skipped where bash is not available.
"""

import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

SETUP = pathlib.Path(__file__).resolve().parent.parent / "setup"
INSTALL_SH = SETUP / "install.sh"
BASH = shutil.which("bash")

TEMPLATE = """\
[Unit]
Description=Splitflap OS Web Server

[Service]
Type=simple
WorkingDirectory=/opt/splitflap-os/server
ExecStart=/opt/splitflap-os/venv/bin/python app.py
Environment=SPLITFLAP_CONFIG=/opt/splitflap-os/server/settings.json

[Install]
WantedBy=multi-user.target
"""


def extract_install_service():
    """Pull install_service() out of install.sh by brace matching."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    start = text.index("install_service() {")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError("install_service() is unbalanced in install.sh")


@unittest.skipUnless(BASH, "bash is not installed")
class InstallServiceTests(unittest.TestCase):
    def install(self, template, installed=None, repo_dir="/srv/splitflap-os"):
        """Run install_service once and return the resulting unit file."""
        work = pathlib.Path(tempfile.mkdtemp(prefix="splitflap-unit-"))
        src = work / "template.service"
        dest = work / "installed.service"
        src.write_text(template, encoding="utf-8")
        if installed is not None:
            dest.write_text(installed, encoding="utf-8")

        script = textwrap.dedent(f"""\
            set -e
            REPO_DIR={repo_dir!r}
            {extract_install_service()}
            install_service {str(src)!r} {str(dest)!r}
        """)
        result = subprocess.run([BASH, "-c", script],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0,
                         f"install_service failed:\n{result.stdout}\n{result.stderr}")
        return dest.read_text(encoding="utf-8")

    def env_lines(self, unit):
        return [l for l in unit.splitlines() if l.startswith("Environment=")]

    def test_a_fresh_install_takes_the_template(self):
        unit = self.install(TEMPLATE)
        self.assertIn("ExecStart=/srv/splitflap-os/venv/bin/python app.py", unit)
        self.assertEqual(
            self.env_lines(unit),
            ["Environment=SPLITFLAP_CONFIG=/srv/splitflap-os/server/settings.json"])

    def test_an_edited_value_survives_the_update(self):
        installed = TEMPLATE.replace(
            "Environment=SPLITFLAP_CONFIG=/opt/splitflap-os/server/settings.json",
            "Environment=SPLITFLAP_CONFIG=/mnt/usb/settings.json")
        unit = self.install(TEMPLATE, installed)
        self.assertEqual(self.env_lines(unit),
                         ["Environment=SPLITFLAP_CONFIG=/mnt/usb/settings.json"])

    def test_the_template_still_supplies_everything_else(self):
        # Preserving Environment= must not pin the rest of the unit to the old
        # copy; an update is how ExecStart changes.
        installed = TEMPLATE.replace("python app.py",
                                     'python -c "import app; app.app.run()"')
        unit = self.install(TEMPLATE, installed)
        self.assertIn("ExecStart=/srv/splitflap-os/venv/bin/python app.py", unit)
        self.assertNotIn("import app; app.app.run()", unit)

    def test_two_variables_do_not_collide(self):
        # The reported bug: both were rewritten to whichever was read last, so
        # SPLITFLAP_CONFIG was replaced by the port and then lost.
        installed = TEMPLATE.replace(
            "[Install]", "Environment=SPLITFLAP_PORT=8080\n\n[Install]")
        unit = self.install(TEMPLATE, installed)
        self.assertEqual(sorted(self.env_lines(unit)), [
            "Environment=SPLITFLAP_CONFIG=/opt/splitflap-os/server/settings.json",
            "Environment=SPLITFLAP_PORT=8080",
        ])

    def test_a_variable_the_template_does_not_know_is_kept(self):
        installed = TEMPLATE.replace(
            "[Install]", "Environment=SPLITFLAP_HOST=127.0.0.1\n\n[Install]")
        unit = self.install(TEMPLATE, installed)
        self.assertIn("Environment=SPLITFLAP_HOST=127.0.0.1", unit)

    def test_a_carried_over_variable_lands_in_the_service_section(self):
        # Environment= means nothing under [Install]; appending at the end of
        # the file would look preserved and do nothing.
        installed = TEMPLATE.replace(
            "[Install]", "Environment=SPLITFLAP_PORT=8080\n\n[Install]")
        unit = self.install(TEMPLATE, installed)
        lines = unit.splitlines()
        self.assertLess(lines.index("Environment=SPLITFLAP_PORT=8080"),
                        lines.index("[Install]"))

    def test_a_unit_ending_in_the_service_section_still_gets_them(self):
        template = TEMPLATE.split("[Install]")[0].rstrip() + "\n"
        installed = template + "Environment=SPLITFLAP_PORT=8080\n"
        unit = self.install(template, installed)
        self.assertIn("Environment=SPLITFLAP_PORT=8080", unit)

    def test_values_containing_shell_and_sed_metacharacters_survive(self):
        # The old version substituted the value through sed, where | ends the
        # expression and & expands to the whole match.
        nasty = "Environment=SPLITFLAP_GATEWAY_PASSWORD=a|b&c$d\\e'f"
        installed = TEMPLATE.replace("[Install]", nasty + "\n\n[Install]")
        unit = self.install(TEMPLATE, installed)
        self.assertIn(nasty, unit)

    def test_a_commented_or_malformed_line_is_not_preserved(self):
        installed = TEMPLATE.replace(
            "[Install]", "#Environment=SPLITFLAP_PORT=8080\nEnvironment=\n\n[Install]")
        unit = self.install(TEMPLATE, installed)
        self.assertNotIn("SPLITFLAP_PORT", unit)
        self.assertEqual(
            self.env_lines(unit),
            ["Environment=SPLITFLAP_CONFIG=/opt/splitflap-os/server/settings.json"])

    def test_repeated_updates_do_not_accumulate_duplicates(self):
        installed = TEMPLATE.replace(
            "[Install]", "Environment=SPLITFLAP_PORT=8080\n\n[Install]")
        for _ in range(3):
            installed = self.install(TEMPLATE, installed)
        self.assertEqual(sorted(self.env_lines(installed)), [
            "Environment=SPLITFLAP_CONFIG=/opt/splitflap-os/server/settings.json",
            "Environment=SPLITFLAP_PORT=8080",
        ])

    def test_the_real_unit_file_round_trips(self):
        # The shipped unit, installed once, then given a port by hand and
        # updated — which is what putting this behind a reverse proxy looks
        # like. Paths are substituted at first install, so the copy on disk
        # for the second run already carries them.
        real = (SETUP / "splitflap.service").read_text(encoding="utf-8")
        first = self.install(real)
        edited = first.replace(
            "[Install]", "Environment=SPLITFLAP_PORT=8080\n\n[Install]")
        unit = self.install(real, edited)

        self.assertIn("Environment=SPLITFLAP_PORT=8080", unit)
        self.assertIn("Environment=SPLITFLAP_CONFIG=/srv/splitflap-os/server/settings.json",
                      unit)
        self.assertIn("ExecStart=/srv/splitflap-os/venv/bin/python app.py", unit)

    def test_a_preserved_value_is_kept_verbatim(self):
        # Preservation means the operator's value, not a re-derived one: a
        # config path pointing outside the repo is deliberate, and rewriting
        # it to follow REPO_DIR would silently move the server off the
        # settings.json holding every module's calibration.
        installed = TEMPLATE.replace(
            "Environment=SPLITFLAP_CONFIG=/opt/splitflap-os/server/settings.json",
            "Environment=SPLITFLAP_CONFIG=/opt/splitflap-os/server/settings.json".replace(
                "/opt/splitflap-os/server", "/mnt/usb"))
        unit = self.install(TEMPLATE, installed, repo_dir="/srv/splitflap-os")
        self.assertIn("Environment=SPLITFLAP_CONFIG=/mnt/usb/settings.json", unit)
        self.assertIn("WorkingDirectory=/srv/splitflap-os/server", unit)


if __name__ == "__main__":
    unittest.main()
