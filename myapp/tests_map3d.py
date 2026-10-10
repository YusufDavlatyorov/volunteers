"""The MapLibre GL map (3D buildings, 2D/3D, route under the buildings) and
road routing through OpenRouteService — ``maps.vector_style`` /
``maps.routing_provider`` / ``maps._ors_route``, the page wiring, and the
"map never covers the topbar" CSS. Network mocked at
``myapp.services.maps.requests.post`` (ORS) / ``.get`` (OSRM)."""

import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

import requests
from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from accounts.models import Users
from myapp.models import HelpRequest
from myapp.services import maps, routing

POST = "myapp.services.maps.requests.post"
GET = "myapp.services.maps.requests.get"
ORIGIN = (38.55, 68.78)
DEST = (38.56, 68.79)
KEY = "ors-test-key-not-real"

ORS_OK = {
    "type": "FeatureCollection",
    "features": [{
        "geometry": {"type": "LineString", "coordinates": [[68.78, 38.55], [68.785, 38.555], [68.788, 38.558], [68.79, 38.56]]},
        "properties": {
            "summary": {"distance": 2500.0, "duration": 420.0},
            "segments": [{"steps": [
                {"type": 11, "name": "Rudaki Ave", "distance": 1200.0, "duration": 200.0, "way_points": [0, 1]},
                {"type": 0, "name": "Ismoili Somoni", "distance": 1000.0, "duration": 180.0, "way_points": [1, 2]},
                {"type": 7, "name": "-", "distance": 300.0, "duration": 40.0, "way_points": [2, 3], "exit_number": 2},
                {"type": "bogus"},
                {"type": 10, "name": "-", "distance": 0, "duration": 0, "way_points": [3, 3]},
            ]}],
        },
    }],
}


def _response(payload):
    r = mock.Mock()
    r.raise_for_status.return_value = None
    r.json.return_value = payload
    return r


class RoutingProviderTests(SimpleTestCase):
    @override_settings(ORS_API_KEY="", ROUTING_PROVIDER="")
    def test_default_is_osrm_without_a_key(self):
        self.assertEqual(maps.routing_provider(), "osrm")

    @override_settings(ORS_API_KEY=KEY, ROUTING_PROVIDER="")
    def test_a_key_switches_to_ors(self):
        self.assertEqual(maps.routing_provider(), "ors")

    @override_settings(ORS_API_KEY=KEY, ROUTING_PROVIDER="osrm")
    def test_explicit_osrm_wins(self):
        self.assertEqual(maps.routing_provider(), "osrm")

    @override_settings(ORS_API_KEY="", ROUTING_PROVIDER="ors")
    def test_ors_without_key_falls_back_to_osrm(self):
        with self.assertLogs("myapp.services.maps", "WARNING"):
            self.assertEqual(maps.routing_provider(), "osrm")

    def test_tests_never_inherit_a_real_key(self):
        # server/settings.py blanks it under `manage.py test` (like the bot tokens).
        self.assertEqual(settings.ORS_API_KEY, "")


class NominatimSettingTests(SimpleTestCase):
    """NOMINATIM_BASE_URL is blank only under `manage.py test`; any other
    process (runserver, gunicorn, cron commands) geocodes against the real
    Nominatim. Settings are evaluated once per process, so the non-test value is
    read in a fresh interpreter whose argv is not `test`."""

    def _nominatim_url_for_argv(self, argv):
        code = (
            "import os, sys; sys.argv = %r; "
            "os.environ['DJANGO_SETTINGS_MODULE'] = 'server.settings'; "
            "from django.conf import settings; print(settings.NOMINATIM_BASE_URL)"
        ) % (argv,)
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=settings.BASE_DIR,
            capture_output=True, text=True, timeout=60, check=True,
        )
        return out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""

    def test_blank_under_manage_py_test(self):
        self.assertEqual(settings.NOMINATIM_BASE_URL, "")
        self.assertEqual(self._nominatim_url_for_argv(["manage.py", "test"]), "")

    def test_real_nominatim_outside_tests(self):
        for argv in (["manage.py", "runserver"], ["gunicorn"], ["manage.py", "geocode_missing"]):
            with self.subTest(argv=argv):
                self.assertEqual(
                    self._nominatim_url_for_argv(argv), "https://nominatim.openstreetmap.org",
                )


