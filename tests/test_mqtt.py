"""Regression tests for the Home Assistant MQTT integration."""

import unittest

from support import (
    SOURCE,
    FakeMessage,
    FakeMqttClient,
    SplitflapTestCase,
    app,
    state,
)


class TextCapacityTests(SplitflapTestCase):
    """The text entity used to advertise max=45 — the module count, with no
    room for the '|' separators its own name tells you to use."""

    def test_advertised_max_covers_the_line_break_characters(self):
        self.set_grid(3, 15)
        # 45 modules + 2 pipes between 3 lines
        self.assertEqual(app._mqtt_text_max(), 47)

    def test_advertised_max_tracks_the_grid_size(self):
        self.set_grid(4, 20)
        self.assertEqual(app._mqtt_text_max(), 83)
        self.set_grid(1, 10)
        self.assertEqual(app._mqtt_text_max(), 10)

    def test_advertised_max_is_capped_at_the_home_assistant_limit(self):
        self.set_grid(20, 50)
        self.assertEqual(app._mqtt_text_max(), 255)

    def test_discovery_advertises_the_computed_max(self):
        self.set_grid(3, 15)
        app.mqtt_publish_discovery()
        config = self.client.last_json("homeassistant/text/splitflap_text/config")
        self.assertEqual(config["max"], 47)
        self.assertNotEqual(config["max"], app.get_module_count())

    def test_discovery_max_follows_a_grid_resize(self):
        self.set_grid(3, 15)
        app.mqtt_publish_discovery()
        self.assertEqual(
            self.client.last_json("homeassistant/text/splitflap_text/config")["max"], 47
        )
        self.set_grid(4, 20)
        app.mqtt_publish_discovery()
        self.assertEqual(
            self.client.last_json("homeassistant/text/splitflap_text/config")["max"], 83
        )

    def test_a_full_grid_message_fits_within_the_advertised_max(self):
        self.set_grid(3, 15)
        payload = "|".join("ABCDEFGHIJKLMNO" for _ in range(3))
        self.assertLessEqual(len(payload), app._mqtt_text_max())
        self.assertEqual(
            app._mqtt_format_text(payload), "ABCDEFGHIJKLMNO" * 3
        )


class TextLayoutTests(SplitflapTestCase):
    """'|' is a line break in both centre modes. With Center Text off the
    payload used to be passed through raw, so '|' reached the display as a
    literal flap character and the lines were never laid out."""

    def test_pipe_splits_lines_when_centered(self):
        self.set_grid(3, 15)
        app.settings["mqtt_center"] = True
        self.assertEqual(
            app._mqtt_format_text("HI|THERE"),
            "       HI      " + "     THERE     " + " " * 15,
        )

    def test_pipe_splits_lines_when_not_centered(self):
        self.set_grid(3, 15)
        app.settings["mqtt_center"] = False
        rendered = app._mqtt_format_text("HI|THERE")
        self.assertNotIn("|", rendered)
        self.assertEqual(
            rendered, "HI             " + "THERE          " + " " * 15
        )

    def test_rendered_page_always_fills_the_grid(self):
        self.set_grid(3, 15)
        for centered in (True, False):
            app.settings["mqtt_center"] = centered
            for payload in ("", "HELLO", "HI|THERE", "A|B|C",
                            "|".join("X" * 15 for _ in range(3))):
                with self.subTest(centered=centered, payload=payload):
                    self.assertEqual(
                        len(app._mqtt_format_text(payload)),
                        app.get_module_count(),
                    )

    def test_lines_beyond_the_grid_height_are_dropped(self):
        self.set_grid(2, 5)
        # two rows of five: "CC" and "DD" have nowhere to go
        self.assertEqual(app._mqtt_format_text("AA|BB|CC|DD"), "  AA   BB ")

    def test_text_command_renders_a_page_and_clears_the_active_app(self):
        self.set_grid(3, 15)
        app.settings["mqtt_center"] = True
        state.active_app = "time"
        state.active_app_playlist = ["time"]

        app._mqtt_on_message(self.client, None, FakeMessage(app.MQTT_TEXT_CMD, "HI|THERE"))

        self.assertIsNone(state.active_app)
        self.assertIsNone(state.active_app_playlist)
        self.assertEqual(state.current_playlist, [app._mqtt_format_text("HI|THERE")])
        self.assertEqual(len(state.current_playlist[0]), app.get_module_count())


class StatePublishTests(SplitflapTestCase):
    """splitflap/text/state was declared and advertised in discovery but
    nothing ever published to it, so the entity sat permanently unknown."""

    def test_text_state_is_published(self):
        app.mqtt_publish_state()
        self.assertIn(app.MQTT_TEXT_STATE, self.client.topics())

    def test_text_state_reflects_the_last_command(self):
        app._mqtt_on_message(self.client, None, FakeMessage(app.MQTT_TEXT_CMD, "HI|THERE"))
        self.assertEqual(self.client.last(app.MQTT_TEXT_STATE), "HI|THERE")

    def test_state_is_retained(self):
        app.mqtt_publish_state()
        retained = {t: r for t, _, r in self.client.published}
        self.assertTrue(retained[app.MQTT_TEXT_STATE])
        self.assertTrue(retained[app.MQTT_STATUS_STATE])

    def test_nothing_is_published_while_disconnected(self):
        state.mqtt_client = FakeMqttClient(connected=False)
        app.mqtt_publish_state()
        self.assertEqual(state.mqtt_client.published, [])


