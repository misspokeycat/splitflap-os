"""Regression tests for the Home Assistant MQTT integration."""

import unittest

from support import SOURCE, FakeMessage, FakeMqttClient, SplitflapTestCase, state

from splitflap import mqtt, startup  # noqa: E402
from splitflap.grid import get_module_count
from splitflap.settings import settings


class TextCapacityTests(SplitflapTestCase):
    """The text entity used to advertise max=45 — the module count, with no
    room for the '|' separators its own name tells you to use."""

    def test_advertised_max_covers_the_line_break_characters(self):
        self.set_grid(3, 15)
        # 45 modules + 2 pipes between 3 lines
        self.assertEqual(mqtt._mqtt_text_max(), 47)

    def test_advertised_max_tracks_the_grid_size(self):
        self.set_grid(4, 20)
        self.assertEqual(mqtt._mqtt_text_max(), 83)
        self.set_grid(1, 10)
        self.assertEqual(mqtt._mqtt_text_max(), 10)

    def test_advertised_max_is_capped_at_the_home_assistant_limit(self):
        self.set_grid(20, 50)
        self.assertEqual(mqtt._mqtt_text_max(), 255)

    def test_discovery_advertises_the_computed_max(self):
        self.set_grid(3, 15)
        mqtt.mqtt_publish_discovery()
        config = self.client.last_json("homeassistant/text/splitflap_text/config")
        self.assertEqual(config["max"], 47)
        self.assertNotEqual(config["max"], get_module_count())

    def test_discovery_max_follows_a_grid_resize(self):
        self.set_grid(3, 15)
        mqtt.mqtt_publish_discovery()
        self.assertEqual(
            self.client.last_json("homeassistant/text/splitflap_text/config")["max"], 47
        )
        self.set_grid(4, 20)
        mqtt.mqtt_publish_discovery()
        self.assertEqual(
            self.client.last_json("homeassistant/text/splitflap_text/config")["max"], 83
        )

    def test_a_full_grid_message_fits_within_the_advertised_max(self):
        self.set_grid(3, 15)
        payload = "|".join("ABCDEFGHIJKLMNO" for _ in range(3))
        self.assertLessEqual(len(payload), mqtt._mqtt_text_max())
        self.assertEqual(
            mqtt._mqtt_format_text(payload), "ABCDEFGHIJKLMNO" * 3
        )


class TextLayoutTests(SplitflapTestCase):
    """'|' is a line break in both centre modes. With Center Text off the
    payload used to be passed through raw, so '|' reached the display as a
    literal flap character and the lines were never laid out."""

    def test_pipe_splits_lines_when_centered(self):
        self.set_grid(3, 15)
        settings["mqtt_center"] = True
        self.assertEqual(
            mqtt._mqtt_format_text("HI|THERE"),
            "       HI      " + "     THERE     " + " " * 15,
        )

    def test_pipe_splits_lines_when_not_centered(self):
        self.set_grid(3, 15)
        settings["mqtt_center"] = False
        rendered = mqtt._mqtt_format_text("HI|THERE")
        self.assertNotIn("|", rendered)
        self.assertEqual(
            rendered, "HI             " + "THERE          " + " " * 15
        )

    def test_rendered_page_always_fills_the_grid(self):
        self.set_grid(3, 15)
        for centered in (True, False):
            settings["mqtt_center"] = centered
            for payload in ("", "HELLO", "HI|THERE", "A|B|C",
                            "|".join("X" * 15 for _ in range(3))):
                with self.subTest(centered=centered, payload=payload):
                    self.assertEqual(
                        len(mqtt._mqtt_format_text(payload)),
                        get_module_count(),
                    )

    def test_lines_beyond_the_grid_height_are_dropped(self):
        self.set_grid(2, 5)
        # two rows of five: "CC" and "DD" have nowhere to go
        self.assertEqual(mqtt._mqtt_format_text("AA|BB|CC|DD"), "  AA   BB ")

    def test_text_command_renders_a_page_and_clears_the_active_app(self):
        self.set_grid(3, 15)
        settings["mqtt_center"] = True
        state.active_app = "time"
        state.active_app_playlist = ["time"]

        mqtt._mqtt_on_message(self.client, None, FakeMessage(mqtt.MQTT_TEXT_CMD, "HI|THERE"))

        self.assertIsNone(state.active_app)
        self.assertIsNone(state.active_app_playlist)
        self.assertEqual(state.current_playlist, [mqtt._mqtt_format_text("HI|THERE")])
        self.assertEqual(len(state.current_playlist[0]), get_module_count())


