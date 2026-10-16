"""Stage 4 — chart design: card headlines vs the previous period, how each card
draws (doughnut / ranked bars / …), the empty state, number formatting and the
one shared palette (charts-theme.js ↔ activity calendar ↔ dashboard sparkline)."""

import re
from datetime import date, datetime, time, timedelta
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import reverse

from myapp.models import HelpRequest
from myapp.services import analytics_v2 as an
from myapp.templatetags.numfmt import ru_num
from myapp.tests_analytics import TODAY, TZ, AnalyticsBase
from myapp.tests_analytics_filters import freeze_today

STATIC = Path(settings.BASE_DIR) / "static"


def _read(rel):
    return (STATIC / rel).read_text(encoding="utf-8")


class ChangePercentTests(SimpleTestCase):
    def test_change(self):
        self.assertEqual(an.change_percent(12, 10), 20)
        self.assertEqual(an.change_percent(5, 10), -50)
        self.assertEqual(an.change_percent(10, 10), 0)
        self.assertEqual(an.change_percent(1, 3), -67)

    def test_an_empty_previous_period_is_not_infinity(self):
        self.assertIsNone(an.change_percent(5, 0))
        self.assertIsNone(an.change_percent(0, 0))
        self.assertIsNone(an.change_percent(5, None))
        self.assertIsNone(an.change_percent(None, 4))


class RuNumTests(SimpleTestCase):
    def test_like_intl_ru(self):
        self.assertEqual(ru_num(1234567), "1 234 567")
        self.assertEqual(ru_num(4.25, 1), "4,2")  # banker's rounding of .25 → like Python's format
        self.assertEqual(ru_num(11.2, 1), "11,2")
        self.assertEqual(ru_num(0), "0")
        self.assertEqual(ru_num(None), "—")


class HeadlineTests(AnalyticsBase):
    def make(self, day, region="dushanbe"):
        task = HelpRequest.objects.create(client=self.client_user, help_type="grocery", description="d",
                                          address="a", phone="+992901234567", region=region)
        HelpRequest.objects.filter(pk=task.pk).update(created_at=datetime.combine(day, time(12), tzinfo=TZ))

    def test_previous_period_is_the_same_length_right_before(self):
        f = an.parse_filters({"period": "7d"}, today=TODAY)
        for i in range(3):
            self.make(TODAY - timedelta(days=i))          # this week: 3
        for i in range(2):
            self.make(TODAY - timedelta(days=7 + i))      # the week before: 2
        self.make(TODAY - timedelta(days=14))              # older — in neither
        head = an.headlines(f)["requests_timeline"]
        self.assertEqual((head["value"], head["previous"], head["change"], head["direction"], head["good"]),
                         (3, 2, 50, "up", True))

    def test_empty_previous_period_gives_no_percentage(self):
        self.make(TODAY)
        head = an.headlines(an.parse_filters({"period": "7d"}, today=TODAY))["requests_timeline"]
        self.assertEqual((head["value"], head["previous"], head["change"], head["direction"]), (1, 0, None, None))

    def test_region_applies_to_both_periods(self):
        self.make(TODAY, region="sogd")
        self.make(TODAY - timedelta(days=8), region="sogd")
        self.make(TODAY - timedelta(days=8), region="dushanbe")
        head = an.headlines(an.parse_filters({"period": "7d", "region": "sogd"}, today=TODAY))["regions"]
        self.assertEqual((head["value"], head["previous"], head["change"]), (1, 1, 0))
        self.assertEqual(head["direction"], "same")

    def test_growth_of_bad_news_is_red(self):
        f = an.parse_filters({"period": "7d"}, today=TODAY)
        heads = an.headlines(f)
        self.assertEqual(set(heads), set(an.CHARTS) - {"top_volunteers"})
        self.assertTrue(an.UP_IS_BAD <= set(heads))

    def test_availability_is_live_without_comparison(self):
        head = an.headlines(an.parse_filters({"period": "30d"}, today=TODAY))["availability"]
        self.assertTrue(head["live"])
        self.assertIsNone(head["previous"])
        self.assertIsNone(head["change"])

    def test_headline_matches_the_chart(self):
        for day in (TODAY, TODAY - timedelta(days=1)):
            self.make(day)
        f = an.parse_filters({"period": "30d"}, today=TODAY)
        heads = an.headlines(f)
        timeline = an.chart("requests_timeline", f)
        self.assertEqual(heads["requests_timeline"]["value"], sum(timeline["series"][0]["values"]))
        self.assertEqual(heads["help_types"]["value"], 2)
        self.assertEqual(heads["ratings"]["value"], None)


