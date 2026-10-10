"""The About / landing page after the Stitch redesign (2026-10-10).

Locks: 200 in all three languages with one <h1> carrying the brand; all nine
design sections (+ the privacy promise); an empty database renders empty
states, never zeros dressed up as figures; only reviews staff marked "can be
published on the site" are public (and hiding / editing withdraws the mark);
no made-up figure, phone, claim or Stitch image URL in the template or its
strings; the volunteers block is the Rating list in the same order; the page
is cached (no queries on a repeat hit) and carries no map script up front.
"""

import json
import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.models import Users
from myapp.models import HelpRequest, TaskReview
from myapp.testing import TempMediaRoot
from myapp.services import feedback, landing, rating
from server.i18n_prerender import translation_table

TEMPLATE = Path(settings.BASE_DIR) / "templates" / "myapp" / "about.html"
SECTIONS = ["hero", "stats", "how", "help", "regions", "volunteers", "reviews", "services", "cta"]
PASSWORD = "About-Tests-2026!"
_MEDIA_ROOT = TempMediaRoot("gc-about-tests-")  # fork-safe (myapp/testing.py)


def tearDownModule():
    _MEDIA_ROOT.cleanup()


def _jpeg():
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (40, 40), (200, 120, 90)).save(buf, "JPEG")
    return SimpleUploadedFile("face.jpg", buf.getvalue(), content_type="image/jpeg")


def _user(username, region="dushanbe", **flags):
    return Users.objects.create_user(username=username, email=f"{username}@example.com",
                                     password=PASSWORD, region=region, **flags)


def _sections(html):
    return re.findall(r'data-section="(\w+)"', html)