@override_settings(ORS_API_KEY=KEY, ROUTING_PROVIDER="", ORS_BASE_URL="https://ors.test")
class OrsRouteTests(SimpleTestCase):
    def test_request_is_server_side_with_key_in_header(self):
        with mock.patch(POST, return_value=_response(ORS_OK)) as post, mock.patch(GET) as get:
            result = maps.route(ORIGIN, DEST, steps=True)
        get.assert_not_called()
        self.assertEqual(post.call_args.args[0], "https://ors.test/v2/directions/driving-car/geojson")
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], KEY)
        self.assertNotIn(KEY, post.call_args.args[0])          # never in the URL
        self.assertEqual(kwargs["json"]["coordinates"], [[68.78, 38.55], [68.79, 38.56]])
        self.assertTrue(kwargs["json"]["instructions"])
        self.assertIn("timeout", kwargs)
        self.assertTrue(result["success"])
        self.assertEqual(result["distance_km"], 2.5)
        self.assertEqual(result["duration_min"], 7.0)
        self.assertEqual(result["geometry"][0], [38.55, 68.78])   # flipped to [lat, lng]
        self.assertEqual(result["profile"], "driving")

    def test_walking_profile(self):
        with mock.patch(POST, return_value=_response(ORS_OK)) as post:
            result = maps.route(ORIGIN, DEST, profile="walking", steps=True)
        self.assertIn("/foot-walking/", post.call_args.args[0])
        self.assertEqual(result["profile"], "walking")

    def test_plain_call_has_no_instructions(self):
        with mock.patch(POST, return_value=_response(ORS_OK)) as post:
            result = maps.route(ORIGIN, DEST)
        self.assertFalse(post.call_args.kwargs["json"]["instructions"])
        self.assertEqual(result["steps"], [])

    def test_steps_map_onto_osrm_maneuvers(self):
        with mock.patch(POST, return_value=_response(ORS_OK)):
            steps = maps.route(ORIGIN, DEST, steps=True)["steps"]
        self.assertEqual(len(steps), 4)   # the malformed one is skipped
        self.assertEqual(steps[0], {
            "maneuver": "depart", "modifier": "", "name": "Rudaki Ave", "distance_m": 1200.0,
            "duration_s": 200.0, "location": [38.55, 68.78],
        })
        self.assertEqual((steps[1]["maneuver"], steps[1]["modifier"]), ("turn", "left"))
        self.assertEqual(steps[1]["location"], [38.555, 68.785])
        self.assertEqual((steps[2]["maneuver"], steps[2]["exit"], steps[2]["name"]), ("roundabout", 2, ""))
        self.assertEqual(steps[3]["maneuver"], "arrive")

    def test_failures_fall_back_without_raising(self):
        for effect in (requests.Timeout("slow"), requests.HTTPError("403"), ValueError("json")):
            with mock.patch(POST, side_effect=effect):
                result = maps.route(ORIGIN, DEST, steps=True)
            self.assertFalse(result["success"], effect)
            self.assertEqual(result["geometry"], [])
            self.assertGreater(result["distance_km"], 0)
        for payload in ({"error": {"code": 2010, "message": "no route"}}, {"features": []},
                        {"features": [{"geometry": {"coordinates": []}, "properties": {}}]}):
            with mock.patch(POST, return_value=_response(payload)):
                self.assertFalse(maps.route(ORIGIN, DEST, steps=True)["success"], payload)

    def test_failure_log_has_no_key(self):
        with mock.patch(POST, side_effect=requests.RequestException(f"boom Authorization: {KEY}")):
            with self.assertLogs("myapp.services.maps", "WARNING") as logs:
                maps.route(ORIGIN, DEST, steps=True)
        self.assertNotIn(KEY, "\n".join(logs.output))


