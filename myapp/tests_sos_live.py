"""Live SOS on the map (2026-10-09): an SOS reaches staff and the volunteers of
its region in real time (``sos.marker`` over the WebSocket + one
``sos_nearby`` notification), shows as a pulsing marker while it is active and
disappears when it is resolved / cancelled. Its location is shared only while
it is active; no event or notification carries a name, phone or reason.
"""

from unittest import mock

from asgiref.sync import async_to_sync, sync_to_async
from channels.layers import channel_layers
from django.core.cache import cache
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from accounts.models import Users
from myapp.models import EmergencyReport, HelpRequest, Notification
from myapp.realtime import events, groups, publish
from myapp.services import emergency
from myapp.tests_realtime import _connect, assert_no_pii

PASSWORD = "Sos-Live-Tests-2026!"
PHONE = "+992901234567"


def _user(username, region="dushanbe", **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region=region, **flags
    )


def _people():
    return {
        "admin": _user("sos_admin", is_superuser=True, is_staff=True),
        "curator": _user("sos_curator", region="sogd", is_curator=True),
        "reporter": _user("sos_reporter", is_volunteer=True),
        "neighbour": _user("sos_neighbour", is_volunteer=True),
        "far": _user("sos_far", region="gbao", is_volunteer=True),
        "client": _user("sos_client", is_client=True),
    }


def _active_task(client, volunteer, **extra):
    return HelpRequest.objects.create(
        client=client, volunteer=volunteer, status="active", help_type="grocery",
        description="Need groceries", address="ул. Рудаки 1", phone=PHONE, region="dushanbe",
        latitude="38.560000", longitude="68.780000", **extra,
    )


class SosPublishTests(TestCase):
    def setUp(self):
        self.p = _people()
        self.task = _active_task(self.p["client"], self.p["reporter"])

    def _report(self, **kw):
        with mock.patch.object(publish, "send_to_group", return_value=True) as sent:
            report, _ = emergency.report_emergency(
                volunteer=self.p["reporter"], help_request=self.task, reason="Dog attacked me", **kw,
            )
        return report, sent

    def test_new_sos_goes_to_staff_and_the_region_volunteers(self):
        report, sent = self._report()
        sos_calls = [c.args for c in sent.call_args_list if c.args[1] == events.SOS_MARKER]
        self.assertEqual({c[0] for c in sos_calls}, {groups.STAFF_GROUP, "volunteers_dushanbe"})
        payload = sos_calls[0][2]
        self.assertEqual(payload["id"], report.pk)
        self.assertEqual(payload["region"], "dushanbe")
        self.assertAlmostEqual(payload["lat"], 38.56)
        self.assertNotIn("removed", payload)
        # ids + point only: no reporter / client name, no phone, no reason.
        assert_no_pii(self, payload, "sos_reporter", "sos_client", "Dog attacked", PHONE)

    def test_resolve_and_cancel_publish_a_removal_without_coordinates(self):
        for action in ("resolve", "cancel"):
            with self.subTest(action=action):
                EmergencyReport.objects.all().delete()
                report, _ = self._report()
                with mock.patch.object(publish, "send_to_group", return_value=True) as sent:
                    getattr(emergency, action)(report, actor=self.p["curator"])
                payloads = [c.args[2] for c in sent.call_args_list if c.args[1] == events.SOS_MARKER]
                self.assertTrue(payloads)
                for payload in payloads:
                    self.assertEqual(payload, {"id": report.pk, "region": "dushanbe", "removed": True})

    def test_acknowledge_keeps_the_marker_with_the_new_status(self):
        report, _ = self._report()
        with mock.patch.object(publish, "send_to_group", return_value=True) as sent:
            emergency.acknowledge(report, actor=self.p["curator"])
        payload = [c.args[2] for c in sent.call_args_list if c.args[1] == events.SOS_MARKER][0]
        self.assertEqual(payload["status"], "acknowledged")
        self.assertIn("lat", payload)

    def test_no_region_reaches_no_volunteer_group(self):
        self.task.region = ""
        self.task.save(update_fields=["region"])
        self.p["reporter"].region = ""
        self.p["reporter"].save(update_fields=["region"])
        _, sent = self._report()
        targets = {c.args[0] for c in sent.call_args_list if c.args[1] == events.SOS_MARKER}
        self.assertEqual(targets, {groups.STAFF_GROUP})
        self.assertFalse(Notification.objects.filter(type="sos_nearby").exists())


