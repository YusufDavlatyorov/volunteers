"""About page: how it works, help types, leaders, testimonials (2026-10-14).

Locks: real data only (no section without rows), leaders = the rating page's
cached list ranked by review average with places #1–#4, testimonials only when
staff opted a 5-star review in, the client as "First L.", the help-type cards
follow HELP_TYPE_CHOICES and pre-select the request form (via ?next= for
visitors), every new string exists in EN/RU/TJ, and the cache keeps the extra
queries bounded.
"""

import re
from datetime import timedelta
from urllib.parse import quote

from django.contrib.admin.sites import site as admin_site
from django.core.cache import cache
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import ABOUT_COMMUNITY_CACHE_KEY, RATING_CACHE_KEY, Profile, Users
from myapp.models import HELP_TYPE_CHOICES, HelpRequest, TaskReview
from myapp.services import feedback

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD = "About-Tests-2026!"


def _make(username, full_name="", **flags):
    user = Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region="dushanbe", **flags
    )
    if full_name:
        Profile.objects.filter(user=user).update(full_name=full_name)
    return user


@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM)
class AboutSectionsBase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client_user = _make("ab_client", full_name="Ходжимурод Тошматов", is_client=True)
        self.staff = _make("ab_admin", is_superuser=True, is_staff=True)

    def completed_task(self, volunteer, client=None, help_type="grocery"):
        task = HelpRequest.objects.create(
            client=client or self.client_user, help_type=help_type, description="d", address="a",
            phone="+992901234567", region="dushanbe",
        )
        HelpRequest.objects.filter(pk=task.pk).update(status="completed", volunteer=volunteer, completed_at=timezone.now())
        task.refresh_from_db()
        return task

    def review(self, volunteer, stars=5, comment="Спасибо большое, очень помогли!", client=None, help_type="grocery"):
        client = client or self.client_user
        task = self.completed_task(volunteer, client=client, help_type=help_type)
        outcome = feedback.submit_review(client, task, stars=stars, comment=comment)
        self.assertTrue(outcome.ok, outcome.code)
        return outcome.review

    def about(self):
        response = self.client.get(reverse("about"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class EmptyAndStaticSectionsTests(AboutSectionsBase):
    def test_renders_without_any_data(self):
        html = self.about()
        self.assertIn('data-i18n="about.how_title"', html)
        self.assertIn('data-i18n="about.help_title"', html)
        # No volunteers / featured reviews → those sections are not rendered.
        self.assertNotIn('data-i18n="about.leaders_title"', html)
        self.assertNotIn('data-i18n="about.reviews_title"', html)
        self.assertNotIn("leader-card", html)
        self.assertNotIn("testimonial-card", html)

    def test_sections_follow_the_hero_in_order(self):
        html = self.about()
        order = [html.index(marker) for marker in (
            'class="hero"', 'about.how_title', 'about.help_title', 'section.volunteers_eyebrow', 'about.net_title',
            'section.photos_eyebrow',
        )]
        self.assertEqual(order, sorted(order))

    def test_help_cards_follow_help_type_choices(self):
        html = self.about()
        for key, _label in HELP_TYPE_CHOICES:
            self.assertIn(f'data-i18n="help.{key}"', html)
            self.assertIn(f'data-i18n="about.help_desc.{key}"', html)
            self.assertIn(f'data-i18n="about.help_cta.{key}"', html)
        self.assertEqual(html.count('class="card help-card reveal"'), len(HELP_TYPE_CHOICES))

    def test_visitor_links_go_through_login_with_next(self):
        html = self.about()
        target = reverse("create_request") + "?help_type=medical"
        self.assertIn(f'href="{reverse("login")}?next={quote(target, safe="")}"', html)
        # "Request any help" — the plain form, also via login.
        self.assertIn(f'href="{reverse("login")}?next={quote(reverse("create_request"), safe="")}"', html)

    def test_signed_in_links_go_straight_to_the_form(self):
        self.client.force_login(self.client_user)
        html = self.about()
        self.assertIn(f'href="{reverse("create_request")}?help_type=medical"', html)
        self.assertNotIn("?next=", html)


class CreateRequestPreselectTests(AboutSectionsBase):
    def test_help_type_is_preselected(self):
        self.client.force_login(self.client_user)
        response = self.client.get(reverse("create_request") + "?help_type=medical")
        self.assertEqual(response.context["form"]["help_type"].value(), "medical")
        self.assertContains(response, '<option value="medical" selected')

    def test_unknown_help_type_is_ignored(self):
        self.client.force_login(self.client_user)
        response = self.client.get(reverse("create_request") + "?help_type=<script>")
        self.assertIsNone(response.context["form"]["help_type"].value())


class LoginNextTests(AboutSectionsBase):
    def login(self, next_url):
        return self.client.post(
            reverse("login") + "?" + f"next={quote(next_url, safe='')}",
            {"username": "ab_client", "password": PASSWORD},
        )

    def test_safe_next_is_followed(self):
        target = reverse("create_request") + "?help_type=grocery"
        response = self.login(target)
        self.assertRedirects(response, target, fetch_redirect_response=False)

    def test_foreign_next_is_ignored(self):
        for bad in ("https://evil.example/", "//evil.example/x", "javascript:alert(1)"):
            self.client.logout()
            with self.subTest(next=bad):
                response = self.login(bad)
                self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)

    def test_no_next_still_goes_to_profile(self):
        response = self.client.post(reverse("login"), {"username": "ab_client", "password": PASSWORD})
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)