class ChartPageTests(AnalyticsBase):
    def setUp(self):
        super().setUp()
        freeze_today(self)

    def page(self, query=""):
        self.client.force_login(self.curator)
        return self.client.get(reverse("crm_analytics") + query).content.decode()

    def card(self, body, name):
        match = re.search(r'<section class="card chart-card[^"]*" data-chart="%s"[^>]*>(.*?)</section>' % name, body, re.S)
        self.assertIsNotNone(match, name)
        return match.group(0)

    def test_display_per_card(self):
        self.seed(2)
        body = self.page()
        expected = {
            "requests_timeline": "line", "funnel": "bars", "priorities": "doughnut",
            "availability": "doughnut", "help_types": "rank", "regions": "rank",
            "donations_category": "rank", "chat_flags_help_types": "stack", "ratings": "bars",
        }
        for name, display in expected.items():
            self.assertIn(f'data-display="{display}"', self.card(body, name), name)
        # > 5 categories never become a doughnut.
        self.assertEqual(len(an.chart("help_types", an.parse_filters({}))["labels"]) > 5, True)

    def test_ranked_boxes_get_their_height_from_the_server(self):
        body = self.page()
        help_types = self.card(body, "help_types")
        rows = len(an.chart("help_types", an.parse_filters({}))["labels"])
        self.assertIn(f"--box-h: {rows * 30 + 8}px", help_types)

    def test_half_cards_share_one_box_height_and_scroll_long_rankings(self):
        """Every non-wide card is built the same way (one-line title with a
        translated title, a 2-line note slot, one 18rem box, table toggle at the
        bottom) so a row of two is always one size; a ranking that needs more
        rows than fit scrolls inside the box (--rows), never grows the card."""
        self.seed(2)
        body = self.page()
        help_types = self.card(body, "help_types")
        self.assertIn("chart-card--half", help_types)
        self.assertIn('title="По видам помощи" data-i18n-title="an.c.help_types"', help_types)
        self.assertIn('class="field-hint chart-card__note" aria-hidden="true"', help_types)
        rows = len(an.chart("help_types", an.parse_filters({}))["labels"])
        self.assertIn(f"--rows: {rows}", help_types)
        self.assertIn('<div class="chart-plot"><canvas', help_types)
        self.assertIn('data-i18n-title="an.n.workload"', self.card(body, "workload"))
        timeline = self.card(body, "requests_timeline")
        self.assertIn("chart-card--wide", timeline)
        self.assertNotIn("chart-card--half", timeline)

        css = _read("css/analytics.css")
        grid = re.search(r"\.chart-grid \{(.*?)\}", css, re.S).group(1)
        self.assertIn("repeat(2, minmax(0, 1fr))", grid)
        self.assertIn("align-items: stretch", grid)
        self.assertIn(".chart-box { position: relative; height: 18rem;", css)
        self.assertIn(".chart-card--half .chart-box { overflow-y: auto;", css)
        self.assertIn(".chart-card--half .chart-table { margin-top: auto; }", css)
        # No per-display heights left for half-width cards (wide ones keep theirs).
        self.assertNotRegex(css, r"(^|[,\n])\s*\.chart-box--(doughnut|rows)\s*[,{]")

    def test_empty_period_shows_text_in_the_chart_area(self):
        body = self.page("?region=gbao")
        card = self.card(body, "requests_timeline")
        self.assertIn('class="chart-empty" data-i18n="an.no_data"', card)
        self.assertIn("Нет данных за выбранный период", card)
        self.assertNotIn("<canvas", card)
        self.assertIn('data-empty="1"', card)
        # …the headline says "—" instead of a percentage.
        self.assertIn('data-i18n-title="an.prev_empty"', card)

    def test_headline_chip(self):
        today = an.local_today()
        for i in range(3):
            task = HelpRequest.objects.create(client=self.client_user, help_type="grocery", description="d",
                                              address="a", phone="+992901234567", region="dushanbe")
            when = today - timedelta(days=i if i < 2 else 8)
            HelpRequest.objects.filter(pk=task.pk).update(created_at=datetime.combine(when, time(12), tzinfo=TZ))
        card = self.card(self.page("?period=7d"), "requests_timeline")
        self.assertIn('data-num="2"', card)
        self.assertIn('chart-delta chart-delta--good" data-change="100" data-direction="up"', card)
        self.assertIn("▲ 100 %", card)
        self.assertIn("рост на 100 %", card)
        live = self.card(self.page("?period=7d"), "availability")
        self.assertIn('data-i18n="an.live_now"', live)
        self.assertNotIn("chart-delta", live)

    def test_scripts_and_no_inline_chart_colours(self):
        body = self.page()
        self.assertIn("js/charts-theme.js", body)
        self.assertLess(body.index("js/charts-theme.js"), body.index("js/crm-analytics.js"))
        page_js = _read("js/crm-analytics.js")
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{6}\b", page_js), "colours belong in charts-theme.js")
        self.assertNotIn("--series-", _read("css/analytics.css"))