class SosNearbyNotificationTests(TestCase):
    def setUp(self):
        self.p = _people()
        self.task = _active_task(self.p["client"], self.p["reporter"])

    def test_one_notification_to_other_volunteers_of_the_region_only(self):
        report, _ = emergency.report_emergency(volunteer=self.p["reporter"], help_request=self.task)
        rows = Notification.objects.filter(type="sos_nearby")
        self.assertEqual(set(rows.values_list("recipient__username", flat=True)), {"sos_neighbour"})
        row = rows.get()
        self.assertEqual(row.link, f"{reverse('map')}?sos={report.pk}")
        self.assertEqual(row.params, {"report_id": report.pk, "region": "dushanbe"})
        # A repeated press returns the same report and sends nothing new.
        emergency.report_emergency(volunteer=self.p["reporter"], help_request=self.task)
        self.assertEqual(Notification.objects.filter(type="sos_nearby").count(), 1)

    def test_texts_exist_and_never_mention_a_location(self):
        from myapp.services import notify_texts

        for lang in ("ru", "tj", "en"):
            subject, body = notify_texts.render_channel("sos_nearby", lang, {"region": "dushanbe"})
            self.assertTrue(subject and body)
            self.assertNotIn("{", body)
            self.assertNotIn("38.", body)
            title, text = notify_texts.render_inapp("ntf.sos_nearby.title", "ntf.sos_nearby.body", {}, lang)
            self.assertTrue(title and text)


class SosMapDataTests(TestCase):
    def setUp(self):
        self.p = _people()
        self.task = _active_task(self.p["client"], self.p["reporter"])
        self.report, _ = emergency.report_emergency(volunteer=self.p["reporter"], help_request=self.task)

    def _sos_points(self, who):
        self.client.force_login(self.p[who])
        data = self.client.get(reverse("map_data")).json()
        return [p for p in data["points"] if p["kind"] == "emergency"]

    def test_region_volunteer_sees_the_point_without_name_or_link(self):
        points = self._sos_points("neighbour")
        self.assertEqual([p["id"] for p in points], [self.report.pk])
        self.assertEqual(points[0]["url"], "")
        assert_no_pii(self, points, "sos_reporter", "sos_client", PHONE)

    def test_reporter_gets_a_link_to_their_own_report(self):
        points = self._sos_points("reporter")
        self.assertEqual(points[0]["url"], reverse("emergency_detail", args=[self.report.pk]))

    def test_other_region_volunteer_and_client_see_nothing(self):
        self.assertEqual(self._sos_points("far"), [])
        self.assertEqual(self._sos_points("client"), [])

    def test_staff_see_it_from_any_region(self):
        self.assertEqual([p["id"] for p in self._sos_points("curator")], [self.report.pk])

    def test_point_disappears_once_resolved(self):
        emergency.resolve(self.report, actor=self.p["curator"])
        self.assertEqual(self._sos_points("neighbour"), [])
        self.assertEqual(self._sos_points("curator"), [])


class SosSocketDeliveryTests(TransactionTestCase):
    """End to end over the consumer: who receives ``sos.marker``."""

    def setUp(self):
        super().setUp()
        cache.clear()
        channel_layers.backends.clear()
        self.p = _people()
        self.task = _active_task(self.p["client"], self.p["reporter"])

    def test_staff_and_same_region_volunteers_receive_others_do_not(self):
        async def run():
            socks = {name: (await _connect(self.p[name]))[0]
                     for name in ("admin", "curator", "neighbour", "far", "client")}
            report, _ = await sync_to_async(emergency.report_emergency)(
                volunteer=self.p["reporter"], help_request=self.task)

            async def sos_frames(comm):
                frames = []
                while not await comm.receive_nothing(timeout=0.3):
                    frame = await comm.receive_json_from()
                    if frame["type"] == events.SOS_MARKER:
                        frames.append(frame)
                return frames

            for name in ("admin", "curator", "neighbour"):
                frames = await sos_frames(socks[name])
                self.assertEqual(len(frames), 1, name)
                self.assertEqual(frames[0]["payload"]["id"], report.pk)
            for name in ("far", "client"):
                self.assertEqual(await sos_frames(socks[name]), [], name)

            await sync_to_async(emergency.cancel)(report, actor=self.p["curator"])
            frames = await sos_frames(socks["neighbour"])
            self.assertEqual(frames[0]["payload"], {"id": report.pk, "region": "dushanbe", "removed": True})
            for comm in socks.values():
                await comm.disconnect()
        async_to_sync(run)()

    def test_guard_drops_the_event_after_the_volunteer_moved_region(self):
        async def run():
            comm, _ = await _connect(self.p["neighbour"])
            await sync_to_async(Users.objects.filter(pk=self.p["neighbour"].pk).update)(region="khatlon")
            # The socket still sits in volunteers_dushanbe; the per-socket guard
            # re-reads the user (forced here) and refuses the delivery.
            consumer_user = self.p["neighbour"]
            consumer_user.region = "khatlon"
            payload = {"id": 1, "region": "dushanbe", "status": "open", "lat": 38.5, "lng": 68.7}
            from myapp.realtime.consumers import SosGuard

            class _Sock:
                is_staff = False
                user = consumer_user
            self.assertFalse(await SosGuard()(_Sock(), payload))
            _Sock.user = self.p["far"]
            self.assertFalse(await SosGuard()(_Sock(), payload))
            await comm.disconnect()
        async_to_sync(run)()


class RegionGroupNameTests(TestCase):
    def test_only_plain_region_codes_make_a_group(self):
        self.assertEqual(groups.region_volunteers_group("sogd"), "volunteers_sogd")
        for bad in ("", None, "all regions", "сугд", "a/b"):
            self.assertIsNone(groups.region_volunteers_group(bad))
