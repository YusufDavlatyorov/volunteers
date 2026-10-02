"""Feature Pack v2, feature 2 — routes + navigation (myapp/services/routing.py,
maps.route profile/steps, task_route_view). Network mocked at
``myapp.services.maps.requests.get``."""

import datetime
from unittest import mock

import requests
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Users
from myapp.models import HelpRequest
from myapp.services import maps, routing

GET = "myapp.services.maps.requests.get"
ORIGIN = (38.55, 68.78)
DEST = (38.56, 68.79)

OSRM_OK = {
    "code": "Ok",
    "routes": [{
        "distance": 2500.0,
        "duration": 420.0,
        "geometry": {"coordinates": [[68.78, 38.55], [68.785, 38.555], [68.79, 38.56]]},
        "legs": [{"steps": [
            {"maneuver": {"type": "depart", "location": [68.78, 38.55]}, "name": "Rudaki Ave",
             "distance": 1200.0, "duration": 200.0},
            {"maneuver": {"type": "turn", "modifier": "left", "location": [68.785, 38.555]},
             "name": "Ismoili Somoni", "distance": 1000.0, "duration": 180.0},
            {"maneuver": {"type": "roundabout", "modifier": "right", "exit": 2, "location": [68.788, 38.558]},
             "name": "", "distance": 300.0, "duration": 40.0},
            {"maneuver": {"type": "turn"}, "name": "malformed — no location"},
            {"maneuver": {"type": "arrive", "location": [68.79, 38.56]}, "name": "", "distance": 0,
             "duration": 0},
        ]}],
    }],
}


def _response(payload):
    r = mock.Mock()
    r.raise_for_status.return_value = None
    r.json.return_value = payload
    return r


@override_settings(OSRM_BASE_URL="https://osrm-car.test", OSRM_FOOT_BASE_URL="https://osrm-foot.test")
class MapsRouteProfileTests(TestCase):
    def test_driving_with_steps_url_and_params(self):
        with mock.patch(GET, return_value=_response(OSRM_OK)) as get:
            result = maps.route(ORIGIN, DEST, steps=True)
        url = get.call_args.args[0]
        self.assertEqual(url, "https://osrm-car.test/route/v1/driving/68.78,38.55;68.79,38.56")
        self.assertEqual(get.call_args.kwargs["params"],
                         {"overview": "full", "geometries": "geojson", "steps": "true"})
        self.assertIn("timeout", get.call_args.kwargs)
        self.assertTrue(result["success"])
        self.assertEqual(result["profile"], "driving")
        self.assertEqual(result["distance_km"], 2.5)
        self.assertEqual(result["duration_min"], 7.0)
        self.assertEqual(result["geometry"][0], [38.55, 68.78])

    def test_walking_uses_foot_server(self):
        with mock.patch(GET, return_value=_response(OSRM_OK)) as get:
            result = maps.route(ORIGIN, DEST, profile="walking", steps=True)
        self.assertEqual(get.call_args.args[0], "https://osrm-foot.test/route/v1/foot/68.78,38.55;68.79,38.56")
        self.assertEqual(result["profile"], "walking")

    def test_steps_parsing(self):
        with mock.patch(GET, return_value=_response(OSRM_OK)):
            steps = maps.route(ORIGIN, DEST, steps=True)["steps"]
        self.assertEqual(len(steps), 4)  # the malformed one is skipped
        self.assertEqual(steps[0], {
            "maneuver": "depart", "modifier": "", "name": "Rudaki Ave", "distance_m": 1200.0,
            "duration_s": 200.0, "location": [38.55, 68.78],
        })
        self.assertEqual(steps[1]["modifier"], "left")
        self.assertEqual(steps[2]["exit"], 2)
        self.assertEqual(steps[-1]["maneuver"], "arrive")

    @override_settings(OSRM_FOOT_BASE_URL="")
    def test_walking_without_foot_server_falls_back(self):
        with mock.patch(GET) as get:
            result = maps.route(ORIGIN, DEST, profile="walking", steps=True)
        get.assert_not_called()
        self.assertFalse(result["success"])
        self.assertGreater(result["distance_km"], 0)
        self.assertEqual(result["steps"], [])

    def test_failures_fall_back_without_raising(self):
        for effect in (requests.Timeout("slow"), requests.RequestException("boom"), ValueError("json")):
            with mock.patch(GET, side_effect=effect):
                result = maps.route(ORIGIN, DEST, steps=True)
            self.assertFalse(result["success"], effect)
            self.assertIsNone(result["duration_min"])
        with mock.patch(GET, return_value=_response({"code": "NoRoute", "routes": []})):
            self.assertFalse(maps.route(ORIGIN, DEST, steps=True)["success"])
        with mock.patch(GET, return_value=_response({"code": "Ok", "routes": [{"geometry": {}}]})):
            self.assertFalse(maps.route(ORIGIN, DEST, steps=True)["success"])

    def test_plain_driving_still_delegates_to_geo(self):
        with mock.patch("myapp.services.maps._osm_route", return_value={"success": True}) as m:
            maps.route(ORIGIN, DEST)
            maps.route(ORIGIN, DEST, profile="bogus")
        self.assertEqual(m.call_count, 2)
        m.assert_called_with(ORIGIN, DEST)


