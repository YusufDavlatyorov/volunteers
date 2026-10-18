"""Public volunteer rating with places (owner 2026-10-14; services/rating.py).

Locks: ranking = completed requests (all time — no period switch, an old
``?period=`` is ignored), then average rating, then review count, then user
id; the header shows only breadcrumbs, "updated" and the title; a podium for
places 1–3 then the table from place 4 (no search / filter / sort — such old
parameters are ignored); search / region
/ sort keep the overall place; who is ranked (active volunteers whose
application, if any, isn't pending/rejected); "You" decided per viewer, never
cached; the About leaders are the same top; empty database; plurals; bounded
queries; invalidation.
"""

import re
from datetime import timedelta

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import RATING_CACHE_KEY, Profile, Users
from myapp.models import HelpRequest, VolunteerApplication
from myapp.services import rating

PASSWORD = "Rating-Tests-2026!"


def _make(username, full_name="", region="dushanbe", **flags):
    user = Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region=region, **flags
    )
    if full_name:
        Profile.objects.filter(user=user).update(full_name=full_name)
    return user


def ranks(html):
    """The overall places of the table rows, in display order."""
    return [int(n) for n in re.findall(r'<tr data-rank="(\d+)"', html)]


@override_settings(TELEGRAM_BOT_TOKEN="")
class RatingBase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client_user = _make("rt_client", is_client=True)

    def volunteer(self, username, full_name, region="dushanbe", done=0, days_ago=1, avg=None, reviews=0, **flags):
        user = _make(username, full_name=full_name, region=region, is_volunteer=True, **flags)
        Profile.objects.filter(user=user).update(review_avg=avg, review_count=reviews)
        self.complete(user, done, days_ago)
        return user

    def complete(self, volunteer, n=1, days_ago=1):
        when = timezone.now() - timedelta(days=days_ago)
        for _ in range(n):
            task = HelpRequest.objects.create(
                client=self.client_user, help_type="grocery", description="d", address="a",
                phone="+992901234567", region="dushanbe",
            )
            HelpRequest.objects.filter(pk=task.pk).update(status="completed", volunteer=volunteer, completed_at=when)

    def page(self, query="", user=None):
        if user is not None:
            self.client.force_login(user)
        response = self.client.get(reverse("rating") + query)
        self.assertEqual(response.status_code, 200)
        return response

    @staticmethod
    def names(rows):
        return [row["name"] for row in rows]


class RankingOrderTests(RatingBase):
    def test_merit_score_then_id(self):
        """Merit score (2026-10-18, services/rating.py): completed requests
        first, the rating weighted by its number of reviews next; on an equal
        score (no photos, same completeness here) the lower user id first."""
        self.volunteer("rt_a", "Анвар", done=2)
        self.volunteer("rt_b", "Бахтиёр", done=5)
        self.volunteer("rt_c", "Валиджон", done=3, avg="4.00", reviews=9)
        self.volunteer("rt_d", "Гулнора", done=3, avg="4.80", reviews=1)  # 3 done + a good rating
        self.volunteer("rt_e", "Дилшод", done=3, avg="4.00", reviews=12)  # 4.0 = the prior: same score as Валиджон
        self.volunteer("rt_f", "Ёсуман", done=2)  # full tie with Анвар: lower user id first
        self.volunteer("rt_g", "Жасур", done=0, avg="5.00", reviews=40)  # nothing done: below everyone who did
        rows = rating.cached()["rows"]
        self.assertEqual(self.names(rows), ["Бахтиёр", "Гулнора", "Валиджон", "Дилшод", "Анвар", "Ёсуман", "Жасур"])
        self.assertEqual([row["score"] for row in rows], [50, 32, 30, 30, 20, 20, 9])
        self.assertEqual([row["rank"] for row in rows], list(range(1, 8)))

    def test_a_poor_rating_costs_points_a_good_one_adds(self):
        self.volunteer("rt_none", "Без отзывов", done=1)
        self.volunteer("rt_low", "С плохим", done=1, avg="2.00", reviews=1)
        self.volunteer("rt_good", "С хорошим", done=1, avg="5.00", reviews=1)
        self.assertEqual(self.names(rating.cached()["rows"]), ["С хорошим", "Без отзывов", "С плохим"])

    def test_week_count(self):
        for i in range(3):  # the podium; "+N this week" is a table column
            self.volunteer(f"rt_top{i}", f"Топ {i}", done=9, days_ago=30)
        vol = self.volunteer("rt_week", "Неделя", done=2, days_ago=2)
        self.complete(vol, 1, days_ago=20)
        row = rating.cached()["rows"][3]
        self.assertEqual((row["name"], row["completed"], row["week"]), ("Неделя", 3, 2))
        self.assertIn('data-i18n="rating.week" data-i18n-args=\'{"n": 2}\'', self.page().content.decode())

    def test_all_time_and_old_period_links_are_ignored(self):
        old = self.volunteer("rt_old", "Давний", done=3, days_ago=400)
        self.volunteer("rt_new", "Недавний", done=2, days_ago=3)
        self.assertEqual(self.names(rating.cached()["rows"]), ["Давний", "Недавний"])  # 400 days ago counts
        plain = [row["rank"] for row in self.page().context["podium"]]
        for query in ("?period=month", "?period=year", "?period=all", "?period=bogus"):
            with self.subTest(query=query):
                response = self.page(query)
                self.assertEqual([row["rank"] for row in response.context["podium"]], plain)
                self.assertNotIn("period", response.context)
        del old