class LeadersTests(AboutSectionsBase):
    """The About leaders are the rating's all-time top (services/rating.py):
    completed requests first, then the average rating — tested in depth in
    myapp/tests_rating.py; here: the About page shows exactly that top."""

    def volunteer(self, username, full_name, done=0, avg=None, count=0, active=True):
        user = _make(username, full_name=full_name, is_volunteer=True, is_active=active)
        Profile.objects.filter(user=user).update(review_avg=avg, review_count=count)
        for _ in range(done):
            self.completed_task(user)
        return user

    def test_leaders_are_the_ratings_top_four(self):
        self.volunteer("ab_v_a", "Анвар Алиев", done=5)
        self.volunteer("ab_v_b", "Бахтиёр Бобоев", done=3, avg="4.00", count=2)
        self.volunteer("ab_v_c", "Валиджон Валиев", done=3, avg="5.00", count=1)  # tie on 3 → better average first
        self.volunteer("ab_v_d", "Гулнора Гуломова", done=2)
        self.volunteer("ab_v_e", "Дилшод Давлатов", done=1)  # 5th — not shown
        self.volunteer("ab_v_none", "Ёсуман Ёрова", avg="5.00", count=9)  # nothing completed — never a leader
        self.volunteer("ab_v_off", "Жасур Жураев", done=9, active=False)
        html = self.about()
        names = re.findall(r'<h3 class="truncate" title="([^"]+)">', html)
        self.assertEqual(names, ["Анвар Алиев", "Валиджон Валиев", "Бахтиёр Бобоев", "Гулнора Гуломова"])
        self.assertEqual(re.findall(r'leader-card__rank">#(\d)<', html), ["1", "2", "3", "4"])
        for absent in ("Дилшод Давлатов", "Ёсуман Ёрова", "Жасур Жураев"):
            self.assertNotIn(absent, html)
        first_card = html[html.index("#1<"):html.index("#2<")]
        self.assertIn("<strong>5</strong>", first_card)
        self.assertIn(f'href="{reverse("rating")}"', html)

    def test_fewer_than_four_shows_what_exists(self):
        self.volunteer("ab_v_one", "Мадина Юсупова", done=1)
        html = self.about()
        self.assertIn('data-i18n="about.leaders_title"', html)
        self.assertEqual(html.count('class="card leader-card reveal"'), 1)
        self.assertIn("Мадина Юсупова", html)

    def test_no_avatar_shows_initial(self):
        self.volunteer("ab_v_init", "Рустам Саидов", done=1)
        self.assertIn('<div class="avatar avatar-fallback" aria-hidden="true">Р</div>', self.about())

    def test_a_completed_task_updates_leaders_at_once(self):
        vol = self.volunteer("ab_v_real", "Фарход Рахимов")
        self.assertNotIn("Фарход Рахимов", self.about())  # cached: nothing completed yet
        self.completed_task(vol)
        vol.profile.add_points(3)  # what task completion does — clears the rating caches
        self.assertIn("Фарход Рахимов", self.about())