@override_settings(ORS_API_KEY=KEY, ROUTING_PROVIDER="")
class OrsTaskRouteViewTests(TestCase):
    def setUp(self):
        cache.clear()
        mk = lambda name, **f: Users.objects.create_user(  # noqa: E731
            username=name, email=f"{name}@example.com", password="pass12345", region="dushanbe", **f)
        self.volunteer = mk("m3_vol", is_volunteer=True)
        self.client_user = mk("m3_client", is_client=True)
        self.task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, help_type="grocery", description="x",
            address="addr", phone="+992900000000", region="dushanbe", status="active",
            latitude=DEST[0], longitude=DEST[1],
        )
        self.volunteer.profile.set_location(*ORIGIN)
        self.url = reverse("task_route", args=[self.task.pk])

    def tearDown(self):
        cache.clear()

    def test_route_json_has_road_geometry_and_never_the_key(self):
        self.client.force_login(self.client_user)
        with mock.patch(POST, return_value=_response(ORS_OK)) as post:
            response = self.client.get(self.url)
            self.client.get(self.url)   # cached: one upstream call
        self.assertEqual(post.call_count, 1)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(len(data["geometry"]), 4)
        self.assertEqual(data["eta_min"], 7.0)
        self.assertNotIn(KEY, response.content.decode())

    def test_unavailable_routing_is_a_graceful_answer(self):
        self.client.force_login(self.client_user)
        with mock.patch(POST, side_effect=requests.ConnectionError("down")):
            response = self.client.get(self.url)
        data = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(data["success"])
        self.assertEqual(data["geometry"], [])     # nothing to draw — the page says so
        self.assertIn("destination", data)

    def test_cache_lasts_a_few_minutes(self):
        self.assertGreaterEqual(routing.ROUTE_CACHE_SECONDS, 180)


class VectorStyleTests(SimpleTestCase):
    def test_default_is_keyless_openfreemap(self):
        style = maps.vector_style()
        self.assertEqual(style["provider"], "openfreemap")
        self.assertTrue(style["url"].startswith("https://tiles.openfreemap.org/styles/"))

    @override_settings(MAPS_VECTOR_STYLE="maptiler", MAPTILER_API_KEY="mt-public-key")
    def test_maptiler_with_key(self):
        style = maps.vector_style()
        self.assertEqual(style["provider"], "maptiler")
        self.assertIn("key=mt-public-key", style["url"])

    @override_settings(MAPS_VECTOR_STYLE="maptiler", MAPTILER_API_KEY="")
    def test_maptiler_without_key_uses_openfreemap(self):
        with self.assertLogs("myapp.services.maps", "WARNING"):
            self.assertEqual(maps.vector_style()["provider"], "openfreemap")

    @override_settings(MAPS_VECTOR_STYLE="off")
    def test_off_means_raster_only(self):
        self.assertEqual(maps.vector_style(), {})


class MapPageWiringTests(TestCase):
    def setUp(self):
        self.user = Users.objects.create_user(
            username="m3_admin", email="m3a@example.com", password="x-Map-12345", is_superuser=True, is_staff=True)
        self.client.force_login(self.user)

    def test_map_config_carries_vector_style_and_raster_fallback(self):
        html = self.client.get(reverse("map")).content.decode()
        raw = re.search(r'<script id="gc-maps-tile" type="application/json">(.*?)</script>', html, re.S).group(1)
        self.assertIn("tiles.openfreemap.org/styles/", raw)
        self.assertIn("tile.openstreetmap.org", raw)        # raster fallback
        self.assertIn("basemaps.cartocdn.com", raw)         # ... and its fallback

    def test_no_leaflet_left_in_templates_or_scripts(self):
        roots = [Path(settings.BASE_DIR) / "templates", Path(settings.BASE_DIR) / "static" / "js"]
        for root in roots:
            for path in list(root.rglob("*.html")) + list(root.rglob("*.js")):
                text = path.read_text(encoding="utf-8")
                with self.subTest(path=str(path)):
                    self.assertNotRegex(text, r"\bL\.(map|marker|tileLayer|heatLayer)\(")
                    self.assertNotIn("leaflet", text.lower())