class WhoIsRankedTests(RatingBase):
    def test_only_active_volunteers_without_a_pending_or_rejected_application(self):
        self.volunteer("rt_plain", "Без заявки", done=1)  # created by an admin / bootstrap — ranked
        approved = self.volunteer("rt_approved", "Одобрен", done=1)
        VolunteerApplication.objects.create(user=approved, status=VolunteerApplication.STATUS_APPROVED)
        pending = self.volunteer("rt_pending", "На проверке", done=1)
        VolunteerApplication.objects.create(user=pending, status=VolunteerApplication.STATUS_PENDING)
        rejected = self.volunteer("rt_rejected", "Отклонён", done=1)
        VolunteerApplication.objects.create(user=rejected, status=VolunteerApplication.STATUS_REJECTED)
        self.volunteer("rt_inactive", "Неактивный", done=5, is_active=False)
        _make("rt_curator", full_name="Куратор", is_curator=True)
        names = set(self.names(rating.cached()["rows"]))
        self.assertEqual(names, {"Без заявки", "Одобрен"})
        html = self.page().content.decode()
        for absent in ("На проверке", "Отклонён", "Неактивный", "Куратор"):
            self.assertNotIn(absent, html)

    def test_application_decision_refreshes_the_ranking(self):
        admin = _make("rt_admin", is_superuser=True, is_staff=True)
        user = _make("rt_applicant", full_name="Заявитель", is_client=True)
        application = VolunteerApplication.objects.create(user=user, status=VolunteerApplication.STATUS_PENDING)
        self.assertEqual(rating.cached()["rows"], [])
        with self.captureOnCommitCallbacks(execute=True):
            application.approve(admin)
        self.assertEqual(self.names(rating.cached()["rows"]), ["Заявитель"])

    def test_one_cache_entry_cleared_by_task_completion(self):
        volunteer = self.volunteer("rt_cache", "Кэш")
        rating.cached()
        self.assertIsNotNone(cache.get(RATING_CACHE_KEY))
        volunteer.profile.add_points(3)  # what completing a task does
        self.assertIsNone(cache.get(RATING_CACHE_KEY))