@override_settings(OSRM_BASE_URL="https://osrm-car.test", OSRM_FOOT_BASE_URL="https://osrm-foot.test")
class RoutingServiceTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_success_adds_eta_profile_links(self):
        with mock.patch(GET, return_value=_response(OSRM_OK)):
            result = routing.route(ORIGIN, DEST)
        self.assertTrue(result["success"])
        self.assertEqual(result["profile"], "driving")
        self.assertEqual(result["eta_min"], 7.0)
        self.assertFalse(result["eta_estimated"])
        self.assertEqual(len(result["steps"]), 4)
        self.assertEqual(set(result["deep_links"]), {"google", "yandex_maps", "yandex_navi", "dgis"})
        self.assertFalse(result["cached"])

    def test_cache_hit_within_rounding(self):
        with mock.patch(GET, return_value=_response(OSRM_OK)) as get:
            routing.route(ORIGIN, DEST)
            # ≈ 30 m away: same 3-dp origin bucket → cache hit.
            again = routing.route((38.5502, 68.7801), DEST)
            routing.route(ORIGIN, DEST, profile="walking")  # other profile → miss
        self.assertEqual(get.call_count, 2)
        self.assertTrue(again["cached"])
        self.assertEqual(again["eta_min"], 7.0)

    def test_cached_result_is_not_shared_mutable_state(self):
        with mock.patch(GET, return_value=_response(OSRM_OK)):
            first = routing.route(ORIGIN, DEST)
            first["geometry"].clear()
            second = routing.route(ORIGIN, DEST)
        self.assertEqual(len(second["geometry"]), 3)

    def test_failure_is_not_cached(self):
        with mock.patch(GET, side_effect=requests.Timeout("slow")) as get:
            failed = routing.route(ORIGIN, DEST)
            routing.route(ORIGIN, DEST)
        self.assertEqual(get.call_count, 2)
        self.assertFalse(failed["success"])
        self.assertIsNone(failed["eta_min"])  # driving fallback: no ETA, as before
        self.assertTrue(failed["deep_links"])

    @override_settings(OSRM_FOOT_BASE_URL="")
    def test_walking_fallback_estimates_eta(self):
        result = routing.route(ORIGIN, DEST, profile="walking")
        self.assertFalse(result["success"])
        self.assertTrue(result["eta_estimated"])
        self.assertAlmostEqual(result["eta_min"], round(result["distance_km"] / 4.5 * 60, 1))

    def test_unknown_profile_and_invalid_coords(self):
        self.assertEqual(routing.normalize_profile("helicopter"), "driving")
        self.assertEqual(routing.normalize_profile(" Walking "), "walking")
        result = routing.route((999, 0), DEST)
        self.assertFalse(result["success"])
        self.assertIsNone(result["distance_km"])
        bad_dest = routing.route(ORIGIN, (999, 0))
        self.assertEqual(bad_dest["deep_links"], {})

    def test_deep_links(self):
        links = routing.deep_links((38.5598, 68.787))
        self.assertEqual(links["google"],
                         "https://www.google.com/maps/dir/?api=1&destination=38.5598,68.787&travelmode=driving")
        self.assertEqual(links["yandex_maps"], "https://yandex.ru/maps/?rtext=~38.5598,68.787&rtt=auto")
        self.assertEqual(links["yandex_navi"], "yandexnavi://build_route_on_map?lat_to=38.5598&lon_to=68.787")
        self.assertEqual(links["dgis"], "https://2gis.ru/routeSearch/rsType/car/to/68.787,38.5598")
        walk = routing.deep_links((38.5598, 68.787), "walking")
        self.assertTrue(walk["google"].endswith("travelmode=walking"))
        self.assertTrue(walk["yandex_maps"].endswith("rtt=pd"))
        self.assertIn("/rsType/pedestrian/", walk["dgis"])