class MapStackingCssTests(SimpleTestCase):
    """The map must never draw over the sticky navbar (milestone: MapLibre)."""

    def test_topbar_is_sticky_on_top(self):
        css = Path(finders.find("css/style.css")).read_text(encoding="utf-8")
        rule = re.search(r"\n\.topbar \{(.*?)\}", css, re.S).group(1)
        for decl in ("position: sticky;", "top: 0;", "z-index: 1000;"):
            self.assertIn(decl, rule)

    def test_map_container_is_an_isolated_layer(self):
        css = Path(finders.find("css/map.css")).read_text(encoding="utf-8")
        rule = re.search(r"\.gc-map \{(.*?)\}", css, re.S).group(1)
        for decl in ("position: relative;", "z-index: 0;", "isolation: isolate;"):
            self.assertIn(decl, rule)


class MapScriptContractTests(SimpleTestCase):
    def setUp(self):
        self.js = Path(finders.find("js/map.js")).read_text(encoding="utf-8")

    def test_route_is_inserted_under_the_buildings(self):
        # Both route layers are added with beforeId = the first building layer.
        for layer in ("gc-route-casing", "gc-route-line"):
            block = re.search(r"id: '" + layer + r"'.*?\}, before\);", self.js, re.S)
            self.assertIsNotNone(block, layer)
        self.assertIn("ROUTE_COLOR: '#2A3BA5'", self.js)
        self.assertIn("'line-cap': 'round'", self.js)

    def test_3d_buildings_and_controls(self):
        for needle in ("fill-extrusion", "NavigationControl", "visualizePitch: true", "FullscreenControl",
                       "gc-ctrl-3d", "PITCH_3D: 55"):
            self.assertIn(needle, self.js)


# --------------------------------------------------------------------------- #
# Walking fix + shorter/faster + alternatives
# --------------------------------------------------------------------------- #

def _ors_two_routes():
    """Two ORS variants: the first faster (2.6 km / 6 min), the second shorter (2.2 km / 8 min)."""
    def feature(dist, dur):
        return {"geometry": {"coordinates": [[68.78, 38.55], [68.79, 38.56]]},
                "properties": {"summary": {"distance": dist, "duration": dur}, "segments": []}}
    return {"features": [feature(2600.0, 360.0), feature(2200.0, 480.0)]}


def _osrm_routes(*pairs):
    return {"code": "Ok", "routes": [
        {"distance": d, "duration": t, "geometry": {"coordinates": [[68.78, 38.55], [68.79, 38.56]]}, "legs": []}
        for d, t in pairs
    ]}


class WalkingBackendTests(SimpleTestCase):
    def test_default_foot_server_is_a_real_foot_router(self):
        # router.project-osrm.org is car-only: asked for "foot" it returns the car route.
        self.assertNotIn("project-osrm.org", settings.OSRM_FOOT_BASE_URL)
        self.assertTrue(settings.OSRM_FOOT_BASE_URL)

    @override_settings(ORS_API_KEY=KEY, ROUTING_PROVIDER="osrm")
    def test_walking_uses_ors_whenever_a_key_exists(self):
        self.assertEqual(maps.routing_provider("driving"), "osrm")
        self.assertEqual(maps.routing_provider("walking"), "ors")
        with mock.patch(POST, return_value=_response(ORS_OK)) as post, mock.patch(GET) as get:
            result = maps.route(ORIGIN, DEST, profile="walking", steps=True)
        get.assert_not_called()
        self.assertIn("/foot-walking/", post.call_args.args[0])
        self.assertTrue(result["success"])

    @override_settings(ORS_API_KEY="", OSRM_BASE_URL="https://osrm-car.test", OSRM_FOOT_BASE_URL="https://osrm-foot.test")
    def test_walking_without_key_goes_to_the_foot_server_with_user_agent(self):
        with mock.patch(GET, return_value=_response(_osrm_routes((1800.0, 1500.0)))) as get:
            result = maps.route(ORIGIN, DEST, profile="walking", steps=True)
        self.assertTrue(get.call_args.args[0].startswith("https://osrm-foot.test/route/v1/foot/"))
        self.assertIn("User-Agent", get.call_args.kwargs["headers"])
        self.assertTrue(result["success"])

    @override_settings(ORS_API_KEY=KEY)
    def test_walking_too_far_is_not_requested(self):
        with mock.patch(POST) as post:
            result = maps.route(ORIGIN, (40.28, 69.62), profile="walking", steps=True)   # Dushanbe → Khujand
        post.assert_not_called()
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], maps.TOO_FAR)
        self.assertIsNone(routing.route(ORIGIN, (40.28, 69.62), profile="walking")["eta_min"])

    @override_settings(ORS_API_KEY=KEY)
    def test_ors_route_not_found_has_a_reason(self):
        failure = mock.Mock(status_code=404)
        failure.json.return_value = {"error": {"code": 2009, "message": "Route could not be found"}}
        with mock.patch(POST, return_value=failure):
            result = maps.route(ORIGIN, DEST, profile="walking", steps=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], maps.NO_ROUTE)

    @override_settings(ORS_API_KEY="", OSRM_FOOT_BASE_URL="https://osrm-foot.test")
    def test_osrm_no_route_has_a_reason(self):
        with mock.patch(GET, return_value=_response({"code": "NoRoute", "routes": []})):
            self.assertEqual(maps.route(ORIGIN, DEST, profile="walking", steps=True)["reason"], maps.NO_ROUTE)


