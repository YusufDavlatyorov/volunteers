"""WebSocket layer (Feature Pack v2, feature 12): consumer, groups, publish
helpers, rate limits, and the "no phone/email in any outbound event" rule.

Consumer tests are TransactionTestCase: the consumer reads the DB through
``database_sync_to_async`` (another thread/connection), which can't see rows
inside a TestCase transaction. The in-memory channel layer + LocMemCache are
the test config — no Redis needed.
"""

import json
import re
from unittest import mock

from asgiref.sync import async_to_sync, sync_to_async
from channels.layers import channel_layers


def _import_communicator():
    """channels.testing's package __init__ imports daphne (for its live-server
    test case), which this project doesn't install — we serve with uvicorn.
    Stub it just for the import, then remove the stub again."""
    import sys
    import types

    try:
        from channels.testing import WebsocketCommunicator as communicator
        return communicator
    except ModuleNotFoundError:
        pass
    added = []
    for name in ("daphne", "daphne.testing"):
        if name not in sys.modules:
            stub = types.ModuleType(name)
            stub.DaphneProcess = object
            sys.modules[name] = stub
            added.append(name)
    try:
        from channels.testing.websocket import WebsocketCommunicator as communicator
    finally:
        for name in added:
            sys.modules.pop(name, None)
    return communicator


WebsocketCommunicator = _import_communicator()
from django.core.cache import cache
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from accounts.models import Users
from myapp.models import HelpRequest, Notification
from myapp.realtime import events, publish, ratelimit
from myapp.realtime.consumers import RealtimeConsumer
from myapp.services import access, notify as notify_service

PASSWORD = "Realtime-Tests-2026!"
CLIENT_PHONE = "+992901234567"

EMAIL_RE = re.compile(r"[^@\s\"]+@[^@\s\"]+\.[a-z]{2,}", re.I)
PHONE_RE = re.compile(r"(\+?992)?\s?\d{2}[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}")
FORBIDDEN_KEYS = {"phone", "email", "contact_phone", "address", "telegram_id", "password"}


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region="dushanbe", **flags
    )