class AboutFixture(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def page(self, url="/"):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def make_activity(self):
        """Two volunteers, one client, two completed tasks with reviews."""
        self.curator = _user("ab_curator", is_curator=True)
        self.vol_a = _user("ab_vol_a", region="sogd", is_volunteer=True)
        self.vol_b = _user("ab_vol_b", is_volunteer=True)
        self.vol_a.profile.full_name = "Бахром Каримов"
        self.vol_a.profile.save()
        self.vol_b.profile.full_name = "Азиза Рахимова"
        self.vol_b.profile.save()
        self.client_user = _user("ab_client", is_client=True)
        self.client_user.profile.full_name = "Зарина Назарова"
        self.client_user.profile.save()
        self.reviews = []
        for vol, stars, text in ((self.vol_a, 5, "Огромное спасибо, всё привезли вовремя"),
                                 (self.vol_b, 4, "Очень вежливый волонтёр")):
            task = HelpRequest.objects.create(
                client=self.client_user, volunteer=vol, help_type="grocery", description="d",
                address="a", phone="+992900000000", region=vol.region, status="completed",
                accepted_at=timezone.now() - timedelta(hours=3), completed_at=timezone.now() - timedelta(hours=2),
            )
            review = TaskReview.objects.create(task=task, client=self.client_user, volunteer=vol,
                                               stars=stars, comment=text)
            feedback.recompute_volunteer_stats(vol)
            self.reviews.append(review)
        cache.clear()


class AboutLanguagesTests(AboutFixture):
    H1 = {"ru": ("Хайрхоҳ", "добро рядом"), "en": ("KhayrKhoh", "kindness close by"), "tj": ("Хайрхоҳ", "некӣ дар наздикӣ")}

    def test_200_in_three_languages_with_one_brand_h1(self):
        for lang, (brand, accent) in self.H1.items():
            with self.subTest(lang=lang), override_settings(SERVER_RENDER_LANGUAGE=lang):
                for url in ("/", reverse("about")):
                    html = self.page(url)
                    h1s = re.findall(r"<h1\b.*?</h1>", html, re.S)
                    self.assertEqual(len(h1s), 1)
                    self.assertIn(brand, h1s[0])
                    self.assertIn(accent, h1s[0])

    def test_text_is_in_the_server_html_not_only_js(self):
        html = self.page()
        for text in ("Как работает сервис взаимопомощи", "Чем мы можем помочь вам сегодня",
                     "Волонтёрская сеть по всему Таджикистану", "Другие сервисы платформы",
                     "Станьте частью добра в вашем родном городе", "Оставьте заявку", "Сопровождение к врачу"):
            self.assertIn(text, html)

    def test_every_ab_key_of_the_template_exists_in_all_three_languages(self):
        src = TEMPLATE.read_text(encoding="utf-8")
        keys = set(re.findall(r'data-i18n(?:-alt|-aria)?="(ab\.[\w.]+)"', src))
        for kind in ("grocery", "medical", "transport", "household", "emotional", "other"):
            keys |= {f"ab.help.{kind}", f"ab.help.{kind}_text", f"ab.help.{kind}_cta"}
        for step in "123":
            keys |= {f"ab.step{step}_title", f"ab.step{step}_text"}
        for region in ("dushanbe", "sogd", "khatlon", "gbao", "rrp"):
            keys.add(f"ab.region_hint.{region}")
        for base in ("ab.vol", "ab.req"):
            keys |= {f"{base}.{cat}" for cat in ("one", "few", "many", "other")}
        keys |= {"ab.ago_min", "ab.ago_hours", "ab.ago_days"}
        self.assertGreater(len(keys), 60)
        for lang in ("en", "ru", "tj"):
            table = translation_table(lang)
            with self.subTest(lang=lang):
                self.assertEqual(sorted(k for k in keys if not table.get(k)), [])

    def test_seo_head_is_kept(self):
        html = self.page(reverse("about"))
        self.assertIn('<link rel="canonical" href="https://khayrkhoh.tj/">', html)
        self.assertIn('<meta property="og:title"', html)
        self.assertIn('<meta name="description"', html)
        self.assertIn('application/ld+json', html)


class AboutSectionsTests(AboutFixture):
    def test_all_nine_sections_and_the_privacy_note(self):
        self.make_activity()
        feedback.publish(self.curator, self.reviews[0])
        sections = _sections(self.page())
        for name in SECTIONS + ["privacy"]:
            self.assertIn(name, sections)
        self.assertEqual(sections[:len(SECTIONS) - 1], SECTIONS[:-1])  # design order

    def test_empty_database_renders_empty_states(self):
        self.assertFalse(Users.objects.exists())
        html = self.page()
        sections = _sections(html)
        for name in SECTIONS:
            if name != "reviews":
                self.assertIn(name, sections)
        self.assertNotIn("reviews", sections)                      # no published reviews → hidden
        self.assertIn('data-i18n="ab.stats_empty_title"', html)     # not a row of zeros
        self.assertNotIn('class="ab-stat__value"', html)
        self.assertIn('data-i18n="ab.vol_empty"', html)
        self.assertIn('data-i18n="ab.regions_empty"', html)
        self.assertNotIn('data-i18n="ab.badge_done"', html)        # no "request completed" badge
        self.assertNotIn('data-i18n="ab.today_requests"', html)
        self.assertNotIn("ab-photo__quote", html)                    # no made-up quote

    def test_counters_regions_and_badges_come_from_the_database(self):
        self.make_activity()
        HelpRequest.objects.create(client=self.client_user, help_type="medical", description="new",
                                   address="a", phone="+992900000000", region="dushanbe")
        cache.clear()
        html = self.page()
        values = re.findall(r'class="ab-stat__value" data-num="(\d+)"', html)
        self.assertEqual(values, ["2", "2", "2"])  # completed, volunteers, regions with volunteers
        self.assertIn('data-num="4.5" data-num-digits="1">4,5<', html)
        regions = json.loads(re.search(r'id="ab-regions-data" type="application/json">(.*?)</script>', html).group(1))
        self.assertEqual({r["key"]: r["count"] for r in regions},
                         {"dushanbe": 1, "sogd": 1, "khatlon": 0, "gbao": 0, "rrp": 0})
        self.assertIn('data-i18n="ab.badge_done"', html)
        # created today: the two completed fixtures + the new one
        self.assertIn('data-i18n="ab.today_requests" data-i18n-args=\'{"n": 3}\'', html)

    def test_volunteers_block_is_the_rating_list_in_its_order(self):
        self.make_activity()
        for i in range(4):
            _user(f"ab_extra_{i}", is_volunteer=True)
        cache.clear()
        html = self.page()
        block = html[html.index('data-section="volunteers"'):html.index('data-section="services"')]
        shown = re.findall(r'<h3 class="truncate" title="([^"]+)"', block)
        expected = [p.full_name or p.user.username for p in rating.rating_profiles()[:4]]
        self.assertEqual(shown, expected)
        self.assertNotRegex(block, r">#\d<")  # no places (owner decision)
        self.assertIn('href="/myapp/rating/"', block)

    def test_volunteer_card_shows_real_rating_and_completed_count(self):
        self.make_activity()
        html = self.page()
        block = html[html.index('data-section="volunteers"'):html.index('data-section="services"')]
        self.assertIn("Азиза Рахимова", block)
        self.assertIn('data-num="1">1<', block)
        self.assertIn('data-i18n="ab.req.one"', block)

    def test_long_names_are_truncated_not_widening(self):
        self.make_activity()
        self.vol_b.profile.full_name = "Ш" * 60
        self.vol_b.profile.save()
        cache.clear()
        html = self.page()
        self.assertIn(f'<h3 class="truncate" title="{"Ш" * 60}">', html)


class AboutReviewsPublishingTests(AboutFixture):
    def setUp(self):
        super().setUp()
        self.make_activity()
        self.review = self.reviews[0]

    def test_unpublished_reviews_are_not_shown(self):
        html = self.page()
        self.assertNotIn("reviews", _sections(html))
        for review in self.reviews:
            self.assertNotIn(review.comment, html)

    def test_published_review_is_shown_with_short_name_and_region(self):
        self.assertTrue(feedback.publish(self.curator, self.review).ok)
        html = self.page()
        self.assertIn("reviews", _sections(html))
        self.assertIn(self.review.comment, html)
        self.assertIn("Зарина Н.", html)
        self.assertNotIn("Назарова", html)
        self.assertNotIn(self.client_user.username, html)
        self.assertNotIn(self.reviews[1].comment, html)  # the other one stays private

    def test_hiding_or_editing_withdraws_the_publication(self):
        feedback.publish(self.curator, self.review)
        feedback.hide(self.curator, self.review)
        self.review.refresh_from_db()
        self.assertIsNone(self.review.published_at)
        self.assertNotIn(self.review.comment, self.page())

        feedback.unhide(self.curator, self.review)
        feedback.publish(self.curator, self.review)
        outcome = feedback.submit_review(self.client_user, self.review.task, stars=5, comment="Другой текст")
        self.assertTrue(outcome.ok)
        self.review.refresh_from_db()
        self.assertIsNone(self.review.published_at)
        self.assertNotIn("Другой текст", self.page())

    def test_hidden_or_empty_reviews_cannot_be_published(self):
        feedback.hide(self.curator, self.review)
        self.assertFalse(feedback.publish(self.curator, self.review).ok)
        empty = self.reviews[1]
        TaskReview.objects.filter(pk=empty.pk).update(comment="")
        empty.refresh_from_db()
        self.assertFalse(feedback.publish(self.curator, empty).ok)

    def test_only_staff_can_publish(self):
        url = reverse("review_publish", args=[self.review.pk])
        for user in (self.vol_a, self.client_user):
            self.client.force_login(user)
            self.client.post(url, {"action": "publish"})
            self.review.refresh_from_db()
            self.assertIsNone(self.review.published_at)
        self.client.force_login(self.curator)
        self.assertEqual(self.client.post(url, {"action": "publish"}).status_code, 302)
        self.review.refresh_from_db()
        self.assertEqual(self.review.published_by, self.curator)
        self.client.post(url, {"action": "unpublish"})
        self.review.refresh_from_db()
        self.assertIsNone(self.review.published_at)
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_crm_list_has_publish_button_and_filter(self):
        self.client.force_login(self.curator)
        html = self.client.get(reverse("crm_reviews")).content.decode()
        self.assertIn(reverse("review_publish", args=[self.review.pk]), html)
        feedback.publish(self.curator, self.review)
        html = self.client.get(reverse("crm_reviews") + "?published=1").content.decode()
        self.assertIn(f"#{self.review.task_id}", html)
        self.assertNotIn(f"#{self.reviews[1].task_id} ", html)

    def test_publishing_shows_at_once_despite_the_page_cache(self):
        self.page()  # cache filled
        feedback.publish(self.curator, self.review)
        self.assertIn(self.review.comment, self.page())


class AboutNoFakeContentTests(AboutFixture):
    # Figures, claims, people and contacts of the Stitch mock that must not ship.
    FORBIDDEN = [
        "1 248", "1248", "312", "142", "4,9", "4.98", "84 отзыва", "118 заявок",
        "100%", "24/7", "68 джамоат", "Круглосуточн", "15 минут", "2 минуты",
        "+992 44", "446000000", "600-00-00", "lh3.googleusercontent", "googleusercontent",
        "Зебуниссо", "Фарход", "Мадина Юсупова", "Рустам Саидов", "Аниса", "Ходжимурод",
        "12 мин назад", "18 новых", "Material Symbols", "tailwind",
    ]

    def test_template_and_its_strings_carry_no_made_up_content(self):
        src = TEMPLATE.read_text(encoding="utf-8")
        ru = translation_table("ru")
        strings = " ".join(v for k, v in ru.items() if k.startswith("ab."))
        for needle in self.FORBIDDEN:
            with self.subTest(needle=needle):
                self.assertNotIn(needle, src)
                self.assertNotIn(needle, strings)

    def test_template_has_no_hardcoded_numbers_in_visible_text(self):
        src = TEMPLATE.read_text(encoding="utf-8")
        src = re.sub(r"\{% comment %\}.*?\{% endcomment %\}", "", src, flags=re.S)
        # The privacy note's 24 h / 90 / 180 days are the retention policy
        # (services/tracking.py, chat_retention.py), not marketing figures.
        src = re.sub(r'<section[^>]*id="privacy".*?</section>', "", src, flags=re.S)
        src = re.sub(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}", "", src, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", src)
        self.assertEqual(re.findall(r"\d{2,}", text), [])

    def test_rendered_page_uses_the_footer_phone_only(self):
        html = self.page()
        self.assertIn("+992 071 81 77 22", html)
        self.assertNotIn("+992 44", html)

    @override_settings(TELEGRAM_BOT_USERNAME="")
    def test_no_bot_card_without_a_configured_bot(self):
        self.assertNotIn("t.me/", self.page())

    @override_settings(TELEGRAM_BOT_USERNAME="@SomeBot")
    def test_bot_card_uses_the_configured_bot(self):
        self.assertIn('href="https://t.me/SomeBot"', self.page())