class PageTests(RatingBase):
    def setUp(self):
        super().setUp()
        self.top = [
            self.volunteer("rt_1", "Первый Один", region="sogd", done=9, avg="4.90", reviews=21),
            self.volunteer("rt_2", "Второй Два", region="dushanbe", done=7),
            self.volunteer("rt_3", "Третий Три", region="khatlon", done=5),
            self.volunteer("rt_4", "Четвёртый", region="sogd", done=3, avg="5.00", reviews=1),
            self.volunteer("rt_5", "Пятый", region="gbao", done=1),
        ]

    def test_podium_holds_the_top_three_and_the_table_starts_at_four(self):
        response = self.page()
        html = response.content.decode()
        self.assertEqual([row["rank"] for row in response.context["podium"]], [1, 2, 3])
        self.assertEqual([row["name"] for row in response.context["podium"]], ["Первый Один", "Второй Два", "Третий Три"])
        self.assertEqual(ranks(html), [4, 5])
        first = re.search(r'<li class="card podium-card podium-card--1">.*?</li>', html, re.S).group(0)
        for text in ("Первый Один", 'data-i18n="rating.leader"', ">#1<", ">9 заявок закрыто<", ">21 отзыв<",
                     'data-i18n="region.sogd"', "rating-avatar__tick", ">ПО<"):
            self.assertIn(text, first)
        self.assertIn("podium-card--2", html)
        self.assertIn("podium-card--3", html)
        self.assertLess(html.index('class="rating-podium"'), html.index("<table"))
        # Paging counts from place 4, in overall places.
        self.assertIn('data-i18n="rating.shown" data-i18n-args=\'{"from": 4, "to": 5, "total": 5}\'', html)

    def test_podium_badges_are_the_top_three_badges(self):
        from myapp.models import Achievement, UserAchievement

        UserAchievement.objects.create(user=self.top[0], achievement=Achievement.objects.get(code="first_help"))
        html = self.page().content.decode()
        first = re.search(r'<li class="card podium-card podium-card--1">.*?</li>', html, re.S).group(0)
        self.assertIn("images/badges/first_help", first)

    def test_old_search_filter_and_sort_links_are_ignored(self):
        plain = self.page()
        for query in ("?q=Пятый", "?region=gbao", "?sort=rating", "?sort=new", "?q=x&region=sogd&sort=new&page=abc",
                      "?region=bogus&sort=bogus"):
            with self.subTest(query=query):
                response = self.page(query)
                self.assertEqual([r["rank"] for r in response.context["podium"]], [1, 2, 3])
                self.assertEqual([r["rank"] for r in response.context["page_rows"]],
                                 [r["rank"] for r in plain.context["page_rows"]])
                html = response.content.decode()
                for gone in ("rating-filters", 'name="q"', 'name="region"', 'name="sort"', "rating.places_title",
                             "rating.search", "rating.sort", "rating.apply", "rating.nothing_found", "rating.js"):
                    self.assertNotIn(gone, html)

    def test_current_user_is_highlighted_at_render_time(self):
        me = self.top[3]
        html = self.page(user=me).content.decode()
        row = re.search(r'<tr data-rank="4" class="is-me">.*?</tr>', html, re.S).group(0)
        self.assertIn("Четвёртый", row)
        self.assertIn('data-i18n="rating.you"', row)
        self.assertIn(f'href="{reverse("dashboard")}"', row)
        self.assertEqual(html.count('class="is-me"'), 1)
        # Same cached ranking, another viewer: no highlight leaks through the cache.
        self.client.logout()
        html = self.page().content.decode()
        self.assertNotIn("is-me", html)
        self.assertNotIn('data-i18n="rating.you"', html)

    def test_leader_highlight_on_the_podium(self):
        html = self.page(user=self.top[0]).content.decode()
        card = re.search(r'<li class="card podium-card podium-card--1 is-me">.*?</li>', html, re.S).group(0)
        self.assertIn('data-i18n="rating.you"', card)
        self.assertIn(f'href="{reverse("dashboard")}"', card)

    def test_profile_button_only_for_staff(self):
        html = self.page().content.decode()
        self.assertNotIn("crm/volunteers/", html)
        html = self.page(user=_make("rt_staff", is_curator=True)).content.decode()
        self.assertIn(reverse("crm_volunteer_detail", args=[self.top[3].pk]), html)  # table row
        self.assertIn('data-i18n="rating.profile"', html)
        self.assertIn(reverse("crm_volunteer_detail", args=[self.top[0].pk]), html)  # podium card
        self.assertIn('data-i18n="rating.view_profile"', html)

    def test_plural_forms(self):
        html = self.page().content.decode()
        self.assertIn(">9 заявок закрыто<", html)   # podium
        self.assertIn(">5 заявок закрыто<", html)
        self.assertIn('data-i18n="rating.n_reviews.one" data-i18n-plural="rating.n_reviews"', html)
        self.assertIn(">21 отзыв<", html)
        self.assertIn(">3 заявки<", html)    # table
        self.assertIn(">1 заявка<", html)

    def test_updated_minutes_ago(self):
        self.page()
        data = rating.cached()
        data["computed_at"] -= timedelta(minutes=5)
        cache.set(RATING_CACHE_KEY, data)
        self.assertIn(">Обновлено 5 минут назад<", self.page().content.decode())

    def test_no_private_data(self):
        html = self.page().content.decode()
        for user in self.top:
            self.assertNotIn(user.email, html)
        self.assertNotIn("+992901234567", html)  # the client's phone on the tasks (the footer has the site's own)
        self.assertNotIn("address", html.lower().split('class="section rating-page"')[1].split("</section>")[0])

    def test_about_leaders_are_the_same_top(self):
        about = self.client.get(reverse("about")).content.decode()
        about_names = re.findall(r'<h3 class="truncate" title="([^"]+)">', about)
        response = self.page()
        top = [row["name"] for row in [*response.context["podium"], *response.context["page_rows"]]][:4]
        self.assertEqual(about_names, top)

    def test_pagination(self):
        for i in range(rating.PAGE_SIZE + 5):
            self.volunteer(f"rt_more{i}", f"Ещё {i}")
        response = self.page()
        self.assertEqual(len(response.context["page_rows"]), rating.PAGE_SIZE)
        self.assertIn('data-i18n="rating.shown" data-i18n-args=\'{"from": 4, "to": 23, "total": 30}\'',
                      response.content.decode())
        self.assertEqual(ranks(response.content.decode())[0], 4)
        response = self.page("?page=2")
        self.assertEqual(ranks(response.content.decode()), list(range(24, 31)))
        self.assertIn('"from": 24, "to": 30, "total": 30', response.content.decode())
        self.assertEqual(response.context["podium"], [])  # the podium is on page 1 only
        self.assertNotIn("rating-podium", response.content.decode())

    def test_queries_bounded(self):
        self.page()
        with CaptureQueriesContext(connection) as warm:
            self.page()
        cache.clear()
        with CaptureQueriesContext(connection) as cold:
            self.page()
        for i in range(10):
            self.volunteer(f"rt_q{i}", f"Запрос {i}", done=1)
        cache.clear()
        with CaptureQueriesContext(connection) as cold_more:
            self.page()
        self.assertLessEqual(len(warm.captured_queries), 1)  # badges only
        self.assertLessEqual(len(cold.captured_queries), 2)  # ranking + badges
        self.assertEqual(len(cold.captured_queries), len(cold_more.captured_queries))


