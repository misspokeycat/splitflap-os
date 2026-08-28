"""App registry helpers."""

import unittest

from support import SplitflapTestCase

from splitflap import plugins
from splitflap.plugins import _plugin_registry, loop_delay_for, resolve_app_id
from splitflap.settings import settings


class ResolveAppIdTests(unittest.TestCase):
    def test_strips_the_legacy_prefix(self):
        self.assertEqual(resolve_app_id("plugin_weather"), "weather")

    def test_leaves_a_bare_id_alone(self):
        self.assertEqual(resolve_app_id("weather"), "weather")

    def test_tolerates_none(self):
        self.assertIsNone(resolve_app_id(None))

    def test_only_strips_a_leading_prefix(self):
        self.assertEqual(resolve_app_id("my_plugin_thing"), "my_plugin_thing")


class LoopDelayTests(SplitflapTestCase):
    """The web UI and the MQTT app selector each had their own copy of this
    rule, and they disagreed: via MQTT an app whose manifest omits loop_delay
    got a hardcoded 5s where the web UI used global_loop_delay."""

    def register(self, app_id, manifest):
        _plugin_registry[app_id] = manifest
        self.addCleanup(_plugin_registry.pop, app_id, None)

    def test_user_setting_wins(self):
        self.register("demo_app", {"loop_delay": 30})
        settings['plugin_demo_app_loop_delay'] = '7'
        self.assertEqual(loop_delay_for("demo_app"), 7.0)

    def test_manifest_is_used_when_the_user_has_not_set_one(self):
        self.register("demo_app", {"loop_delay": 30})
        self.assertEqual(loop_delay_for("demo_app"), 30.0)

    def test_global_default_is_used_when_the_manifest_has_none(self):
        self.register("demo_app", {})
        settings['global_loop_delay'] = 12
        self.assertEqual(loop_delay_for("demo_app"), 12.0)

    def test_unknown_app_falls_back_to_the_global_default(self):
        settings['global_loop_delay'] = 9
        self.assertEqual(loop_delay_for("no-such-app"), 9.0)

    def test_animations_use_the_animation_speed(self):
        self.register("anim_demo", {"animation": True})
        settings['anim_speed'] = '0.25'
        self.assertEqual(loop_delay_for("anim_demo"), 0.25)

    def test_animation_speed_is_floored(self):
        # A zero here would spin the display loop with no delay at all.
        self.register("anim_demo", {"animation": True})
        settings['anim_speed'] = '0'
        self.assertEqual(loop_delay_for("anim_demo"), 0.1)

    def test_the_prefixed_and_bare_forms_agree(self):
        self.register("demo_app", {"loop_delay": 30})
        self.assertEqual(loop_delay_for("plugin_demo_app"), loop_delay_for("demo_app"))

    def test_both_entry_points_agree(self):
        # The regression this consolidation fixes: starting the same app from
        # Home Assistant and from the web UI must hold pages for equally long.
        self.register("demo_app", {})
        settings['global_loop_delay'] = 20

        from splitflap import mqtt
        from support import FakeMessage, state
        mqtt._mqtt_on_message(self.client, None,
                              FakeMessage(mqtt.MQTT_MODE_CMD, "demo_app"))
        over_mqtt = state.loop_delay

        import app as server
        server.app.config["TESTING"] = False
        server.app.test_client().post("/run_app", json={"app": "demo_app"})
        over_http = state.loop_delay

        self.assertEqual(over_mqtt, over_http)
        self.assertEqual(over_http, 20.0)


if __name__ == "__main__":
    unittest.main()