class StatePublishTests(SplitflapTestCase):
    """splitflap/text/state was declared and advertised in discovery but
    nothing ever published to it, so the entity sat permanently unknown."""

    def test_text_state_is_published(self):
        mqtt.mqtt_publish_state()
        self.assertIn(mqtt.MQTT_TEXT_STATE, self.client.topics())

    def test_text_state_reflects_the_last_command(self):
        mqtt._mqtt_on_message(self.client, None, FakeMessage(mqtt.MQTT_TEXT_CMD, "HI|THERE"))
        self.assertEqual(self.client.last(mqtt.MQTT_TEXT_STATE), "HI|THERE")

    def test_text_state_follows_a_display_change_from_elsewhere(self):
        # The state topic is retained, so a stale value is not just wrong once
        # — it is republished on every send and survives a restart.
        from splitflap.display import send_to_display
        mqtt._mqtt_on_message(self.client, None, FakeMessage(mqtt.MQTT_TEXT_CMD, "FROM MQTT"))
        send_to_display("FROM THE WEB UI")
        self.assertEqual(mqtt.state.mqtt_last_text, "FROM THE WEB UI")
        mqtt.mqtt_publish_state()
        self.assertEqual(self.client.last(mqtt.MQTT_TEXT_STATE), "FROM THE WEB UI")

    def test_homing_clears_the_text_entity(self):
        mqtt._mqtt_on_message(self.client, None, FakeMessage(mqtt.MQTT_TEXT_CMD, "HELLO"))
        mqtt._mqtt_on_message(
            self.client, None, FakeMessage(f"{mqtt.MQTT_TOPIC_PREFIX}/home/set", "PRESS"))
        self.assertEqual(self.client.last(mqtt.MQTT_TEXT_STATE), "")

    def test_state_is_retained(self):
        mqtt.mqtt_publish_state()
        retained = {t: r for t, _, r in self.client.published}
        self.assertTrue(retained[mqtt.MQTT_TEXT_STATE])
        self.assertTrue(retained[mqtt.MQTT_STATUS_STATE])

    def test_nothing_is_published_while_disconnected(self):
        state.mqtt_client = FakeMqttClient(connected=False)
        mqtt.mqtt_publish_state()
        self.assertEqual(state.mqtt_client.published, [])


class HomeButtonTests(SplitflapTestCase):
    """The home handler assigned is_homed / current_indices /
    current_display_string without a `global` declaration, so the writes
    landed on function locals and were thrown away."""

    def setUp(self):
        super().setUp()
        self.set_grid(3, 15)
        state.is_homed = False
        state.current_indices = [7] * get_module_count()
        state.current_display_string = "X" * get_module_count()

    def home(self):
        mqtt._mqtt_on_message(
            self.client, None, FakeMessage(f"{mqtt.MQTT_TOPIC_PREFIX}/home/set", "PRESS")
        )

    def test_home_command_updates_the_module_state(self):
        self.home()
        self.assertTrue(state.is_homed)
        self.assertEqual(state.current_indices, [0] * get_module_count())
        self.assertEqual(state.current_display_string, " " * get_module_count())

    def test_home_command_sends_the_home_instruction(self):
        self.home()
        self.assertEqual(self.sent, ["m**h"])

    def test_home_command_stops_the_running_app(self):
        state.active_app = "time"
        state.active_app_playlist = ["time"]
        self.home()
        self.assertIsNone(state.active_app)
        self.assertIsNone(state.active_app_playlist)

    def test_home_command_interrupts_the_display_loop(self):
        # _run_app_playlist captures the entry list into a local and loops over
        # it. Clearing state.active_app_playlist does not reach it, so without
        # stop_event the playlist keeps driving the display after homing while
        # the UI and Home Assistant both report nothing playing.
        state.active_app_playlist = ["time"]
        state.stop_event.clear()
        self.home()
        self.assertTrue(state.stop_event.is_set())


