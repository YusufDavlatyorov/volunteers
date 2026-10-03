"""Volunteer community bot (Feature Pack v2, milestone 5, feature 5).

Telegram is never contacted: ``myapp.telegram.api.requests.post`` is mocked
and records every Bot API call. Locks: only eligible users get single-use
expiring invites (hash stored, not the link), joiners are bound to their
invite or removed, revocation and the sweep remove people who lost
eligibility, moderation rules, the weekly "thank you" without rankings that
respects the opt-out, and mirrored posts that never duplicate.
"""

from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile, Users
from myapp.models import (
    Achievement,
    Broadcast,
    CommunityInvite,
    CommunityMember,
    CommunityPost,
    HelpRequest,
    ModerationAction,
    Notification,
    UserAchievement,
    VolunteerApplication,
)
from myapp.services import community, task_actions
from myapp.telegram.community_bot import core
from myapp.telegram.community_bot.core import GroupMessage

PASSWORD = "Tg-Community-2026!"
GENERAL = -1001111
SOGD = -1002222
SETTINGS = dict(
    TELEGRAM_COMMUNITY_BOT_TOKEN="555:COMMUNITY", TELEGRAM_COMMUNITY_CHAT_IDS=f"general:{GENERAL},sogd:{SOGD}",
    TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)


def _make(username, tg=None, **flags):
    user = Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD,
        region=flags.pop("region", "dushanbe"), telegram_id=tg, **flags,
    )
    Profile.objects.filter(user=user).update(full_name=username.title())
    return Users.objects.get(pk=user.pk)


class FakeTelegram:
    """Records Bot API calls made through myapp.telegram.api."""

    def __init__(self):
        self.calls = []
        self.counter = 0

    def __call__(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json))
        self.counter += 1
        result = True
        if method == "createChatInviteLink":
            result = {"invite_link": f"https://t.me/+invite{self.counter}"}
        elif method == "sendMessage":
            result = {"message_id": 100 + self.counter}
        response = mock.Mock(status_code=200)
        response.json.return_value = {"ok": True, "result": result}
        return response

    def methods(self):
        return [m for m, _ in self.calls]


@override_settings(**SETTINGS)
class CommunityBase(TestCase):
    def setUp(self):
        cache.clear()
        self.tg = FakeTelegram()
        patcher = mock.patch("myapp.telegram.api.requests.post", side_effect=self.tg)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.vol = _make("cm_vol", tg=2001, is_volunteer=True, region="sogd")
        self.vol2 = _make("cm_vol2", tg=2002, is_volunteer=True)
        self.curator = _make("cm_curator", tg=2003, is_curator=True)
        self.client_user = _make("cm_client", tg=2004, is_client=True)
        self.unlinked = _make("cm_unlinked", is_volunteer=True)

    def invite_url(self, user, key="general"):
        outcome = community.create_invites(user)
        self.assertTrue(outcome.ok, outcome.code)
        cache.delete(f"community:invite:{user.pk}")
        return dict(outcome.links)[key]

    def gm(self, from_id, text="hi", chat_id=GENERAL, **kw):
        return GroupMessage(chat_id=chat_id, message_id=kw.pop("message_id", 50), from_id=from_id, text=text, **kw)


class ConfigTests(CommunityBase):
    def test_chat_ids_parsing(self):
        self.assertEqual(community.chats(), {"general": GENERAL, "sogd": SOGD})
        with self.settings(TELEGRAM_COMMUNITY_CHAT_IDS="-100123, junk, mars:-100, sogd:x"):
            self.assertEqual(community.chats(), {"general": -100123})
        with self.settings(TELEGRAM_COMMUNITY_CHAT_IDS=""):
            self.assertFalse(community.configured())

    def test_eligibility(self):
        self.assertTrue(community.is_eligible(self.vol))
        self.assertTrue(community.is_eligible(self.curator))
        self.assertFalse(community.is_eligible(self.client_user))
        self.assertFalse(community.is_eligible(self.unlinked))
        Users.objects.filter(pk=self.vol2.pk).update(is_active=False)
        self.vol2.refresh_from_db()
        self.assertFalse(community.is_eligible(self.vol2))

    def test_chats_for(self):
        self.assertEqual(community.chats_for(self.vol), [("general", GENERAL), ("sogd", SOGD)])
        self.assertEqual(community.chats_for(self.vol2), [("general", GENERAL)])
        self.assertEqual(len(community.chats_for(self.curator)), 2)


