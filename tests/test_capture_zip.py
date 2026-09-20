"""The zip the browser hands you has to be a zip.

Camera Tune collects its captured frames in the page and offers them as a
single archive — nothing is written to the Pi, because a sweep is a few
thousand PNGs and the card the display boots from is the one piece of storage
here already known to drop writes.

That archive is built by hand in camcal.js, which means the format is this
project's problem now. The writer is a pure function — bytes in, bytes out,
no Blob and no DOM — so node can run it and a real zip reader can check what
comes out. Skipped where node is not installed.
"""

import io
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest
import zipfile

CAMCAL = pathlib.Path(__file__).resolve().parent.parent / "server" / "static" / "camcal.js"
NODE = shutil.which("node")

# Enough of a browser for the zip writer: it uses TextEncoder (global in node)
# and nothing else.
DRIVER = """
const fs = require('fs');
const src = fs.readFileSync(%(camcal)s, 'utf8');
const grab = name => {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error(name + ' not found');
  let depth = 0, started = false;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') { depth++; started = true; }
    else if (src[j] === '}') { depth--; if (started && depth === 0) return src.slice(i, j + 1); }
  }
  throw new Error(name + ' is unbalanced');
};
const make = new Function('CC_CRC_TABLE',
  grab('ccCrc32') + '\\n' + grab('ccZipBytes') + '; return ccZipBytes;');
const ccZipBytes = make(null);
const files = JSON.parse(fs.readFileSync(%(spec)s, 'utf8')).map(f => ({
  name: f.name, bytes: Buffer.from(f.body, 'latin1'),
}));
fs.writeFileSync(%(out)s, Buffer.from(ccZipBytes(files)));
"""


@unittest.skipUnless(NODE, "node is not installed")
class CaptureZipTests(unittest.TestCase):
    def build(self, files):
        """Run camcal.js's zip writer over `files` and return the archive."""
        work = pathlib.Path(tempfile.mkdtemp(prefix="splitflap-zip-"))
        spec = work / "spec.json"
        out = work / "out.zip"
        spec.write_text(json.dumps(
            [{"name": n, "body": b.decode("latin1")} for n, b in files]),
            encoding="utf-8")
        script = DRIVER % {
            "camcal": json.dumps(str(CAMCAL)),
            "spec": json.dumps(str(spec)),
            "out": json.dumps(str(out)),
        }
        result = subprocess.run([NODE, "-e", script],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0,
                         f"zip writer failed:\n{result.stdout}\n{result.stderr}")
        return out.read_bytes()

    def test_a_session_round_trips_through_a_real_zip_reader(self):
        files = [
            ("p1_i10_m00.png", b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 3),
            ("manifest.json", json.dumps({"grid": {"rows": 3, "cols": 15}}).encode()),
        ]
        with zipfile.ZipFile(io.BytesIO(self.build(files))) as z:
            self.assertIsNone(z.testzip(), "a CRC in the archive is wrong")
            self.assertEqual(z.namelist(), [n for n, _ in files])
            for name, body in files:
                self.assertEqual(z.read(name), body)

    def test_the_manifest_survives_as_json(self):
        manifest = {"grid": {"rows": 3, "cols": 15, "count": 45},
                    "positions": [{"index": 10, "char": "J",
                                   "modules": [{"id": 0, "read": "K", "err": 1}]}]}
        body = json.dumps(manifest, indent=2).encode()
        with zipfile.ZipFile(io.BytesIO(self.build([("manifest.json", body)]))) as z:
            self.assertEqual(json.loads(z.read("manifest.json")), manifest)

    def test_a_whole_sweeps_worth_of_entries(self):
        # 45 modules x 26 positions is the real shape, and the central
        # directory has to describe every one of them.
        files = [(f"p1_i{i:02d}_m{m:02d}.png", b"\x89PNG\r\n\x1a\n" + bytes([i, m]) * 40)
                 for i in range(26) for m in range(45)]
        with zipfile.ZipFile(io.BytesIO(self.build(files))) as z:
            self.assertEqual(len(z.namelist()), 26 * 45)
            self.assertIsNone(z.testzip())
            self.assertEqual(z.read("p1_i25_m44.png"),
                             b"\x89PNG\r\n\x1a\n" + bytes([25, 44]) * 40)

    def test_binary_bytes_are_not_mangled(self):
        # Stored, not deflated — so every byte value has to pass through
        # untouched, including the ones that look like structure.
        body = bytes(range(256)) + b"PK\x03\x04" + b"\x00" * 16
        with zipfile.ZipFile(io.BytesIO(self.build([("raw.bin", body)]))) as z:
            self.assertEqual(z.read("raw.bin"), body)

    def test_an_empty_file_is_still_an_entry(self):
        with zipfile.ZipFile(io.BytesIO(self.build([("empty.png", b"")]))) as z:
            self.assertEqual(z.namelist(), ["empty.png"])
            self.assertEqual(z.read("empty.png"), b"")

    def test_entries_are_stored_rather_than_deflated(self):
        # PNGs are already compressed; deflating them again would only cost
        # time on a phone that is also running three OCR workers.
        body = b"\x89PNG\r\n\x1a\n" + bytes(200)
        with zipfile.ZipFile(io.BytesIO(self.build([("a.png", body)]))) as z:
            info = z.getinfo("a.png")
            self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
            self.assertEqual(info.file_size, len(body))


if __name__ == "__main__":
    unittest.main()
