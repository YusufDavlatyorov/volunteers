"""Demo data for a demo stand — NEVER production.

Guards (checked in ``handle`` before anything is read or written): the command
refuses unless ``settings.DEMO_MODE`` is on, when ``ALLOWED_HOSTS`` names the
production domain (``khayrkhoh.tj`` or a subdomain), and when the database name
is a production one (``settings.PRODUCTION_DB_NAMES``).

What it writes (every user, request, review and day of activity carries
``is_demo=True``; real accounts never get any of it):

* demo accounts — the older set across the five regions (``VOLUNTEERS`` /
  ``CLIENTS`` / ``CURATORS`` / ``admin_demo``) and a Dushanbe set with city
  districts (``DUSHANBE_VOLUNTEERS`` / ``DUSHANBE_CLIENTS``), registered on
  1 September 2026 or before (a few joined later, to show "since registration").
  An existing account is never duplicated and its login, password, e-mail and
  photo are never changed; a username that belongs to a real (non-demo)
  account is skipped. No avatar image is generated — demo users show the CSS
  initials until the owner uploads photos by hand;
* a **timeline** of help requests from 1 September 2026: a fixed, seeded plan
  (``SEED``) up to ``TIMELINE_END``, of which every step dated up to now is
  replayed **through the existing services** — ``task_actions.create_request`` /
  ``accept`` / ``complete``, ``HelpRequest.advance_work_stage``,
  ``feedback.submit_review`` — with the clock set to the step's time. Statuses
  are never written directly. Volunteers are picked from those registered
  before the step and free at that moment (the self-accept "one active request"
  rule holds over the whole timeline). Demo requests have no photo report (the
  photo-report models are never written here), so the reward of a completed
  demo request is released through the same ``photo_reports.credit_task`` an
  approval uses (points + achievements);
* ``UserDailyActivity`` for every demo account, every day from
  max(1 September 2026, registration) to today (never a gap; more on the days
  of its requests);
* the older demo extras: events, broadcasts, an open + a resolved SOS on active
  demo requests, in-kind donation offers, a Lost & Found board, a few chat lines.

Idempotent: accounts by username, requests by (client, created_at), reviews one
per request, activity by (user, date) — a second run on the same day changes
no counts. Deterministic: the same seed and the same database state give the
same data. Quiet: in-app / WebSocket / e-mail / Telegram deliveries are
switched off for the run (``_quiet``), the services themselves are unchanged.
All in one transaction. ``--reset`` first deletes ``is_demo=True`` rows only —
and refuses to touch anything a photo report points at.
"""

from __future__ import annotations

import datetime
import heapq
import logging
import random
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from unittest import mock
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Count, Q
from django.utils import timezone

from accounts.models import Profile
from myapp.models import (
    Broadcast,
    Donation,
    EmergencyReport,
    Event,
    HelpRequest,
    PetReport,
    Product,
    TaskReview,
    UserDailyActivity,
)

User = get_user_model()
PASSWORD = "Volunteer2026!"
TZ = ZoneInfo("Asia/Dushanbe")
SEED = 20260901
TIMELINE_START = datetime.datetime(2026, 9, 1, 8, 0, tzinfo=TZ)
# A fixed horizon: the plan never depends on today; only steps up to now run.
TIMELINE_END = datetime.datetime(2027, 3, 1, tzinfo=TZ)


REGIONS = ["dushanbe", "sogd", "khatlon", "gbao", "rrp"]

# Approximate city centres so demo markers land on the right part of the map.
REGION_CENTERS = {
    "dushanbe": (38.5598, 68.7870),
    "sogd": (40.2839, 69.6220),   # Khujand
    "khatlon": (37.8300, 68.7800),  # Bokhtar
    "gbao": (37.4900, 71.5500),   # Khorog
    "rrp": (38.5200, 68.5500),    # Hisor
    "all": (38.5598, 68.7870),
}

# Dushanbe's four city districts: centre, Russian / Tajik name, streets.
DISTRICTS = {
    "sino": ((38.5880, 68.7300), "Сино", "Сино",
             ["ул. Н. Карабаева", "ул. Сино", "мкр. 82", "мкр. Испечак", "ул. Дехоти"]),
    "firdavsi": ((38.5560, 68.7380), "Фирдавси", "Фирдавсӣ",
                 ["ул. Фирдавси", "мкр. 101", "ул. Б. Гафурова", "мкр. 91"]),
    "somoni": ((38.5800, 68.7790), "Исмоили Сомони", "Исмоили Сомонӣ",
               ["пр. Рудаки", "ул. Шотемур", "пр. Исмоили Сомони", "ул. Мирзо Турсунзода"]),
    "shohmansur": ((38.5550, 68.8110), "Шохмансур", "Шоҳмансур",
                   ["ул. Айни", "ул. Шохмансур", "мкр. Зарафшон", "ул. Фаизова"]),
}
REGION_TOWNS = {"sogd": "Худжанд, ул. Ленина", "khatlon": "Бохтар, ул. Вахдат",
                "gbao": "Хорог, ул. Ленина", "rrp": "Гиссар, ул. Айни"}

_AVAILABILITY_CYCLE = ["available", "available", "available", "busy", "offline"]
_HELP_TYPES = ["medical", "grocery", "transport", "household", "emotional", "documents", "other"]