class InviteTests(CommunityBase):
    def test_single_use_expiring_links_and_only_the_hash_is_stored(self):
        outcome = community.create_invites(self.vol)
        self.assertTrue(outcome.ok)
        self.assertEqual([key for key, _ in outcome.links], ["general", "sogd"])
        created = [p for m, p in self.tg.calls if m == "createChatInviteLink"]
        self.assertTrue(all(p["member_limit"] == 1 for p in created))
        self.assertTrue(all(abs(p["expire_date"] - (timezone.now() + timedelta(hours=24)).timestamp()) < 60
                            for p in created))
        invites = CommunityInvite.objects.filter(user=self.vol)
        self.assertEqual(invites.count(), 2)
        stored = " ".join(i.link_hash for i in invites)
        for _, url in outcome.links:
            self.assertNotIn(url, stored)
            self.assertEqual(len(community.link_hash(url)), 64)

    def test_not_eligible_rate_limited_not_configured(self):
        self.assertEqual(community.create_invites(self.client_user).code, community.NOT_ELIGIBLE)
        self.assertEqual(community.create_invites(self.unlinked).code, community.NOT_ELIGIBLE)
        community.create_invites(self.vol2)
        self.assertEqual(community.create_invites(self.vol2).code, community.RATE_LIMITED)
        with self.settings(TELEGRAM_COMMUNITY_BOT_TOKEN=""):
            self.assertEqual(community.create_invites(self.vol).code, community.NOT_CONFIGURED)

    def test_site_page_and_profile_card(self):
        self.client.force_login(self.vol2)
        profile = self.client.get(reverse("profile"))
        self.assertContains(profile, reverse("community_invite"))
        page = self.client.post(reverse("community_invite"))
        self.assertContains(page, "https://t.me/+invite")
        self.client.force_login(self.client_user)
        self.assertNotContains(self.client.get(reverse("profile")), reverse("community_invite"))
        self.assertEqual(self.client.post(reverse("community_invite")).status_code, 400)
        self.assertEqual(self.client.get(reverse("community_invite")).status_code, 405)

    def test_main_bot_button(self):
        from myapp.telegram.main_bot import core as main_core

        reply = main_core.handle(main_core.Event(chat_id=2002, kind="callback", data="com")).replies[0]
        self.assertTrue(any(b.url.startswith("https://t.me/+invite") for row in reply.buttons for b in row))


class JoinTests(CommunityBase):
    def test_invite_holder_is_bound_and_the_link_is_consumed(self):
        url = self.invite_url(self.vol2)
        actions = core.on_member(GENERAL, 2002, "left", "member", url)
        self.assertEqual([a.kind for a in actions], ["reply"])
        self.assertIn("Cm_Vol2", actions[0].text)
        member = CommunityMember.objects.get(chat_id=GENERAL, tg_user_id=2002)
        self.assertEqual(member.user, self.vol2)
        self.assertIsNotNone(CommunityInvite.objects.get(user=self.vol2).used_at)
        # leaving and coming back with the same (used) link → removed
        core.on_member(GENERAL, 2002, "member", "left")
        self.assertEqual([a.kind for a in core.on_member(GENERAL, 2002, "left", "member", url)], ["kick"])

    def test_someone_elses_link_expired_link_and_strangers_are_removed(self):
        url = self.invite_url(self.vol2)
        self.assertEqual(core.on_member(GENERAL, 9999, "left", "member", url)[0].kind, "kick")
        url2 = self.invite_url(self.vol)
        CommunityInvite.objects.filter(user=self.vol).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(core.on_member(GENERAL, 2001, "left", "member", url2)[0].kind, "kick")
        self.assertEqual(core.on_member(GENERAL, 2004, "left", "member", None)[0].kind, "kick")
        self.assertEqual(ModerationAction.objects.filter(reason="unknown_joiner").count(), 3)

    def test_staff_may_join_without_an_invite_and_other_chats_are_ignored(self):
        self.assertEqual(core.on_member(GENERAL, 2003, "left", "member")[0].kind, "reply")
        self.assertEqual(core.on_member(-100999, 2004, "left", "member"), [])