def assert_no_pii(testcase, event, *extra_forbidden):
    """Recursively: no contact-ish keys, no email/phone-looking strings."""

    def walk(value, path="$"):
        if isinstance(value, dict):
            for key, item in value.items():
                testcase.assertNotIn(str(key).lower(), FORBIDDEN_KEYS, f"forbidden key at {path}.{key}")
                walk(item, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                walk(item, f"{path}[{i}]")
        elif isinstance(value, str):
            testcase.assertIsNone(EMAIL_RE.search(value), f"email-like text at {path}: {value!r}")
            testcase.assertIsNone(PHONE_RE.search(value), f"phone-like text at {path}: {value!r}")
            for needle in extra_forbidden:
                testcase.assertNotIn(needle, value, f"{needle!r} leaked at {path}")

    walk(event)
    text = json.dumps(event, ensure_ascii=False)
    for needle in extra_forbidden:
        testcase.assertNotIn(needle, text)


def _communicator(user=None, path="/ws/"):
    comm = WebsocketCommunicator(RealtimeConsumer.as_asgi(), path)
    if user is not None:
        comm.scope["user"] = user
    else:
        from django.contrib.auth.models import AnonymousUser

        comm.scope["user"] = AnonymousUser()
    return comm


async def _connect(user):
    comm = _communicator(user)
    connected, _ = await comm.connect()
    assert connected
    hello = await comm.receive_json_from()
    return comm, hello


class _LayerMixin:
    def setUp(self):
        super().setUp()
        cache.clear()
        channel_layers.backends.clear()  # a fresh in-memory layer per test


class ConsumerConnectTests(_LayerMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.volunteer = _make("rt_vol", is_volunteer=True)
        self.curator = _make("rt_cur", is_curator=True)
        self.admin = Users.objects.create_superuser(username="rt_admin", email="rt_admin@example.com", password=PASSWORD)

    def test_anonymous_is_closed_with_4401(self):
        async def run():
            comm = _communicator(None)
            connected, _ = await comm.connect()
            self.assertTrue(connected)  # accepted so the close code reaches the browser
            out = await comm.receive_output()
            self.assertEqual(out["type"], "websocket.close")
            self.assertEqual(out["code"], events.CLOSE_UNAUTHENTICATED)
            await comm.disconnect()
        async_to_sync(run)()

    def test_inactive_user_is_closed_with_4401(self):
        Users.objects.filter(pk=self.volunteer.pk).update(is_active=False)
        self.volunteer.refresh_from_db()

        async def run():
            comm = _communicator(self.volunteer)
            await comm.connect()
            out = await comm.receive_output()
            self.assertEqual(out.get("code"), events.CLOSE_UNAUTHENTICATED)
            await comm.disconnect()
        async_to_sync(run)()

    def test_hello_carries_id_and_role_only(self):
        async def run():
            comm, hello = await _connect(self.volunteer)
            self.assertEqual(hello["type"], "hello")
            self.assertEqual(hello["payload"], {"user_id": self.volunteer.pk, "role": "volunteer"})
            self.assertIn("ts", hello)
            await comm.disconnect()
        async_to_sync(run)()

    def test_staff_groups_only_for_staff(self):
        async def run():
            vol, _ = await _connect(self.volunteer)
            cur, _ = await _connect(self.curator)
            adm, _ = await _connect(self.admin)
            await sync_to_async(publish.send_to_staff)("crm.counters", {"open_sos": 1})
            await sync_to_async(publish.send_to_map_staff)("map.marker", {"kind": "task", "id": 1})
            for comm in (cur, adm):
                first = await comm.receive_json_from()
                second = await comm.receive_json_from()
                self.assertEqual({first["type"], second["type"]}, {"crm.counters", "map.marker"})
            self.assertTrue(await vol.receive_nothing(timeout=0.2))
            for comm in (vol, cur, adm):
                await comm.disconnect()
        async_to_sync(run)()

    def test_ping_pong_echoes_client_id(self):
        async def run():
            comm, _ = await _connect(self.volunteer)
            await comm.send_json_to({"type": "ping", "id": "abc"})
            pong = await comm.receive_json_from()
            self.assertEqual(pong["type"], "pong")
            self.assertEqual(pong.get("id"), "abc")
            await comm.disconnect()
        async_to_sync(run)()

    def test_unknown_type_and_bad_json_answer_with_error(self):
        async def run():
            comm, _ = await _connect(self.volunteer)
            await comm.send_json_to({"type": "nope", "id": 7})
            err = await comm.receive_json_from()
            self.assertEqual((err["type"], err["payload"]["code"], err["payload"]["ref"]), ("error", "unknown_type", 7))
            await comm.send_to(text_data="{not json")
            err = await comm.receive_json_from()
            self.assertEqual(err["payload"]["code"], "bad_json")
            await comm.send_json_to({"type": "chat.send", "payload": {"task_id": 1, "text": "hi"}})
            err = await comm.receive_json_from()
            self.assertEqual(err["payload"]["code"], "unsupported")
            await comm.disconnect()
        async_to_sync(run)()

    def test_oversized_message_closes_4408(self):
        async def run():
            comm, _ = await _connect(self.volunteer)
            await comm.send_to(text_data=json.dumps({"type": "ping", "pad": "x" * 5000}))
            out = await comm.receive_output()
            self.assertEqual(out["type"], "websocket.close")
            self.assertEqual(out["code"], events.CLOSE_POLICY)
        async_to_sync(run)()

    def test_rate_limit_errors_then_closes_on_abuse(self):
        async def run():
            comm, _ = await _connect(self.volunteer)
            limit, _window = ratelimit.LIMITS["default"]
            for _ in range(limit):
                await comm.send_json_to({"type": "ping"})
                self.assertEqual((await comm.receive_json_from())["type"], "pong")
            await comm.send_json_to({"type": "ping"})
            err = await comm.receive_json_from()
            self.assertEqual(err["payload"]["code"], "rate_limited")
            closed = None
            for _ in range(limit * ratelimit.ABUSE_FACTOR):
                await comm.send_json_to({"type": "ping"})
                out = await comm.receive_output()
                if out["type"] == "websocket.close":
                    closed = out
                    break
            self.assertIsNotNone(closed)
            self.assertEqual(closed["code"], events.CLOSE_POLICY)
        async_to_sync(run)()

    def test_heartbeat_and_connect_mark_presence_when_available(self):
        calls = []

        def fake_mark_seen(user, *, source):
            calls.append((user.pk, source))

        from myapp.services import presence

        with mock.patch.object(presence, "mark_seen", fake_mark_seen, create=True):
            async def run():
                comm, _ = await _connect(self.volunteer)
                await comm.send_json_to({"type": "presence.heartbeat", "payload": {"visible": True}})
                await comm.send_json_to({"type": "ping"})
                await comm.receive_json_from()  # pong: the heartbeat was processed first
                await comm.disconnect()
            async_to_sync(run)()
        self.assertEqual(calls, [(self.volunteer.pk, "ws"), (self.volunteer.pk, "ws")])


class TaskSubscribeTests(_LayerMixin, TransactionTestCase):
    """subscribe {topic: task} follows exactly the HTTP task-detail rule."""

    def setUp(self):
        super().setUp()
        self.client_user = _make("rt_client", is_client=True)
        self.other_client = _make("rt_client2", is_client=True)
        self.vol = _make("rt_sub_vol", is_volunteer=True)
        self.far_vol = Users.objects.create_user(
            username="rt_far_vol", email="rt_far_vol@example.com", password=PASSWORD, region="sogd", is_volunteer=True
        )
        self.curator = _make("rt_sub_cur", is_curator=True)
        self.task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="d", address="a",
            phone=CLIENT_PHONE, region="dushanbe",
        )

    async def _subscribe(self, user, task_id):
        comm, _ = await _connect(user)
        await comm.send_json_to({"type": "subscribe", "id": "s1", "payload": {"topic": "task", "id": task_id}})
        reply = await comm.receive_json_from()
        return comm, reply

    def test_matches_http_rule_for_every_role(self):
        cases = [
            (self.client_user, True), (self.other_client, False), (self.vol, True),
            (self.far_vol, False), (self.curator, True),
        ]

        async def run():
            for user, expected in cases:
                comm, reply = await self._subscribe(user, self.task.pk)
                with self.subTest(user=user.username):
                    self.assertEqual(reply["type"] == "subscribed", expected)
                    self.assertEqual(expected, await sync_to_async(access.can_view_task)(user, self.task))
                    if not expected:
                        self.assertEqual(reply["payload"]["code"], "forbidden")
                await comm.disconnect()
        async_to_sync(run)()

    def test_http_detail_view_agrees(self):
        for user, status in [(self.other_client, 302), (self.far_vol, 302), (self.client_user, 200)]:
            self.client.force_login(user)
            response = self.client.get(reverse("task_detail", args=[self.task.pk]))
            self.assertEqual(response.status_code, status, user.username)

    def test_missing_task_looks_like_forbidden(self):
        async def run():
            comm, reply = await self._subscribe(self.curator, 999999)
            self.assertEqual(reply["payload"]["code"], "forbidden")
            await comm.disconnect()
        async_to_sync(run)()

    def test_task_events_reach_only_subscribers_and_unsubscribe_stops_them(self):
        async def run():
            sub, reply = await self._subscribe(self.client_user, self.task.pk)
            self.assertEqual(reply["type"], "subscribed")
            denied, _ = await self._subscribe(self.other_client, self.task.pk)
            await sync_to_async(publish.send_to_task)(self.task, events.TASK_UPDATED,
                                                      events.task_updated_payload(self.task))
            got = await sub.receive_json_from()
            self.assertEqual(got["type"], "task.updated")
            self.assertEqual(got["payload"]["task_id"], self.task.pk)
            self.assertTrue(await denied.receive_nothing(timeout=0.2))

            await sub.send_json_to({"type": "unsubscribe", "payload": {"topic": "task", "id": self.task.pk}})
            self.assertEqual((await sub.receive_json_from())["type"], "unsubscribed")
            await sync_to_async(publish.send_to_task)(self.task.pk, events.TASK_UPDATED, {"task_id": self.task.pk})
            self.assertTrue(await sub.receive_nothing(timeout=0.2))
            await sub.disconnect()
            await denied.disconnect()
        async_to_sync(run)()

    def test_bad_subscribe_payloads(self):
        async def run():
            comm, _ = await _connect(self.curator)
            for payload in ({"topic": "user", "id": 1}, {"topic": "task", "id": "x"}, {"topic": "task", "id": True},
                            {"topic": "task"}):
                await comm.send_json_to({"type": "subscribe", "payload": payload})
                err = await comm.receive_json_from()
                self.assertEqual(err["payload"]["code"], "bad_request", payload)
            await comm.disconnect()
        async_to_sync(run)()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class NotificationOverSocketTests(_LayerMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.client_user = _make("rt_n_client", is_client=True)
        self.vol = _make("rt_n_vol", is_volunteer=True)

    def test_accept_flow_pushes_notification_without_contact_details(self):
        task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="call me at +992 90 765 43 21",
            address="Rudaki 10", phone=CLIENT_PHONE, region="dushanbe",
        )

        async def run():
            comm, _ = await _connect(self.client_user)
            await comm.send_json_to({"type": "subscribe", "payload": {"topic": "task", "id": task.pk}})
            await comm.receive_json_from()

            def accept():
                from django.test import Client

                c = Client()
                c.force_login(self.vol)
                return c.post(reverse("accept_task", args=[task.pk])).status_code

            self.assertEqual(await sync_to_async(accept)(), 302)
            # The live status change (task.updated) precedes the notification.
            update = await comm.receive_json_from()
            self.assertEqual(update["type"], "task.updated")
            self.assertEqual(update["payload"], {"task_id": task.pk, "status": "active", "work_stage": "assigned"})
            assert_no_pii(self, update, CLIENT_PHONE, "Rudaki", self.client_user.email, self.vol.email)
            event = await comm.receive_json_from()
            self.assertEqual(event["type"], "notification.new")
            self.assertEqual(event["payload"]["type"], "task_accepted")
            self.assertEqual(event["payload"]["params"]["task_id"], task.pk)
            assert_no_pii(self, event, CLIENT_PHONE, "Rudaki", self.client_user.email, self.vol.email)
            await comm.disconnect()
        async_to_sync(run)()

    def test_notification_read_over_socket_updates_every_tab(self):
        n = Notification.objects.create(recipient=self.vol, type="broadcast", title_key="t", body_key="b")
        other = Notification.objects.create(recipient=self.client_user, type="broadcast", title_key="t", body_key="b")

        async def run():
            tab1, _ = await _connect(self.vol)
            tab2, _ = await _connect(self.vol)
            # Someone else's id is ignored silently.
            await tab1.send_json_to({"type": "notification.read", "payload": {"ids": [n.pk, other.pk]}})
            for tab in (tab1, tab2):
                ev = await tab.receive_json_from()
                self.assertEqual(ev["type"], "notification.read")
                self.assertEqual(ev["payload"]["ids"], [n.pk])
                self.assertEqual(ev["payload"]["unread"], 0)
            await tab1.send_json_to({"type": "notification.read", "payload": {"all": True}})
            self.assertEqual((await tab2.receive_json_from())["payload"]["all"], True)
            await tab1.disconnect()
            await tab2.disconnect()
        async_to_sync(run)()
        n.refresh_from_db()
        other.refresh_from_db()
        self.assertIsNotNone(n.read_at)
        self.assertIsNone(other.read_at)


class AsgiApplicationTests(_LayerMixin, TransactionTestCase):
    """The real server/asgi.py stack: origin check + session auth."""

    def test_anonymous_through_full_stack_gets_4401(self):
        from server.asgi import application

        async def run():
            comm = WebsocketCommunicator(application, "/ws/", headers=[(b"origin", b"http://localhost"), (b"host", b"localhost")])
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            out = await comm.receive_output()
            self.assertEqual(out["code"], events.CLOSE_UNAUTHENTICATED)
            await comm.disconnect()
        with override_settings(ALLOWED_HOSTS=["localhost"]):
            async_to_sync(run)()

    def test_foreign_origin_is_rejected(self):
        # The validator snapshots ALLOWED_HOSTS when built, so build the same
        # stack as server/asgi.py under the overridden setting.
        from channels.auth import AuthMiddlewareStack
        from channels.routing import URLRouter
        from channels.security.websocket import AllowedHostsOriginValidator, OriginValidator

        from myapp.realtime.routing import websocket_urlpatterns
        from server.asgi import application

        self.assertIsInstance(application.application_mapping["websocket"], OriginValidator)

        async def run():
            stack = AllowedHostsOriginValidator(AuthMiddlewareStack(URLRouter(websocket_urlpatterns)))
            comm = WebsocketCommunicator(stack, "/ws/", headers=[(b"origin", b"https://evil.example")])
            connected, _ = await comm.connect()
            self.assertFalse(connected)
        with override_settings(ALLOWED_HOSTS=["localhost"], DEBUG=False):
            async_to_sync(run)()

    def test_session_cookie_authenticates(self):
        from server.asgi import application
        from django.test import Client

        user = _make("rt_cookie", is_client=True)
        c = Client()
        c.force_login(user)
        cookie = f"sessionid={c.cookies['sessionid'].value}".encode()

        async def run():
            comm = WebsocketCommunicator(application, "/ws/", headers=[
                (b"origin", b"http://localhost"), (b"host", b"localhost"), (b"cookie", cookie)])
            connected, _ = await comm.connect()
            self.assertTrue(connected)
            hello = await comm.receive_json_from()
            self.assertEqual(hello["payload"], {"user_id": user.pk, "role": "client"})
            await comm.disconnect()
        with override_settings(ALLOWED_HOSTS=["localhost"]):
            async_to_sync(run)()


class PublishHelperTests(TestCase):
    def test_publish_never_raises_when_layer_breaks(self):
        broken = mock.Mock()
        broken.group_send = mock.AsyncMock(side_effect=RuntimeError("redis down"))
        with mock.patch("myapp.realtime.publish.get_channel_layer", return_value=broken), \
                self.assertLogs("myapp.realtime.publish", level="WARNING"):
            self.assertFalse(publish.send_to_user(1, "x", {}))
        with mock.patch("myapp.realtime.publish.get_channel_layer", side_effect=RuntimeError("bad config")), \
                self.assertLogs("myapp.realtime.publish", level="WARNING"):
            self.assertFalse(publish.send_to_staff("x", {}))

    def test_publish_without_layer_is_a_noop(self):
        with mock.patch("myapp.realtime.publish.get_channel_layer", return_value=None):
            self.assertFalse(publish.send_to_task(5, "x", {}))

    def test_group_names(self):
        self.assertEqual(publish.user_group(3), "user_3")
        self.assertEqual(publish.task_group(4), "task_4")
        self.assertEqual((publish.STAFF_GROUP, publish.MAP_STAFF_GROUP), ("staff", "map_staff"))

    def test_notify_survives_a_broken_layer(self):
        user = _make("rt_broken", is_client=True)
        with mock.patch("myapp.realtime.publish.get_channel_layer", side_effect=RuntimeError("down")), \
                self.assertLogs("myapp.realtime.publish", level="WARNING"):
            reached = notify_service.notify([user], "task_completed", params={"task_id": 1})
        self.assertEqual(reached, 1)
        self.assertEqual(Notification.objects.filter(recipient=user).count(), 1)


class OutboundEventShapeTests(TestCase):
    """Every builder in myapp/realtime/events.py, fed with objects that DO hold
    contact details, must emit none of them."""

    def test_every_event_builder_is_free_of_contact_details(self):
        client_user = _make("shape_client", is_client=True)
        volunteer = _make("shape_vol", is_volunteer=True)
        task = HelpRequest.objects.create(
            client=client_user, volunteer=volunteer, status="active", help_type="grocery", description="d",
            address="Somoni 5", phone=CLIENT_PHONE, region="dushanbe",
        )
        notify_service.notify(
            [client_user], "task_accepted",
            params={"task_id": task.pk, "volunteer_name": volunteer.username,
                    "phone": CLIENT_PHONE, "email": volunteer.email, "note": "write to x@y.tj or +992 93 111 22 33"},
            extra={"phone": CLIENT_PHONE, "address": task.address, "client_name": client_user.username},
        )
        n = Notification.objects.get(recipient=client_user)
        built = [
            events.hello(client_user),
            events.pong("r"),
            events.error("forbidden", "r"),
            events.subscribed("task", task.pk),
            events.unsubscribed("task", task.pk),
            events.envelope(events.NOTIFICATION_NEW, events.notification_payload(n)),
            events.envelope(events.NOTIFICATION_READ, events.notification_read_payload(ids=[n.pk], unread=0)),
            events.envelope(events.NOTIFICATION_READ, events.notification_read_payload(all=True)),
            events.envelope(events.TASK_UPDATED, events.task_updated_payload(task)),
            events.envelope(events.AVAILABILITY_CHANGED, events.availability_changed_payload(volunteer.pk, "busy")),
        ]
        for event in built:
            with self.subTest(event=event["type"]):
                self.assertEqual(set(event) - {"id"}, {"type", "payload", "ts"})
                assert_no_pii(self, event, CLIENT_PHONE, "Somoni", client_user.email, volunteer.email)

    def test_every_notification_type_row_is_free_of_contact_details(self):
        """Run notify() for every registered type with contact details in
        `extra` (channel-only) and hostile params: the stored/published shape
        stays clean."""
        user = _make("shape_all", is_client=True)
        for code in notify_service.NOTIFICATION_TYPES:
            notify_service.notify(
                [user], code,
                params={"task_id": 1, "phone": CLIENT_PHONE, "contact_email": "a@b.tj", "x": "ring +992931112233"},
                extra={"phone": CLIENT_PHONE, "address": "Secret st. 1"},
            )
        for n in Notification.objects.filter(recipient=user):
            with self.subTest(type=n.type):
                assert_no_pii(self, events.envelope(events.NOTIFICATION_NEW, events.notification_payload(n)),
                              CLIENT_PHONE, "Secret st.")
