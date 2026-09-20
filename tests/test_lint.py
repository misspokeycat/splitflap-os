"""Static checks over the server package.

Splitting app.py into modules turned "undefined name" from an impossible
mistake into an easy one — a helper moves and the three callers left behind
only fail when someone hits that route. pyflakes catches those in a second,
so it runs as part of the suite rather than as something to remember.
"""

import re
import subprocess
import sys
import unittest

from support import SERVER_DIR

# Lines carrying an explicit "# noqa" are intentional — app.py imports several
# modules purely so that importing it boots the server.
NOQA = "# noqa"

# pyflakes reports "path:line:col: message". Splitting that on the first colon
# takes the drive letter off a Windows path and calls it the filename, so the
# noqa lookup below found nothing and every intentional import was reported —
# the suite was red on Windows and green everywhere else. The path is
# whatever precedes the first ":<line>:", which a drive letter is not.
FINDING_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+):(?:\d+:)? ")


class PyflakesTests(unittest.TestCase):
    def findings(self):
        targets = [str(SERVER_DIR / "app.py")]
        targets += [str(p) for p in sorted((SERVER_DIR / "splitflap").rglob("*.py"))]
        result = subprocess.run(
            [sys.executable, "-m", "pyflakes", *targets],
            capture_output=True, text=True, timeout=120,
        )
        out = []
        for line in result.stdout.splitlines():
            match = FINDING_RE.match(line)
            if match is None:
                out.append(line)     # not a finding we can read: report it anyway
                continue
            try:
                with open(match["path"], encoding="utf-8") as f:
                    source = f.read().splitlines()[int(match["line"]) - 1]
            except (IndexError, OSError):
                source = ""
            if NOQA not in source:
                out.append(line)
        return out

    def test_no_undefined_or_unused_names(self):
        findings = self.findings()
        self.assertEqual(findings, [], "pyflakes findings:\n" + "\n".join(findings))


if __name__ == "__main__":
    unittest.main()