class RevocationTests(CommunityBase):
    def _join(self, user, chat=GENERAL):
        CommunityMember.objects.create(chat_id=chat, tg_user_id=user.telegram_id, user=user)

    def test_rejected_application_removes_the_volunteer(self):
        self._join(self.vol2)
        app = VolunteerApplication.objects.create(user=self.vol2, region="dushanbe")
        task_actions.reject_application(self.curator, app)  # Celery runs inline in tests
        self.assertIn("banChatMember", self.tg.methods())
        self.assertIn("unbanChatMember", self.tg.methods())  # kick, not a permanent ban
        self.assertIsNotNone(CommunityMember.objects.get(tg_user_id=2002).left_at)

    def test_unlinking_telegram_removes_them(self):
        self._join(self.vol2)
        self.client.force_login(self.vol2)
        self.client.post(reverse("telegram_unlink"))
        self.assertIsNotNone(CommunityMember.objects.get(tg_user_id=2002).left_at)

    def test_sweep_removes_only_the_no_longer_eligible(self):
        self._join(self.vol)
        self._join(self.vol2)
        self._join(self.curator)
        Users.objects.filter(pk=self.vol2.pk).update(is_active=False)
        CommunityMember.objects.create(chat_id=GENERAL, tg_user_id=7777, user=None)  # unbound
        self.assertEqual(community.sweep(), 2)
        active = set(CommunityMember.objects.filter(left_at__isnull=True).values_list("tg_user_id", flat=True))
        self.assertEqual(active, {2001, 2003})
        self.assertEqual(community.sweep(), 0)  # idempotent


class ModerationTests(CommunityBase):
    def setUp(self):
        super().setUp()
        CommunityMember.objects.create(chat_id=GENERAL, tg_user_id=2001, user=self.vol)

    def test_links_from_members_are_deleted_staff_links_stay(self):
        kinds = [a.kind for a in core.on_message(self.gm(2001, "https://spam.example", has_link=True))]
        self.assertEqual(kinds, ["delete", "reply"])
        self.assertEqual(core.on_message(self.gm(2003, "https://khayrkhoh.tj", has_link=True)), [])
        self.assertTrue(ModerationAction.objects.filter(action="delete", reason="link").exists())

    def test_flood_mutes(self):
        for _ in range(community.FLOOD_MESSAGES):
            self.assertEqual(core.on_message(self.gm(2001, "привет")), [])
        actions = core.on_message(self.gm(2001, "привет"))
        self.assertEqual(actions[0].kind, "restrict")
        self.assertEqual(actions[0].minutes, community.FLOOD_MUTE_MINUTES)

    def test_non_members_are_removed_with_their_message(self):
        actions = core.on_message(self.gm(2004, "hello"))  # a client — not eligible
        self.assertEqual([a.kind for a in actions], ["delete", "kick"])

    def test_report_notifies_staff_without_copying_text(self):
        actions = core.on_message(self.gm(2001, "/report", reply_to_message_id=40, reply_to_user_id=2002))
        self.assertEqual([a.kind for a in actions], ["delete", "reply"])
        note = Notification.objects.get(recipient=self.curator, type="community_report")
        self.assertEqual(note.link, f"https://t.me/c/1111/40")
        self.assertEqual(note.params, {"message_id": 40})

    def test_staff_commands(self):
        target = dict(reply_to_message_id=41, reply_to_user_id=2001)
        for i in range(2):
            self.assertEqual(core.on_message(self.gm(2003, "/warn", **target))[-1].kind, "reply")
        third = core.on_message(self.gm(2003, "/warn", **target))
        self.assertIn("restrict", [a.kind for a in third])
        mute = core.on_message(self.gm(2003, "/mute 99999", **target))
        self.assertEqual(next(a for a in mute if a.kind == "restrict").minutes, community.MAX_MUTE_MINUTES)
        ban = core.on_message(self.gm(2003, "/ban", **target))
        self.assertIn("ban", [a.kind for a in ban])
        self.assertIsNotNone(CommunityMember.objects.get(tg_user_id=2001).left_at)

    def test_members_cannot_use_staff_commands(self):
        actions = core.on_message(self.gm(2001, "/ban", reply_to_message_id=41, reply_to_user_id=2003))
        self.assertEqual([a.kind for a in actions], ["delete", "reply"])
        self.assertFalse(ModerationAction.objects.filter(action="ban").exists())

    def test_mentor_badge_by_hand(self):
        actions = core.on_message(self.gm(2003, "/mentor", reply_to_message_id=42, reply_to_user_id=2001))
        self.assertIn("Cm_Vol", actions[-1].text)
        self.assertTrue(UserAchievement.objects.filter(user=self.vol, achievement__code="mentor").exists())
        again = core.on_message(self.gm(2003, "/mentor", reply_to_message_id=42, reply_to_user_id=2001))
        self.assertIn("уже", again[-1].text)
        self.assertEqual(UserAchievement.objects.filter(user=self.vol, achievement__code="mentor").count(), 1)

    def test_id_works_in_unconfigured_groups_and_private(self):
        self.assertIn("-100777", core.on_message(self.gm(5, "/id", chat_id=-100777))[0].text)
        self.assertIn("KhayrKhoh", core.on_private(5, "/start")[0].text)


