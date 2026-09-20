"""Coverage for the HTTP surface.

These tests exist to make the app.py breakup safe. Nothing here asserts much
about behaviour — the point is that all 58 endpoints keep existing, keep their
methods and endpoint names, and keep answering without blowing up, however the
code behind them is rearranged.
"""

import unittest
from unittest import mock

from support import SplitflapTestCase, app, state

from splitflap.grid import get_module_count
from splitflap.settings import read_version, settings


# Frozen inventory of the HTTP surface: (rule, endpoint, methods).
# The URL and method columns are the contract the web UI depends on and must
# never change while code moves. Endpoints are blueprint-qualified
# ("display.index"): that name is internal to Flask, nothing calls url_for,
# and the UI addresses every one of these by literal URL.
EXPECTED_ROUTES = [
    ("/", "display.index", "GET"),
    ("/app_library", "apps.app_library", "GET"),
    ("/app_library/install", "apps.app_library_install", "POST"),
    ("/app_library/uninstall", "apps.app_library_uninstall", "POST"),
    ("/app_playlists", "playlists.app_playlists", "GET,POST"),
    ("/app_playlists/<path:name>", "playlists.delete_app_playlist", "DELETE"),
    ("/apply_tuning", "tuning.apply_tuning", "POST"),
    ("/apply_update", "system.apply_update", "POST"),
    ("/assign_id", "tuning.assign_id", "POST"),
    ("/auto_tune", "tuning.auto_tune_route", "POST"),
    ("/backup_settings", "tuning.backup_settings", "GET"),
    ("/check_update", "system.check_update", "GET"),
    ("/connection", "hardware.connection_config", "GET,POST"),
    ("/current_state", "display.current_state", "GET"),
    ("/custom_tune", "tuning.custom_tune", "POST"),
    ("/grid_config", "display.grid_config", "GET"),
    ("/home_all", "display.home_all", "GET"),
    ("/installed_apps", "apps.installed_apps", "GET"),
    ("/location_search", "search.location_search_route", "GET"),
    ("/location_timezone", "search.location_timezone_route", "GET"),
    ("/module_audit", "tuning.module_audit", "POST"),
    ("/mqtt_reconnect", "network.mqtt_reconnect_route", "POST"),
    ("/network_config", "network.network_config", "POST"),
    ("/network_status", "network.network_status", "GET"),
    ("/notify", "notify.notify_clear", "DELETE"),
    ("/notify", "notify.notify_list", "GET"),
    ("/notify", "notify.notify_push", "POST"),
    ("/playlists", "playlists.playlists", "GET,POST"),
    ("/playlists/<path:name>", "playlists.delete_playlist", "DELETE"),
    ("/restore_settings", "tuning.restore_settings", "POST"),
    ("/run_app", "display.run_app", "POST"),
    ("/run_app_playlist", "playlists.run_app_playlist", "POST"),
    ("/schedule_tick", "playlists.schedule_tick_route", "POST"),
    ("/schedules", "playlists.schedules_route", "GET,POST"),
    ("/serial_port", "hardware.set_serial_port", "POST"),
    ("/serial_ports", "hardware.list_serial_ports", "GET"),
    ("/settings", "tuning.handle_settings", "GET,POST"),
    ("/sports_follow", "apps.sports_follow", "POST"),
    ("/sports_leagues", "apps.sports_leagues_route", "GET"),
    ("/sports_teams/<league_key>", "apps.sports_teams_route", "GET"),
    ("/stocks_search", "search.stocks_search_route", "GET"),
    ("/stop_app", "display.stop_app", "POST"),
    ("/sync_all", "tuning.sync_all", "POST"),
    ("/sync_module", "tuning.sync_module", "POST"),
    ("/timezones", "search.timezones_route", "GET"),
    ("/toggle_autohome", "tuning.toggle_autohome", "POST"),
    ("/toggle_sim", "display.toggle_sim", "POST"),
    ("/triggers", "apps.triggers_route", "GET,POST"),
    ("/tuning_status", "tuning.tuning_status", "GET"),
    ("/universal/deprovision", "hardware.universal_deprovision", "POST"),
    ("/universal/diagnose", "hardware.universal_diagnose", "POST"),
    ("/universal/home", "hardware.universal_home", "POST"),
    ("/universal/provision", "hardware.universal_provision", "POST"),
    ("/universal/scan", "hardware.universal_scan", "POST"),
    ("/universal/status", "hardware.universal_status", "GET"),
    ("/update_playlist", "display.update_playlist", "POST"),
    ("/version", "system.version_route", "GET"),
    ("/wifi_connect", "network.wifi_connect", "POST"),
    ("/wifi_scan", "network.wifi_scan", "GET"),
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
    ("POST", "/module_audit", {"ids": [0]}),
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

    def test_every_route_belongs_to_a_blueprint(self):
        # app.py registers no routes of its own; it is a composition root.
        for _, endpoint, _ in self.actual():
            self.assertIn(".", endpoint, f"{endpoint} is not on a blueprint")

    def test_urls_are_unique_per_method(self):
        seen = set()
        for rule, _, methods in self.actual():
            for method in methods.split(","):
                self.assertNotIn((rule, method), seen, f"{method} {rule} registered twice")
                seen.add((rule, method))

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

    def test_current_state_reports_the_auto_home_setting(self):
        # The UI needs this to decide whether to prompt for homing: with
        # auto-home on, the modules do it themselves at power-up and the
        # "HOMING REQUIRED" overlay is just noise.
        from splitflap.settings import settings as cfg
        cfg['auto_home'] = True
        self.assertIs(self.http.get("/current_state").get_json()["auto_home"], True)
        cfg['auto_home'] = False
        self.assertIs(self.http.get("/current_state").get_json()["auto_home"], False)

    def test_toggling_auto_home_pushes_the_flag_to_the_modules(self):
        self.http.post("/toggle_autohome", json={"enabled": True})
        self.assertIn("m**a1", self.sent)
        self.http.post("/toggle_autohome", json={"enabled": False})
        self.assertIn("m**a0", self.sent)

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
        self.assertEqual(state.current_display_string, " " * get_module_count())


class UpdateCheckTests(RouteSmokeTestCase):
    """/check_update caches its answer for an hour and returns the cached
    value without a try block, so a result that cannot be serialised poisons
    the route rather than failing once.

    What counts as an update is git's answer, not the release feed's: releases
    are cut from one branch and say nothing about whichever branch a given Pi
    is actually following.
    """

    def setUp(self):
        super().setUp()
        from splitflap.web.system import _update_cache
        self._cache = _update_cache
        _update_cache.update(checked_at=0, result=None)
        self.addCleanup(_update_cache.update, checked_at=0, result=None)
        self.on_branch("main", behind=2)

    def on_branch(self, branch, behind=0, reachable=True):
        """Put the checkout on a branch, so the route has one to report."""
        from splitflap import updates
        answers = {
            "rev-parse --abbrev-ref HEAD": branch,
            "config --get branch.{}.remote".format(branch): "origin",
            "config --get branch.{}.merge".format(branch): "refs/heads/" + branch,
            "remote get-url origin": "https://github.com/csader/splitflap-os.git",
            "rev-list --count HEAD..FETCH_HEAD": str(behind),
        }

        def git(*args, **kwargs):
            key = " ".join(str(a) for a in args)
            if not reachable and key.startswith("fetch"):
                return False, "could not resolve host"
            return True, answers.get(key, "")

        patch = mock.patch.object(updates, "git", git)
        patch.start()
        self.addCleanup(patch.stop)

    def test_an_unserialisable_upstream_payload_does_not_poison_the_cache(self):
        # The mocked requests.get returns a MagicMock, so every field read out
        # of resp.json() is itself a MagicMock — the shape that used to be
        # cached and then blow up on the next call.
        self.assertNotCrashed(self.http.get("/check_update"), "first call")
        self.assertNotCrashed(self.http.get("/check_update"), "cached call")

    def test_a_good_result_is_cached_and_served(self):
        payload = {"tag_name": "v9.9.9", "name": "Release", "html_url": "http://x"}
        with mock.patch("requests.get", return_value=_fake_response(payload)):
            first = self.http.get("/check_update").get_json()
        self.assertEqual(first["latest"], "9.9.9")
        self.assertIs(first["has_update"], True)
        # served from cache now, with no upstream call at all
        with mock.patch("requests.get", side_effect=AssertionError("should not refetch")):
            self.assertEqual(self.http.get("/check_update").get_json(), first)

    def test_has_update_is_a_boolean_even_when_upstream_is_empty(self):
        self.on_branch("main", behind=0)
        with mock.patch("requests.get", return_value=_fake_response({"tag_name": None})):
            body = self.http.get("/check_update").get_json()
        self.assertIs(body["has_update"], False)

    def test_it_reports_the_branch_this_checkout_actually_follows(self):
        self.on_branch("refactor/modularize-server", behind=3)
        body = self.http.get("/check_update").get_json()

        self.assertEqual(body["branch"], "refactor/modularize-server")
        self.assertEqual(body["tracking"], "origin/refactor/modularize-server")
        self.assertEqual(body["commits_behind"], 3)
        self.assertIs(body["has_update"], True)

    def test_a_branch_with_no_release_of_its_own_still_reports_an_update(self):
        # The release feed describes main. A branch that has never been
        # released from would otherwise look permanently up to date.
        self.on_branch("topic", behind=5)
        with mock.patch("requests.get",
                        return_value=_fake_response({"tag_name": "v" + read_version()})):
            body = self.http.get("/check_update").get_json()

        self.assertIs(body["has_update"], True)
        self.assertEqual(body["commits_behind"], 5)

    def test_a_detached_head_is_reported_rather_than_updated(self):
        self.on_branch("HEAD")
        body = self.http.get("/check_update").get_json()

        self.assertIs(body["has_update"], False)
        self.assertIn("detached", body["error"])

    def test_an_unreachable_remote_falls_back_to_comparing_versions(self):
        self.on_branch("main", reachable=False)
        with mock.patch("requests.get", return_value=_fake_response({"tag_name": "v9.9.9"})):
            body = self.http.get("/check_update").get_json()

        self.assertIsNone(body["commits_behind"])
        self.assertIs(body["has_update"], True)


class MalformedInputTests(RouteSmokeTestCase):
    """Every write route, against payloads the UI would never send.

    A 400 is a fine answer and so is a 200; a 500 means the handler raised on
    input it should have rejected. This found five routes at once — a present
    but null "id" is not a missing "id", so .get("id", "") returns None and
    .strip() raises, and int(None) does the same a line later.
    """

    # Deliberately excluded: it restarts the service.
    UNSAFE = {"/apply_update"}

    PAYLOADS = [
        {},
        {"action": "nonsense"},
        {"id": None},
        {"id": []},
        {"id": {"nested": 1}},
        {"name": None},
        {"enabled": "yes"},
        {"pages": "not-a-list"},
        {"schedules": "nope"},
        {"triggers": 5},
        {"entries": "x"},
        {"app": 123},
        {"text": None},
        {"port": None},
    ]

    def test_no_write_route_raises_on_malformed_input(self):
        routes = [
            (rule, method)
            for rule, _, methods in EXPECTED_ROUTES
            for method in methods.split(",")
            if method in ("POST", "DELETE")
            and "<" not in rule
            and rule not in self.UNSAFE
        ]
        self.assertGreater(len(routes), 20, "route inventory looks wrong")
        for rule, method in sorted(routes):
            for payload in self.PAYLOADS:
                with self.subTest(route=f"{method} {rule}", payload=payload):
                    response = self.http.open(rule, method=method, json=payload)
                    self.assertNotCrashed(response, f"{method} {rule} {payload}")


if __name__ == "__main__":
    unittest.main()
