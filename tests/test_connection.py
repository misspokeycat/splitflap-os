"""Changing the active connection.

Each of these routes closes the current transport, opens a new one, and
reports what it opened. The descriptor it reports has to be the one it just
opened, not the one the process booted with.
"""

import unittest

from support import SplitflapTestCase, app, state

from splitflap import transport
from splitflap.settings import settings


class FakePort:
    """Just enough of a pyserial handle for the routes to close it."""

    def __init__(self, name):
        self.name = name
        self.closed = False

    def close(self):
        self.closed = True


class ConnectionSwitchTests(SplitflapTestCase):
    def setUp(self):
        super().setUp()
        app.app.config["TESTING"] = False
        self.http = app.app.test_client()
        state.ser = FakePort("/dev/ttyUSB0")
        state.serial_port = "/dev/ttyUSB0"
        state.sim_mode = False
        self.patch_everywhere(
            "open_serial", lambda port=None: (FakePort(port), port))
        self.patch_everywhere(
            "open_gateway",
            lambda cfg=None: (FakePort("gw"), f"gateway:{cfg['broker']}:{cfg['port']}"))
        self.patch_everywhere("universal_firmware", _NoopFirmware())

    def test_setting_a_serial_port_reports_the_new_port(self):
        body = self.http.post("/serial_port", json={"port": "/dev/ttyACM1"}).get_json()
        self.assertEqual(body["port"], "/dev/ttyACM1")

    def test_setting_a_serial_port_updates_the_shared_state(self):
        self.http.post("/serial_port", json={"port": "/dev/ttyACM1"})
        self.assertEqual(state.serial_port, "/dev/ttyACM1")

    def test_the_port_listing_reflects_the_change(self):
        self.http.post("/serial_port", json={"port": "/dev/ttyACM1"})
        self.assertEqual(self.http.get("/serial_ports").get_json()["current"],
                         "/dev/ttyACM1")

    def test_switching_to_a_gateway_reports_the_gateway(self):
        body = self.http.post("/connection", json={
            "type": "gateway", "broker": "broker.local", "port": 1883,
        }).get_json()
        self.assertEqual(body["descriptor"], "gateway:broker.local:1883")
        self.assertEqual(state.serial_port, "gateway:broker.local:1883")

    def test_switching_back_to_serial_reports_the_serial_port(self):
        self.http.post("/connection", json={"type": "gateway", "broker": "b", "port": 1883})
        settings['serial_port'] = "/dev/ttyACM2"
        body = self.http.post("/connection", json={"type": "serial"}).get_json()
        self.assertEqual(body["descriptor"], "/dev/ttyACM2")

    def test_the_previous_transport_is_closed(self):
        old = state.ser
        self.http.post("/serial_port", json={"port": "/dev/ttyACM1"})
        self.assertTrue(old.closed, "the old port was left open")

    def test_a_failed_open_falls_back_to_simulation(self):
        self.patch_everywhere("open_serial", lambda port=None: (None, port))
        body = self.http.post("/serial_port", json={"port": "/dev/nope"}).get_json()
        self.assertTrue(body["sim_mode"])
        self.assertTrue(state.sim_mode)


class _NoopFirmware:
    def reset(self):
        pass

    def is_running(self):
        return False


if __name__ == "__main__":
    unittest.main()