class MentorCrmTests(CommunityBase):
    def test_crm_button(self):
        self.client.force_login(self.curator)
        page = self.client.get(reverse("crm_volunteer_detail", args=[self.vol.pk]))
        self.assertContains(page, reverse("crm_award_mentor", args=[self.vol.pk]))
        self.client.post(reverse("crm_award_mentor", args=[self.vol.pk]))
        self.assertTrue(UserAchievement.objects.filter(user=self.vol, achievement__code="mentor").exists())
        page = self.client.get(reverse("crm_volunteer_detail", args=[self.vol.pk]))
        self.assertNotContains(page, reverse("crm_award_mentor", args=[self.vol.pk]))
        self.client.force_login(self.vol2)
        self.client.post(reverse("crm_award_mentor", args=[self.vol.pk]))
        self.assertEqual(UserAchievement.objects.filter(achievement__code="mentor").count(), 1)

    def test_mentor_is_never_awarded_automatically(self):
        from myapp.services import achievements

        self.assertTrue(Achievement.objects.get(code="mentor").is_active)
        achievements.evaluate(self.vol)
        self.assertFalse(UserAchievement.objects.filter(achievement__code="mentor").exists())


class PostTests(CommunityBase):
    def test_broadcast_mirrored_once_and_pinned(self):
        broadcast = task_actions.send_broadcast(self.curator, region="sogd", subject="Субботник", message="В 10:00")
        sends = [p for m, p in self.tg.calls if m == "sendMessage"]
        self.assertEqual([p["chat_id"] for p in sends], [SOGD])
        self.assertIn("pinChatMessage", self.tg.methods())
        community.mirror_broadcast(broadcast)
        self.assertEqual(len([m for m in self.tg.methods() if m == "sendMessage"]), 1)
        self.assertEqual(CommunityPost.objects.filter(kind="broadcast").count(), 1)

    def test_event_mirrored_from_the_site(self):
        self.client.force_login(self.curator)
        self.client.post(reverse("create_event"), {
            "title": "Посадка деревьев", "description": "Парк", "region": "all",
            "date": (timezone.now() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M"),
        })
        self.assertTrue(CommunityPost.objects.filter(kind="event", chat_id=GENERAL).exists())

    def test_weekly_thank_you_no_ranking_opt_out_and_once_per_week(self):
        task = HelpRequest.objects.create(client=self.client_user, help_type="grocery", description="d",
                                          address="secret address", phone="+992901234567", region="sogd")
        HelpRequest.objects.filter(pk=task.pk).update(status="completed", volunteer=self.vol, completed_at=timezone.now())
        first = Achievement.objects.get(code="first_help")
        UserAchievement.objects.create(user=self.vol, achievement=first)
        UserAchievement.objects.create(user=self.vol2, achievement=first)
        Profile.objects.filter(user=self.vol2).update(community_shoutouts=False)
        numbers = community.weekly_numbers()
        self.assertEqual(numbers["completed"], 1)
        self.assertEqual(list(numbers["named"]), ["Cm_Vol"])
        self.assertEqual(numbers["anonymous"], 1)
        text = community.weekly_text(numbers)
        for secret in ("secret address", "+992901234567", "Cm_Client", "Cm_Vol2"):
            self.assertNotIn(secret, text)
        self.assertIn("Ташаккури ҳафта", text)
        self.assertTrue(community.post_weekly())
        self.assertFalse(community.post_weekly())
        self.assertEqual(len([m for m in self.tg.methods() if m == "sendMessage"]), 1)

    def test_profile_opt_out_checkbox(self):
        self.client.force_login(self.vol)
        page = self.client.get(reverse("edit_profile"))
        self.assertContains(page, "community_shoutouts")
        self.client.force_login(self.client_user)
        self.assertNotContains(self.client.get(reverse("edit_profile")), "community_shoutouts")


from django.test import TransactionTestCase  # noqa: E402


@override_settings(**SETTINGS)
class CommunityAdapterTests(TransactionTestCase):
    """Whole PTB pipeline offline (fake HTTP layer); TransactionTestCase because
    the adapter reaches the DB from its worker thread."""

    def test_a_stranger_joining_is_kicked(self):
        import asyncio

        from telegram import Update

        from myapp.telegram.community_bot import app as bot_app
        from myapp.tests_telegram_main import _FakeRequest

        cache.clear()
        fake = _FakeRequest()

        async def go():
            application = bot_app.build("555:COMMUNITY", request=fake.request, get_updates_request=fake.request)
            async with application:
                await application.process_update(Update.de_json({"update_id": 1, "chat_member": {
                    "chat": {"id": GENERAL, "type": "supergroup", "title": "Volunteers"},
                    "from": {"id": 31337, "is_bot": False, "first_name": "x"}, "date": 0,
                    "old_chat_member": {"status": "left", "user": {"id": 31337, "is_bot": False, "first_name": "x"}},
                    "new_chat_member": {"status": "member", "user": {"id": 31337, "is_bot": False, "first_name": "x"}},
                }}, application.bot))

        asyncio.run(go())
        endpoints = [e for e, _ in fake.calls]
        self.assertIn("banChatMember", endpoints)
        self.assertIn("unbanChatMember", endpoints)
        self.assertTrue(ModerationAction.objects.filter(target_tg_id=31337, reason="unknown_joiner").exists())

    def test_link_detection(self):
        from telegram import Bot, Message

        from myapp.telegram.community_bot.app import has_link

        bot = Bot("555:COMMUNITY")
        plain = Message.de_json({"message_id": 1, "date": 0, "chat": {"id": GENERAL, "type": "supergroup"},
                                 "text": "встречаемся у парка"}, bot)
        linked = Message.de_json({"message_id": 2, "date": 0, "chat": {"id": GENERAL, "type": "supergroup"},
                                  "text": "смотри t.me/joinchat/abc"}, bot)
        hidden = Message.de_json({"message_id": 3, "date": 0, "chat": {"id": GENERAL, "type": "supergroup"},
                                  "text": "тут", "entities": [{"type": "text_link", "offset": 0, "length": 3,
                                                               "url": "https://spam.example"}]}, bot)
        self.assertEqual((has_link(plain), has_link(linked), has_link(hidden)), (False, True, True))