class HomeButtonTests(SplitflapTestCase):
    """The home handler assigned is_homed / current_indices /
    current_display_string without a `global` declaration, so the writes
    landed on function locals and were thrown away."""

    def setUp(self):
        super().setUp()
        self.set_grid(3, 15)
        state.is_homed = False
        state.current_indices = [7] * app.get_module_count()
        state.current_display_string = "X" * app.get_module_count()

    def home(self):
        app._mqtt_on_message(
            self.client, None, FakeMessage(f"{app.MQTT_TOPIC_PREFIX}/home/set", "PRESS")
        )

    def test_home_command_updates_the_module_state(self):
        self.home()
        self.assertTrue(state.is_homed)
        self.assertEqual(state.current_indices, [0] * app.get_module_count())
        self.assertEqual(state.current_display_string, " " * app.get_module_count())

    def test_home_command_sends_the_home_instruction(self):
        self.home()
        self.assertEqual(self.sent, ["m**h"])

    def test_home_command_stops_the_running_app(self):
        state.active_app = "time"
        state.active_app_playlist = ["time"]
        self.home()
        self.assertIsNone(state.active_app)
        self.assertIsNone(state.active_app_playlist)


class AutoHomeOnBootTests(SplitflapTestCase):
    """auto_home was only ever read by /toggle_autohome; nothing consulted it
    at startup, so 'Auto-Home on Boot' did nothing."""

    def setUp(self):
        super().setUp()
        self.set_grid(3, 15)
        state.is_homed = False
        state.current_indices = [7] * app.get_module_count()
        state.current_display_string = "X" * app.get_module_count()

    def test_homes_when_enabled(self):
        app.settings["auto_home"] = True
        self.assertTrue(app.apply_auto_home_on_boot())
        self.assertEqual(self.sent, ["m**a1", "m**h"])
        self.assertTrue(state.is_homed)
        self.assertEqual(state.current_indices, [0] * app.get_module_count())
        self.assertEqual(state.current_display_string, " " * app.get_module_count())

    def test_reasserts_the_firmware_flag_without_homing_when_disabled(self):
        app.settings["auto_home"] = False
        self.assertFalse(app.apply_auto_home_on_boot())
        self.assertEqual(self.sent, ["m**a0"])
        self.assertFalse(state.is_homed)
        self.assertEqual(state.current_display_string, "X" * app.get_module_count())

    def test_homing_is_reported_over_mqtt(self):
        app.settings["auto_home"] = True
        app.apply_auto_home_on_boot()
        self.assertEqual(
            self.client.last(app.MQTT_STATUS_STATE), " " * app.get_module_count()
        )

    def test_auto_home_is_registered_as_a_startup_task(self):
        # Source-level guard: the reported bug was that no startup task
        # consulted the setting at all.
        self.assertIn("_start_background_task(_startup_auto_home)", SOURCE)


class ConnectSequenceTests(SplitflapTestCase):
    def test_connect_callback_publishes_availability_discovery_and_state(self):
        app._mqtt_on_connect(self.client, None, {}, 0)
        topics = self.client.topics()
        self.assertIn(app.MQTT_AVAIL_TOPIC, topics)
        self.assertIn("homeassistant/text/splitflap_text/config", topics)
        self.assertIn("homeassistant/button/splitflap_home/config", topics)
        self.assertIn(app.MQTT_STATUS_STATE, topics)
        self.assertIn(app.MQTT_TEXT_CMD, self.client.subscribed)
        self.assertIn(f"{app.MQTT_TOPIC_PREFIX}/home/set", self.client.subscribed)

    def test_broker_connects_after_the_state_it_publishes_is_defined(self):
        # mqtt_setup() used to run at import time, above the plugin registry
        # and the playlist globals. A fast broker could fire on_connect before
        # they existed, and the resulting NameError killed the whole discovery
        # publish, so no entities appeared in Home Assistant at all.
        #
        # The playlist half is now structurally safe: that state lives on
        # splitflap.state, imported at the top of app.py. The plugin registry
        # is still module-level, so it still needs guarding.
        connect_at = SOURCE.index("if BACKGROUND_TASKS:\n    mqtt_setup()")
        for definition in ("\n_plugin_registry = {}", "\nload_installed_plugins()"):
            self.assertLess(
                SOURCE.index(definition), connect_at,
                f"mqtt_setup() runs before {definition.strip()}",
            )

    def test_discovery_options_read_the_populated_plugin_registry(self):
        # The failure mode this guards: discovery publishing an empty app list.
        self.assertIn("time", app._get_mqtt_app_options())

    def test_setup_is_skipped_when_mqtt_is_disabled(self):
        app.settings["mqtt_enabled"] = False
        state.mqtt_client = None
        app.mqtt_setup()
        self.assertIsNone(state.mqtt_client)

    def test_setup_does_not_connect_when_the_setting_is_absent(self):
        app.settings.pop("mqtt_enabled", None)
        state.mqtt_client = None
        app.mqtt_setup()
        self.assertIsNone(state.mqtt_client)


if __name__ == "__main__":
    unittest.main()