class AboutAssetsTests(AboutFixture):
    def test_hero_photo_is_a_local_placeholder_with_size_and_alt(self):
        html = self.page()
        img = re.search(r'<img class="ab-photo__img"[^>]*>', html).group(0)
        for attr in ('width="960"', 'height="1080"', 'alt="Волонтёр', "/static/images/about/hero-volunteer-960.jpg"):
            self.assertIn(attr, img)
        self.assertIn("hero-volunteer-480.webp 480w", html)

    @override_settings(MEDIA_ROOT=_MEDIA_ROOT.path)
    def test_below_the_fold_images_are_lazy(self):
        self.make_activity()
        self.vol_b.profile.image = _jpeg()
        self.vol_b.profile.save()
        cache.clear()
        html = self.page()
        below = html[html.index('data-section="stats"'):html.index('<footer')]
        self.assertIn("<img", below)  # the volunteer avatars below need a photo for this test
        for img in re.findall(r"<img\b[^>]*>", below):
            self.assertIn('loading="lazy"', img)

    def test_no_map_script_up_front_and_map_is_lazy(self):
        html = self.page()
        self.assertNotIn("unpkg.com", html)
        self.assertNotIn("js/map.js?", html.split('id="ab-map"')[0])
        self.assertIn('data-map-js="/static/js/map.js', html)
        self.assertIn('data-maplibre-js="/static/vendor/maplibre-gl-5.24.0/maplibre-gl.js', html)
        js = (Path(settings.BASE_DIR) / "static" / "js" / "about.js").read_text(encoding="utf-8")
        self.assertIn("IntersectionObserver", js)
        self.assertIn("sha384-", js)

    def test_repeat_hit_runs_no_queries(self):
        self.make_activity()
        self.page()
        with CaptureQueriesContext(connection) as ctx:
            self.page()
        self.assertEqual(len(ctx.captured_queries), 0)

    def test_landing_cache_is_one_entry(self):
        self.page()
        self.assertIsNotNone(cache.get(landing.ABOUT_CACHE_KEY))


class CreateRequestPrefillTests(TestCase):
    def test_help_card_link_preselects_the_type(self):
        client_user = _user("ab_prefill", is_client=True)
        self.client.force_login(client_user)
        form = self.client.get(reverse("create_request") + "?help_type=medical").context["form"]
        self.assertEqual(form.initial.get("help_type"), "medical")
        form = self.client.get(reverse("create_request") + "?help_type=bogus").context["form"]
        self.assertNotIn("help_type", form.initial)
