"""FAQ page (/faq/): only active content, language fallback, SEO, admin, i18n keys."""

import json
import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from accounts.models import Users
from myapp.models import FAQCategory, FAQItem
from myapp.services import faq

BASE = Path(settings.BASE_DIR)


class FAQPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.general = FAQCategory.objects.create(name_ru="Общие", name_en="General", order=1)
        cls.q_active = FAQItem.objects.create(
            category=cls.general, order=2,
            question_ru="Как попросить помощь?", question_en="How do I ask for help?",
            answer_ru="Зарегистрируйтесь и создайте заявку.",
        )
        cls.q_first = FAQItem.objects.create(
            category=cls.general, order=1, question_ru="Это бесплатно?", answer_ru="Да, полностью.",
        )
        cls.q_hidden = FAQItem.objects.create(
            category=cls.general, is_active=False,
            question_ru="Скрытый вопрос", answer_ru="Скрытый ответ",
        )
        # An inactive category hides its questions, even active ones.
        off = FAQCategory.objects.create(name_ru="Выключенная категория", is_active=False)
        FAQItem.objects.create(category=off, question_ru="Вопрос выключенной категории", answer_ru="—")
        # An active category with no active question is not shown at all.
        bare = FAQCategory.objects.create(name_ru="Пустая категория", order=2)
        FAQItem.objects.create(category=bare, is_active=False, question_ru="Тоже скрыт", answer_ru="—")

    def get(self):
        response = self.client.get(reverse("faq"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_page_opens_anonymously_at_faq(self):
        self.assertEqual(reverse("faq"), "/faq/")
        html = self.get()
        self.assertIn('data-i18n="faq.title"', html)
        self.assertIn("Как попросить помощь?", html)

    def test_inactive_questions_and_categories_are_not_shown(self):
        html = self.get()
        for text in ("Скрытый вопрос", "Скрытый ответ", "Выключенная категория",
                     "Вопрос выключенной категории", "Пустая категория", "Тоже скрыт"):
            with self.subTest(text=text):
                self.assertNotIn(text, html)

    def test_order_and_query_count(self):
        with self.assertNumQueries(2):
            categories = faq.public_categories()
            questions = [item.question_ru for cat in categories for item in cat.active_items]
        self.assertEqual([c.name_ru for c in categories], ["Общие"])
        self.assertEqual(questions, ["Это бесплатно?", "Как попросить помощь?"])

    def test_every_language_rendered_with_russian_fallback(self):
        html = self.get()
        self.assertIn('<span data-faq-lang="en" lang="en">How do I ask for help?</span>', html)
        # No Tajik / English text → the Russian one in that slot.
        self.assertIn('<span data-faq-lang="tj" lang="tg">Как попросить помощь?</span>', html)
        self.assertIn('<span data-faq-lang="en" lang="en">Это бесплатно?</span>', html)

    def test_faqpage_json_ld_has_only_active_questions(self):
        html = self.get()
        blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
        data = [json.loads(b) for b in blocks]
        page = next(d for d in data if d.get("@type") == "FAQPage")
        names = [q["name"] for q in page["mainEntity"]]
        self.assertEqual(names, ["Это бесплатно?", "Как попросить помощь?"])
        self.assertEqual(page["mainEntity"][0]["acceptedAnswer"]["text"], "Да, полностью.")

    def test_json_ld_cannot_break_out_of_the_script(self):
        self.q_first.question_ru = "</script><script>alert(1)</script>"
        self.q_first.save()
        html = self.get()
        self.assertNotIn("<script>alert(1)", html)

    def test_public_seo_and_links(self):
        html = self.get()
        self.assertNotIn('content="noindex', html)
        self.assertIn('<link rel="canonical" href="https://khayrkhoh.tj/faq/">', html)
        self.assertIn(f'href="{reverse("faq")}" data-i18n="nav.faq"', html)  # footer / menu
        robots = self.client.get("/robots.txt").content.decode()
        self.assertIn("Allow: /faq/", robots)
        self.assertIn("https://khayrkhoh.tj/faq/", self.client.get("/sitemap.xml").content.decode())

    def test_contact_block(self):
        with self.settings(TELEGRAM_BOT_USERNAME=""):
            html = self.get()
        self.assertIn('data-i18n="faq.help_title"', html)
        self.assertIn('href="tel:+992071817722"', html)
        self.assertNotIn("t.me/", html)
        with self.settings(TELEGRAM_BOT_USERNAME="@khayrkhoh_bot"):
            self.assertIn('href="https://t.me/khayrkhoh_bot"', self.get())


class FAQEmptyTests(TestCase):
    def test_no_demo_data_from_migrations_and_empty_page(self):
        self.assertFalse(FAQCategory.objects.exists())
        self.assertFalse(FAQItem.objects.exists())
        html = self.client.get(reverse("faq")).content.decode()
        self.assertIn('data-i18n="faq.no_items"', html)
        self.assertNotIn('"FAQPage"', html)
        self.assertNotIn("js/faq.js", html)


class FAQAdminTests(TestCase):
    def test_admin_lists_and_inline_open(self):
        admin = Users.objects.create_superuser(username="faq_admin", email="a@example.com", password="x-Strong-pass-1")
        self.client.force_login(admin)
        cat = FAQCategory.objects.create(name_ru="Общие")
        FAQItem.objects.create(category=cat, question_ru="Вопрос", answer_ru="Ответ")
        for url in (reverse("admin:myapp_faqcategory_changelist"),
                    reverse("admin:myapp_faqitem_changelist") + "?q=Вопрос",
                    reverse("admin:myapp_faqcategory_change", args=[cat.pk])):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class FAQStaticTests(TestCase):
    def test_every_key_exists_in_three_languages(self):
        source = (BASE / "static/js/i18n.js").read_text(encoding="utf-8")
        text = (BASE / "templates/myapp/faq.html").read_text(encoding="utf-8")
        keys = set(re.findall(r'data-i18n(?:-ph)?="([^"{]+)"', text)) | {"nav.faq"}
        for key in sorted(keys):
            with self.subTest(key=key):
                self.assertEqual(len(re.findall(rf'"{re.escape(key)}"\s*:', source)), 3)

    def test_reduced_motion_is_respected(self):
        self.assertIn("prefers-reduced-motion", (BASE / "static/css/faq.css").read_text(encoding="utf-8"))
        self.assertIn("prefers-reduced-motion", (BASE / "static/js/faq.js").read_text(encoding="utf-8"))