class AutoHomeSettingTests(SplitflapTestCase):
    """Auto-home is a module firmware mode, not something the server does.

    When it is on, each module homes itself as it powers up. The flag lives in
    module RAM and is lost on every power cycle, so the server re-asserts it at
    startup — that is the whole job. An earlier version of this also sent
    "m**h", which homed all 45 modules on every service restart: a twelve
    second flap storm for something the modules had already done themselves.
    """

    def setUp(self):
        super().setUp()
        self.set_grid(3, 15)
        state.is_homed = False
        state.current_indices = [7] * get_module_count()
        state.current_display_string = "X" * get_module_count()

    def test_the_flag_is_asserted_when_enabled(self):
        settings['auto_home'] = True
        self.assertTrue(startup.apply_auto_home_setting())
        self.assertEqual(self.sent, ["m**a1"])

    def test_the_flag_is_cleared_when_disabled(self):
        settings['auto_home'] = False
        self.assertFalse(startup.apply_auto_home_setting())
        self.assertEqual(self.sent, ["m**a0"])

    def test_nothing_is_homed_on_startup(self):
        settings['auto_home'] = True
        startup.apply_auto_home_setting()
        self.assertNotIn("m**h", self.sent)

    def test_the_display_state_is_left_alone(self):
        # The server cannot know whether the modules power-cycled, so it must
        # not claim to know where the flaps are.
        settings['auto_home'] = True
        startup.apply_auto_home_setting()
        self.assertFalse(state.is_homed)
        self.assertEqual(state.current_display_string, "X" * get_module_count())
        self.assertEqual(state.current_indices, [7] * get_module_count())

    def test_it_is_registered_as_a_startup_task(self):
        self.assertIn("start_background_task(_startup_auto_home)", SOURCE)


class ConnectSequenceTests(SplitflapTestCase):
    def test_connect_callback_publishes_availability_discovery_and_state(self):
        mqtt._mqtt_on_connect(self.client, None, {}, 0)
        topics = self.client.topics()
        self.assertIn(mqtt.MQTT_AVAIL_TOPIC, topics)
        self.assertIn("homeassistant/text/splitflap_text/config", topics)
        self.assertIn("homeassistant/button/splitflap_home/config", topics)
        self.assertIn(mqtt.MQTT_STATUS_STATE, topics)
        self.assertIn(mqtt.MQTT_TEXT_CMD, self.client.subscribed)
        self.assertIn(f"{mqtt.MQTT_TOPIC_PREFIX}/home/set", self.client.subscribed)

    def test_discovery_lists_the_installed_apps(self):
        # mqtt_setup() used to run at import time, above the plugin registry
        # and the playlist globals. A fast broker could fire on_connect before
        # they existed; the NameError killed the whole discovery publish and
        # no entities appeared in Home Assistant at all.
        #
        # Both halves are now structurally safe — that state lives in
        # splitflap.state and splitflap.plugins, which Python fully executes
        # before app.py continues. What is worth asserting is the symptom:
        # a connect that publishes a real app list, not an empty one.
        mqtt._mqtt_on_connect(self.client, None, {}, 0)
        config = self.client.last_json("homeassistant/select/splitflap_app/config")
        self.assertIn("time", config["options"])
        self.assertGreater(len(config["options"]), 1)

    def test_discovery_options_read_the_populated_plugin_registry(self):
        # The failure mode this guards: discovery publishing an empty app list.
        self.assertIn("time", mqtt._get_mqtt_app_options())

    def test_setup_is_skipped_when_mqtt_is_disabled(self):
        settings["mqtt_enabled"] = False
        state.mqtt_client = None
        mqtt.mqtt_setup()
        self.assertIsNone(state.mqtt_client)

    def test_setup_does_not_connect_when_the_setting_is_absent(self):
        settings.pop("mqtt_enabled", None)
        state.mqtt_client = None
        mqtt.mqtt_setup()
        self.assertIsNone(state.mqtt_client)


if __name__ == "__main__":
    unittest.main()
