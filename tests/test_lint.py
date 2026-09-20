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


# ── The same check, for the browser ──────────────────────────

REPO_DIR = SERVER_DIR.parent
CAMCAL = SERVER_DIR / "static" / "camcal.js"
INDEX = SERVER_DIR / "templates" / "index.html"

# camcal.js prefixes everything it owns, so its own names are the ones that
# can be checked without resolving the rest of the page.
CC_NAME = re.compile(r"\bcc[A-Z]\w*")
CC_CALL = re.compile(r"\b(cc[A-Z]\w*)\s*\(")
CC_DEF = re.compile(r"^(?:async\s+)?function\s+(cc[A-Z]\w*)\s*\(|"
                    r"^const\s+(cc[A-Z]\w*)\s*=", re.M)
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)


class BrowserNameTests(unittest.TestCase):
    """camcal.js calls what it defines, and defines what it calls.

    Same failure as the one pyflakes covers above, in the half of the code
    pyflakes cannot see: a helper is deleted along with the section it lived
    in, and the callers left behind only fail when someone opens the camera.
    Both directions have gone wrong here — a function removed with the OCR
    engine while the dump still called it, and before that a seam repair
    written, tested and never wired to anything.
    """

    def setUp(self):
        self.source = CAMCAL.read_text(encoding="utf-8")
        # Comments name functions while discussing them; that is not a use.
        self.code = LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", self.source))
        self.defined = {a or b for a, b in CC_DEF.findall(self.source)}
        self.called = set(CC_CALL.findall(self.code))
        # onclick="ccFoo()" is a real call site, just not a JavaScript one.
        # Only call syntax counts: the markup is also full of ccFoo element
        # ids, which name nothing in the script.
        self.from_html = set(CC_CALL.findall(INDEX.read_text(encoding="utf-8")))

    def test_there_is_something_to_check(self):
        self.assertGreater(len(self.defined), 20)
        self.assertGreater(len(self.called), 20)

    def test_every_name_it_calls_is_defined(self):
        missing = sorted(self.called - self.defined)
        self.assertEqual(missing, [], "called in camcal.js but never defined: " + ", ".join(missing))

    def test_every_name_it_defines_is_reachable(self):
        # Reachable means mentioned somewhere other than its own definition,
        # or wired to markup. Mentioned rather than called, because a helper
        # can legitimately be passed by name — .map(ccGrayOf) is a use.
        # Anything left is a fix that was written and never connected.
        unused = []
        for name in sorted(self.defined):
            if name in self.from_html:
                continue
            if len(re.findall(r"\b" + name + r"\b", self.code)) <= 1:
                unused.append(name)
        self.assertEqual(unused, [], "defined in camcal.js but never used: " + ", ".join(unused))

    def test_the_markup_only_calls_functions_that_exist(self):
        missing = sorted(n for n in self.from_html if n not in self.defined)
        self.assertEqual(missing, [], "index.html calls missing functions: " + ", ".join(missing))


if __name__ == "__main__":
    unittest.main()
