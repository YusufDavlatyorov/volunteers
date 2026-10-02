"""WebSocket transport for live tracking (Feature Pack v2, milestone 2):
``location.update`` / ``tracking.stop`` handlers, per-socket delivery guards
(``location.updated`` / ``tracking.stopped`` / ``map.marker`` re-checked
against ``access.can_see_location`` at delivery time), subscription drop on
lost visibility, deactivated-user close, and the milestone-2 notification
texts (``tracking_stopped``, ``task_assigned`` + ``urgent_override``).

The handler tests mock the tracking service (``consumers._tracking``) so they
don't depend on its internals; ``RealTrackingServiceIntegrationTests`` runs
against the real ``myapp/services/tracking.py``.
"""

import importlib.util
from types import SimpleNamespace
from unittest import mock

from asgiref.sync import async_to_sync, sync_to_async
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from accounts.models import Profile, Users
from myapp.models import HelpRequest, Notification
from myapp.realtime import consumers, events, publish, ratelimit
from myapp.services import notify as notify_service, notify_texts
from myapp.tests_realtime import CLIENT_PHONE, PASSWORD, _LayerMixin, _connect, _make, assert_no_pii

LAT, LNG = 38.5598, 68.7870  # Dushanbe
LOCMEM = "django.core.mail.backends.locmem.EmailBackend"


NOISE = {events.AVAILABILITY_CHANGED}  # presence churn as test sockets come and go (staff group)


async def collect(comm, timeout=0.3):
    """Every frame currently queued for ``comm`` (until ``timeout`` of
    silence), minus presence noise."""
    out = []
    while not await comm.receive_nothing(timeout=timeout):
        frame = await comm.receive_json_from()
        if frame.get("type") not in NOISE:
            out.append(frame)
    return out


def types_of(frames):
    return [f["type"] for f in frames]


async def subscribe(comm, task_id):
    await comm.send_json_to({"type": "subscribe", "payload": {"topic": "task", "id": task_id}})
    reply = await comm.receive_json_from()
    assert reply["type"] == "subscribed", reply
    return reply


def _location(task, lat=LAT, lng=LNG):
    return events.location_updated_payload(task.pk, lat, lng, timezone.now(), accuracy=12)


class _TaskFixture(_LayerMixin):
    def setUp(self):
        super().setUp()
        self.client_user = _make("trk_client", is_client=True)
        self.vol = _make("trk_vol", is_volunteer=True)
        self.other_vol = _make("trk_other_vol", is_volunteer=True)
        self.curator = _make("trk_cur", is_curator=True)
        self.task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.vol, status="active", help_type="grocery",
            description="d", address="Rudaki 10", phone=CLIENT_PHONE, region="dushanbe",
        )


# --------------------------------------------------------------------------- #
# inbound: location.update
# --------------------------------------------------------------------------- #

