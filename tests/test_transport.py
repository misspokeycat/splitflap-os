"""Parsing the module `A` (config) response.

The documented layout is

    ver:id:serial:offset:steps:autoHome:curIdx:tunedPairs:flapCount:charMap

but the field count varies between firmware builds — the original parser
looked for flapCount at index 8 *or* 9 rather than indexing it, which is the
only surviving evidence of that. So flapCount is found, and everything else
is read at a fixed offset from it: a shift in the leading fields moves them
all together and the parse still lines up.

No sample of a real response exists in this repo, so these are built from the
documented layout. The parse is deliberately lossy — any field that does not
validate is dropped rather than guessed at.
"""

import unittest

from support import app  # noqa: F401  (sets up sys.path)

from splitflap.transport import parse_module_config

CHARS = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$&()-+=;q:%'.,/?*roygbpw"


def response(ver="31", mod_id="03", serial="A3F24C0018E7D29B3F01", offset="2832",
             steps="4096", auto_home="1", cur_idx="7", tuned="0",
             flap_count="64", char_map=CHARS, extra=None):
    """Build an A response body in the documented field order."""
    fields = [ver, mod_id, serial, offset, steps, auto_home, cur_idx, tuned]
    if extra is not None:
        fields.insert(1, extra)     # a build with one more leading field
    fields += [flap_count, char_map]
    return ":".join(fields).encode("cp1252")


class ParseTests(unittest.TestCase):
    def test_reads_the_flap_count_and_character_map(self):
        config = parse_module_config(response())
        self.assertEqual(config["flap_count"], 64)
        self.assertEqual(config["char_map"], CHARS)

    def test_reads_the_auto_home_flag(self):
        self.assertIs(parse_module_config(response(auto_home="1"))["auto_home"], True)
        self.assertIs(parse_module_config(response(auto_home="0"))["auto_home"], False)

    def test_reads_the_current_flap_index(self):
        self.assertEqual(parse_module_config(response(cur_idx="41"))["current_index"], 41)

    def test_a_character_map_containing_a_colon_survives(self):
        # The default map has ':' in it, so the map cannot be split naively.
        config = parse_module_config(response())
        self.assertIn(":", config["char_map"])
        self.assertEqual(len(config["char_map"]), 64)

    def test_an_extra_leading_field_shifts_everything_together(self):
        # This is the case the original 8-or-9 scan was working around.
        config = parse_module_config(response(extra="X", cur_idx="12", auto_home="0"))
        self.assertEqual(config["flap_count"], 64)
        self.assertEqual(config["current_index"], 12)
        self.assertIs(config["auto_home"], False)

    def test_a_tuned_pair_count_cannot_masquerade_as_the_flap_count(self):
        # With a shifted layout, tunedPairs lands where flapCount is looked
        # for. If it happens to be a plausible count the scan takes it, and
        # every field read relative to that anchor is then wrong — including
        # the flap position the display rotates from. The character map
        # describes the flaps, so the reading where the two agree is the real
        # one.
        config = parse_module_config(
            response(extra="X", tuned="5", cur_idx="7", flap_count="64"))
        self.assertEqual(config["flap_count"], 64)
        self.assertEqual(len(config["char_map"]), 64)
        self.assertEqual(config["current_index"], 7)

    def test_an_unconfirmed_anchor_yields_no_positional_fields(self):
        # If no reading has a map length matching its count, the anchor is a
        # guess; report what the old parser did and trust nothing derived.
        config = parse_module_config(response(flap_count="40"))
        self.assertEqual(config["flap_count"], 40)
        self.assertNotIn("current_index", config)
        self.assertNotIn("auto_home", config)

    # ── a wrong value is worse than a missing one ───────────────────
    def test_an_out_of_range_current_index_is_dropped(self):
        config = parse_module_config(
            response(cur_idx="99", flap_count="40", char_map=CHARS[:40]))
        self.assertNotIn("current_index", config)
        self.assertEqual(config["flap_count"], 40)

    def test_a_non_numeric_current_index_is_dropped(self):
        self.assertNotIn("current_index", parse_module_config(response(cur_idx="?")))

    def test_a_nonsense_auto_home_flag_is_dropped(self):
        self.assertNotIn("auto_home", parse_module_config(response(auto_home="7")))

    def test_an_implausible_flap_count_is_refused(self):
        self.assertEqual(parse_module_config(response(flap_count="900")), {})

    def test_a_truncated_response_is_refused(self):
        self.assertEqual(parse_module_config(b"31:03:AAAA"), {})

    def test_an_empty_response_is_refused(self):
        self.assertEqual(parse_module_config(b""), {})

    def test_a_missing_character_map_is_refused(self):
        self.assertEqual(parse_module_config(response(char_map="")), {})


if __name__ == "__main__":
    unittest.main()