class EmptyAndI18nTests(RatingBase):
    def test_empty_database(self):
        for query in ("", "?period=month"):
            with self.subTest(query=query):
                html = self.page(query).content.decode()
                self.assertIn('data-i18n="rating.empty"', html)
                self.assertNotIn("<table", html)
                self.assertNotIn("rating-podium", html)

    def test_volunteers_but_nothing_completed(self):
        self.volunteer("rt_idle", "Пока без заявок")
        response = self.page()
        self.assertEqual(response.context["podium"], [])  # no "0 requests" podium
        self.assertNotIn("rating-podium", response.content.decode())
        self.assertEqual(ranks(response.content.decode()), [1])

    def test_fewer_than_three_on_the_podium(self):
        self.volunteer("rt_one", "Единственный", done=2)
        self.volunteer("rt_two", "Второй", done=1)
        self.volunteer("rt_zero", "Без заявок")
        response = self.page()
        html = response.content.decode()
        self.assertEqual([row["rank"] for row in response.context["podium"]], [1, 2])
        self.assertIn('class="podium podium--2"', html)
        self.assertNotIn("podium-card--3", html)
        self.assertEqual(ranks(html), [3])  # the table goes on right after the podium

    def test_keys_exist_in_every_language(self):
        from server.i18n_prerender import translation_table

        self.volunteer("rt_i18n", "Ключи", done=3, avg="4.00", reviews=2)
        self.volunteer("rt_i18n2", "Ключи Два", done=1)
        self.volunteer("rt_i18n3", "Ключи Три", done=1)
        self.volunteer("rt_i18n4", "Ключи Четыре", done=1)
        html = self.page().content.decode()
        keys = set(re.findall(r'data-i18n(?:-ph|-aria)?="([^"]+)"', html[html.index('class="section rating-page"'):]))
        plural_bases = set(re.findall(r'data-i18n-plural="([^"]+)"', html))
        keys |= {f"{base}.{cat}" for base in plural_bases | {"rating.updated"} for cat in ("one", "few", "many", "other")}
        keys |= {"rating.profile", "rating.view_profile", "rating.my_cabinet", "rating.updated_now",
                 "rating.leader", "rating.podium_title"}
        for lang in ("en", "ru", "tj"):
            table = translation_table(lang)
            for key in sorted(keys):
                if key.startswith("ach."):
                    continue
                self.assertTrue(table.get(key), f"{lang} {key}")

    def test_header_has_only_crumbs_updated_and_title(self):
        from server.i18n_prerender import translation_table

        self.volunteer("rt_head", "Шапка", done=1)
        for query in ("", "?period=month"):
            with self.subTest(query=query):
                html = self.page(query).content.decode()
                self.assertIn('class="breadcrumbs"', html)
                self.assertIn('class="rating-updated"', html)
                self.assertIn("Рейтинг волонтёров Таджикистана", html)
                for gone in ("rating-community", "rating.community", "rating.desc", "rating-periods",
                             "rating.period_", "rating-stats", "stat-card", "rating.stat_",
                             "Активных волонтёров", "Добрых дел", "Охват", "Месяц"):
                    self.assertNotIn(gone, html)
        for lang in ("en", "ru", "tj"):
            table = translation_table(lang)
            for key in ("rating.community", "rating.desc", "rating.period_month", "rating.period_all",
                        "rating.stat_volunteers", "rating.stat_deeds", "rating.stat_avg", "rating.stat_reach",
                        "rating.no_data_period"):
                self.assertNotIn(key, table, f"{lang} {key}")