class TestimonialsTests(AboutSectionsBase):
    def setUp(self):
        super().setUp()
        self.vol = _make("ab_vol", full_name="Аниса Шарипова", is_volunteer=True)

    def feature(self, review):
        outcome = feedback.set_landing(self.staff, review, True)
        self.assertTrue(outcome.ok)
        return review

    def test_unmoderated_review_is_not_shown(self):
        self.review(self.vol, comment="Пришла через 40 минут, очень культурная.")
        html = self.about()
        self.assertNotIn("Пришла через 40 минут", html)
        self.assertNotIn('data-i18n="about.reviews_title"', html)

    def test_featured_five_star_review_is_shown_with_short_name(self):
        self.feature(self.review(self.vol, comment="Пришла через 40 минут, очень культурная.", help_type="medical"))
        html = self.about()
        self.assertIn('data-i18n="about.reviews_title"', html)
        self.assertIn("«Пришла через 40 минут, очень культурная.»", html)
        self.assertIn("Ходжимурод Т.", html)
        self.assertNotIn("Тошматов", html)
        self.assertNotIn("ab_client", html)  # never the username
        self.assertIn(">ХТ</span>", html)  # initials avatar
        card = html[html.index("testimonial-card reveal"):]
        self.assertIn('data-i18n="help.medical"', card)
        self.assertIn('data-i18n="region.dushanbe"', card)

    def test_only_five_stars_with_text_and_visible(self):
        four = self.feature(self.review(self.vol, stars=4, comment="Четыре звезды"))
        empty = self.feature(self.review(self.vol, stars=5, comment=""))
        hidden = self.feature(self.review(self.vol, stars=5, comment="Скрытый отзыв"))
        feedback.hide(self.staff, hidden)
        html = self.about()
        self.assertNotIn("Четыре звезды", html)
        self.assertNotIn("Скрытый отзыв", html)
        self.assertNotIn("testimonial-card", html)
        del four, empty

    def test_newest_three_only(self):
        reviews = [self.feature(self.review(self.vol, comment=f"Отзыв номер {i}")) for i in range(4)]
        for age, review in enumerate(reversed(reviews)):
            TaskReview.objects.filter(pk=review.pk).update(created_at=timezone.now() - timedelta(days=age))
        cache.clear()
        html = self.about()
        self.assertEqual(html.count('class="card testimonial-card reveal"'), 3)
        self.assertNotIn("Отзыв номер 0", html)
        self.assertLess(html.index("Отзыв номер 3"), html.index("Отзыв номер 1"))

    def test_client_without_name_gets_neutral_label(self):
        anon = _make("zamira_khodjaeva", is_client=True)
        self.feature(self.review(self.vol, client=anon, comment="Без имени в профиле"))
        html = self.about()
        self.assertIn('data-i18n="about.reviews_anon"', html)
        self.assertNotIn("zamira_khodjaeva", html)

    def test_featuring_and_hiding_take_effect_at_once(self):
        review = self.review(self.vol, comment="Сразу на главной")
        self.assertNotIn("Сразу на главной", self.about())  # now cached
        self.feature(review)
        self.assertIn("Сразу на главной", self.about())
        feedback.hide(self.staff, review)
        self.assertNotIn("Сразу на главной", self.about())

    def test_editing_the_comment_needs_new_approval(self):
        review = self.feature(self.review(self.vol, comment="Одобренный текст"))
        self.assertIn("Одобренный текст", self.about())
        outcome = feedback.submit_review(self.client_user, review.task, stars=5, comment="Подменённый текст")
        self.assertTrue(outcome.ok)
        review.refresh_from_db()
        self.assertFalse(review.show_on_landing)
        html = self.about()
        self.assertNotIn("Подменённый текст", html)
        self.assertNotIn("Одобренный текст", html)

    def test_flag_defaults_off_and_is_staff_only(self):
        review = self.review(self.vol)
        self.assertFalse(review.show_on_landing)
        self.assertFalse(feedback.set_landing(self.client_user, review, True).ok)
        self.assertFalse(feedback.set_landing(self.vol, review, True).ok)
        review.refresh_from_db()
        self.assertFalse(review.show_on_landing)

    def test_admin_checkbox_is_the_only_editable_field(self):
        model_admin = admin_site._registry[TaskReview]
        request = RequestFactory().get("/")
        request.user = self.staff
        readonly = model_admin.get_readonly_fields(request)
        self.assertNotIn("show_on_landing", readonly)
        self.assertIn("comment", readonly)
        self.assertIn("stars", readonly)
        self.assertIn("feature_on_landing", model_admin.actions)


