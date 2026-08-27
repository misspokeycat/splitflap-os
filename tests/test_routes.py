"""Coverage for the HTTP surface.

These tests exist to make the app.py breakup safe. Nothing here asserts much
about behaviour — the point is that all 58 endpoints keep existing, keep their
methods and endpoint names, and keep answering without blowing up, however the
code behind them is rearranged.
"""

import unittest
from unittest import mock

from support import SplitflapTestCase, app, state


# Frozen inventory of the HTTP surface: (rule, endpoint, methods).
# Moving a route between modules must not change any of these. Adding a route
# means adding a line here deliberately.
EXPECTED_ROUTES = [
    ("/", "index", "GET"),
    ("/app_library", "app_library", "GET"),
    ("/app_library/install", "app_library_install", "POST"),
    ("/app_library/uninstall", "app_library_uninstall", "POST"),
    ("/app_playlists", "app_playlists", "GET,POST"),
    ("/app_playlists/<path:name>", "delete_app_playlist", "DELETE"),
    ("/apply_update", "apply_update", "POST"),
    ("/assign_id", "assign_id", "POST"),
    ("/auto_tune", "auto_tune_route", "POST"),
    ("/backup_settings", "backup_settings", "GET"),
    ("/check_update", "check_update", "GET"),
    ("/connection", "connection_config", "GET,POST"),
    ("/crypto_search", "crypto_search_route", "GET"),
    ("/current_state", "current_state", "GET"),
    ("/custom_tune", "custom_tune", "POST"),
    ("/grid_config", "grid_config", "GET"),
    ("/home_all", "home_all", "GET"),
    ("/installed_apps", "installed_apps", "GET"),
    ("/location_search", "location_search_route", "GET"),
    ("/location_timezone", "location_timezone_route", "GET"),
    ("/mqtt_reconnect", "mqtt_reconnect_route", "POST"),
    ("/network_config", "network_config", "POST"),
    ("/network_status", "network_status", "GET"),
    ("/notify", "notify_clear", "DELETE"),
    ("/notify", "notify_list", "GET"),
    ("/notify", "notify_push", "POST"),
    ("/playlists", "playlists", "GET,POST"),
    ("/playlists/<path:name>", "delete_playlist", "DELETE"),
    ("/restore_settings", "restore_settings", "POST"),
    ("/run_app", "run_app", "POST"),
    ("/run_app_playlist", "run_app_playlist", "POST"),
    ("/schedule_tick", "schedule_tick_route", "POST"),
    ("/schedules", "schedules_route", "GET,POST"),
    ("/serial_port", "set_serial_port", "POST"),
    ("/serial_ports", "list_serial_ports", "GET"),
    ("/settings", "handle_settings", "GET,POST"),
    ("/sports_follow", "sports_follow", "POST"),
    ("/sports_leagues", "sports_leagues_route", "GET"),
    ("/sports_teams/<league_key>", "sports_teams_route", "GET"),
    ("/stocks_search", "stocks_search_route", "GET"),
    ("/stop_app", "stop_app", "POST"),
    ("/sync_all", "sync_all", "POST"),
    ("/sync_module", "sync_module", "POST"),
    ("/timezones", "timezones_route", "GET"),
    ("/toggle_autohome", "toggle_autohome", "POST"),
    ("/toggle_sim", "toggle_sim", "POST"),
    ("/triggers", "triggers_route", "GET,POST"),
    ("/tuning_status", "tuning_status", "GET"),
    ("/universal/deprovision", "universal_deprovision", "POST"),
    ("/universal/diagnose", "universal_diagnose", "POST"),
    ("/universal/home", "universal_home", "POST"),
    ("/universal/provision", "universal_provision", "POST"),
    ("/universal/scan", "universal_scan", "POST"),
    ("/universal/status", "universal_status", "GET"),
    ("/update_playlist", "update_playlist", "POST"),
    ("/version", "version_route", "GET"),
    ("/wifi_connect", "wifi_connect", "POST"),
    ("/wifi_scan", "wifi_scan", "GET"),
]

# GET routes that must not be called in a test: /apply_update is POST-only so
# it is excluded by method, but this is where anything destructive belongs.
UNCALLABLE = set()

# Query strings that push a GET route down its real path rather than an
# early return. Anything not listed is called bare.
GET_ARGS = {
    "/location_search": "?q=boston",
    "/location_timezone": "?lat=42.35&lon=-71.06",
    "/timezones": "?q=east",
    "/stocks_search": "?q=aapl",
    "/crypto_search": "?q=bitcoin",
    "/sports_teams/<league_key>": "",
}

# POST/DELETE calls with payloads representative of what the UI sends.
WRITE_CALLS = [
    ("POST", "/toggle_sim", {"enabled": True}),
    ("POST", "/settings", {"action": "save_global", "currency_symbol": "$"}),
    ("POST", "/update_playlist", {"pages": ["HELLO"], "delay": 5}),
    ("POST", "/run_app", {"app": "time"}),
    ("POST", "/stop_app", {}),
    ("POST", "/assign_id", {"id": 3}),
    ("POST", "/toggle_autohome", {"enabled": True}),
    ("POST", "/custom_tune", {"action": "goto", "id": 0, "step": 100, "index": 1}),
    ("POST", "/auto_tune", {"action": "home"}),
    ("POST", "/sync_module", {"id": 0}),
    ("POST", "/playlists", {"name": "test-pl", "pages": ["HI"], "delay": 5}),
    ("DELETE", "/playlists/test-pl", None),
    ("POST", "/app_playlists", {"name": "test-apl", "entries": [], "loop": True}),
    ("DELETE", "/app_playlists/test-apl", None),
    ("POST", "/run_app_playlist", {"name": "nope"}),
    ("POST", "/schedules", {"schedules": []}),
    ("POST", "/schedule_tick", {}),
    ("POST", "/triggers", {"triggers": [], "enabled": True}),
    ("POST", "/notify", {"message": "HELLO"}),
    ("DELETE", "/notify", None),
    ("POST", "/sports_follow", {"league": "nfl", "teams": []}),
    ("POST", "/mqtt_reconnect", {}),
    ("POST", "/universal/scan", {}),
]