@override_settings(ORS_API_KEY=KEY, ROUTING_PROVIDER="", ORS_BASE_URL="https://ors.test")
class OrsPreferenceTests(SimpleTestCase):
    def _route(self, **kw):
        with mock.patch(POST, return_value=_response(_ors_two_routes())) as post:
            result = maps.route(ORIGIN, DEST, steps=True, **kw)
        return result, post.call_args.kwargs["json"]

    def test_default_preference_per_profile(self):
        self.assertEqual(self._route()[1]["preference"], "fastest")
        self.assertEqual(self._route(profile="walking")[1]["preference"], "shortest")
        self.assertEqual(self._route(preference="bogus")[1]["preference"], "fastest")

    def test_alternatives_requested_and_ranked(self):
        fastest, body = self._route(preference="fastest", alternatives=True)
        self.assertEqual(body["alternative_routes"]["target_count"], 3)
        self.assertEqual([r["distance_km"] for r in fastest["routes"]], [2.6, 2.2])
        shortest, body = self._route(preference="shortest", alternatives=True)
        self.assertEqual(body["preference"], "shortest")
        self.assertEqual([r["distance_km"] for r in shortest["routes"]], [2.2, 2.6])
        self.assertEqual(shortest["distance_km"], 2.2)
        self.assertLessEqual(shortest["distance_km"], fastest["distance_km"])

    def test_no_alternatives_unless_asked(self):
        self.assertNotIn("alternative_routes", self._route()[1])

    def test_alternatives_over_the_limit_retry_without_them(self):
        limit = mock.Mock(status_code=400)
        limit.json.return_value = {"error": {"code": 2004, "message": "limit"}}
        with mock.patch(POST, side_effect=[limit, _response(ORS_OK)]) as post:
            result = maps.route(ORIGIN, DEST, steps=True, alternatives=True)
        self.assertEqual(post.call_count, 2)
        self.assertNotIn("alternative_routes", post.call_args.kwargs["json"])
        self.assertTrue(result["success"])


@override_settings(ORS_API_KEY="", OSRM_BASE_URL="https://osrm-car.test", OSRM_FOOT_BASE_URL="https://osrm-foot.test")
class OsrmPreferenceTests(SimpleTestCase):
    PAYLOAD = _osrm_routes((2600.0, 360.0), (2200.0, 480.0), (3000.0, 400.0))

    def test_shortest_picks_least_distance_fastest_least_time(self):
        with mock.patch(GET, return_value=_response(self.PAYLOAD)) as get:
            fastest = maps.route(ORIGIN, DEST, steps=True, alternatives=True)
            shortest = maps.route(ORIGIN, DEST, steps=True, preference="shortest", alternatives=True)
        self.assertEqual(get.call_args.kwargs["params"]["alternatives"], "3")
        self.assertEqual((fastest["distance_km"], fastest["duration_min"]), (2.6, 6.0))
        self.assertEqual((shortest["distance_km"], shortest["duration_min"]), (2.2, 8.0))
        self.assertLessEqual(shortest["distance_km"], fastest["distance_km"])
        self.assertEqual(len(shortest["routes"]), 3)