@override_settings(OSRM_BASE_URL="https://osrm-car.test", OSRM_FOOT_BASE_URL="https://osrm-foot.test")
class TaskRouteViewV2Tests(TestCase):
    def setUp(self):
        cache.clear()
        mk = lambda name, **f: Users.objects.create_user(  # noqa: E731
            username=name, email=f"{name}@example.com", password="pass12345", region="dushanbe", **f)
        self.volunteer = mk("rv_vol", is_volunteer=True)
        self.other_volunteer = mk("rv_vol2", is_volunteer=True)
        self.client_user = mk("rv_client", is_client=True)
        self.other_client = mk("rv_client2", is_client=True)
        self.curator = mk("rv_cur", is_curator=True)
        self.task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, help_type="grocery", description="x",
            address="addr", phone="+992900000000", region="dushanbe", status="active",
            latitude=DEST[0], longitude=DEST[1],
        )
        self.volunteer.profile.set_location(*ORIGIN)
        self.url = reverse("task_route", args=[self.task.pk])

    def tearDown(self):
        cache.clear()

    def _get(self, user, **params):
        self.client.force_login(user)
        with mock.patch(GET, return_value=_response(OSRM_OK)) as get:
            response = self.client.get(self.url, params)
        return response, get

    def test_response_shape_driving(self):
        response, get = self._get(self.volunteer)
        data = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["profile"], "driving")
        self.assertEqual(data["eta_min"], 7.0)
        self.assertEqual(len(data["steps"]), 4)
        self.assertIn("google", data["deep_links"])
        self.assertEqual(data["origin"]["source"], "saved")
        self.assertIn("/route/v1/driving/", get.call_args.args[0])

    def test_walking_profile(self):
        response, get = self._get(self.client_user, profile="walking")
        self.assertEqual(response.json()["profile"], "walking")
        self.assertIn("osrm-foot.test/route/v1/foot/", get.call_args.args[0])

    def test_bogus_profile_is_driving(self):
        response, _ = self._get(self.curator, profile="rocket")
        self.assertEqual(response.json()["profile"], "driving")

    def test_origin_live_for_volunteer(self):
        response, get = self._get(self.volunteer, lat="38.5", lng="68.7")
        self.assertEqual(response.json()["origin"]["source"], "live")
        self.assertIn("/68.7,38.5;", get.call_args.args[0])

    def test_origin_tracking_beats_older_saved_location(self):
        HelpRequest.objects.filter(pk=self.task.pk).update(
            volunteer_last_lat=38.552, volunteer_last_lng=68.782, volunteer_last_at=timezone.now())
        for user in (self.client_user, self.curator, self.volunteer):
            response, _ = self._get(user)
            origin = response.json()["origin"]
            self.assertEqual(origin["source"], "tracking", user.username)
            self.assertEqual(origin["lat"], 38.552)

    def test_newer_saved_location_beats_stale_tracking(self):
        HelpRequest.objects.filter(pk=self.task.pk).update(
            volunteer_last_lat=38.552, volunteer_last_lng=68.782,
            volunteer_last_at=timezone.now() - datetime.timedelta(hours=1))
        response, _ = self._get(self.client_user)
        self.assertEqual(response.json()["origin"]["source"], "saved")

    def test_client_cannot_inject_origin(self):
        response, _ = self._get(self.client_user, lat="1.0", lng="2.0")
        self.assertEqual(response.json()["origin"]["source"], "saved")

    def test_unrelated_users_forbidden(self):
        for user in (self.other_volunteer, self.other_client):
            response, get = self._get(user)
            self.assertEqual(response.status_code, 403)
            get.assert_not_called()

    def test_party_of_inactive_task_gets_no_route(self):
        self.task.status = "completed"
        self.task.save(update_fields=["status"])
        for user in (self.client_user, self.volunteer):
            response, get = self._get(user)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"success": False, "reason": "task_not_active"})
            get.assert_not_called()
        # Staff still can (CRM review of a finished task).
        response, _ = self._get(self.curator)
        self.assertEqual(response.status_code, 200)
        self.assertIn("origin", response.json())

    def test_no_contact_details_in_response(self):
        response, _ = self._get(self.client_user)
        body = response.content.decode()
        self.assertNotIn("@example.com", body)
        self.assertNotIn("+992900000000", body)
