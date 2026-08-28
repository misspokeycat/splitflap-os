"""Static checks over the server package.

Splitting app.py into modules turned "undefined name" from an impossible
mistake into an easy one — a helper moves and the three callers left behind
only fail when someone hits that route. pyflakes catches those in a second,
so it runs as part of the suite rather than as something to remember.
"""

import subprocess
import sys
import unittest

from support import SERVER_DIR

# Lines carrying an explicit "# noqa" are intentional — app.py imports several
# modules purely so that importing it boots the server.
NOQA = "# noqa"


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
            path, _, rest = line.partition(":")
            lineno = rest.split(":")[0]
            try:
                source = open(path, encoding="utf-8").read().splitlines()[int(lineno) - 1]
            except (ValueError, IndexError, OSError):
                source = ""
            if NOQA not in source:
                out.append(line)
        return out

    def test_no_undefined_or_unused_names(self):
        findings = self.findings()
        self.assertEqual(findings, [], "pyflakes findings:\n" + "\n".join(findings))


if __name__ == "__main__":
    unittest.main()