@override_settings(ORS_API_KEY=KEY, ROUTING_PROVIDER="", ORS_BASE_URL="https://ors.test")
class RoutingPreferenceCacheTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_key_has_profile_and_preference(self):
        keys = {routing.cache_key(ORIGIN, DEST, p, True, pref, True)
                for p in ("driving", "walking") for pref in ("fastest", "shortest")}
        self.assertEqual(len(keys), 4)

    def test_switching_never_returns_the_other_answer(self):
        with mock.patch(POST, return_value=_response(_ors_two_routes())) as post:
            a = routing.route(ORIGIN, DEST, preference="fastest", alternatives=True)
            b = routing.route(ORIGIN, DEST, preference="shortest", alternatives=True)
            w = routing.route(ORIGIN, DEST, profile="walking", alternatives=True)
            a2 = routing.route(ORIGIN, DEST, preference="fastest", alternatives=True)
        self.assertEqual(post.call_count, 3)
        self.assertTrue(a2["cached"])
        self.assertEqual((a["distance_km"], b["distance_km"]), (2.6, 2.2))
        self.assertEqual(w["profile"], "walking")
        self.assertEqual(b["shortest_index"], 0)
        self.assertEqual(a["shortest_index"], 1)
        self.assertEqual([v["eta_min"] for v in b["routes"]], [8.0, 6.0])


@override_settings(ORS_API_KEY=KEY, ROUTING_PROVIDER="", ORS_BASE_URL="https://ors.test")
class RouteViewPreferenceTests(OrsTaskRouteViewTests):
    def test_view_passes_preference_and_returns_variants(self):
        self.client.force_login(self.client_user)
        with mock.patch(POST, return_value=_response(_ors_two_routes())) as post:
            data = self.client.get(self.url, {"profile": "driving", "preference": "shortest"}).json()
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["preference"], "shortest")
        self.assertIn("alternative_routes", body)
        self.assertEqual(data["preference"], "shortest")
        self.assertEqual(len(data["routes"]), 2)
        self.assertEqual(data["distance_km"], 2.2)
        self.assertEqual(data["shortest_index"], 0)

    def test_failure_reason_reaches_the_page(self):
        failure = mock.Mock(status_code=404)
        failure.json.return_value = {"error": {"code": 2010}}
        self.client.force_login(self.client_user)
        with mock.patch(POST, return_value=failure):
            data = self.client.get(self.url, {"profile": "walking"}).json()
        self.assertFalse(data["success"])
        self.assertEqual(data["reason"], "no_route")


class RouteVariantsFrontendTests(SimpleTestCase):
    def test_map_draws_alternatives_and_dashed_walking_via_setdata(self):
        js = Path(finders.find("js/map.js")).read_text(encoding="utf-8")
        for needle in ("gc-route-alt", "gc-route-alt-hit", "'line-dasharray'", "ROUTE_DASH", "setData(map.__gcRouteAltData)",
                       "dashed: data.profile === 'walking'", "select(index)"):
            self.assertIn(needle, js)
        # sources are only added when missing — never a duplicate addSource on a mode switch
        for src in ("gc-route", "gc-route-alt"):
            self.assertNotRegex(js, r"(?<!if \(!map\.getSource\(id\)\) )map\.addSource\('" + src + "'")

    def test_page_has_preference_toggle_and_variants_panel(self):
        html = (Path(settings.BASE_DIR) / "templates" / "myapp" / "task_detail.html").read_text(encoding="utf-8")
        for needle in ('data-preference="shortest"', 'data-preference="fastest"', 'id="routeVariants"',
                       'data-i18n="route.pref_shortest"', 'data-i18n="route.pref_fastest"'):
            self.assertIn(needle, html)
        js = Path(finders.find("js/task-route.js")).read_text(encoding="utf-8")
        self.assertIn(".seg-toggle__btn[data-profile]", js)   # the mode toggle must not grab the preference buttons
