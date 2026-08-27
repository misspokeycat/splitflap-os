"""Shared test harness for the Splitflap OS server.

Importing ``server/app.py`` normally opens the serial port, homes the
hardware, starts six background loops and connects to the MQTT broker. This
module imports it with all of that switched off and with settings pointed at
a throwaway file, so tests can never touch a real display, a real broker or
the developer's own settings.json.
"""

import os
import pathlib
import sys
import tempfile
import unittest


SERVER_DIR = pathlib.Path(__file__).resolve().parents[1] / "server"

os.environ["SPLITFLAP_NO_BACKGROUND_TASKS"] = "1"
os.environ.setdefault(
    "SPLITFLAP_CONFIG",
    os.path.join(tempfile.mkdtemp(prefix="splitflap-test-"), "settings.json"),
)

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import app  # noqa: E402

SOURCE = (SERVER_DIR / "app.py").read_text(encoding="utf-8")


class FakeMqttClient:
    """Stands in for paho's client; records what would go on the wire."""

    def __init__(self, connected=True):
        self._connected = connected
        self.published = []
        self.subscribed = []

    def is_connected(self):
        return self._connected

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def topics(self):
        return [t for t, _, _ in self.published]

    def last(self, topic):
        for t, payload, _ in reversed(self.published):
            if t == topic:
                return payload
        raise AssertionError(f"nothing was published to {topic}")

    def last_json(self, topic):
        import json
        return json.loads(self.last(topic))


class FakeMessage:
    """A paho message, minus paho."""

    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode("utf-8")


class SplitflapTestCase(unittest.TestCase):
    """Snapshots the module globals a test may mutate and puts them back.

    The server keeps its runtime state in module globals shared between the
    request handlers and the background loops, so without this every test
    leaks into the next one.
    """

    STATE = (
        "settings", "mqtt_client", "mqtt_last_text", "is_homed", "sim_mode",
        "current_indices", "current_display_string", "current_playlist",
        "last_sent_page", "active_app", "active_app_playlist",
        "app_playlist_name", "app_playlist_loop", "loop_delay", "send_raw",
        "_notify_queue", "_crypto_cache",
    )

    def setUp(self):
        self._saved = {name: getattr(app, name) for name in self.STATE}
        app.settings = dict(app.settings)
        self.client = FakeMqttClient()
        app.mqtt_client = self.client
        self.sent = []
        app.send_raw = self.sent.append

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(app, name, value)
        app.stop_event.clear()
        app.resize_grid()

    def set_grid(self, rows, cols):
        app.settings["sim_rows"] = rows
        app.settings["sim_cols"] = cols
        app.resize_grid()