class ShortNameTests(TestCase):
    def test_short_public_name(self):
        cases = {
            "Ходжимурод Тошматов": "Ходжимурод Т.",
            "  Зарина   назарова ": "Зарина Н.",
            "Иван Петрович Сидоров": "Иван П. С.",
            "Мадина": "Мадина",
            "": "",
            None: "",
        }
        for full_name, expected in cases.items():
            with self.subTest(full_name=full_name):
                self.assertEqual(feedback.short_public_name(full_name), expected)


class CacheAndQueryBudgetTests(AboutSectionsBase):
    def populate(self, leaders, testimonials):
        start = Users.objects.count()
        for i in range(start, start + leaders):
            vol = _make(f"ab_q_v{i}", full_name=f"Волонтёр {i}", is_volunteer=True)
            Profile.objects.filter(user=vol).update(review_avg="4.50", review_count=i + 1)
            self.completed_task(vol)
        vol = Users.objects.filter(is_volunteer=True).first()
        for i in range(start, start + testimonials):
            client = _make(f"ab_q_c{i}", full_name=f"Клиент Номер{i}", is_client=True)
            feedback.set_landing(self.staff, self.review(vol, client=client, comment=f"Отзыв {i}"), True)

    def community_queries(self):
        """Queries of a hit with the stats cached but leaders/testimonials/rating cold."""
        self.client.get(reverse("about"))
        cache.delete_many([ABOUT_COMMUNITY_CACHE_KEY, RATING_CACHE_KEY])
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse("about"))
        return len(ctx.captured_queries) - 1  # minus the photo-report list

    def test_cold_cache_costs_at_most_three_queries_and_does_not_grow(self):
        self.populate(leaders=1, testimonials=1)
        small = self.community_queries()
        self.populate(leaders=6, testimonials=4)
        large = self.community_queries()
        self.assertLessEqual(small, 3)
        self.assertEqual(small, large)

    def test_warm_cache_adds_no_queries(self):
        self.populate(leaders=4, testimonials=3)
        self.client.get(reverse("about"))
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse("about"))
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(ctx.captured_queries), 1)  # only the photo-report list


class I18nKeysTests(AboutSectionsBase):
    def test_every_new_key_exists_in_every_language(self):
        from server.i18n_prerender import translation_table

        vol = _make("ab_i18n_vol", full_name="Аниса Шарипова", is_volunteer=True)
        feedback.set_landing(self.staff, self.review(vol), True)
        html = self.about()
        keys = set(re.findall(r'data-i18n="((?:about|help)\.[^"]+)"', html))
        self.assertIn("about.leaders_completed", keys)
        self.assertIn("about.reviews_eyebrow", keys)
        for key, _label in HELP_TYPE_CHOICES:
            self.assertIn(f"about.help_cta.{key}", keys)
        for lang in ("en", "ru", "tj"):
            table = translation_table(lang)
            for key in sorted(keys):
                self.assertTrue(table.get(key), f"{lang} {key}")

    def test_russian_is_prerendered(self):
        html = self.about()
        self.assertIn("Как работает сервис взаимопомощи", html)
        self.assertIn("Чем мы можем помочь вам сегодня", html)
        self.assertIn("Хайрхох", html)  # the brand as the site spells it in Russian


class TemplateCommentLeakTests(TestCase):
    """A Django ``{# … #}`` comment is single-line only: one spanning lines is
    printed on the page as text (it happened on About, 2026-10-14). Multi-line
    notes use ``{% comment %}…{% endcomment %}``."""

    def test_pages_show_no_comment_markers(self):
        cache.clear()
        self.addCleanup(cache.clear)
        for url in (reverse("about"), reverse("home"), reverse("rating")):
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                self.assertNotIn("{#", html)
                self.assertNotIn("#}", html)

    def test_no_template_has_a_multiline_short_comment(self):
        from pathlib import Path

        from django.conf import settings

        offenders = []
        for root in settings.TEMPLATES[0]["DIRS"]:
            for path in Path(root).rglob("*.html"):
                text = path.read_text(encoding="utf-8")
                for match in re.finditer(r"\{#(.*?)#\}", text, re.S):
                    if "\n" in match.group(1):
                        offenders.append(f"{path}:{text[:match.start()].count(chr(10)) + 1}")
                for number, line in enumerate(text.split("\n"), 1):
                    if "{#" in line and "#}" not in line:
                        offenders.append(f"{path}:{number}")
        self.assertEqual(offenders, [])
