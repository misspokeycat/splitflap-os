"""Shared test harness for the Splitflap OS server.

Importing ``server/app.py`` normally opens the serial port, homes the
hardware, starts six background loops and connects to the MQTT broker. This
module imports it with all of that switched off and with settings pointed at
a throwaway file, so tests can never touch a real display, a real broker or
the developer's own settings.json.
"""

import copy
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
from splitflap.settings import settings  # noqa: E402
from splitflap.state import resize_grid, state  # noqa: E402

# Every line of server source, concatenated. A few tests assert on structure
# that cannot be observed at runtime (what runs at import, and in what order);
# spanning the whole package keeps them working when code moves between files.
SOURCE = "\n".join(
    path.read_text(encoding="utf-8")
    for path in [SERVER_DIR / "app.py"] + sorted((SERVER_DIR / "splitflap").rglob("*.py"))
)


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
    """Snapshots the shared runtime state a test may mutate and puts it back.

    The display loop, the scheduler and the request handlers all read and
    write one RuntimeState instance, so without this every test leaks into
    the next one.
    """

    def _splitflap_modules(self):
        """app plus every splitflap module currently imported."""
        return [app] + [
            mod for name, mod in list(sys.modules.items())
            if mod is not None and (name == "splitflap" or name.startswith("splitflap."))
        ]

    def patch_everywhere(self, attr, value):
        """Rebind a name in every module that imported it.

        Modules do `from splitflap.transport import send_raw`, which binds the
        function object at import time. Patching a single module's copy leaves
        every other module calling the real one — which, for send_raw, means
        writing to the serial port during a test.
        """
        for mod in self._splitflap_modules():
            if hasattr(mod, attr):
                self._patched.append((mod, attr, getattr(mod, attr)))
                setattr(mod, attr, value)

    def setUp(self):
        self._patched = []
        self._saved_state = {
            k: copy.copy(v) if isinstance(v, (list, dict, set)) else v
            for k, v in vars(state).items()
        }
        # settings is one dict shared by every module, so it has to be
        # snapshotted and restored in place — rebinding it would leave
        # splitflap.grid and friends reading the original.
        self._saved_settings = copy.deepcopy(settings)
        self.client = FakeMqttClient()
        state.mqtt_client = self.client
        self.sent = []
        self.patch_everywhere("send_raw", self.sent.append)

    def tearDown(self):
        vars(state).clear()
        vars(state).update(self._saved_state)
        for mod, attr, original in reversed(self._patched):
            setattr(mod, attr, original)
        settings.clear()
        settings.update(self._saved_settings)
        state.stop_event.clear()
        resize_grid()

    def set_grid(self, rows, cols):
        settings["sim_rows"] = rows
        settings["sim_cols"] = cols
        resize_grid()