class LocationUpdateHandlerTests(_TaskFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.service = mock.Mock()
        self.service.accept_point.return_value = SimpleNamespace(ok=True, code="ok", payload={})
        patcher = mock.patch.object(consumers, "_tracking", return_value=self.service)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _send(self, payloads, user=None):
        async def run():
            comm, _ = await _connect(user or self.vol)
            replies = []
            for payload in payloads:
                await comm.send_json_to({"type": "location.update", "id": "L", "payload": payload})
                replies.append(await comm.receive_json_from())
            await comm.disconnect()
            return replies
        return async_to_sync(run)()

    def test_ok_point_is_dispatched_and_acked(self):
        [reply] = self._send([{"task_id": self.task.pk, "lat": LAT, "lng": LNG, "accuracy": 8,
                               "speed": 1.5, "heading": 90}])
        self.assertEqual(reply["type"], "ack")
        self.assertEqual(reply["id"], "L")
        self.assertEqual(reply["payload"], {"for": "location.update", "task_id": self.task.pk, "code": "ok"})
        args, kwargs = self.service.accept_point.call_args
        self.assertEqual(args[0].pk, self.vol.pk)
        self.assertEqual(args[1:], (self.task.pk, LAT, LNG))
        self.assertEqual(kwargs, {"accuracy": 8.0, "speed": 1.5, "heading": 90.0, "source": "ws"})

    def test_optional_fields_default_to_none(self):
        self._send([{"task_id": self.task.pk, "lat": LAT, "lng": LNG}])
        _, kwargs = self.service.accept_point.call_args
        self.assertEqual((kwargs["accuracy"], kwargs["speed"], kwargs["heading"]), (None, None, None))

    def test_rejected_points_relay_the_service_code(self):
        for code in ("forbidden", "invalid", "inaccurate", "jump", "too_soon"):
            with self.subTest(code=code):
                cache.clear()  # fresh rate-limit window per sub-case
                self.service.accept_point.return_value = SimpleNamespace(ok=False, code=code, payload=None)
                [reply] = self._send([{"task_id": self.task.pk, "lat": LAT, "lng": LNG, "accuracy": 5}])
                self.assertEqual((reply["type"], reply["payload"]["code"], reply["id"]), ("error", code, "L"))

    def test_unknown_service_code_degrades_to_forbidden(self):
        self.service.accept_point.return_value = SimpleNamespace(ok=False, code="weird", payload=None)
        [reply] = self._send([{"task_id": self.task.pk, "lat": LAT, "lng": LNG}])
        self.assertEqual(reply["payload"]["code"], "forbidden")

    def test_malformed_payloads_are_bad_request_and_never_reach_the_service(self):
        bad = [
            {"lat": LAT, "lng": LNG},
            {"task_id": "x", "lat": LAT, "lng": LNG},
            {"task_id": True, "lat": LAT, "lng": LNG},
            {"task_id": self.task.pk, "lat": "38.5", "lng": LNG},
            {"task_id": self.task.pk, "lat": True, "lng": LNG},
            {"task_id": self.task.pk, "lat": LAT},
            {"task_id": self.task.pk, "lat": LAT, "lng": LNG, "accuracy": "10"},
            {"task_id": self.task.pk, "lat": LAT, "lng": LNG, "heading": [1]},
        ]
        # Malformed frames hit the per-user location bucket too; give each a fresh window.
        with mock.patch("myapp.realtime.ratelimit.LIMITS", {**ratelimit.LIMITS, "location.update": (100, 5)}):
            replies = self._send(bad)
        for payload, reply in zip(bad, replies):
            with self.subTest(payload=payload):
                self.assertEqual(reply["payload"]["code"], "bad_request")
        self.service.accept_point.assert_not_called()

    def test_rate_limit_is_an_error_not_a_close(self):
        frozen = mock.Mock()
        frozen.time.return_value = 1_000_000.0  # one fixed 5 s window for the whole test

        async def run():
            comm, _ = await _connect(self.vol)
            payload = {"task_id": self.task.pk, "lat": LAT, "lng": LNG}
            await comm.send_json_to({"type": "location.update", "payload": payload})
            self.assertEqual((await comm.receive_json_from())["type"], "ack")
            for _ in range(10):  # well past 2× the limit: still no close
                await comm.send_json_to({"type": "location.update", "payload": payload})
                reply = await comm.receive_json_from()
                self.assertEqual(reply["payload"]["code"], "rate_limited")
            await comm.send_json_to({"type": "ping"})
            self.assertEqual((await comm.receive_json_from())["type"], "pong")
            await comm.disconnect()

        with mock.patch("myapp.realtime.ratelimit.time", frozen):
            async_to_sync(run)()
        self.assertEqual(self.service.accept_point.call_count, 1)

    def test_service_crash_answers_server_error_and_keeps_the_socket(self):
        self.service.accept_point.side_effect = RuntimeError("boom")

        async def run():
            comm, _ = await _connect(self.vol)
            await comm.send_json_to({"type": "location.update", "payload": {"task_id": self.task.pk, "lat": LAT, "lng": LNG}})
            self.assertEqual((await comm.receive_json_from())["payload"]["code"], "server_error")
            await comm.send_json_to({"type": "ping"})
            self.assertEqual((await comm.receive_json_from())["type"], "pong")
            await comm.disconnect()

        with self.assertLogs("myapp.realtime.consumers", level="ERROR"):
            async_to_sync(run)()

    def test_missing_service_answers_unsupported(self):
        with mock.patch.object(consumers, "_tracking", side_effect=ImportError("no tracking yet")):
            [reply] = self._send([{"task_id": self.task.pk, "lat": LAT, "lng": LNG}])
        self.assertEqual(reply["payload"]["code"], "unsupported")


# --------------------------------------------------------------------------- #
# inbound: tracking.stop
# --------------------------------------------------------------------------- #

class TrackingStopHandlerTests(_TaskFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.service = mock.Mock()
        self.service.stop.return_value = True
        patcher = mock.patch.object(consumers, "_tracking", return_value=self.service)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _stop(self, user, task_id):
        async def run():
            comm, _ = await _connect(user)
            await comm.send_json_to({"type": "tracking.stop", "id": 9, "payload": {"task_id": task_id}})
            reply = await comm.receive_json_from()
            await comm.disconnect()
            return reply
        return async_to_sync(run)()

    def test_assigned_volunteer_can_stop(self):
        reply = self._stop(self.vol, self.task.pk)
        self.assertEqual(reply["type"], "ack")
        self.assertEqual(reply["payload"], {"for": "tracking.stop", "task_id": self.task.pk, "code": "ok"})
        args, kwargs = self.service.stop.call_args
        self.assertEqual((args[0].pk, args[1].pk, kwargs), (self.vol.pk, self.task.pk, {"reason": "volunteer"}))

    def test_nobody_else_can_stop(self):
        for user in (self.other_vol, self.client_user, self.curator):
            with self.subTest(user=user.username):
                reply = self._stop(user, self.task.pk)
                self.assertEqual((reply["type"], reply["payload"]["code"]), ("error", "forbidden"))
        reply = self._stop(self.vol, 999999)
        self.assertEqual(reply["payload"]["code"], "forbidden")
        self.service.stop.assert_not_called()

    def test_a_refused_stop_is_forbidden(self):
        self.service.stop.return_value = False  # e.g. the task is no longer active
        self.assertEqual(self._stop(self.vol, self.task.pk)["payload"]["code"], "forbidden")
        self.service.stop.return_value = True
        self.assertEqual(self._stop(self.vol, self.task.pk)["type"], "ack")


# --------------------------------------------------------------------------- #
# outbound: delivery guards
# --------------------------------------------------------------------------- #

class DeliveryGuardTests(_TaskFixture, TransactionTestCase):
    """The owner requirement: location events reach a socket only if
    ``access.can_see_location`` holds for its user *now*."""

    def setUp(self):
        super().setUp()
        # The other volunteer joins while the task is still pending (allowed).
        HelpRequest.objects.filter(pk=self.task.pk).update(status="pending", volunteer=None)

    def _activate(self):
        HelpRequest.objects.filter(pk=self.task.pk).update(status="active", volunteer=self.vol)

    def test_only_location_viewers_receive_location_updated(self):
        async def run():
            other, _ = await _connect(self.other_vol)
            await subscribe(other, self.task.pk)
            # Assigned behind the socket's back (no task.updated): the guard
            # alone must keep the location away from the unrelated volunteer.
            await sync_to_async(self._activate)()
            parties = {}
            for user in (self.client_user, self.vol, self.curator):
                comm, _ = await _connect(user)
                await subscribe(comm, self.task.pk)
                parties[user.username] = comm
            await sync_to_async(publish.send_to_task)(self.task, events.LOCATION_UPDATED, _location(self.task))
            for name, comm in parties.items():
                frames = await collect(comm)
                with self.subTest(user=name):
                    self.assertEqual(types_of(frames), ["location.updated"])
                    self.assertEqual(frames[0]["payload"]["task_id"], self.task.pk)
                    assert_no_pii(self, frames[0], CLIENT_PHONE, "Rudaki", self.client_user.email, self.vol.email)
                await comm.disconnect()
            self.assertEqual(await collect(other), [])
            await other.disconnect()
        async_to_sync(run)()

    def test_lost_visibility_unsubscribes_with_reason_forbidden(self):
        async def run():
            other, _ = await _connect(self.other_vol)
            await subscribe(other, self.task.pk)
            await sync_to_async(self._activate)()
            task = await sync_to_async(HelpRequest.objects.get)(pk=self.task.pk)
            await sync_to_async(publish.task_changed)(task)
            frames = await collect(other)
            self.assertEqual(types_of(frames), ["task.updated", "unsubscribed"])
            self.assertEqual(frames[1]["payload"], {"topic": "task", "id": self.task.pk, "reason": "forbidden"})
            # Really gone from the group: later task events don't arrive.
            await sync_to_async(publish.send_to_task)(self.task, events.TASK_UPDATED, {"task_id": self.task.pk})
            await sync_to_async(publish.send_to_task)(self.task, events.LOCATION_UPDATED, _location(self.task))
            self.assertEqual(await collect(other), [])
            await other.disconnect()
        async_to_sync(run)()

    def test_client_stops_receiving_location_after_completion(self):
        self._activate()

        async def run():
            client, _ = await _connect(self.client_user)
            await subscribe(client, self.task.pk)
            await sync_to_async(publish.send_to_task)(self.task, events.LOCATION_UPDATED, _location(self.task))
            self.assertEqual(types_of(await collect(client)), ["location.updated"])  # cached: allowed

            def complete():
                HelpRequest.objects.filter(pk=self.task.pk).update(status="completed")
                task = HelpRequest.objects.get(pk=self.task.pk)
                publish.task_changed(task)
                publish.send_to_task(task, events.TRACKING_STOPPED,
                                     events.tracking_stopped_payload(task.pk, "task_ended"))
                publish.send_to_task(task, events.LOCATION_UPDATED, _location(task))  # a late point

            await sync_to_async(complete)()
            frames = await collect(client)
            # task.updated invalidated the cached "yes"; the client keeps the
            # subscription (they may still open the task), learns tracking
            # ended, and never gets the late point.
            self.assertEqual(types_of(frames), ["task.updated", "tracking.stopped"])
            self.assertEqual(frames[1]["payload"], {"task_id": self.task.pk, "reason": "task_ended"})
            await client.disconnect()
        async_to_sync(run)()

    def test_tracking_stopped_never_reaches_unrelated_subscribers(self):
        async def run():
            other, _ = await _connect(self.other_vol)
            await subscribe(other, self.task.pk)
            await sync_to_async(self._activate)()
            await sync_to_async(publish.send_to_task)(
                self.task, events.TRACKING_STOPPED, events.tracking_stopped_payload(self.task.pk, "volunteer"))
            self.assertEqual(await collect(other), [])
            await other.disconnect()
        async_to_sync(run)()

    def test_map_markers_need_active_staff_and_a_live_task(self):
        self._activate()

        async def run():
            cur, _ = await _connect(self.curator)
            marker = events.volunteer_live_marker_payload(self.task.pk, LAT, LNG, timezone.now())
            await sync_to_async(publish.send_to_map_staff)(events.MAP_MARKER, marker)
            frames = await collect(cur)
            self.assertEqual(types_of(frames), ["map.marker"])
            assert_no_pii(self, frames[0], CLIENT_PHONE, self.vol.email)

            def finish():
                HelpRequest.objects.filter(pk=self.task.pk).update(status="completed")
                publish.task_changed(HelpRequest.objects.get(pk=self.task.pk))  # staff group too
                publish.send_to_map_staff(events.MAP_MARKER, marker)

            await sync_to_async(finish)()
            self.assertEqual(types_of(await collect(cur)), ["task.updated"])  # stale marker dropped
            # ...but the removal marker (no coordinates) still reaches staff.
            removal = {"kind": "volunteer_live", "task_id": self.task.pk, "removed": True}
            await sync_to_async(publish.send_to_map_staff)(events.MAP_MARKER, removal)
            self.assertEqual([f["payload"] for f in await collect(cur)], [removal])
            await cur.disconnect()
        async_to_sync(run)()

    def test_demoted_curator_stops_getting_staff_events(self):
        async def run():
            cur, _ = await _connect(self.curator)
            await sync_to_async(Users.objects.filter(pk=self.curator.pk).update)(is_curator=False, is_volunteer=True)
            with mock.patch.object(consumers, "USER_RECHECK_SECONDS", 0):
                await sync_to_async(publish.send_to_map_staff)(events.MAP_MARKER, {"kind": "task", "id": 1})
                self.assertEqual(await collect(cur), [])
                await sync_to_async(publish.send_to_staff)("crm.counters", {"open_sos": 1})
                self.assertEqual(await collect(cur), [])  # left the staff groups
            await cur.disconnect()
        async_to_sync(run)()

    def test_deactivated_user_is_closed_4401(self):
        async def run():
            for trigger in ("inbound", "outbound"):
                comm, _ = await _connect(self.client_user)
                await sync_to_async(Users.objects.filter(pk=self.client_user.pk).update)(is_active=False)
                with mock.patch.object(consumers, "USER_RECHECK_SECONDS", 0):
                    if trigger == "inbound":
                        await comm.send_json_to({"type": "presence.heartbeat", "payload": {"visible": True}})
                    else:
                        await sync_to_async(publish.send_to_user)(self.client_user, "notification.read", {"all": True})
                    out = await comm.receive_output()
                with self.subTest(trigger=trigger):
                    self.assertEqual((out["type"], out.get("code")), ("websocket.close", events.CLOSE_UNAUTHENTICATED))
                await comm.disconnect()
                await sync_to_async(Users.objects.filter(pk=self.client_user.pk).update)(is_active=True)
        async_to_sync(run)()

    def test_guard_cache_is_bounded_and_per_task(self):
        consumer = consumers.RealtimeConsumer()
        consumer.guard_cache = {("location", 1): True, ("location_party", 1): True, ("location", 2): False}
        consumer.invalidate_task(1)
        self.assertEqual(consumer.guard_cache, {("location", 2): False})
        self.assertIn(events.LOCATION_UPDATED, consumers.DELIVERY_GUARDS)
        self.assertIn(events.TRACKING_STOPPED, consumers.DELIVERY_GUARDS)
        self.assertIn(events.MAP_MARKER, consumers.DELIVERY_GUARDS)


class TrackingEventShapeTests(TestCase):
    def test_new_builders_are_free_of_contact_details(self):
        now = timezone.now()
        built = [
            events.ack("location.update", 5),
            events.unsubscribed("task", 5, reason="forbidden"),
            events.envelope(events.LOCATION_UPDATED, events.location_updated_payload(5, LAT, LNG, now, accuracy=7)),
            events.envelope(events.LOCATION_UPDATED,
                            events.location_updated_payload(5, LAT, LNG, now, accuracy=None, eta_min=4)),
            events.envelope(events.TRACKING_STOPPED, events.tracking_stopped_payload(5, "volunteer")),
            events.envelope(events.MAP_MARKER, events.volunteer_live_marker_payload(5, LAT, LNG, now)),
        ]
        for event in built:
            with self.subTest(event=event["type"]):
                self.assertEqual(set(event) - {"id"}, {"type", "payload", "ts"})
                assert_no_pii(self, event, CLIENT_PHONE)
        loc = built[2]["payload"]
        self.assertEqual(set(loc), {"task_id", "lat", "lng", "ts", "accuracy"})
        self.assertEqual(set(built[5]["payload"]), {"kind", "task_id", "lat", "lng", "ts"})


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #

@override_settings(EMAIL_BACKEND=LOCMEM, TELEGRAM_BOT_TOKEN="")
class TrackingNotificationTests(TestCase):
    def setUp(self):
        self.curator = _make("trk_n_cur", is_curator=True)
        self.vol = _make("trk_n_vol", is_volunteer=True)
        self.client_user = _make("trk_n_client", is_client=True)
        mail.outbox = []  # welcome mails from user creation

    def test_tracking_stopped_is_staff_in_app_only(self):
        ntype = notify_service.NOTIFICATION_TYPES["tracking_stopped"]
        self.assertEqual(set(ntype.roles), {"admin", "curator"})
        reached = notify_service.notify([self.curator], "tracking_stopped", params={"task_id": 7})
        self.assertEqual(reached, 1)
        self.assertEqual(mail.outbox, [])
        n = Notification.objects.get(recipient=self.curator)
        self.assertEqual((n.title_key, n.body_key), ("ntf.tracking_stopped.title", "ntf.tracking_stopped.body"))
        self.assertEqual(notify_texts.render_inapp(n.title_key, n.body_key, n.params, "ru")[1],
                         "Волонтёр прекратил передачу местоположения по запросу #7.")

    def test_urgent_override_assignment_texts(self):
        Profile.objects.filter(user=self.client_user).update(language="en")
        extra = {"help_type_name": "Продукты и покупки", "client_name": self.client_user.username,
                 "phone": CLIENT_PHONE, "address": "Rudaki 10"}
        params = {"task_id": 42, "help_type": "grocery"}
        notify_service.notify([self.vol], "task_assigned", variant="urgent_override", params=params, extra=extra)
        notify_service.notify([self.client_user], "task_assigned", params=params, extra=extra)

        vol_row = Notification.objects.get(recipient=self.vol)
        self.assertEqual(vol_row.title_key, "ntf.task_assigned.urgent_override.title")
        title, body = notify_texts.render_inapp(vol_row.title_key, vol_row.body_key, vol_row.params, "ru")
        self.assertEqual(title, "Срочное назначение")
        self.assertEqual(body, "Куратор срочно назначил вам запрос #42, хотя у вас включён режим «не беспокоить».")
        client_row = Notification.objects.get(recipient=self.client_user)
        self.assertEqual(client_row.title_key, "ntf.task_assigned.title")

        by_to = {m.to[0]: m for m in mail.outbox}
        vol_mail = by_to[self.vol.email]
        self.assertEqual(vol_mail.subject, "Срочное назначение")
        self.assertIn("не беспокоить", vol_mail.body)
        self.assertIn("#42", vol_mail.body)
        self.assertEqual(by_to[self.client_user.email].subject, "A help request has been assigned")

    def test_variant_rendering_falls_back_and_covers_every_language(self):
        base = notify_texts.render_channel("task_assigned", "ru", {"task_id": 1})
        self.assertEqual(notify_texts.render_channel("task_assigned", "ru", {"task_id": 1}, "nope"), base)
        for lang, subject in (("ru", "Срочное назначение"), ("tj", "Таъиноти фаврӣ"), ("en", "Urgent assignment")):
            self.assertEqual(notify_texts.render_channel("task_assigned", lang, {}, "urgent_override")[0], subject)
        for lang in notify_texts.LANGS:
            keys = notify_texts.all_i18n_keys(lang)
            for key in ("ntf.tracking_stopped.title", "ntf.tracking_stopped.body",
                        "ntf.task_assigned.urgent_override.title", "ntf.task_assigned.urgent_override.body",
                        "ntf.type.tracking_stopped"):
                self.assertIn(key, keys)


# --------------------------------------------------------------------------- #
# integration with the real tracking service
# --------------------------------------------------------------------------- #

HAS_TRACKING = importlib.util.find_spec("myapp.services.tracking") is not None


@override_settings(EMAIL_BACKEND=LOCMEM, TELEGRAM_BOT_TOKEN="")
@mock.patch("myapp.realtime.ratelimit.LIMITS", {**ratelimit.LIMITS, "location.update": (100, 5)})
class RealTrackingServiceIntegrationTests(_TaskFixture, TransactionTestCase):
    def setUp(self):
        if not HAS_TRACKING:
            self.skipTest("myapp/services/tracking.py (geo-engineer, milestone 2) not present yet")
        super().setUp()
        from myapp.services import tracking

        self.tracking = tracking
        tracking.give_consent(self.vol)

    def test_point_reaches_client_and_staff_map_then_stop(self):
        async def run():
            vol, _ = await _connect(self.vol)
            client, _ = await _connect(self.client_user)
            await subscribe(client, self.task.pk)
            other, _ = await _connect(self.other_vol)
            cur, _ = await _connect(self.curator)

            await vol.send_json_to({"type": "location.update", "id": "p1",
                                    "payload": {"task_id": self.task.pk, "lat": LAT, "lng": LNG, "accuracy": 10}})
            ack = [f for f in await collect(vol) if f["type"] in ("ack", "error")]
            self.assertEqual(ack[0]["type"], "ack", ack)

            frames = await collect(client)
            located = [f for f in frames if f["type"] == "location.updated"]
            self.assertEqual(len(located), 1, frames)
            self.assertEqual(located[0]["payload"]["task_id"], self.task.pk)
            self.assertAlmostEqual(float(located[0]["payload"]["lat"]), LAT, places=4)
            assert_no_pii(self, located[0], CLIENT_PHONE, "Rudaki", self.client_user.email, self.vol.email)

            markers = [f for f in await collect(cur) if f["type"] == "map.marker"]
            self.assertEqual(len(markers), 1)
            assert_no_pii(self, markers[0], CLIENT_PHONE, self.vol.email)
            self.assertEqual(await collect(other), [])

            await vol.send_json_to({"type": "tracking.stop", "payload": {"task_id": self.task.pk}})
            self.assertIn("ack", types_of(await collect(vol)))
            stopped = [f for f in await collect(client) if f["type"] == "tracking.stopped"]
            self.assertEqual(stopped[0]["payload"], {"task_id": self.task.pk, "reason": "volunteer"})
            for comm in (vol, client, other, cur):
                await comm.disconnect()
        async_to_sync(run)()
        self.assertTrue(Notification.objects.filter(recipient=self.curator, type="tracking_stopped").exists())

    def test_task_end_tells_the_client_and_removes_the_staff_marker(self):
        async def run():
            vol, _ = await _connect(self.vol)
            client, _ = await _connect(self.client_user)
            await subscribe(client, self.task.pk)
            cur, _ = await _connect(self.curator)
            await vol.send_json_to({"type": "location.update",
                                    "payload": {"task_id": self.task.pk, "lat": LAT, "lng": LNG, "accuracy": 10}})
            await collect(vol)
            await collect(client)
            await collect(cur)

            def complete():
                HelpRequest.objects.filter(pk=self.task.pk).update(status="completed")
                task = HelpRequest.objects.get(pk=self.task.pk)
                self.tracking.end(task)
                publish.task_changed(task)

            await sync_to_async(complete)()
            frames = await collect(client)
            self.assertIn({"task_id": self.task.pk, "reason": "task_ended"},
                          [f["payload"] for f in frames if f["type"] == "tracking.stopped"])
            removed = [f["payload"] for f in await collect(cur) if f["type"] == "map.marker"]
            self.assertEqual([m.get("removed") for m in removed], [True])

            await vol.send_json_to({"type": "location.update",
                                    "payload": {"task_id": self.task.pk, "lat": LAT, "lng": LNG, "accuracy": 10}})
            reply = [f for f in await collect(vol) if f["type"] in ("ack", "error")][0]
            self.assertEqual(reply["payload"]["code"], "forbidden")
            await vol.send_json_to({"type": "tracking.stop", "payload": {"task_id": self.task.pk}})
            reply = [f for f in await collect(vol) if f["type"] in ("ack", "error")][0]
            self.assertEqual(reply["payload"]["code"], "forbidden")
            self.assertEqual([f for f in await collect(client) if f["type"] == "location.updated"], [])
            for comm in (vol, client, cur):
                await comm.disconnect()
        async_to_sync(run)()

    def test_point_from_non_assigned_volunteer_is_forbidden(self):
        self.tracking.give_consent(self.other_vol)

        async def run():
            comm, _ = await _connect(self.other_vol)
            await comm.send_json_to({"type": "location.update",
                                     "payload": {"task_id": self.task.pk, "lat": LAT, "lng": LNG, "accuracy": 10}})
            reply = await comm.receive_json_from()
            self.assertEqual((reply["type"], reply["payload"]["code"]), ("error", "forbidden"))
            await comm.disconnect()
        async_to_sync(run)()