class PaletteTests(SimpleTestCase):
    def palette(self):
        js = _read("js/charts-theme.js")
        out = {}
        for mode in ("light", "dark"):
            block = re.search(mode + r": \{([^}]*)\}", js).group(1)
            out[mode] = dict(re.findall(r"(\w+): '(#[0-9A-Fa-f]{6})'", block))
        heat = {mode: re.findall(r"'(#[0-9A-Fa-f]{6})'", re.search(mode + r": \[([^\]]*)\]", js).group(1))
                for mode in ("light", "dark")}
        return out, heat

    def test_brand_colours(self):
        palette, _ = self.palette()
        self.assertEqual(palette["light"]["primary"], "#2A3BA5")
        self.assertEqual(palette["light"]["accent"], "#F2A23A")
        self.assertEqual(len(palette["light"]), 5)
        self.assertEqual(set(palette["light"]), set(palette["dark"]))

    def test_colours_differ_in_lightness_too(self):
        def lightness(hex_):
            r, g, b = (int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))
            lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
            y = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
            return 116 * y ** (1 / 3) - 16
        palette, _ = self.palette()
        values = sorted(lightness(c) for c in palette["light"].values())
        self.assertTrue(all(b - a >= 8 for a, b in zip(values, values[1:])), values)
        for name in palette["light"]:  # dark = lighter versions
            self.assertGreater(lightness(palette["dark"][name]) + 1, 50, name)

    def test_heatmap_uses_the_activity_calendar_scale(self):
        _, heat = self.palette()
        css = _read("css/activity.css")
        light = re.search(r"--act-1: (#\w+);\s*--act-2: (#\w+);\s*--act-3: (#\w+);\s*--act-4: (#\w+);", css, re.I)
        self.assertEqual([c.lower() for c in heat["light"]], [c.lower() for c in light.groups()])
        dark_block = css[css.index('[data-theme="dark"]'):]
        dark = re.search(r"--act-1: (#\w+);\s*--act-2: (#\w+);\s*--act-3: (#\w+);\s*--act-4: (#\w+);", dark_block, re.I)
        self.assertEqual([c.lower() for c in heat["dark"]], [c.lower() for c in dark.groups()])

    def test_dashboard_trend_legend_uses_the_same_entity_colours(self):
        # The dashboard's 14-day chart is drawn by charts-theme.js (entity keys
        # created / completed); its HTML legend dots must match those colours.
        palette, _ = self.palette()
        css = _read("css/style.css")
        self.assertIn(f".trend-legend__item::before {{ content: \"\"; width: 0.6rem; height: 0.6rem; border-radius: 50%; background: {palette['light']['primary']}; }}", css)
        self.assertIn(f".trend-legend__item--completed::before {{ background: {palette['light']['teal']}; }}", css)
        self.assertIn(f'[data-theme="dark"] .trend-legend__item::before {{ background: {palette["dark"]["primary"]}; }}', css)
        self.assertIn(f'[data-theme="dark"] .trend-legend__item--completed::before {{ background: {palette["dark"]["teal"]}; }}', css)
        js = _read("js/dashboard.js")
        self.assertIn("key: 'created'", js)
        self.assertIn("key: 'completed'", js)