def _fake_response(payload=None):
    """A requests.Response stand-in that satisfies .json()/.text/.content."""
    resp = mock.MagicMock()
    resp.status_code = 200
    resp.json.return_value = {} if payload is None else payload
    resp.text = ""
    resp.content = b""
    resp.raise_for_status.return_value = None
    return resp


class RouteInventoryTests(unittest.TestCase):
    """The HTTP contract the web UI depends on."""

    def actual(self):
        return sorted(
            (str(r), r.endpoint, ",".join(sorted(r.methods - {"HEAD", "OPTIONS"})))
            for r in app.app.url_map.iter_rules()
            if r.endpoint != "static"
        )

    def test_every_expected_route_is_registered(self):
        self.assertEqual(self.actual(), sorted(EXPECTED_ROUTES))

    def test_route_count_is_unchanged(self):
        self.assertEqual(len(self.actual()), len(EXPECTED_ROUTES))

    def test_no_endpoint_name_is_reused(self):
        endpoints = [ep for _, ep, _ in self.actual()]
        self.assertEqual(sorted(endpoints), sorted(set(endpoints)))


class RouteSmokeTestCase(SplitflapTestCase):
    """Calls routes with the outside world stubbed out.

    requests, urllib and subprocess are all patched, so a route that reaches
    for the network or the shell gets a canned answer instead. A 5xx that the
    route returns deliberately (502 on a failed upstream) is fine; a 500 means
    something raised, which is what these tests are hunting for.
    """

    def setUp(self):
        super().setUp()
        app.app.config["TESTING"] = False
        self.http = app.app.test_client()
        patches = [
            mock.patch("requests.get", return_value=_fake_response()),
            mock.patch("requests.post", return_value=_fake_response()),
            mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout="", stderr="")),
            mock.patch("subprocess.check_output", return_value=b""),
            mock.patch("urllib.request.urlopen"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def assertNotCrashed(self, response, label):
        self.assertNotEqual(
            response.status_code, 500,
            f"{label} raised: {response.get_data(as_text=True)[:400]}",
        )


class GetRouteSmokeTests(RouteSmokeTestCase):
    def test_every_get_route_answers(self):
        for rule, endpoint, methods in EXPECTED_ROUTES:
            if "GET" not in methods.split(",") or rule in UNCALLABLE:
                continue
            path = rule.replace("<league_key>", "nfl").replace("<path:name>", "x")
            path += GET_ARGS.get(rule, "")
            with self.subTest(route=rule, endpoint=endpoint):
                self.assertNotCrashed(self.http.get(path), f"GET {path}")

    def test_index_renders_the_ui(self):
        response = self.http.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("<html", response.get_data(as_text=True).lower())

    def test_current_state_reports_the_grid(self):
        self.set_grid(3, 15)
        body = self.http.get("/current_state").get_json()
        self.assertEqual((body["rows"], body["cols"]), (3, 15))
        self.assertEqual(len(body["state"]), 45)

    def test_grid_config_matches_current_state(self):
        self.set_grid(4, 20)
        grid = self.http.get("/grid_config").get_json()
        self.assertEqual(grid["total"], 80)
        self.assertEqual(grid["rows"], 4)


class WriteRouteSmokeTests(RouteSmokeTestCase):
    def test_every_write_route_answers(self):
        for method, path, payload in WRITE_CALLS:
            with self.subTest(route=f"{method} {path}"):
                call = getattr(self.http, method.lower())
                response = call(path, json=payload) if payload is not None else call(path)
                self.assertNotCrashed(response, f"{method} {path}")

    def test_settings_round_trips_a_saved_value(self):
        self.http.post("/settings", json={"action": "save_global", "zip_code": "94110"})
        self.assertEqual(self.http.get("/settings").get_json()["zip_code"], "94110")

    def test_run_app_then_stop_app_moves_the_active_app(self):
        self.http.post("/run_app", json={"app": "time"})
        self.assertEqual(self.http.get("/current_state").get_json()["active_app"], "time")
        self.http.post("/stop_app")
        self.assertIsNone(self.http.get("/current_state").get_json()["active_app"])

    def test_update_playlist_replaces_the_playlist(self):
        self.http.post("/update_playlist", json={"pages": ["ONE", "TWO"], "delay": 3})
        self.assertEqual(state.current_playlist, ["ONE", "TWO"])
        self.assertIsNone(state.active_app)

    def test_home_all_marks_the_display_homed(self):
        state.is_homed = False
        self.http.get("/home_all")
        self.assertTrue(state.is_homed)
        self.assertEqual(state.current_display_string, " " * app.get_module_count())


if __name__ == "__main__":
    unittest.main()