def _jitter(seed_str, spread=0.035):
    """Deterministic small lat/lng offset (± ~spread degrees ≈ a few km),
    seeded from a string so re-running seed_demo keeps markers stable."""
    h = sum(ord(c) * (i + 1) for i, c in enumerate(seed_str))
    d_lat = ((h % 1000) / 1000 - 0.5) * 2 * spread
    d_lng = (((h // 1000) % 1000) / 1000 - 0.5) * 2 * spread
    return d_lat, d_lng


def _coords_for(region, seed_str):
    base_lat, base_lng = REGION_CENTERS.get(region, REGION_CENTERS["dushanbe"])
    d_lat, d_lng = _jitter(seed_str)
    return round(base_lat + d_lat, 6), round(base_lng + d_lng, 6)


# --------------------------------------------------------------------------- #
# people
# --------------------------------------------------------------------------- #

# The older set (kept: check_demo_password / check_demo_data read these names).
VOLUNTEERS = [
    ("aziz_k", "Азиз Каримов", "dushanbe", "перевозки, покупки, помощь с документами", 31),
    ("malika_s", "Малика Саидова", "sogd", "общение, продукты, сопровождение", 26),
    ("farhod_r", "Фарход Рахимов", "khatlon", "домашние дела и мелкий ремонт", 34),
    ("nigina_m", "Нигина Мирзоева", "dushanbe", "медицинское сопровождение", 24),
    ("rustam_n", "Рустам Назаров", "rrp", "транспорт и закупки", 29),
    ("dilnoza_t", "Дилноза Турсунова", "sogd", "социальные визиты", 28),
    ("umar_j", "Умар Джураев", "gbao", "помощь по дому", 37),
    ("shabnam_a", "Шабнам Алиева", "khatlon", "документы и звонки", 25),
    ("behruz_q", "Бехруз Каюмов", "dushanbe", "срочные поручения", 32),
    ("mavluda_h", "Мавлуда Хасанова", "rrp", "уход и поддержка", 41),
    ("said_p", "Саид Пулодов", "sogd", "субботники и акции", 23),
    ("zebo_l", "Зебо Латифова", "gbao", "помощь пожилым людям", 30),
    ("komron_y", "Комрон Юсуфов", "khatlon", "транспорт, продукты", 27),
    ("madina_b", "Мадина Бобоева", "dushanbe", "обучение телефону", 22),
    ("sorbon_i", "Сорбон Исмоилов", "rrp", "ремонт и доставка", 35),
]

CLIENTS = [
    ("client_zamira", "Замира Холова", "dushanbe", 68),
    ("client_rahmon", "Рахмон Шарипов", "sogd", 74),
    ("client_oygul", "Ойгул Назарова", "khatlon", 71),
    ("client_safar", "Сафар Беков", "gbao", 79),
    ("client_mavjuda", "Мавжуда Саидова", "rrp", 66),
    ("client_karim", "Карим Муродов", "dushanbe", 82),
]

CURATORS = [
    ("curator_samira", "Самира Курбонова", "dushanbe"),
    ("curator_jamshed", "Джамшед Одинаев", "sogd"),
    ("curator_lola", "Лола Мирсаидова", "khatlon"),
]

# Dushanbe volunteers: username, full name, district, registered, age, language.
DUSHANBE_VOLUNTEERS = [
    ("parviz_rahimzoda", "Парвиз Рахимзода", "sino", "2026-05-18", 27, "tj"),
    ("nilufar_karimova", "Нилуфар Каримова", "firdavsi", "2026-05-27", 23, "ru"),
    ("farrukh_saidov", "Фаррух Саидов", "somoni", "2026-06-04", 31, "ru"),
    ("zarina_ismoilova", "Зарина Исмоилова", "shohmansur", "2026-06-09", 25, "tj"),
    ("bakhodur_nazarov", "Баходур Назаров", "sino", "2026-06-15", 34, "tj"),
    ("sitora_hakimova", "Ситора Хакимова", "somoni", "2026-06-21", 22, "ru"),
    ("daler_sharipov", "Далер Шарипов", "firdavsi", "2026-06-26", 29, "ru"),
    ("mehrangez_yusufi", "Мехрангез Юсуфи", "shohmansur", "2026-07-01", 24, "tj"),
    ("firdavs_odinaev", "Фирдавс Одинаев", "sino", "2026-07-06", 20, "tj"),
    ("gulnoza_sobirova", "Гулноза Собирова", "somoni", "2026-07-10", 28, "ru"),
    ("jahongir_ashurov", "Джахонгир Ашуров", "shohmansur", "2026-07-14", 33, "ru"),
    ("madina_rajabova", "Мадина Раджабова", "firdavsi", "2026-07-19", 26, "tj"),
    ("suhrob_kurbonov", "Сухроб Курбонов", "sino", "2026-07-23", 30, "tj"),
    ("tahmina_safarova", "Тахмина Сафарова", "somoni", "2026-07-28", 21, "ru"),
    ("bahrom_mirzoev", "Бахром Мирзоев", "shohmansur", "2026-08-02", 38, "tj"),
    ("shahnoza_umarova", "Шахноза Умарова", "firdavsi", "2026-08-06", 27, "ru"),
    ("rustam_davlatov", "Рустам Давлатов", "somoni", "2026-08-10", 35, "ru"),
    ("malohat_jurayeva", "Малохат Джураева", "sino", "2026-08-14", 42, "tj"),
    ("sherzod_hamidov", "Шерзод Хамидов", "firdavsi", "2026-08-18", 24, "ru"),
    ("farangis_aminova", "Фарангис Аминова", "shohmansur", "2026-08-21", 19, "tj"),
    ("ismoil_qodirov", "Исмоил Кодиров", "somoni", "2026-08-25", 45, "tj"),
    ("nozanin_valieva", "Нозанин Валиева", "sino", "2026-08-28", 23, "ru"),
    ("khurshed_boboev", "Хуршед Бобоев", "firdavsi", "2026-08-31", 29, "ru"),
    ("dilorom_nematova", "Дилором Нематова", "shohmansur", "2026-09-01", 31, "tj"),
    ("umed_salimov", "Умед Салимов", "somoni", "2026-09-07", 26, "ru"),
    ("parvina_zokirova", "Парвина Зокирова", "sino", "2026-09-14", 22, "tj"),
    ("anushervon_rasulov", "Анушервон Расулов", "firdavsi", "2026-09-22", 28, "ru"),
    ("muhabbat_olimova", "Мухаббат Олимова", "shohmansur", "2026-09-29", 37, "tj"),
    ("siyovush_latipov", "Сиёвуш Латипов", "somoni", "2026-10-09", 21, "ru"),
    ("rukhshona_azizova", "Рухшона Азизова", "sino", "2026-10-12", 20, "tj"),
]
# Registered in summer but haven't helped yet (score 0 — the photo /
# completeness tie-breaks decide their order in the rating).
QUIET_VOLUNTEERS = {"sitora_hakimova", "khurshed_boboev"}

# Dushanbe clients (elderly): username, full name, district, registered, age, language.
DUSHANBE_CLIENTS = [
    ("bibihol_saidova", "Бибихол Саидова", "sino", "2026-07-03", 74, "tj"),
    ("abdurahmon_aliev", "Абдурахмон Алиев", "somoni", "2026-07-08", 81, "ru"),
    ("gulchehra_nazarova", "Гулчехра Назарова", "firdavsi", "2026-07-15", 69, "ru"),
    ("saidali_rahmonov", "Саидали Рахмонов", "shohmansur", "2026-07-20", 77, "tj"),
    ("oysha_mahmudova", "Ойша Махмудова", "sino", "2026-07-26", 72, "tj"),
    ("nurullo_sharifov", "Нурулло Шарифов", "somoni", "2026-07-31", 84, "ru"),
    ("mohira_tursunova", "Мохира Турсунова", "firdavsi", "2026-08-04", 66, "ru"),
    ("hojimurod_ismoilov", "Ходжимурод Исмоилов", "shohmansur", "2026-08-09", 79, "tj"),
    ("zuhro_qosimova", "Зухро Косимова", "sino", "2026-08-13", 71, "tj"),
    ("mirzo_safarov", "Мирзо Сафаров", "somoni", "2026-08-17", 86, "ru"),
    ("sanavbar_yusupova", "Санавбар Юсупова", "firdavsi", "2026-08-21", 75, "ru"),
    ("abduqodir_karimov", "Абдукодир Каримов", "shohmansur", "2026-08-24", 68, "tj"),
    ("tojinisso_rajabova", "Тоджиниссо Раджабова", "sino", "2026-08-27", 82, "tj"),
    ("hakim_sultonov", "Хаким Султонов", "somoni", "2026-08-30", 73, "ru"),
    ("marhabo_davlatova", "Мархабо Давлатова", "firdavsi", "2026-09-10", 70, "ru"),
    ("nizomiddin_boboev", "Низомиддин Бобоев", "shohmansur", "2026-09-18", 78, "tj"),
]
# Where the older Dushanbe clients live, and the language they write in.
LEGACY_CLIENT_HOME = {"client_zamira": ("somoni", "ru"), "client_karim": ("shohmansur", "tj")}

# --------------------------------------------------------------------------- #
# texts (Russian and Tajik — the client's language)
# --------------------------------------------------------------------------- #

REQUEST_TEXTS = {
    "grocery": {
        "ru": ["Купить продукты на неделю: хлеб, молоко, крупы, масло. Список дам.",
               "Нужно купить овощи и фрукты на рынке, сама не дойду — болят ноги.",
               "Принести продукты из магазина рядом с домом, деньги отдам при встрече.",
               "Купить муку, сахар и чай — тяжёлые сумки сам не донесу.",
               "Помогите купить продукты к выходным, удобно после 10 утра."],
        "tj": ["Барои як ҳафта хӯрокворӣ харидан лозим: нон, шир, ярма, равған.",
               "Аз бозор сабзавот ва мева харед, худам рафта наметавонам — пойҳоям дард мекунанд.",
               "Аз мағозаи наздик маҳсулот оред, пулро ҳангоми вохӯрӣ медиҳам.",
               "Орд, қанд ва чой харидан лозим, халтаҳои вазнинро худам бурда наметавонам."],
    },
    "medical": {
        "ru": ["Сопроводить в поликлинику к терапевту, приём в 9:30.",
               "Купить лекарства по рецепту в аптеке и принести домой.",
               "Проводить на анализы в городскую поликлинику, нужна помощь в очереди.",
               "Доставить лекарства от давления, рецепт у меня на руках.",
               "Сопроводить к окулисту и помочь добраться обратно."],
        "tj": ["Ба поликлиника назди духтури терапевт ҳамроҳӣ кунед, қабул соати 9:30.",
               "Доруҳоро аз рӯи дорухат аз дорухона харида ба хона оред.",
               "Барои таҳлил ба поликлиникаи шаҳрӣ ҳамроҳӣ кунед.",
               "Доруи фишори хунро расонед, дорухат дар дасти ман аст."],
    },
    "household": {
        "ru": ["Помочь с уборкой квартиры перед праздником.",
               "Поменять лампочки и проверить розетку на кухне.",
               "Помочь вынести старую мебель и мусор.",
               "Помыть окна, сама не достаю.",
               "Помочь снять и постирать шторы."],
        "tj": ["Пеш аз ид дар тозакунии хона кумак кунед.",
               "Лампочкаҳоро иваз кунед ва розеткаи ошхонаро санҷед.",
               "Мебели кӯҳна ва партовро баровардан лозим.",
               "Тирезаҳоро шӯед, дастам намерасад."],
    },
    "transport": {
        "ru": ["Отвезти на рынок «Корвон» и помочь донести покупки.",
               "Подвезти к родственникам в соседний район.",
               "Отвезти в пенсионный фонд и обратно."],
        "tj": ["Ба бозори «Корвон» баред ва дар бурдани харидҳо кумак кунед.",
               "То хонаи хешовандон дар ноҳияи ҳамсоя расонед.",
               "Ба хазинаи нафақа ва баргашт баред."],
    },
    "emotional": {
        "ru": ["Хочется поговорить и попить чаю, живу одна.",
               "Научить пользоваться телефоном и звонить внукам по видео."],
        "tj": ["Мехоҳам суҳбат кунам ва чой нӯшам, танҳо зиндагӣ мекунам.",
               "Истифодаи телефон ва бо наберагон видеозанг заданро омӯзонед."],
    },
    "documents": {
        "ru": ["Помочь заполнить заявление на пенсию и сделать копии.",
               "Помочь оплатить коммунальные услуги через банк."],
        "tj": ["Дар пур кардани ариза барои нафақа ва нусхабардорӣ кумак кунед.",
               "Дар пардохти хизматрасониҳои коммуналӣ тавассути бонк кумак кунед."],
    },
}
# Only the existing categories; weights = how often elderly clients ask.
HELP_TYPE_WEIGHTS = [("grocery", 30), ("medical", 27), ("household", 20), ("transport", 8),
                     ("emotional", 8), ("documents", 7)]
# Every request text the timeline can write (check_demo_data matches on these).
DEMO_REQUEST_TEXTS = frozenset(t for langs in REQUEST_TEXTS.values() for texts in langs.values() for t in texts)

REVIEW_COMMENTS = {
    5: {"ru": ["Большое спасибо! Всё сделали быстро и аккуратно.", "Очень вежливый и внимательный человек, спасибо!",
               "Пришли вовремя, помогли донести сумки до квартиры.", "Спасибо за заботу, дай Бог здоровья!"],
        "tj": ["Ташаккури зиёд! Ҳамаашро зуд ва бодиққат иҷро карданд.", "Одами хеле боодоб ва ғамхор, раҳмат!",
               "Сари вақт омаданд ва халтаҳоро то хона бурданд.", "Барои ғамхорӣ ташаккур, Худо саломатӣ диҳад!"]},
    4: {"ru": ["Всё хорошо, немного опоздали, но предупредили.", "Спасибо, помощь пришла вовремя."],
        "tj": ["Ҳама хуб, каме дер монданд, вале огоҳ карданд.", "Ташаккур, кумак сари вақт расид."]},
    3: {"ru": ["Помогли, но забыли купить хлеб.", "Нормально, но пришлось долго ждать."],
        "tj": ["Кумак карданд, вале нон харидро фаромӯш карданд.", "Хуб, вале дер интизор шудам."]},
    2: {"ru": ["Опоздали на час и не предупредили."], "tj": ["Як соат дер монданд ва огоҳ накарданд."]},
    1: {"ru": ["Купили не те лекарства, пришлось менять."], "tj": ["Доруи дигар хариданд, иваз кардан лозим шуд."]},
}
STARS_WEIGHTS = [(5, 62), (4, 27), (3, 7), (2, 3), (1, 1)]
GOOD_TAGS = ["polite", "punctual", "careful", "friendly", "above_and_beyond", "communicative"]

# --------------------------------------------------------------------------- #
# older demo extras (texts at module level: check_demo_data matches on them)
# --------------------------------------------------------------------------- #

# Bundled photos an earlier seed_demo copied into media/reports/. Kept only so
# check_demo_data can recognise such files — this command writes no photo
# reports and no media files any more.
REPORT_PHOTOS = [
    "volunteers/aid-delivery-elderly.jpg",
    "volunteers/community-registration.jpg",
    "volunteers/aid-distribution.jpg",
    "community/elderly-man-dushanbe.jpg",
    "volunteers/youth-community-activity.jpg",
    "community/elderly-portrait.jpg",
    "community/family-ishkashim.jpg",
    "volunteers/aid-delivery-elderly.jpg",
]

DEMO_EVENTS = [
    ("Субботник у городского парка", "Команда убирает аллею, красит лавочки и помогает пожилым жителям района.", "dushanbe"),
    ("Посадка деревьев", "Волонтеры высаживают молодые деревья рядом с домом ветеранов.", "sogd"),
    ("Теплый визит", "Кураторы собирают волонтеров для общения, доставки продуктов и проверки бытовых нужд.", "khatlon"),
    ("День цифровой помощи", "Обучаем пожилых людей пользоваться телефоном, мессенджерами и онлайн-услугами.", "rrp"),
    ("Региональная акция заботы", "Общая акция для всех регионов с фотоотчетами и рейтингом участников.", "all"),
]

# Requests an earlier seed_demo wrote (recognised by check_demo_data and adopted
# as demo rows — see _adopt_legacy). Not written any more.
DEMO_REQUESTS = [
    ("grocery", "Купить продукты на неделю и занести домой.", "ул. Рудаки 12", "dushanbe", "completed"),
    ("medical", "Сопроводить в поликлинику утром.", "пр. Исмоили Сомони 44", "sogd", "active"),
    ("household", "Помочь переставить мебель и проверить лампочки.", "ул. Бохтар 7", "khatlon", "pending"),
    ("documents", "Помочь заполнить документы и отправить заявление.", "ул. Ленина 19", "gbao", "completed"),
    ("emotional", "Прийти поговорить и помочь настроить телефон.", "мкр. 82, дом 5", "rrp", "pending"),
    ("transport", "Отвезти на рынок и помочь донести сумки.", "ул. Айни 3", "dushanbe", "active"),
    ("grocery", "Купить лекарства по списку и хлеб.", "ул. Сино 15", "sogd", "pending"),
    ("other", "Помочь подготовиться к семейному мероприятию.", "ул. Вахдат 21", "khatlon", "completed"),
]

# Photo reports an earlier seed_demo wrote (check_demo_data only).
DEMO_REPORTS = [
    ("После субботника", "Команда привела в порядок аллею и собрала 18 мешков мусора.", "dushanbe"),
    ("Доставка продуктов", "Волонтер доставил продукты и помог разобрать покупки дома.", "sogd"),
    ("Новые деревья", "Участники посадили деревья возле дома ветеранов.", "khatlon"),
    ("Цифровая помощь", "Пожилые жители научились отправлять сообщения и звонить по видео.", "rrp"),
    ("Помощь с документами", "Волонтер помог клиенту подготовить заявление и копии документов.", "gbao"),
    ("Теплый визит", "Команда провела вечер общения и проверила бытовые нужды.", "dushanbe"),
    ("Маршрут заботы", "Волонтеры закрыли несколько адресов за один день.", "sogd"),
    ("Акция региона", "Куратор собрал отчет по помощи пожилым людям.", "khatlon"),
]

DEMO_CATALOGUE = [
    ("Продуктовый набор на неделю", "Крупы, масло, консервы и хлеб для одного клиента.", "food", "наборов"),
    ("Набор средств гигиены", "Мыло, зубная паста, шампунь, средства ухода.", "hygiene", "наборов"),
    ("Тёплый плед", "Для пожилых клиентов в холодный сезон.", "blankets", "шт"),
    ("Зимняя куртка", "Тёплая верхняя одежда, разные размеры.", "clothing", "шт"),
    ("Школьный набор", "Тетради, ручки, рюкзак для детей из семей клиентов.", "school", "наборов"),
]

DEMO_BROADCASTS = [
    ("Большой субботник в эту субботу", "Берем перчатки, воду и хорошее настроение. Сбор в 09:00 у парка.", "dushanbe", 12),
    ("Акция помощи пожилым людям", "На этой неделе проверяем заявки, помогаем с продуктами и делаем фотоотчеты.", "all", 25),
]

DEMO_EMERGENCY_REASONS = (
    "Клиент не открывает дверь, соседи говорят о шуме внутри.",
    "Плохое самочувствие клиента.",
)

DEMO_DONATION_ORGS = ("Пекарня «Нон»", "Магазин «Баракат»")

# reporter (role, index), type, name, species, breed, region, description, status
DEMO_PETS = [
    (("client", 0), "lost", "Барсик", "cat", "дворовая", "dushanbe",
     "Серый кот, белые лапы, убежал во дворе на Рудаки. Отзывается на кличку.", "open"),
    (("volunteer", 0), "found", "", "cat", "", "dushanbe",
     "Серый кот с белыми лапами возле парка. Ухоженный, ласковый, ждёт хозяина.", "open"),
    (("client", 1), "lost", "Рекс", "dog", "овчарка", "sogd",
     "Молодая овчарка, тёмный окрас, синий ошейник. Потерялся у рынка в Худжанде.", "matched"),
    (("volunteer", 2), "found", "", "dog", "", "khatlon",
     "Небольшая рыжая собака без ошейника, дружелюбная. Найдена в Бохтаре.", "open"),
    (("client", 2), "lost", "Кеша", "bird", "попугай", "rrp",
     "Волнистый попугай, зелёный. Улетел с балкона. Воссоединён с хозяином.", "resolved"),
]

DEMO_EMAIL_DOMAIN = "gc.local"
DEMO_HISTORY_PREFIX = "[история] Демо-запрос #"
DEMO_CHAT_MSG_ID_PREFIX = "seed-"
DEMO_REPORT_IMAGE_PREFIX = "reports/report_"


def demo_email(username):
    """The e-mail seed_demo gives a demo account (the admin's was always admin@)."""
    return f"{'admin' if username == 'admin_demo' else username}@{DEMO_EMAIL_DOMAIN}"


def demo_usernames():
    """Every username this command may create."""
    return {"admin_demo", *(row[0] for row in (*VOLUNTEERS, *CLIENTS, *CURATORS,
                                               *DUSHANBE_VOLUNTEERS, *DUSHANBE_CLIENTS))}


# --------------------------------------------------------------------------- #
# guards
# --------------------------------------------------------------------------- #

def refusal_reason():
    """Why seed_demo must not run here, or None. Checked before any DB access."""
    if not getattr(settings, "DEMO_MODE", False):
        return ("seed_demo refuses to run without DEMO_MODE=True (DJANGO_DEMO_MODE): demo data must never "
                "reach production — a local copy or a separate demo stand with its own database only.")
    domain = getattr(settings, "PRODUCTION_DOMAIN", "khayrkhoh.tj").lower()
    for host in settings.ALLOWED_HOSTS:
        name = host.strip().lower().lstrip(".")
        if name == domain or name.endswith("." + domain):
            return f"seed_demo refuses to run: ALLOWED_HOSTS names the production domain ({domain})."
    db_name = str(settings.DATABASES["default"].get("NAME") or "")
    if db_name in getattr(settings, "PRODUCTION_DB_NAMES", ()):
        return "seed_demo refuses to run: this is the production database (PRODUCTION_DB_NAMES)."
    return None


# --------------------------------------------------------------------------- #
# quiet run + the clock
# --------------------------------------------------------------------------- #

class _Clock:
    """``timezone.now()`` during the replay: the planned time of the step."""

    def __init__(self, real_now):
        self.real_now = real_now
        self.at = None

    def __call__(self):
        return self.at if self.at is not None else self.real_now()


def _silent_notify(recipients, *args, **kwargs):
    """Delivers nothing, but answers like a delivery did (the number of
    recipients), so the zero-recipient ERROR safety nets stay quiet."""
    return len(list(recipients or []))


@contextmanager
def _quiet():
    """No in-app notification rows, WebSocket events, e-mails or Telegram
    messages while seeding — the delivery entry points are swapped for no-ops
    for the run; nothing in the services changes."""
    from myapp import notifications
    from myapp.realtime import publish
    from myapp.services import notify as notify_service

    original_notify = notify_service.notify
    with ExitStack() as stack:
        for name, module in list(sys.modules.items()):
            if (name.startswith("myapp.") or name.startswith("accounts.")) and \
                    getattr(module, "notify", None) is original_notify:
                stack.enter_context(mock.patch.object(module, "notify", _silent_notify))
        stack.enter_context(mock.patch.object(publish, "send_to_group", lambda *a, **k: False))
        stack.enter_context(mock.patch.object(notifications, "queue_email", lambda *a, **k: None))
        stack.enter_context(mock.patch.object(notifications, "send_telegram_message", lambda *a, **k: False))
        stack.enter_context(mock.patch.object(notifications, "_dispatch", lambda *a, **k: None))
        yield


@contextmanager
def _info_logs_off():
    """The services log one INFO line per step (IDs only) — a few thousand for
    a seed run. Warnings and errors still show."""
    loggers = [logging.getLogger(name) for name in ("myapp", "accounts")]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for logger, level in zip(loggers, levels):
            logger.setLevel(level)


# --------------------------------------------------------------------------- #
# the plan (pure: no database)
# --------------------------------------------------------------------------- #

@dataclass
class Persona:
    username: str
    full_name: str
    role: str                 # admin / curator / volunteer / client
    region: str
    joined: datetime.datetime
    age: int | None = None
    bio: str = ""
    lang: str = "ru"
    district: str = ""
    skills: list = field(default_factory=list)
    location: tuple | None = None
    energy: float = 0.0       # volunteers: how often they take requests
    address: str = ""
    phone: str = ""


@dataclass
class PlannedRequest:
    seq: int
    client: str
    region: str
    created: datetime.datetime
    help_type: str
    priority: str
    description: str
    address: str
    phone: str
    lat: float
    lng: float
    lang: str
    accept_after: datetime.timedelta
    duration: datetime.timedelta
    review: tuple | None      # (delay, stars, comment, tags)
    stage_offsets: tuple      # minutes after accept: en_route, arrived, in_progress
    volunteer: str | None = None
    accepted: datetime.datetime | None = None
    completed: datetime.datetime | None = None


def _day(text, hour=10):
    return datetime.datetime.combine(datetime.date.fromisoformat(text), datetime.time(hour), tzinfo=TZ)


def _daytime(moment, rng):
    """Move a moment that falls at night (22:00–07:00) to the next morning."""
    local = moment.astimezone(TZ)
    if 7 <= local.hour < 22:
        return local
    day = local.date() if local.hour < 7 else local.date() + datetime.timedelta(days=1)
    return datetime.datetime.combine(day, datetime.time(7, rng.randrange(0, 60, 5)), tzinfo=TZ)


def _weighted(rng, pairs):
    return rng.choices([v for v, _ in pairs], weights=[w for _, w in pairs])[0]


def build_personas():
    rng = random.Random(f"{SEED}:energy")
    people = [Persona("admin_demo", "Админ KhayrKhoh", "admin", "dushanbe", _day("2026-05-04"))]
    for i, (username, full_name, region) in enumerate(CURATORS):
        people.append(Persona(username, full_name, "curator", region, _day("2026-05-10") + datetime.timedelta(days=4 * i)))
    for i, (username, full_name, region, bio, age) in enumerate(VOLUNTEERS):
        h = sum(ord(c) for c in username)
        people.append(Persona(
            username, full_name, "volunteer", region, _day("2026-06-01") + datetime.timedelta(days=4 * i),
            age=age, bio=bio, skills=sorted({_HELP_TYPES[(h + k) % len(_HELP_TYPES)] for k in range(3)}),
            location=_coords_for(region, username), energy=1.0,
        ))
    for i, (username, full_name, district, joined, age, lang) in enumerate(DUSHANBE_VOLUNTEERS):
        (lat, lng), d_ru, d_tj, _streets = DISTRICTS[district]
        vr = random.Random(f"{SEED}:vol:{username}")
        skills = sorted(vr.sample([k for k, _ in HELP_TYPE_WEIGHTS], vr.randint(2, 3)))
        bio = (f"Дар ноҳияи {d_tj} зиндагӣ мекунам. Ба пиронсолон дар харид, роҳ ва корҳои хона кумак мекунам."
               if lang == "tj" else
               f"Живу в районе {d_ru}. Помогаю пожилым соседям с покупками, дорогой и делами по дому.")
        person = Persona(username, full_name, "volunteer", "dushanbe", _day(joined, 9 + i % 9), age=age, bio=bio,
                         lang=lang, district=district, skills=skills,
                         location=(round(lat + vr.uniform(-0.012, 0.012), 6), round(lng + vr.uniform(-0.012, 0.012), 6)))
        # Profiles of different completeness (the rating's second tie-break).
        if i % 6 == 1:
            person.bio = ""
        elif i % 6 == 3:
            person.age, person.skills = None, []
        elif i % 6 == 5:
            person.location = None
        people.append(person)
    # Energy: a seeded shuffle of Dushanbe's volunteers, Zipf-like weights — a
    # few very active people, most helping now and then, the quiet ones never.
    dushanbe = [p for p in people if p.role == "volunteer" and p.region == "dushanbe"
                and p.username not in QUIET_VOLUNTEERS and p.joined < _day("2026-10-08")]
    rng.shuffle(dushanbe)
    for rank, person in enumerate(dushanbe, start=1):
        person.energy = rank ** -0.75
    for person in people:
        if person.role == "volunteer" and (person.username in QUIET_VOLUNTEERS or person.joined >= _day("2026-10-08")):
            person.energy = 0.0

    for i, (username, full_name, region, age) in enumerate(CLIENTS):
        district, lang = LEGACY_CLIENT_HOME.get(username, ("", "ru"))
        people.append(Persona(username, full_name, "client", region, _day("2026-07-01") + datetime.timedelta(days=6 * i),
                              age=age, lang=lang, district=district))
    for username, full_name, district, joined, age, lang in DUSHANBE_CLIENTS:
        people.append(Persona(username, full_name, "client", "dushanbe", _day(joined, 11), age=age, lang=lang,
                              district=district))
    for i, person in enumerate(p for p in people if p.role == "client"):
        cr = random.Random(f"{SEED}:home:{person.username}")
        if person.district:
            (lat, lng), _ru, _tj, streets = DISTRICTS[person.district]
            person.location = (round(lat + cr.uniform(-0.01, 0.01), 6), round(lng + cr.uniform(-0.01, 0.01), 6))
            person.address = f"{cr.choice(streets)}, д. {cr.randint(2, 98)}, кв. {cr.randint(1, 64)}"
        else:
            person.location = _coords_for(person.region, person.username)
            person.address = f"{REGION_TOWNS[person.region]}, д. {cr.randint(2, 60)}"
        person.phone = f"+99290000{i:02d}{cr.randint(10, 99)}"
        person.bio = ""
    return people


def build_plan(people, blocked=frozenset()):
    """Every planned request up to TIMELINE_END, with its volunteer and times.
    ``blocked``: volunteers who hold an active request outside this plan."""
    requests = []
    for person in (p for p in people if p.role == "client"):
        cr = random.Random(f"{SEED}:requests:{person.username}")
        t = max(TIMELINE_START, person.joined + datetime.timedelta(days=1)) + datetime.timedelta(days=cr.uniform(0.2, 3))
        while t < TIMELINE_END:
            created = datetime.datetime.combine(t.astimezone(TZ).date(), datetime.time(cr.randint(8, 19), cr.randrange(0, 60, 5)), tzinfo=TZ)
            created = max(created, person.joined + datetime.timedelta(hours=1))
            help_type = _weighted(cr, HELP_TYPE_WEIGHTS)
            urgent = 0.25 if help_type == "medical" else 0.08
            roll = cr.random()
            priority = "emergency" if help_type == "medical" and roll < 0.03 else "high" if roll < urgent else "normal"
            band = cr.random()
            accept_after = datetime.timedelta(hours=cr.uniform(0.3, 3) if band < 0.45 else
                                              cr.uniform(3, 20) if band < 0.78 else cr.uniform(20, 60))
            band = cr.random()
            duration = datetime.timedelta(hours=cr.uniform(1, 3) if band < 0.4 else
                                          cr.uniform(3, 8) if band < 0.7 else cr.uniform(18, 40))
            review = None
            if cr.random() < 0.72:
                stars = _weighted(cr, STARS_WEIGHTS)
                comment = "" if cr.random() < 0.2 else cr.choice(REVIEW_COMMENTS[stars][person.lang])
                tags = sorted(cr.sample(GOOD_TAGS, cr.randint(1, 2))) if stars == 5 else \
                    [cr.choice(GOOD_TAGS)] if stars == 4 and cr.random() < 0.5 else []
                review = (datetime.timedelta(hours=cr.uniform(1, 30)), stars, comment, tags)
            lat, lng = person.location
            requests.append(PlannedRequest(
                seq=len(requests), client=person.username, region=person.region, created=created,
                help_type=help_type, priority=priority,
                description=cr.choice(REQUEST_TEXTS[help_type][person.lang]),
                address=person.address, phone=person.phone,
                lat=round(lat + cr.uniform(-0.0008, 0.0008), 6), lng=round(lng + cr.uniform(-0.0008, 0.0008), 6),
                lang=person.lang, accept_after=accept_after, duration=duration, review=review,
                stage_offsets=(cr.randint(10, 35), cr.randint(10, 25), cr.randint(5, 15)),
            ))
            t = created + datetime.timedelta(days=cr.uniform(2, 5))

    # Assignment in time order. A volunteer may take a request only after
    # registering and only while free (busy from accept to completion) — the
    # self-accept "one active request" rule, over the whole timeline.
    arng = random.Random(f"{SEED}:assign")
    volunteers = [p for p in people if p.role == "volunteer" and p.energy > 0 and p.username not in blocked]
    busy_until = {p.username: TIMELINE_START for p in volunteers}
    heap = [(_daytime(r.created + r.accept_after, arng), r.seq) for r in requests]
    heapq.heapify(heap)
    by_seq = {r.seq: r for r in requests}
    while heap:
        at, seq = heapq.heappop(heap)
        if at >= TIMELINE_END:
            continue
        task = by_seq[seq]
        local = [p for p in volunteers if p.region == task.region and p.joined <= at]
        free = [p for p in local if busy_until[p.username] <= at]
        if not free:
            later = [busy_until[p.username] for p in local] or \
                    [p.joined for p in volunteers if p.region == task.region and p.joined > at]
            if later:
                heapq.heappush(heap, (_daytime(max(min(later), at) + datetime.timedelta(minutes=10), arng), seq))
            continue
        weights = [p.energy * (1.6 if task.help_type in p.skills else 1.0) for p in free]
        chosen = arng.choices(free, weights=weights)[0]
        task.volunteer, task.accepted = chosen.username, at
        task.completed = _daytime(at + task.duration, arng)
        busy_until[chosen.username] = task.completed
    return requests


def plan_events(requests):
    """(time, order, seq, kind, payload) — complete before review before create
    before accept before stage at the same instant."""
    out = []
    for r in requests:
        out.append((r.created, 2, r.seq, "create", None))
        if r.accepted is None:
            continue
        out.append((r.accepted, 3, r.seq, "accept", None))
        at = r.accepted
        for minutes, stage in zip(r.stage_offsets, HelpRequest.WORK_STAGE_ORDER[1:]):
            at = at + datetime.timedelta(minutes=minutes)
            if at < r.completed:
                out.append((at, 4, r.seq, "stage", stage))
        out.append((r.completed, 0, r.seq, "complete", None))
        if r.review:
            out.append((r.completed + r.review[0], 1, r.seq, "review", None))
    out.sort(key=lambda e: (e[0], e[1], e[2]))
    return out


# --------------------------------------------------------------------------- #
# the command
# --------------------------------------------------------------------------- #

class Command(BaseCommand):
    help = ("Заполняет демо-стенд демонстрационными пользователями, заявками, отзывами и активностью "
            "(is_demo=True). Только при DEMO_MODE=True и не на production-хосте/базе.")

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true",
                            help="Delete is_demo=True rows first (never anything a photo report points at), then seed.")

    def handle(self, *args, **options):
        reason = refusal_reason()
        if reason:
            raise CommandError(reason)
        settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
        self.real_now = timezone.now()
        self.clock = _Clock(timezone.now)
        self.skipped = []
        self.failed_steps = 0
        with transaction.atomic(), _quiet(), _info_logs_off():
            if options["reset"]:
                self._reset()
            self._adopt_legacy()
            with mock.patch("django.utils.timezone.now", self.clock):
                users = self._upsert_people()
                plan = self._replay(users)
            admin = users.get("admin_demo")
            curators = [users[u] for u, *_ in CURATORS if u in users]
            volunteers = [users[u] for u, *_ in (*VOLUNTEERS, *DUSHANBE_VOLUNTEERS) if u in users]
            clients = [users[u] for u, *_ in (*CLIENTS, *DUSHANBE_CLIENTS) if u in users]
            if admin and curators and volunteers and clients:
                self._create_events(curators)
                self._create_broadcasts(admin, curators)
                self._create_emergencies(admin)
                self._create_donations(clients, volunteers, admin)
                self._create_pet_reports(clients, volunteers, admin)
                self._create_flagged_chat()
            self._final_presence(volunteers)
            self._award_achievements(volunteers)
            # Last: every action the rows above left (requests, reviews,
            # donations, pets) must show on its day.
            self._daily_activity(users, plan)
        self._summary()

    # ----------------------------------------------------------------- reset

    def _reset(self):
        """Delete is_demo=True users / requests / reviews / activity. Anything
        a photo report points at stays (photo reports are never modified), and
        the run stops if a deletion would touch a real request or review."""
        from django.apps import apps
        from django.db.models.deletion import Collector, ProtectedError

        report_model = apps.get_model("myapp", "PhotoReport")  # read-only: what must never be touched
        refs = list(report_model.objects.values_list("author_id", "uploaded_by_id", "reviewed_by_id", "help_request_id"))
        kept_requests = {r[3] for r in refs if r[3]}
        kept_users = {u for r in refs for u in r[:3] if u}
        kept_users |= set(HelpRequest.objects.filter(pk__in=kept_requests).values_list("client_id", flat=True))
        TaskReview.objects.filter(is_demo=True).delete()
        UserDailyActivity.objects.filter(is_demo=True).delete()
        collector = Collector(using=connection.alias)
        try:
            # collect() takes one model at a time
            collector.collect(list(HelpRequest.objects.filter(is_demo=True).exclude(pk__in=kept_requests)))
            collector.collect(list(User.objects.filter(is_demo=True).exclude(pk__in=kept_users)))
        except ProtectedError as exc:
            raise CommandError(f"--reset stopped: protected rows ({exc.protected_objects.__class__.__name__}).")
        def real_rows(model, batch):
            """Does a deleted / updated batch hold a non-demo request or review?"""
            if model not in (HelpRequest, TaskReview):
                return False
            if hasattr(batch, "filter"):  # a queryset
                return batch.filter(is_demo=False).exists()
            return any(not row.is_demo for row in batch)

        batches = [(model, rows) for model, rows in collector.data.items()]
        batches += [(qs.model, qs) for qs in collector.fast_deletes]
        for (fld, _value), groups in collector.field_updates.items():
            batches += [(fld.model, group) for group in groups]
        # Querysets are registered unevaluated (often empty): only rows count.
        touched = {model for model, batch in batches
                   if (batch.exists() if hasattr(batch, "exists") else bool(batch))}
        if report_model in touched:
            raise CommandError("--reset stopped: a photo report would be changed — nothing deleted.")
        for model, batch in batches:
            if real_rows(model, batch):
                raise CommandError(f"--reset stopped: a real {model.__name__} would be changed — nothing deleted.")
        collector.delete()
        if kept_users or kept_requests:
            self.stdout.write(f"--reset kept {len(kept_users)} demo account(s) and {len(kept_requests)} demo request(s) "
                              "that photo reports point at.")

    # ---------------------------------------------------------------- people

    def _adopt_legacy(self):
        """Rows an earlier seed_demo wrote before is_demo existed: its accounts
        (demo username + exactly the e-mail it gave them), their requests with
        the older demo texts, those requests' reviews. Marked is_demo, nothing
        else changes."""
        names = demo_usernames()
        legacy = [pk for pk, username, email in User.objects.filter(username__in=names, is_demo=False)
                  .values_list("pk", "username", "email") if email == demo_email(username)]
        if legacy:
            User.objects.filter(pk__in=legacy).update(is_demo=True)
        texts = [row[1] for row in DEMO_REQUESTS]
        rows = HelpRequest.objects.filter(client__is_demo=True, is_demo=False).filter(
            Q(description__in=texts) | Q(description__startswith=DEMO_HISTORY_PREFIX))
        ids = list(rows.values_list("pk", flat=True))
        if ids:
            HelpRequest.objects.filter(pk__in=ids).update(is_demo=True)
            TaskReview.objects.filter(task_id__in=ids).update(is_demo=True)

    def _upsert_people(self):
        self.people = build_personas()
        existing = {u.username: u for u in User.objects.filter(username__in=[p.username for p in self.people])}
        users = {}
        for person in self.people:
            user = existing.get(person.username)
            if user is not None and not user.is_demo:
                self.skipped.append(person.username)  # a real account: never touched
                continue
            if user is None:
                self.clock.at = person.joined  # date_joined is auto_now_add
                user = User.objects.create_user(
                    username=person.username, email=demo_email(person.username), password=PASSWORD,
                    region=person.region, is_demo=True, is_active=True, is_email_verified=True,
                    is_superuser=person.role == "admin", is_staff=person.role == "admin",
                    is_curator=person.role == "curator", is_volunteer=person.role == "volunteer",
                    is_client=person.role == "client",
                )
                self.clock.at = None
                self._fill_profile(user, person)
            users[person.username] = user
        for person in self.people:
            if person.username in users:
                person.joined = max(person.joined, users[person.username].date_joined)  # an older account keeps its date
        return users

    def _fill_profile(self, user, person):
        """Profile of a NEW demo account (an existing one is left as it is).
        No image: the initials show until the owner uploads a photo."""
        profile, _ = Profile.objects.get_or_create(user=user)
        profile.full_name = person.full_name
        profile.age = person.age
        profile.bio = person.bio
        profile.language = person.lang if person.lang in ("ru", "tj", "en") else "ru"
        if person.role == "volunteer":
            profile.skills = list(person.skills)
        if person.location:
            profile.latitude, profile.longitude = person.location
            profile.location_updated_at = self.real_now  # a fresh pin, not a "stale location" banner
        profile.save()

    # -------------------------------------------------------------- timeline

    def _replay(self, users):
        from myapp.forms import HelpRequestForm
        from myapp.services import feedback, photo_reports, task_actions

        people = [p for p in self.people if p.username in users]
        clients = {p.username for p in people if p.role == "client"}
        draft = build_plan(people)
        keys = {(users[r.client].pk, r.created) for r in draft}
        # Volunteers holding an active request that isn't one of this plan's
        # can't self-accept anything — leave them out of the plan.
        blocked = {
            u for u, uid in ((p.username, users[p.username].pk) for p in people if p.role == "volunteer")
            if any((c, t) not in keys for c, t in HelpRequest.objects.filter(volunteer_id=uid, status="active")
                   .values_list("client_id", "created_at"))
        }
        plan = build_plan(people, frozenset(blocked)) if blocked else draft
        by_seq = {r.seq: r for r in plan}
        rows = {(r.client_id, r.created_at): r for r in
                HelpRequest.objects.filter(is_demo=True, client__username__in=clients)}

        def row_for(r):
            return rows.get((users[r.client].pk, r.created))

        for at, _order, seq, kind, payload in plan_events(plan):
            if at > self.real_now:
                break
            r = by_seq[seq]
            self.clock.at = at
            task = row_for(r)
            if kind == "create":
                if task is not None:
                    continue
                form = HelpRequestForm(data={
                    "help_type": r.help_type, "priority": r.priority, "description": r.description,
                    "address": r.address, "phone": r.phone, "latitude": r.lat, "longitude": r.lng,
                })
                if not form.is_valid():
                    raise CommandError(f"demo request form invalid: {form.errors.as_json()}")
                task = task_actions.create_request(users[r.client], form)
                rows[(task.client_id, task.created_at)] = task
                continue
            if task is None:
                continue
            task.refresh_from_db()
            volunteer = users[r.volunteer]
            if kind == "accept" and task.status == "pending":
                if not task_actions.accept(volunteer, task).ok:
                    self.failed_steps += 1
            elif kind == "stage" and task.status == "active" and task.volunteer_id == volunteer.pk:
                order = HelpRequest.WORK_STAGE_ORDER
                if order.index(payload) > order.index(task.work_stage):
                    task.advance_work_stage(payload)
            elif kind == "complete" and task.status == "active" and task.volunteer_id == volunteer.pk:
                if task_actions.complete(volunteer, task).ok:
                    # No photo report on demo requests: release the reward
                    # through the same path an approval uses — demo requests
                    # ONLY; a real request is paid only after an approved report.
                    if task.is_demo:
                        photo_reports.credit_task(task)
                else:
                    self.failed_steps += 1
            elif kind == "review" and task.status == "completed" and not TaskReview.objects.filter(task=task).exists():
                _delay, stars, comment, tags = r.review
                if not feedback.submit_review(users[r.client], task, stars=stars, comment=comment, tags=tags).ok:
                    self.failed_steps += 1
        self.clock.at = None
        return plan

    # -------------------------------------------------------------- activity

    def _daily_activity(self, users, plan):
        """A UserDailyActivity row for every demo account and every day from
        max(1 Sep 2026, registration) to today — never a gap; more on the days
        of the account's requests. Deterministic per (user, day)."""
        from myapp.services import activity

        today = self.real_now.astimezone(TZ).date()
        by_seq = {r.seq: r for r in plan}
        per_day = {}  # (username, local day) -> timeline steps (work stages included)
        for at, _o, seq, kind, _p in plan_events(plan):
            if at > self.real_now:
                break
            req = by_seq[seq]
            who = req.client if kind in ("create", "review") else req.volunteer
            key = (who, at.astimezone(TZ).date())
            per_day[key] = per_day.get(key, 0) + 1
        demo_users = {p.username: (users[p.username], p) for p in self.people if p.username in users}
        existing = {(row.user_id, row.date): row for row in
                    UserDailyActivity.objects.filter(user__in=[u for u, _p in demo_users.values()])}
        create, update = [], []
        for username, (user, person) in demo_users.items():
            # What the user's rows say they did, per day (requests, reviews,
            # donations, pets — activity.daily_counts minus the counter itself).
            derived = {day: sum(n for kind, n in kinds.items() if kind != "site")
                       for day, kinds in activity.daily_counts(user, today).items()}
            base = random.Random(f"{SEED}:act:{username}").uniform(0.6, 2.2) * (0.6 if person.role == "client" else 1)
            day = max(TIMELINE_START.date(), user.date_joined.astimezone(TZ).date())
            while day <= today:
                dr = random.Random(f"{SEED}:act:{username}:{day.isoformat()}")
                weekend = 0.6 if day.weekday() == 6 else 0.85 if day.weekday() == 5 else 1.0
                count = max(1, round(base * weekend * (0.5 + 2.5 * dr.random() ** 2)))
                happened = max(per_day.get((username, day), 0), derived.get(day, 0))
                if happened:
                    count += happened + dr.randint(1, 3)
                row = existing.get((user.pk, day))
                if row is None:
                    create.append(UserDailyActivity(user=user, date=day, actions_count=count, is_demo=True,
                                                    source="demo"))
                elif row.actions_count != count or not row.is_demo or row.source != "demo":
                    row.actions_count, row.is_demo, row.source = count, True, "demo"
                    update.append(row)
                day += datetime.timedelta(days=1)
        UserDailyActivity.objects.bulk_create(create, batch_size=500)
        UserDailyActivity.objects.bulk_update(update, ["actions_count", "is_demo", "source"], batch_size=500)

    # ---------------------------------------------------------------- extras

    def _create_events(self, curators):
        for idx, (title, description, region) in enumerate(DEMO_EVENTS):
            Event.objects.update_or_create(
                title=title,
                defaults={
                    "curator": curators[idx % len(curators)],
                    "description": description,
                    "region": region,
                    "date": timezone.now() + datetime.timedelta(days=idx + 2),
                    "notifications_sent": True,
                },
            )

    def _create_broadcasts(self, admin, curators):
        senders = [curators[0], admin]
        for (subject, message, region, sent_count), sender in zip(DEMO_BROADCASTS, senders):
            Broadcast.objects.update_or_create(
                subject=subject,
                defaults={"sender": sender, "message": message, "region": region, "sent_count": sent_count},
            )

    def _create_emergencies(self, admin):
        """One open + one resolved SOS on active demo requests, so the CRM isn't empty."""
        active = list(HelpRequest.objects.filter(is_demo=True, status="active", volunteer__isnull=False)
                      .order_by("accepted_at", "pk"))
        if not active:
            return
        open_task = active[0]
        EmergencyReport.objects.get_or_create(
            help_request=open_task, volunteer=open_task.volunteer, status=EmergencyReport.STATUS_OPEN,
            defaults={"reason": DEMO_EMERGENCY_REASONS[0], "region": open_task.region,
                      "latitude": open_task.latitude, "longitude": open_task.longitude,
                      "notified_at": timezone.now()},
        )
        if len(active) > 1:
            done_task = active[1]
            now = timezone.now()
            EmergencyReport.objects.get_or_create(
                help_request=done_task, volunteer=done_task.volunteer, status=EmergencyReport.STATUS_RESOLVED,
                defaults={"reason": DEMO_EMERGENCY_REASONS[1], "region": done_task.region,
                          "notified_at": now - datetime.timedelta(hours=2),
                          "acknowledged_at": now - datetime.timedelta(hours=2), "acknowledged_by": admin,
                          "resolved_at": now - datetime.timedelta(hours=1), "resolved_by": admin,
                          "resolution_note": "Вызвали 103, клиент осмотрен, всё в порядке."},
            )

    def _create_donations(self, clients, volunteers, admin):
        """A small catalogue of needed items + a few in-kind offers across the
        lifecycle (only on a database without any offers yet)."""
        products = []
        for name, description, category, unit in DEMO_CATALOGUE:
            product, _ = Product.objects.update_or_create(
                name=name, defaults={"description": description, "category": category, "unit": unit, "is_active": True},
            )
            products.append(product)
        if Donation.objects.exists():
            return
        vol = volunteers[0]
        plan = [
            (clients[0], "individual", "", products[0], "", "food", 3, "наборов", "dushanbe", Donation.APPROVED, False),
            (clients[0], "business", DEMO_DONATION_ORGS[0], None, "Свежий хлеб", "bakery", 50, "буханок", "dushanbe", Donation.DISTRIBUTED, False),
            (clients[1 % len(clients)], "individual", "", products[2], "", "blankets", 10, "шт", "sogd", Donation.READY, True),
            (clients[2 % len(clients)], "individual", "", None, "Детская одежда до 5 лет", "clothing", 15, "шт", "khatlon", Donation.PENDING, False),
            (clients[0], "business", DEMO_DONATION_ORGS[1], products[1], "", "hygiene", 20, "наборов", "dushanbe", Donation.RECEIVED, True),
        ]
        for donor, dtype, org, product, item_name, category, qty, unit, region, target, assign in plan:
            offer = Donation.objects.create(
                donor=donor, donor_type=dtype, organization_name=org, product=product, item_name=item_name,
                category=category, quantity=qty, unit=unit, region=region,
                fulfilment="dropoff" if dtype == "business" else "pickup", status=Donation.PENDING, message="",
            )
            steps = {
                Donation.APPROVED: ["approve"],
                Donation.READY: ["approve", "mark_ready"],
                Donation.RECEIVED: ["approve", "mark_ready", "mark_received"],
                Donation.DISTRIBUTED: ["approve", "mark_ready", "mark_received", "mark_distributed"],
            }.get(target, [])
            for step in steps:
                getattr(offer, step)(admin)
                if assign and step == "approve":
                    offer.assign_volunteer(vol, admin)

    def _create_pet_reports(self, clients, volunteers, admin):
        """A small Lost & Found board (only on a database without one yet)."""
        if PetReport.objects.exists():
            return
        people = {"client": clients, "volunteer": volunteers}
        for (role, index), rtype, name, species, breed, region, description, status in DEMO_PETS:
            group = people[role]
            reporter = group[index % len(group)]
            lat, lng = _coords_for(region, f"pet-{name}-{description}")
            report = PetReport.objects.create(
                reporter=reporter, report_type=rtype, pet_name=name, species=species, breed=breed, region=region,
                description=description, latitude=lat, longitude=lng,
                contact_phone="+992 90 000 0000" if rtype == "lost" else "",
            )
            if status == "matched":
                report.mark_matched(admin)
            elif status == "resolved":
                report.resolve(reporter)

    def _create_flagged_chat(self):
        """A few chat lines on recent completed demo requests, some flagged, so
        the CRM "chat safety flags" charts are not empty."""
        from myapp.models import ChatMessage
        from myapp.services import chat_flags

        lines = ["Я в пути", "Купил продукты на 85 сомони, чек отдам", "Позвоните мне",
                 "Напишите мне в WhatsApp", "Мой номер +992 90 123 45 67", "Спасибо!"]
        recent = HelpRequest.objects.filter(is_demo=True, status="completed").order_by("-completed_at", "pk")[:12]
        for i, task in enumerate(recent):
            text = lines[i % len(lines)]
            sender = task.volunteer if i % 2 == 0 else task.client
            msg, created = ChatMessage.objects.get_or_create(
                task=task, sender=sender, client_msg_id=f"{DEMO_CHAT_MSG_ID_PREFIX}{task.pk}",
                defaults={"body": text, "flags": chat_flags.detect(text)},
            )
            if created:
                ChatMessage.objects.filter(pk=msg.pk).update(created_at=task.completed_at - datetime.timedelta(minutes=30))

    def _final_presence(self, volunteers):
        """Availability as of now (system-computed): a recent "last seen" for
        the online ones, then the usual recompute."""
        from myapp.services import presence

        now = timezone.now()
        for user in volunteers:
            status = _AVAILABILITY_CYCLE[sum(ord(c) for c in user.username) % len(_AVAILABILITY_CYCLE)]
            Profile.objects.filter(user=user).update(last_seen_at=None if status == "offline" else now)
            presence.recompute(user, now=now)

    def _award_achievements(self, volunteers):
        """Recalculate badges for the demo volunteers — silently (the timeline
        already awarded most of them when each request completed)."""
        from myapp.services import achievements

        for volunteer in volunteers:
            achievements.evaluate(volunteer, send_notifications=False)

    # --------------------------------------------------------------- summary

    def _summary(self):
        demo = User.objects.filter(is_demo=True)
        by_status = dict(HelpRequest.objects.filter(is_demo=True).values_list("status").annotate(n=Count("pk")))
        self.stdout.write(self.style.SUCCESS("Demo data ready."))
        self.stdout.write(
            f"Demo users: {demo.count()} (volunteers {demo.filter(is_volunteer=True).count()}, "
            f"clients {demo.filter(is_client=True).count()}, curators {demo.filter(is_curator=True).count()}, "
            f"admins {demo.filter(is_superuser=True).count()})"
        )
        self.stdout.write("Demo requests: " + ", ".join(f"{k} {by_status.get(k, 0)}" for k in
                                                         ("pending", "active", "completed", "cancelled")))
        self.stdout.write(f"Demo reviews: {TaskReview.objects.filter(is_demo=True).count()} | "
                          f"activity rows: {UserDailyActivity.objects.filter(is_demo=True).count()}")
        if self.skipped:
            self.stdout.write(self.style.WARNING(
                f"Skipped (username belongs to a real account): {', '.join(sorted(self.skipped))}"))
        if self.failed_steps:
            self.stdout.write(self.style.WARNING(f"Timeline steps refused by a service: {self.failed_steps}"))
        self.stdout.write(f"Demo password for all new demo users: {PASSWORD}")
