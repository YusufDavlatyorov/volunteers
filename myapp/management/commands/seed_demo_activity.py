"""History for the IMPORTED demo accounts (e-mail ``@khayrkhoh.invalid``).

The owner's decision (2026-10-19): the imported volunteers and clients on
khayrkhoh.tj are fictional accounts the owner created; this command gives
exactly those accounts a believable history from 1 August 2026 to now, while
the site shows the "Демонстрационный стенд" banner (``DEMO_MODE``). It is NOT
``seed_demo`` (that one creates its own accounts for a separate stand and keeps
refusing the production host) and it never creates, edits or deletes an account.

Participants (decided in code, nowhere else): active accounts whose e-mail ends
with ``DEMO_EMAIL_SUFFIX``, role volunteer or client, never staff. Every other
account is *protected*: it is never an author, a party, a recipient or a
counterpart, and no row that points at it is created or changed. Volunteers
already holding an active request, and anyone who is a party of an existing
request, are left out as well.

What it writes — always through the website's own services, with the clock set
to each step's time (``timezone.now`` patched), so statuses, timestamps,
points and badges follow the real rules:

* ``HelpRequest`` (``is_demo=True``) — created by demo clients
  (``task_actions.create_request``), self-accepted by demo volunteers
  (``task_actions.accept``, the one-active-request rule holds over the whole
  timeline), work stages (``actions.advance_work_stage_for``), completed
  (``task_actions.complete``; no photo reports — the reward is released through
  ``photo_reports.credit_task``, exactly as ``seed_demo`` does) or withdrawn by the
  client while pending (``cancellation.cancel_request``). Addresses and pins in
  Dushanbe's four districts; phones in the unassigned ``+992 00…`` range so no
  real subscriber is ever shown;
* ``TaskReview`` (``is_demo=True``) client → volunteer (``feedback.submit_review``;
  ``show_on_landing`` is never set — reviews stay off the landing page);
* ``ChatMessage`` / ``ChatReadState`` — short client ↔ volunteer chats while the
  task is active (``chat.send_message`` / ``chat.mark_read``);
* ``Notification`` — the in-app rows the services would create, for demo
  accounts only (the delivery layer is replaced: no WebSocket, e-mail or Telegram);
* ``UserAchievement`` — awarded by the achievement engine from the data
  (``achievements.evaluate``), never by hand;
* ``UserDailyActivity`` (``is_demo=True``, ``source="demo"``) on the days the
  account did something.

Dates: from ``WINDOW_START`` (1 Aug 2026, Asia/Dushanbe) to now, nothing in the
future; activity grows towards today (August least, the current month most).
The rating needs no write of its own: it is computed from completed requests,
reviews and badges (the cache is cleared at the end).

Safety:

* ``--i-confirm-demo-accounts`` is required for every write (``--dry-run`` needs
  none) and writes need ``DEMO_MODE=True`` (the banner on);
* nothing leaves the server: the notification fan-out is replaced by an
  in-app-only writer, WebSocket publishing is a no-op, and any attempt at
  e-mail, Telegram, Celery or outbound HTTP aborts the run and rolls it back;
* one transaction (REPEATABLE READ on PostgreSQL); before committing, every
  table is compared with a snapshot taken at the start: no ``Users`` row may
  differ, new rows may appear only in the seven models above and may point only
  at demo accounts and the new requests, existing rows may differ only in the
  derived ``Profile`` fields (``ALLOWED_PROFILE_CHANGES``) of demo accounts —
  anything else rolls the whole run back;
* the active / pending demo requests get ``alarm_sent`` / ``stale_alert_sent``
  so the overdue / stale cron sweeps never alert staff about them.

Manifest: the ids of every row created (all seven models) and the demo
profiles' derived fields before / after are written to ``--manifest``
(default ``<BASE_DIR>/demo_seed_manifest.json``, gitignored). A second run with
a manifest present changes nothing. ``--purge`` deletes strictly the rows the
manifest lists (and refuses if the deletion would cascade into anything else),
restores the profiles' derived fields and recomputes badges-free review stats;
``--reset`` = ``--purge`` and a fresh run.

``--dry-run`` performs the whole run inside the transaction, prints what would
be created and rolls back (nothing is written, no manifest).
"""

from __future__ import annotations

import datetime
import hashlib
import heapq
import json
import logging
import os
import random
import subprocess
import sys
from collections import Counter, defaultdict
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.core.cache import cache
from django.db import connection, models, transaction
from django.db.models import Q
from django.utils import timezone

User = get_user_model()
TZ = ZoneInfo("Asia/Dushanbe")
SEED = 20260801

DEMO_EMAIL_SUFFIX = "@khayrkhoh.invalid"
CONFIRM_OPTION = "--i-confirm-demo-accounts"
WINDOW_START = datetime.datetime(2026, 8, 1, 0, 0, tzinfo=TZ)
DEFAULT_REQUESTS = 72
MIN_REQUESTS, MAX_REQUESTS = 40, 80
MANIFEST_NAME = "demo_seed_manifest.json"
MANIFEST_VERSION = 1

# The only tables that may gain rows (label -> model name in apps).
MANIFEST_MODELS = (
    "myapp.HelpRequest", "myapp.TaskReview", "myapp.ChatMessage", "myapp.ChatReadState",
    "myapp.Notification", "myapp.UserAchievement", "myapp.UserDailyActivity",
)
# Derived Profile fields the services update for a demo account (points,
# review stats, system-computed availability) — the only existing-row changes.
ALLOWED_PROFILE_CHANGES = frozenset({"rating", "review_avg", "review_count", "availability_status", "updated_at"})
# The one account field a run changes (and a purge restores) — demo accounts only.
ALLOWED_USER_CHANGES = frozenset({"date_joined"})
USERS_LABEL = User._meta.label
# A cascade of a purge may also drop these UI-state rows of a demo request
# (a staff member who opened its chat later has a read cursor on it).
PURGE_EXTRA_CASCADE = frozenset({"myapp.ChatReadState", "myapp.TaskLocationPoint"})

# --------------------------------------------------------------------------- #
# places — Dushanbe's four districts (centre, streets in ru / tj / en)
# --------------------------------------------------------------------------- #

DISTRICTS = {
    "sino": ((38.5880, 68.7300), [
        ("ул. Н. Карабаева", "кӯчаи Н. Қарабоев", "N. Karaboev St"),
        ("ул. Сино", "кӯчаи Сино", "Sino St"),
        ("мкр. 82", "микрорайони 82", "82nd microdistrict"),
        ("мкр. Испечак", "микрорайони Испечак", "Ispechak microdistrict"),
    ]),
    "firdavsi": ((38.5560, 68.7380), [
        ("ул. Б. Гафурова", "кӯчаи Б. Ғафуров", "B. Gafurov St"),
        ("мкр. 101", "микрорайони 101", "101st microdistrict"),
        ("мкр. 91", "микрорайони 91", "91st microdistrict"),
    ]),
    "somoni": ((38.5800, 68.7790), [
        ("пр. Рудаки", "хиёбони Рӯдакӣ", "Rudaki Ave"),
        ("ул. Шотемур", "кӯчаи Шотемур", "Shotemur St"),
        ("ул. М. Турсунзаде", "кӯчаи М. Турсунзода", "M. Tursunzoda St"),
    ]),
    "shohmansur": ((38.5550, 68.8110), [
        ("ул. С. Айни", "кӯчаи С. Айнӣ", "S. Ayni St"),
        ("мкр. Зарафшон", "микрорайони Зарафшон", "Zarafshon microdistrict"),
    ]),
}
LANG_WEIGHTS = [("ru", 45), ("tj", 40), ("en", 15)]

# --------------------------------------------------------------------------- #
# texts (the client's language; the volunteer answers in it)
# --------------------------------------------------------------------------- #

REQUEST_TEXTS = {
    "grocery": {
        "ru": ["Купить продукты на неделю: хлеб, молоко, рис, подсолнечное масло. Список отдам у двери.",
               "Нужно принести овощи и фрукты с рынка — самостоятельно не дойду, болят колени.",
               "Купите, пожалуйста, муку и сахар — тяжёлые пакеты мне не донести.",
               "Нужны продукты к выходным: картошка, лук, яйца, чай. Удобно после 11:00."],
        "tj": ["Барои як ҳафта хӯрокворӣ лозим: нон, шир, биринҷ, равғани пахта. Рӯйхатро дар назди дар медиҳам.",
               "Аз бозор сабзавот ва мева биёред, худам рафта наметавонам — зонуҳоям дард мекунанд.",
               "Лутфан орд ва қанд харед, халтаҳои вазнинро бардошта наметавонам.",
               "Барои рӯзҳои истироҳат картошка, пиёз, тухм ва чой лозим. Баъд аз соати 11 қулай аст."],
        "en": ["Need groceries for the week: bread, milk, rice and cooking oil. I'll hand you the list at the door.",
               "Could someone bring vegetables and fruit from the bazaar? My knees hurt and I can't walk that far.",
               "Please buy flour and sugar — the bags are too heavy for me to carry.",
               "Groceries for the weekend: potatoes, onions, eggs and tea. Any time after 11 am works."],
    },
    "medical": {
        "ru": ["Проводить в поликлинику к терапевту, приём в 9:30. Нужна помощь в очереди.",
               "Купить лекарства по рецепту в аптеке и принести домой. Рецепт на руках.",
               "Сопроводить сдать анализы, утром натощак — выйти нужно к 8:00.",
               "Нужно съездить к окулисту и помочь вернуться домой."],
        "tj": ["Ба поликлиника назди духтури терапевт ҳамроҳӣ кунед, қабул соати 9:30.",
               "Доруҳоро аз рӯи дорухат аз дорухона харида ба хона оред. Дорухат дар дасти ман аст.",
               "Барои супоридани таҳлил ҳамроҳӣ кунед, субҳ соати 8:00 баромадан лозим.",
               "Ба назди духтури чашм бурдан ва баъд ба хона баргардонидан лозим."],
        "en": ["Please accompany me to the clinic, my appointment with the GP is at 9:30.",
               "Pick up my prescription from the pharmacy and bring it home. I have the prescription.",
               "I need someone to go with me for blood tests early in the morning, around 8:00.",
               "Take me to the eye doctor and help me get back home."],
    },
    "household": {
        "ru": ["Помогите поменять перегоревшие лампочки на кухне и в коридоре.",
               "Нужно помыть окна перед холодами, мне не дотянуться.",
               "Помочь вынести старый шкаф и мешки с мусором.",
               "Прибраться в квартире перед приездом родственников."],
        "tj": ["Дар ошхона ва долон лампочкаҳои сӯхтаро иваз кунед.",
               "Пеш аз сармо тирезаҳоро шустан лозим, дастам намерасад.",
               "Ҷевони кӯҳна ва халтаҳои партовро баровардан лозим.",
               "Пеш аз омадани хешовандон хонаро тоза кардан лозим."],
        "en": ["Help me replace the burnt-out bulbs in the kitchen and the hallway.",
               "The windows need washing before winter, I can't reach them.",
               "Need help carrying out an old wardrobe and some rubbish bags.",
               "Tidy up the flat before my relatives arrive."],
    },
    "transport": {
        "ru": ["Отвезти на рынок «Шохмансур» и помочь донести покупки.",
               "Подвезти к дочери в 82-й микрорайон и обратно.",
               "Отвезти в пенсионный фонд к 10:00 и подождать."],
        "tj": ["Ба бозори «Шоҳмансур» баред ва дар бурдани харидҳо кумак кунед.",
               "То хонаи духтарам дар микрорайони 82 ва баргашт расонед.",
               "Ба хазинаи нафақа то соати 10:00 баред ва интизор шавед."],
        "en": ["Drive me to Shohmansur bazaar and help carry the shopping.",
               "A lift to my daughter's in the 82nd microdistrict and back.",
               "Take me to the pension fund office by 10:00 and wait for me."],
    },
    "emotional": {
        "ru": ["Живу одна, хочется поговорить и вместе попить чаю.",
               "Научите звонить внукам по видео с телефона."],
        "tj": ["Танҳо зиндагӣ мекунам, мехоҳам суҳбат кунам ва якҷоя чой нӯшем.",
               "Бо телефон ба наберагон видеозанг заданро омӯзонед."],
        "en": ["I live alone and would love some company for a cup of tea.",
               "Teach me to video-call my grandchildren on my phone."],
    },
    "documents": {
        "ru": ["Помочь заполнить заявление на перерасчёт пенсии и сделать копии.",
               "Помогите разобраться с квитанциями за коммунальные услуги в банке."],
        "tj": ["Дар пур кардани ариза барои аз нав ҳисоб кардани нафақа ва нусхабардорӣ кумак кунед.",
               "Дар бонк бо квитансияҳои хизматрасониҳои коммуналӣ кумак кунед."],
        "en": ["Help me fill in the pension recalculation form and make copies.",
               "Help me sort out the utility bills at the bank."],
    },
    "other": {
        "ru": ["Собрать новую тумбочку — инструкция непонятная.",
               "Помочь забрать посылку с почты и донести до квартиры."],
        "tj": ["Ҷевончаи навро ҷамъ кунед, дастурамал фаҳмо нест.",
               "Аз почта бастаро гирифта то хона оред."],
        "en": ["Assemble a new bedside cabinet — the instructions are confusing.",
               "Collect a parcel from the post office and bring it up to my flat."],
    },
}
HELP_TYPE_WEIGHTS = [("grocery", 30), ("medical", 25), ("household", 18), ("transport", 9),
                     ("emotional", 8), ("documents", 6), ("other", 4)]

# Chat lines: (who, text) — "v" volunteer, "c" client.
CHAT = {
    "ru": {
        "greet": ["Здравствуйте! Ваша заявка у меня, скоро буду.",
                  "Добрый день! Это волонтёр KhayrKhoh, заявку взяли в работу.",
                  "Здравствуйте! Удобно, если подойду через час?"],
        "reply": ["Здравствуйте, спасибо! Жду вас.", "Хорошо. Второй подъезд, четвёртый этаж.",
                  "Да, удобно, я весь день дома."],
        "grocery": [("v", "Я в магазине. Молоко какое брать, 2,5 или 3,2?"), ("c", "Лучше 3,2.")],
        "grocery2": [("v", "Белого хлеба нет, взять лепёшки?"), ("c", "Да, две лепёшки, пожалуйста.")],
        "medical": [("v", "Аптеку нашли, лекарство есть, беру."), ("c", "Спасибо!")],
        "medical2": [("v", "К терапевту очередь небольшая, минут через 15 зайдём."), ("c", "Хорошо, я рядом.")],
        "transport": [("v", "Машина у подъезда, белая «Нексия»."), ("c", "Выхожу.")],
        "money": [("v", "Чек на 86 сомони, оставлю на столе вместе со сдачей."), ("c", "Хорошо, спасибо.")],
        "en_route": [("v", "Выхожу, буду минут через 20."), ("c", "Жду.")],
        "arrived": ["Я у подъезда.", "На месте, звоню в домофон."],
        "done": [("v", "Всё готово. Хорошего вам дня!"), ("c", "Спасибо большое, дай Бог здоровья!")],
        "client_extra": [[("c", "Если можно, возьмите ещё одну лепёшку."), ("v", "Хорошо, возьму.")],
                         [("c", "Я у соседки, подождите пять минут, пожалуйста."), ("v", "Конечно, подожду.")],
                         [("c", "Спасибо, что пишете — так спокойнее."), ("v", "Пожалуйста! Пишите, если что-то нужно.")]],
    },
    "tj": {
        "greet": ["Ассалому алайкум! Дархости шумо дар назди ман, ба наздикӣ меоям.",
                  "Рӯз ба хайр! Ман волонтёри KhayrKhoh ҳастам, дархости шуморо гирифтам.",
                  "Ассалому алайкум! Агар баъд аз як соат биёям, қулай аст?"],
        "reply": ["Ваалейкум ассалом, раҳмат! Интизорам.", "Хуб. Подъезди дуюм, ошёнаи чорум.",
                  "Ҳа, қулай аст, ман тамоми рӯз дар хона ҳастам."],
        "grocery": [("v", "Ман дар мағоза ҳастам. Ширро кадомашро гирам?"), ("c", "Аз ҳама ғафсашро.")],
        "grocery2": [("v", "Нони сафед нест, нони тандирӣ гирам?"), ("c", "Ҳа, ду нон гиред.")],
        "medical": [("v", "Дорухонаро ёфтам, дору ҳаст, мегирам."), ("c", "Раҳмат!")],
        "medical2": [("v", "Навбат ба назди духтур кам аст, баъд аз 15 дақиқа медароем."), ("c", "Хуб, ман дар наздикӣ ҳастам.")],
        "transport": [("v", "Мошин дар назди подъезд, «Нексия»-и сафед."), ("c", "Мебароям.")],
        "money": [("v", "Чек 86 сомонӣ шуд, бо бақияи пул дар рӯи миз мегузорам."), ("c", "Хуб, раҳмат.")],
        "en_route": [("v", "Баромадам, баъд аз 20 дақиқа мерасам."), ("c", "Интизорам.")],
        "arrived": ["Ман дар назди подъезд ҳастам.", "Расидам, домофонро пахш мекунам."],
        "done": [("v", "Ҳама тайёр. Рӯзи хуш!"), ("c", "Ташаккури зиёд, Худо саломатӣ диҳад!")],
        "client_extra": [[("c", "Агар мумкин бошад, боз як нон гиред."), ("v", "Хуб, мегирам.")],
                         [("c", "Ман дар хонаи ҳамсоя ҳастам, лутфан панҷ дақиқа интизор шавед."), ("v", "Албатта, интизор мешавам.")],
                         [("c", "Раҳмат, ки менависед — ҳамин тавр ором ҳастам."), ("v", "Марҳамат! Агар чизе лозим бошад, нависед.")]],
    },
    "en": {
        "greet": ["Hello! I've got your request, I'll be with you soon.",
                  "Hi, I'm a KhayrKhoh volunteer — your request is with me now.",
                  "Hello! Would it suit you if I come in about an hour?"],
        "reply": ["Hello, thank you! I'll be waiting.", "Great. Second entrance, fourth floor.",
                  "Yes, that's fine, I'm home all day."],
        "grocery": [("v", "I'm at the shop. Which milk do you prefer, 2.5% or 3.2%?"), ("c", "3.2%, please.")],
        "grocery2": [("v", "No white bread left — shall I get flatbread instead?"), ("c", "Yes, two loaves please.")],
        "medical": [("v", "Found the pharmacy, they have the medicine."), ("c", "Thank you!")],
        "medical2": [("v", "The queue for the doctor is short, we'll be in within 15 minutes."), ("c", "Good, I'm nearby.")],
        "transport": [("v", "The car is at your entrance, a white Nexia."), ("c", "Coming down.")],
        "money": [("v", "The receipt came to 86 somoni, I'll leave it on the table with your change."), ("c", "Fine, thanks.")],
        "en_route": [("v", "On my way, about 20 minutes."), ("c", "Waiting for you.")],
        "arrived": ["I'm at your entrance.", "I'm here, ringing the intercom."],
        "done": [("v", "All done. Have a lovely day!"), ("c", "Thank you so much, God bless you!")],
        "client_extra": [[("c", "If you can, please grab one more flatbread."), ("v", "Sure, will do.")],
                         [("c", "I'm at my neighbour's, could you wait five minutes, please?"), ("v", "Of course, I'll wait.")],
                         [("c", "Thank you for keeping me posted, it's reassuring."), ("v", "You're welcome! Message me if you need anything.")]],
    },
}

REVIEW_COMMENTS = {
    5: {"ru": ["Большое спасибо! Всё сделали быстро и аккуратно.", "Очень вежливый и внимательный человек.",
               "Пришли вовремя и донесли сумки до квартиры.", "Спасибо за заботу, дай Бог здоровья!"],
        "tj": ["Ташаккури зиёд! Ҳамаашро зуд ва бодиққат иҷро карданд.", "Одами хеле хушмуомила ва ғамхор.",
               "Сари вақт омаданд ва халтаҳоро то хона бурданд.", "Барои ғамхорӣ раҳмат, Худо саломатӣ диҳад!"],
        "en": ["Thank you so much! Everything was done quickly and carefully.", "A very kind and attentive person.",
               "Came on time and carried the bags all the way up.", "Thank you for your care, God bless!"]},
    4: {"ru": ["Всё хорошо, немного задержались, но предупредили.", "Спасибо, помогли вовремя."],
        "tj": ["Ҳама хуб, каме дер монданд, вале огоҳ карданд.", "Раҳмат, кумак сари вақт расид."],
        "en": ["All good — a little late, but they let me know.", "Thanks, the help came on time."]},
    3: {"ru": ["Помогли, но забыли купить хлеб.", "Нормально, но пришлось долго ждать."],
        "tj": ["Кумак карданд, вале нон харидро фаромӯш карданд.", "Хуб, вале дер интизор шудам."],
        "en": ["Helped, but forgot the bread.", "Okay, but I had to wait a long time."]},
    2: {"ru": ["Опоздали на час и не предупредили."], "tj": ["Як соат дер монданд ва огоҳ накарданд."],
        "en": ["An hour late with no warning."]},
    1: {"ru": ["Купили не то лекарство, пришлось менять."], "tj": ["Доруи дигар хариданд, иваз кардан лозим шуд."],
        "en": ["Bought the wrong medicine, it had to be exchanged."]},
}
STARS_WEIGHTS = [(5, 60), (4, 27), (3, 8), (2, 3), (1, 2)]
GOOD_TAGS = ["polite", "punctual", "careful", "friendly", "above_and_beyond", "communicative"]
REVIEW_SHARE = 0.75
# (share of the shuffled volunteers up to, energy): the first ~7 % very active,
# up to ~26 % a middle group, the rest now and then.
VOLUNTEER_TIERS = ((0.07, 2.0), (0.26, 0.3), (1.0, 0.1))
FIRST_TASK_BOOST = 2.5
# Volunteers WITH a profile photo carry the activity (the rating's tie-break
# is the photo; the score itself is never changed): (share of the photo group
# up to, energy). Without a photo: little activity, at most NO_PHOTO_MAX_TASKS.
# With no photo anywhere the plain VOLUNTEER_TIERS apply.
PHOTO_TIERS = ((0.25, 2.5), (1.0, 1.0))
NO_PHOTO_ENERGY = 0.08
NO_PHOTO_MAX_TASKS = 1
PHOTO_BOOST_UNTIL = 3      # photo volunteers: the boost lasts for their first 3 requests
REVIEW_SHARE_PHOTO = 0.9
REVIEW_SHARE_NO_PHOTO = 0.45
# Days with any activity (requests, chats, … or just a visit to the site) as a
# share of the account's days since max(WINDOW_START, registration), by how
# much it helped / asked: (min completed or created requests, (low, high)).
ACTIVITY_TIERS = ((4, (0.30, 0.70)), (2, (0.10, 0.30)), (0, (0.03, 0.10)))
# Once profile photos exist (owner 2026-10-19): photo accounts are the most
# active on the site, the rest clearly less — per role, shares of active days.
PHOTO_SHARES = {("volunteer", True): (0.45, 0.70), ("volunteer", False): (0.03, 0.25),
                ("client", True): (0.35, 0.55), ("client", False): (0.03, 0.25)}
PHOTO_CLIENT_ENERGY = 1.0
NO_PHOTO_CLIENT_ENERGY = 0.12
OCTOBER_VISIT_ACTIONS = (3, 7)   # photo volunteers: October visits weigh more (darkest squares)
# Registration of a demo account: a random moment between JOINED_FROM and one
# day before its first demo activity; no activity: up to JOINED_IDLE_UNTIL.
JOINED_FROM = datetime.datetime(2026, 7, 25, 0, 0, tzinfo=ZoneInfo("Asia/Dushanbe"))
JOINED_IDLE_UNTIL = datetime.datetime(2026, 9, 15, 21, 0, tzinfo=ZoneInfo("Asia/Dushanbe"))
RECENT_REQUESTS = 8   # spread over the last RECENT_HOURS, ending pending / active / done
RECENT_HOURS = 96

CANCEL_REASONS = {
    "ru": ["Уже не нужно, помогли соседи.", "Внук приехал, сам всё сделает."],
    "tj": ["Дигар лозим нест, ҳамсояҳо кумак карданд.", "Набераам омад, худаш ҳамаашро мекунад."],
    "en": ["Not needed any more, my neighbours helped.", "My grandson came over and will do it."],
}
CANCEL_SHARE = 0.09


def all_texts():
    """Every free text this command can write (tests check them for chat flags)."""
    out = {"request": [], "chat": [], "review": [], "cancel": []}
    for langs in REQUEST_TEXTS.values():
        for texts in langs.values():
            out["request"] += texts
    for lang in CHAT.values():
        for key, value in lang.items():
            for item in value:
                if isinstance(item, list):  # client_extra: a list of exchanges
                    out["chat"] += [(key, line[1]) for line in item]
                else:
                    out["chat"].append((key, item[1] if isinstance(item, tuple) else item))
    for by_lang in REVIEW_COMMENTS.values():
        for texts in by_lang.values():
            out["review"] += texts
    for texts in CANCEL_REASONS.values():
        out["cancel"] += texts
    return out


# --------------------------------------------------------------------------- #
# participants
# --------------------------------------------------------------------------- #

def demo_accounts():
    """The ONLY accounts this command lets act or receive anything."""
    return (User.objects.filter(email__iendswith=DEMO_EMAIL_SUFFIX, is_active=True,
                                is_superuser=False, is_staff=False, is_curator=False)
            .filter(Q(is_volunteer=True) | Q(is_client=True)).order_by("pk"))


# --------------------------------------------------------------------------- #
# the plan (pure: no database)
# --------------------------------------------------------------------------- #

@dataclass
class Person:
    pk: int
    role: str
    lang: str = "ru"
    energy: float = 0.0
    photo: bool = False             # volunteers: the profile has a photo
    home: tuple | None = None       # (lat, lng)
    address: str = ""
    phone: str = ""


@dataclass
class Planned:
    seq: int
    client: int
    created: datetime.datetime
    help_type: str
    priority: str
    lang: str
    description: str
    address: str
    phone: str
    lat: float
    lng: float
    accept_after: datetime.timedelta
    duration: datetime.timedelta
    cancel_after: datetime.timedelta | None
    review: tuple | None            # (delay, stars, comment, tags)
    rng_seed: str
    target: str = "done"            # the last three days: pending / active / done
    review_roll: float = 0.0
    client_photo: bool = False
    volunteer: int | None = None
    accepted: datetime.datetime | None = None
    completed: datetime.datetime | None = None
    cancelled: datetime.datetime | None = None
    events: list = field(default_factory=list)


def _weighted(rng, pairs):
    return rng.choices([v for v, _ in pairs], weights=[w for _, w in pairs])[0]


def _daytime(moment):
    """A moment at night (22:00–07:30) moves to the next morning."""
    local = moment.astimezone(TZ)
    if datetime.time(7, 30) <= local.time() < datetime.time(22, 0):
        return local
    day = local.date() if local.time() < datetime.time(7, 30) else local.date() + datetime.timedelta(days=1)
    minute = (local.minute * 7 + local.second) % 50
    return datetime.datetime.combine(day, datetime.time(7, 30 + minute // 10 * 5), tzinfo=TZ)


def _finish_time(moment):
    """A completion during the day, not before 10:00 — the work stages that
    lead up to it (on the way, there, working) must not fall at night."""
    local = _daytime(moment)
    if local.time() < datetime.time(10, 0):
        local = local.replace(hour=10, minute=local.minute % 30)
    return local


def build_people(volunteer_ids, client_ids, with_photo=frozenset()):
    """Deterministic personas over the real account ids. ``with_photo``: the
    accounts (volunteers and clients) whose profile has a photo — they carry
    the activity of their role."""
    rng = random.Random(f"{SEED}:people")
    people = {}
    order = list(volunteer_ids)
    rng.shuffle(order)
    quiet = max(1, len(order) * 15 // 100) if len(order) > 4 else 0
    photo = [pk for pk in order if pk in with_photo]
    if photo:
        no_photo = [pk for pk in order if pk not in with_photo]
        for rank, pk in enumerate(photo, start=1):
            share = rank / len(photo)
            people[pk] = Person(pk, "volunteer", energy=next(e for top, e in PHOTO_TIERS if share <= top),
                                photo=True)
        idle = min(quiet, len(no_photo))
        for rank, pk in enumerate(no_photo, start=1):
            people[pk] = Person(pk, "volunteer", energy=0.0 if rank > len(no_photo) - idle else NO_PHOTO_ENERGY)
    else:
        # Three tiers: a few very active volunteers, a middle group, most now
        # and then; ~15 % never helped yet (see VOLUNTEER_TIERS).
        for rank, pk in enumerate(order, start=1):
            share = rank / max(1, len(order))
            energy = 0.0 if rank > len(order) - quiet else next(e for top, e in VOLUNTEER_TIERS if share <= top)
            people[pk] = Person(pk, "volunteer", energy=energy)
    corder = list(client_ids)
    rng.shuffle(corder)
    client_photo = any(pk in with_photo for pk in corder)
    if client_photo:  # photo clients first, so the quiet ones come from the rest
        corder = [pk for pk in corder if pk in with_photo] + [pk for pk in corder if pk not in with_photo]
    cquiet = max(1, len(corder) * 12 // 100) if len(corder) > 4 else 0
    districts = list(DISTRICTS)
    for rank, pk in enumerate(corder, start=1):
        cr = random.Random(f"{SEED}:client:{pk}")
        lang = _weighted(cr, LANG_WEIGHTS)
        district = districts[(pk + rank) % len(districts)]
        (lat, lng), streets = DISTRICTS[district]
        street = cr.choice(streets)
        house, flat = cr.randint(2, 98), cr.randint(1, 72)
        address = {"ru": f"{street[0]}, д. {house}, кв. {flat}",
                   "tj": f"{street[1]}, хонаи {house}, ҳуҷраи {flat}",
                   "en": f"{street[2]}, bld {house}, apt {flat}"}[lang]
        if client_photo:
            energy = (PHOTO_CLIENT_ENERGY if pk in with_photo else
                      0.0 if rank > len(corder) - cquiet else NO_PHOTO_CLIENT_ENERGY)
        else:
            energy = 0.0 if rank > len(corder) - cquiet else rank ** -0.55
        people[pk] = Person(
            pk, "client", lang=lang, energy=energy, photo=pk in with_photo,
            home=(round(lat + cr.uniform(-0.009, 0.009), 6), round(lng + cr.uniform(-0.009, 0.009), 6)),
            address=address,
            # "+992 00 …": the 00 prefix is not assigned to any operator in
            # Tajikistan — a demo number can never ring a real subscriber.
            phone=f"+99200{pk % 1000:03d}{cr.randint(1000, 9999)}",
        )
    return people


def request_times(rng, n, start, now):
    """``n`` creation moments in [start, now], growing towards now: the daily
    weight is (1 + 2·f)³ over the window fraction f, Sundays quieter."""
    first, last = start.astimezone(TZ).date(), now.astimezone(TZ).date()
    days = [first + datetime.timedelta(days=i) for i in range((last - first).days + 1)]
    span = max(1, len(days) - 1)
    weights = [(1 + 2 * i / span) ** 3 * (0.6 if d.weekday() == 6 else 1.0) for i, d in enumerate(days)]
    recent = min(RECENT_REQUESTS, n // 5)
    out = []
    for day in sorted(rng.choices(days, weights=weights, k=n - recent)):
        moment = datetime.datetime.combine(day, datetime.time(rng.randint(8, 19), rng.randrange(0, 60, 5)), tzinfo=TZ)
        if moment > now - datetime.timedelta(minutes=30):
            moment = now - datetime.timedelta(minutes=rng.randint(30, 300))
        out.append(max(moment, start + datetime.timedelta(hours=8)))
    # The last days are never empty: these end in every open state (see build_plan).
    for _ in range(recent):
        moment = _daytime(now - datetime.timedelta(hours=rng.uniform(1, RECENT_HOURS)))
        out.append(min(max(moment, start + datetime.timedelta(hours=8)), now - datetime.timedelta(minutes=40)))
    return sorted(out)


def build_plan(people, n, now, blocked=frozenset()):
    rng = random.Random(f"{SEED}:plan:{n}")
    clients = [p for p in people.values() if p.role == "client" and p.energy > 0]
    if not clients:
        return []
    plan = []
    for seq, created in enumerate(request_times(rng, n, WINDOW_START, now)):
        client = rng.choices(clients, weights=[c.energy for c in clients])[0]
        help_type = _weighted(rng, HELP_TYPE_WEIGHTS)
        roll = rng.random()
        priority = ("emergency" if help_type == "medical" and roll < 0.04 else
                    "high" if roll < (0.25 if help_type == "medical" else 0.08) else "normal")
        band = rng.random()
        accept_after = datetime.timedelta(hours=rng.uniform(0.3, 3) if band < 0.45 else
                                          rng.uniform(3, 18) if band < 0.8 else rng.uniform(18, 46))
        # The last three days end in every open state: still waiting for a
        # volunteer, in progress right now, or done.
        recent = now - created
        target = "done"
        if recent < datetime.timedelta(hours=RECENT_HOURS):
            target = _weighted(rng, [("pending", 40), ("active", 30), ("done", 30)])
        if target == "pending":
            accept_after = recent + datetime.timedelta(hours=rng.uniform(1, 30))  # still waiting now
        elif target == "active":
            accept_after = datetime.timedelta(hours=rng.uniform(0.3, max(0.4, min(8.0, recent.total_seconds() / 3600 - 0.5))))
        cancel_after = None
        if target == "done" and rng.random() < CANCEL_SHARE:
            cancel_after = datetime.timedelta(hours=rng.uniform(1, 16))
            accept_after = max(accept_after, cancel_after + datetime.timedelta(hours=2))
        band = rng.random()
        duration = datetime.timedelta(hours=rng.uniform(1, 3) if band < 0.45 else
                                      rng.uniform(3, 8) if band < 0.8 else rng.uniform(16, 30))
        # The review is drawn now; whether it is written depends on the
        # volunteer (with / without a photo) — decided after the assignment.
        review_roll = rng.random()
        stars = _weighted(rng, STARS_WEIGHTS)
        comment = "" if rng.random() < 0.2 else rng.choice(REVIEW_COMMENTS[stars][client.lang])
        tags = (sorted(rng.sample(GOOD_TAGS, rng.randint(1, 2))) if stars == 5 else
                [rng.choice(GOOD_TAGS)] if stars == 4 and rng.random() < 0.5 else [])
        review = (datetime.timedelta(hours=rng.uniform(0.5, 26)), stars, comment, tags)
        lat, lng = client.home
        plan.append(Planned(
            seq=seq, client=client.pk, created=created, help_type=help_type, priority=priority,
            lang=client.lang, description=rng.choice(REQUEST_TEXTS[help_type][client.lang]),
            address=client.address, phone=client.phone,
            lat=round(lat + rng.uniform(-0.0006, 0.0006), 6), lng=round(lng + rng.uniform(-0.0006, 0.0006), 6),
            accept_after=accept_after, duration=duration, cancel_after=cancel_after, review=review,
            rng_seed=f"{SEED}:req:{seq}", target=target, review_roll=review_roll, client_photo=client.photo,
        ))

    # Assignment in time order: a volunteer takes a request only while free
    # (busy from accept to completion) — the self-accept "one active request" rule.
    volunteers = [p for p in people.values() if p.role == "volunteer" and p.energy > 0 and p.pk not in blocked]
    busy_until = {v.pk: WINDOW_START for v in volunteers}
    taken = Counter()
    any_photo = any(v.photo for v in volunteers)
    heap = [(_daytime(r.created + r.accept_after), r.seq) for r in plan]
    heapq.heapify(heap)
    by_seq = {r.seq: r for r in plan}
    arng = random.Random(f"{SEED}:assign:{n}")
    while heap:
        at, seq = heapq.heappop(heap)
        task = by_seq[seq]
        if task.cancel_after is not None and at >= task.created + task.cancel_after:
            task.cancelled = _daytime(task.created + task.cancel_after)
            if task.cancelled > now:
                task.cancelled = None  # would happen later: still pending now
            continue
        if at > now or not volunteers:
            continue
        able = [v for v in volunteers if v.photo or not any_photo or taken[v.pk] < NO_PHOTO_MAX_TASKS]
        free = [v for v in able if busy_until[v.pk] <= at]
        if not free:
            # Retry once the next volunteer who may still take one is free;
            # nobody left at all → the request stays pending.
            later = [busy_until[v.pk] for v in able if busy_until[v.pk] > at]
            if later:
                heapq.heappush(heap, (_daytime(min(later) + datetime.timedelta(minutes=15)), seq))
            continue
        # A volunteer's first request comes sooner (FIRST_TASK_BOOST): most of
        # them have helped at least once, the very active ones still lead.
        boost_until = lambda v: PHOTO_BOOST_UNTIL if v.photo else 1  # noqa: E731
        chosen = arng.choices(free, weights=[v.energy * (FIRST_TASK_BOOST if taken[v.pk] < boost_until(v) else 1.0)
                                             for v in free])[0]
        taken[chosen.pk] += 1
        task.volunteer, task.accepted = chosen.pk, at
        task.completed = _finish_time(at + task.duration)
        if task.target == "active" and task.completed <= now:
            task.completed = _finish_time(now + datetime.timedelta(hours=arng.uniform(1, 20)))
        busy_until[chosen.pk] = task.completed
    for task in plan:
        if task.volunteer is not None:
            share = REVIEW_SHARE
            if any_photo:
                share = REVIEW_SHARE_PHOTO if people[task.volunteer].photo else REVIEW_SHARE_NO_PHOTO
            if task.review_roll >= share:
                task.review = None
            elif any_photo and people[task.volunteer].photo and task.review[1] < 4:
                # A photo volunteer's reviews are 4–5 stars (they lead the rating).
                rr = random.Random(task.rng_seed + ":review")
                stars = 4 if rr.random() < 0.5 else 5
                comment = rr.choice(REVIEW_COMMENTS[stars][task.lang])
                tags = sorted(rr.sample(GOOD_TAGS, rr.randint(1, 2)))
                task.review = (task.review[0], stars, comment, tags)
        else:
            task.review = None
        task.events = plan_task_events(task)
    return plan


def plan_joined(plan, user_ids, now):
    """{user id: registration moment}: random between JOINED_FROM and one day
    before the account's first demo activity (a client's first request, a
    volunteer's first accept); no activity: up to JOINED_IDLE_UNTIL."""
    first = {}
    for r in plan:
        if r.created <= now:
            first[r.client] = min(first.get(r.client, r.created), r.created)
        if r.volunteer is not None and r.accepted is not None and r.accepted <= now:
            first[r.volunteer] = min(first.get(r.volunteer, r.accepted), r.accepted)
    out = {}
    for uid in sorted(user_ids):
        jr = random.Random(f"{SEED}:joined:{uid}")
        until = first[uid] - datetime.timedelta(days=1) if uid in first else min(JOINED_IDLE_UNTIL, now)
        span = max(0.0, (until - JOINED_FROM).total_seconds())
        day = (JOINED_FROM + datetime.timedelta(seconds=jr.uniform(0, span))).astimezone(TZ)
        # A daytime moment (09:00–21:00 local) that is still not after ``until``.
        moment = day.replace(hour=jr.randint(9, 20), minute=jr.randrange(0, 60, 5), second=0, microsecond=0)
        out[uid] = min(moment, until) if moment > until else moment
    return out


def plan_task_events(task):
    """(time, order, kind, payload) for one request; order breaks ties so a
    completion comes before a review before a cancel before a create …"""
    rng = random.Random(task.rng_seed + ":events")
    ev = [(task.created, 3, "create", None)]
    if task.cancelled is not None:
        ev.append((task.cancelled, 2, "cancel", rng.choice(CANCEL_REASONS[task.lang])))
        return ev
    if task.accepted is None:
        return ev
    acc, done = task.accepted, task.completed
    ev.append((acc, 4, "accept", None))
    texts = CHAT[task.lang]
    mins = datetime.timedelta(minutes=1)
    # Work stages lead up to the completion: on the way, there, working.
    work = datetime.timedelta(minutes=rng.randint(25, 110))
    in_progress = done - work
    arrived = in_progress - rng.randint(4, 12) * mins
    en_route = arrived - rng.randint(15, 40) * mins
    stages = [(t, s) for t, s in ((en_route, "en_route"), (arrived, "arrived"), (in_progress, "in_progress"))
              if t > acc + 3 * mins]
    for t, stage in stages:
        ev.append((t, 5, "stage", stage))
    chat = []
    greet = acc + rng.randint(2, 12) * mins
    chat.append((greet, "v", rng.choice(texts["greet"])))
    chat.append((greet + rng.randint(2, 25) * mins, "c", rng.choice(texts["reply"])))
    extra_key = {"grocery": rng.choice(["grocery", "grocery2"]), "medical": rng.choice(["medical", "medical2"]),
                 "transport": "transport"}.get(task.help_type)
    stage_at = dict((s, t) for t, s in stages)
    if "en_route" in stage_at and rng.random() < 0.7:
        t = stage_at["en_route"]
        (_, v_text), (_, c_text) = texts["en_route"]
        chat += [(t + mins, "v", v_text), (t + rng.randint(2, 6) * mins, "c", c_text)]
    if "arrived" in stage_at and rng.random() < 0.6:
        chat.append((stage_at["arrived"] + mins, "v", rng.choice(texts["arrived"])))
    if extra_key and rng.random() < 0.65:
        t = stage_at.get("in_progress", done - 20 * mins) + rng.randint(3, 10) * mins
        (_, v_text), (_, c_text) = texts[extra_key]
        chat += [(t, "v", v_text), (t + rng.randint(1, 4) * mins, "c", c_text)]
    if task.help_type in ("grocery", "medical") and rng.random() < 0.12:
        (_, v_text), (_, c_text) = texts["money"]
        chat += [(done - 6 * mins, "v", v_text), (done - 4 * mins, "c", c_text)]
    if rng.random() < (0.9 if task.client_photo else 0.55):
        (_, v_text), (_, c_text) = texts["done"]
        chat += [(done - 3 * mins, "v", v_text), (done - 2 * mins, "c", c_text)]
    if task.client_photo:
        # A client with a photo writes more: one or two extra exchanges.
        for k, exchange in enumerate(rng.sample(texts["client_extra"], rng.randint(1, 2))):
            t = acc + (done - acc) * (0.3 + 0.3 * k) + rng.randint(0, 5) * mins
            (_, c_text), (_, v_text) = exchange
            chat += [(t, "c", c_text), (t + rng.randint(1, 4) * mins, "v", v_text)]
    for i, (t, who, text) in enumerate(sorted(c for c in chat if acc < c[0] < done)):
        ev.append((t, 6, "chat", (who, text, i)))
    ev.append((done, 0, "complete", None))
    if task.review:
        ev.append((done + task.review[0], 1, "review", None))
    return ev


def plan_visits(event_days, windows, bands, today, heavy=frozenset()):
    """{user: {day: actions}} — extra "visited the site" days so the share of
    active days lands in the account's band. ``event_days``: {user: set of days
    that already have activity}; ``windows``: {user: first day}; ``bands``:
    {user: (low, high)} share of active days; ``heavy``: users whose visits in
    the current month weigh more (``OCTOBER_VISIT_ACTIONS``). Deterministic
    per user; more visits towards ``today`` (same growth as the requests); a
    day that already has activity is never chosen."""
    out = {}
    for uid in sorted(windows):
        first = windows[uid]
        if first > today:
            continue
        days = [first + datetime.timedelta(days=i) for i in range((today - first).days + 1)]
        low, high = bands[uid]
        vr = random.Random(f"{SEED}:visits:{uid}")
        target = round(vr.uniform(low, high) * len(days))
        have = {d for d in event_days.get(uid, ()) if first <= d <= today}
        free = [d for d in days if d not in have]
        need = min(max(0, target - len(have)), len(free))
        if not need:
            continue
        span = max(1, len(days) - 1)
        weights = [(1 + 2 * (d - first).days / span) ** 3 * (0.6 if d.weekday() == 6 else 1.0) for d in free]
        chosen = set()
        while len(chosen) < need:  # weighted sampling without replacement
            pick = vr.choices(free, weights=weights)[0]
            chosen.add(pick)
        out[uid] = {d: (vr.randint(*OCTOBER_VISIT_ACTIONS) if uid in heavy and (d.year, d.month) == (today.year, today.month)
                        else vr.randint(1, 3)) for d in sorted(chosen)}
    return out


def activity_bands(users, photo, done):
    """{user: (low, high)}: ``PHOTO_SHARES`` for a role once any account of that
    role has a photo, else ``ACTIVITY_TIERS`` by ``done``. ``users``: {id: is_volunteer}."""
    roles_with_photo = {("volunteer" if users[uid] else "client") for uid in photo if uid in users}
    out = {}
    for uid, is_volunteer in users.items():
        role = "volunteer" if is_volunteer else "client"
        if role in roles_with_photo:
            out[uid] = PHOTO_SHARES[(role, uid in photo)]
        else:
            out[uid] = next(band for threshold, band in ACTIVITY_TIERS if done.get(uid, 0) >= threshold)
    return out


def demo_photo_ids(user_ids):
    from accounts.models import Profile
    return frozenset(Profile.objects.filter(user_id__in=user_ids).exclude(image="").exclude(image__isnull=True)
                     .values_list("user_id", flat=True))


# --------------------------------------------------------------------------- #
# quiet run: in-app rows for demo accounts only, nothing leaves the server
# --------------------------------------------------------------------------- #

class _Clock:
    def __init__(self, real_now):
        self.real_now = real_now
        self.at = None

    def __call__(self):
        return self.at if self.at is not None else self.real_now()


class ExternalSendAttempt(Exception):
    pass


@contextmanager
def quiet(allowed_ids, stats, joined=None):
    """Swap the delivery layer: ``notify()`` writes the in-app rows for demo
    accounts only (same keys / params / dedupe as the real one), WebSocket
    publishing is a no-op, and every road out of the server (e-mail, Telegram,
    Celery, HTTP) is a tripwire that records the attempt — the run then rolls
    back. ``joined`` ({user id: registration}, filled later is fine): nobody
    gets a notification dated before their registration."""
    from myapp import notifications
    from myapp.models import Notification
    from myapp.realtime import publish
    from myapp.services import notify as notify_service
    from myapp.services import notify_texts

    # Import everything the run calls first, so no module binds the
    # replacement at import time (the leftover scan below is the backstop).
    import myapp.forms  # noqa: F401
    import myapp.services.achievements  # noqa: F401
    import myapp.services.ai.actions  # noqa: F401
    import myapp.services.cancellation  # noqa: F401
    import myapp.services.chat  # noqa: F401
    import myapp.services.feedback  # noqa: F401
    import myapp.services.photo_reports  # noqa: F401
    import myapp.services.presence  # noqa: F401
    import myapp.services.task_actions  # noqa: F401
    import myapp.services.tracking  # noqa: F401

    original_notify = notify_service.notify
    allowed = frozenset(allowed_ids)

    def inapp_only(recipients, type, *, params=None, link="", dedupe_key=None, variant=None, **_ignored):
        if type not in notify_service.NOTIFICATION_TYPES:
            raise ValueError(f"unknown notification type: {type}")
        users = notify_service._resolve(recipients)
        moment = timezone.now()
        demo = [u for u in users if u.pk in allowed]
        keep = [u for u in demo if not joined or joined.get(u.pk, moment) <= moment]
        stats["notify_dropped"] += len(users) - len(demo)
        stats["notify_before_joined"] += len(demo) - len(keep)
        if keep:
            stored = notify_service.safe_params(params)
            title_key, body_key = notify_texts.inapp_keys(type, variant)
            already = set()
            if dedupe_key:
                already = set(Notification.objects.filter(recipient_id__in=[u.pk for u in keep], dedupe_key=dedupe_key)
                              .values_list("recipient_id", flat=True))
            rows = [Notification(recipient_id=u.pk, type=type, title_key=title_key, body_key=body_key,
                                 params=stored, link=link or "", dedupe_key=dedupe_key)
                    for u in keep if u.pk not in already]
            Notification.objects.bulk_create(rows)
            stats["notify_rows"] += len(rows)
        return len(users)  # "reached": the zero-recipient safety nets stay quiet

    def tripwire(name):
        def _blocked(*args, **kwargs):
            stats["external_attempts"].append(name)
            raise ExternalSendAttempt(name)
        return _blocked

    def ws_noop(*args, **kwargs):
        stats["ws_suppressed"] += 1
        return False

    with ExitStack() as stack:
        for modname, module in list(sys.modules.items()):
            if (modname.startswith("myapp.") or modname.startswith("accounts.")) and \
                    getattr(module, "notify", None) is original_notify:
                stack.enter_context(mock.patch.object(module, "notify", inapp_only))
        stack.enter_context(mock.patch.object(publish, "send_to_group", ws_noop))
        stack.enter_context(mock.patch.object(notifications, "_dispatch", tripwire("notifications._dispatch")))
        stack.enter_context(mock.patch.object(notifications, "queue_email", tripwire("notifications.queue_email")))
        stack.enter_context(mock.patch.object(notifications, "send_telegram_message",
                                              tripwire("notifications.send_telegram_message")))
        if hasattr(notifications, "notify_users"):
            stack.enter_context(mock.patch.object(notifications, "notify_users", tripwire("notifications.notify_users")))
        stack.enter_context(mock.patch("django.core.mail.message.EmailMessage.send", tripwire("email")))
        stack.enter_context(mock.patch("requests.sessions.Session.request", tripwire("http(requests)")))
        try:
            import httpx
            stack.enter_context(mock.patch.object(httpx.Client, "send", tripwire("http(httpx)")))
            stack.enter_context(mock.patch.object(httpx.AsyncClient, "send", tripwire("http(httpx)")))
        except ImportError:  # pragma: no cover
            pass
        try:
            from celery.app.task import Task
            stack.enter_context(mock.patch.object(Task, "apply_async", tripwire("celery")))
        except ImportError:  # pragma: no cover
            pass
        try:
            yield
        finally:
            # A module first imported during the run may have bound the
            # replacement by name: give it the real notify() back.
            for module in list(sys.modules.values()):
                if getattr(module, "notify", None) is inapp_only:
                    module.notify = original_notify


@contextmanager
def info_logs_off():
    loggers = [logging.getLogger(name) for name in ("myapp", "accounts")]
    levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for lg, level in zip(loggers, levels):
            lg.setLevel(level)


# --------------------------------------------------------------------------- #
# whole-database snapshot
# --------------------------------------------------------------------------- #

def _models():
    out = []
    for model in apps.get_models(include_auto_created=True):
        meta = model._meta
        if meta.proxy or not meta.managed:
            continue
        out.append(model)
    return out


def _label(model):
    return model._meta.label


def snapshot():
    """{label: {pk: row tuple}} for every table (the DB is small)."""
    snap = {}
    for model in _models():
        names = [f.attname for f in model._meta.concrete_fields]
        rows = model._base_manager.order_by("pk").values_list(*names)
        snap[_label(model)] = {"fields": names, "rows": {row[names.index(model._meta.pk.attname)]: row for row in rows}}
    return snap


def compare(before, after, demo_ids):
    """(problems, created) — ``created`` = {label: [new pks]} in MANIFEST_MODELS."""
    problems, created = [], {}
    for label, data in after.items():
        old = before.get(label, {"rows": {}, "fields": data["fields"]})
        names = data["fields"]
        missing = set(old["rows"]) - set(data["rows"])
        if missing:
            problems.append(f"{label}: {len(missing)} row(s) disappeared (ids {sorted(missing)[:10]})")
        new = sorted(set(data["rows"]) - set(old["rows"]))
        if new:
            if label in MANIFEST_MODELS:
                created[label] = new
            else:
                problems.append(f"{label}: {len(new)} unexpected new row(s) (ids {new[:10]})")
        for pk, row in old["rows"].items():
            now_row = data["rows"].get(pk)
            if now_row is None or now_row == row:
                continue
            changed = {n for n, a, b in zip(names, row, now_row) if a != b}
            if label == "accounts.Profile":
                user_id = row[names.index("user_id")]
                if user_id in demo_ids and changed <= ALLOWED_PROFILE_CHANGES:
                    continue
            if label == USERS_LABEL and pk in demo_ids and changed <= ALLOWED_USER_CHANGES:
                continue
            problems.append(f"{label} #{pk}: existing row changed ({', '.join(sorted(changed))})")
    return problems, created


def compare_purge(before, after, listed, demo_ids):
    """A purge may only remove the listed rows (+ UI-state cascades of demo
    requests) and touch the derived Profile fields of demo accounts."""
    problems = []
    for label, data in after.items():
        old = before.get(label, {"rows": {}, "fields": data["fields"]})
        names = data["fields"]
        new = set(data["rows"]) - set(old["rows"])
        if new:
            problems.append(f"{label}: {len(new)} new row(s) during a purge")
        gone = set(old["rows"]) - set(data["rows"])
        extra = gone - listed.get(label, set())
        if extra and label not in PURGE_EXTRA_CASCADE:
            problems.append(f"{label}: unlisted row(s) deleted {sorted(extra)[:10]}")
        for pk, row in old["rows"].items():
            now_row = data["rows"].get(pk)
            if now_row is None or now_row == row:
                continue
            changed = {n for n, a, b in zip(names, row, now_row) if a != b}
            if label == "accounts.Profile" and row[names.index("user_id")] in demo_ids and \
                    changed <= ALLOWED_PROFILE_CHANGES:
                continue
            if label == USERS_LABEL and pk in demo_ids and changed <= ALLOWED_USER_CHANGES:
                continue
            problems.append(f"{label} #{pk}: existing row changed ({', '.join(sorted(changed))})")
    return problems


def check_new_rows(created, demo_ids, window_start, now):
    """Every new row points only at demo accounts / new requests; its dates lie
    in [window_start, now]."""
    problems = []
    new_tasks = set(created.get("myapp.HelpRequest", []))
    for label, pks in created.items():
        model = apps.get_model(label)
        for obj in model._base_manager.filter(pk__in=pks):
            for f in model._meta.concrete_fields:
                if not f.is_relation:
                    if isinstance(f, models.DateTimeField):
                        value = getattr(obj, f.attname)
                        if value is not None and not (window_start <= value <= now):
                            problems.append(f"{label} #{obj.pk}.{f.name} = {value.isoformat()} outside the window")
                    elif isinstance(f, models.DateField):
                        value = getattr(obj, f.attname)
                        if value is not None and not (window_start.date() <= value <= now.astimezone(TZ).date()):
                            problems.append(f"{label} #{obj.pk}.{f.name} = {value} outside the window")
                    continue
                value = getattr(obj, f.attname)
                if value is None:
                    continue
                if f.related_model is User and value not in demo_ids:
                    problems.append(f"{label} #{obj.pk}.{f.name} points at a protected account #{value}")
                elif f.related_model._meta.label == "myapp.HelpRequest" and value not in new_tasks:
                    problems.append(f"{label} #{obj.pk}.{f.name} points at an existing request #{value}")
            if hasattr(obj, "is_demo") and not obj.is_demo:
                problems.append(f"{label} #{obj.pk}: is_demo is False")
    return problems


def git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=settings.BASE_DIR, capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


class _DryRun(Exception):
    pass


# --------------------------------------------------------------------------- #
# the command
# --------------------------------------------------------------------------- #

class Command(BaseCommand):
    help = ("Наполняет историей ТОЛЬКО импортированные демо-аккаунты (@khayrkhoh.invalid): заявки, отклики, "
            "чаты, отзывы, уведомления, достижения, активность — с 1 августа 2026. Новых аккаунтов не создаёт.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Run inside a transaction, print, roll back.")
        parser.add_argument("--i-confirm-demo-accounts", dest="confirm", action="store_true",
                            help="Required for any write: the @khayrkhoh.invalid accounts are fictional demo accounts.")
        parser.add_argument("--reset", action="store_true", help="Purge what the manifest lists, then seed again.")
        parser.add_argument("--purge", action="store_true", help="Only delete what the manifest lists.")
        parser.add_argument("--requests", type=int, default=DEFAULT_REQUESTS,
                            help=f"How many help requests ({MIN_REQUESTS}–{MAX_REQUESTS}).")
        parser.add_argument("--manifest", default=None, help=f"Manifest path (default <BASE_DIR>/{MANIFEST_NAME}).")
        parser.add_argument("--per-user", action="store_true", help="Print the per-account table.")
        parser.add_argument("--fill-activity", action="store_true",
                            help="Top up the site-visit days (UserDailyActivity) of an existing run; ids go to the manifest.")
        parser.add_argument("--as-of", default=None, help=__import__("argparse").SUPPRESS)  # tests only

    # ----------------------------------------------------------------- entry

    def handle(self, *args, **options):
        self.options = options
        self.manifest_path = Path(options["manifest"] or (Path(settings.BASE_DIR) / MANIFEST_NAME))
        n = options["requests"]
        if not MIN_REQUESTS <= n <= MAX_REQUESTS:
            raise CommandError(f"--requests must be between {MIN_REQUESTS} and {MAX_REQUESTS}.")
        if options["reset"] and options["purge"]:
            raise CommandError("--reset and --purge are exclusive.")
        dry = options["dry_run"]
        if not dry:
            if not options["confirm"]:
                raise CommandError(f"Refusing to write without {CONFIRM_OPTION}.")
            if not getattr(settings, "DEMO_MODE", False):
                raise CommandError("Refusing to write without DEMO_MODE=True (DJANGO_DEMO_MODE): the site must show "
                                   "the demo-stand banner while demo activity exists.")
        self.real_now = (datetime.datetime.fromisoformat(options["as_of"]) if options["as_of"] else timezone.now())
        if options["fill_activity"]:
            if options["reset"] or options["purge"]:
                raise CommandError("--fill-activity works on an existing run alone (no --reset / --purge).")
            self._fill(dry)
            return
        if options["reset"] and dry and self.manifest_path.exists():
            # The whole --reset in one transaction — purge, then the fresh run
            # with its full report — and everything rolled back.
            try:
                with transaction.atomic():
                    if connection.vendor == "postgresql":
                        with connection.cursor() as cur:
                            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                    self._purge(False, finalize=False)
                    self._seed(n, True)
                    raise _DryRun()
            except _DryRun:
                pass
            self.stdout.write(self.style.WARNING("DRY RUN of --reset: the purge and the fresh run above were both "
                                                 "rolled back — nothing written, the manifest is unchanged."))
            return
        if options["purge"] or options["reset"]:
            self._purge(dry)
            if options["purge"]:
                return
        elif self.manifest_path.exists():
            self.stdout.write(f"Manifest {self.manifest_path} exists — already seeded, nothing to do "
                              f"(--reset to recreate, --purge to delete).")
            return
        self._seed(n, dry)

    # ------------------------------------------------------------------ seed

    def _seed(self, n, dry):
        from myapp.models import HelpRequest

        demo = list(demo_accounts())
        self.demo_ids = {u.pk for u in demo}
        if not demo:
            raise CommandError(f"No active volunteer/client accounts with an e-mail ending in {DEMO_EMAIL_SUFFIX}.")
        stray = HelpRequest.objects.filter(is_demo=True, client_id__in=self.demo_ids)
        if stray.exists():
            raise CommandError(f"Demo requests of these accounts exist without a manifest ({stray.count()}) — "
                               "refusing to guess; restore the manifest or clean up by hand.")
        # Parties of existing requests stay out of it entirely.
        parties = set(HelpRequest.objects.filter(is_demo=False).values_list("client_id", flat=True)) | \
            set(HelpRequest.objects.filter(is_demo=False, volunteer__isnull=False).values_list("volunteer_id", flat=True))
        excluded = sorted(self.demo_ids & parties)
        # From here on "demo" = the participants: a left-out account gets
        # nothing either (no notification, no change).
        self.demo_ids -= parties
        volunteers = [u for u in demo if u.pk in self.demo_ids and u.is_volunteer]
        clients = [u for u in demo if u.pk in self.demo_ids and u.is_client]
        users = {u.pk: u for u in demo if u.pk in self.demo_ids}
        self.stats = {"notify_rows": 0, "notify_dropped": 0, "notify_before_joined": 0, "ws_suppressed": 0, "external_attempts": [],
                      "refused": Counter()}
        user_count_before = User.objects.count()

        try:
            self.joined = {}
            outer = connection.in_atomic_block  # the --reset dry run holds one already
            with quiet(self.demo_ids, self.stats, self.joined), info_logs_off(), transaction.atomic():
                if connection.vendor == "postgresql" and not outer:
                    with connection.cursor() as cur:
                        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                before = snapshot()
                profiles_before = self._profile_fields()
                from accounts.models import Profile
                with_photo = demo_photo_ids([u.pk for u in (*volunteers, *clients)])
                people = build_people([u.pk for u in volunteers], [u.pk for u in clients], with_photo)
                plan = build_plan(people, n, self.real_now)
                # Registration dates before the first demo activity (old values -> manifest).
                joined_before = dict(User.objects.filter(pk__in=self.demo_ids).values_list("pk", "date_joined"))
                self.joined.update(plan_joined(plan, self.demo_ids, self.real_now))
                for uid, moment in self.joined.items():
                    User.objects.filter(pk=uid).update(date_joined=moment)
                fresh = {u.pk: u for u in User.objects.filter(pk__in=self.demo_ids)}
                users = {pk: fresh[pk] for pk in users}
                volunteers = [fresh[u.pk] for u in volunteers]
                self.with_photo = with_photo
                self.clock = _Clock(lambda: self.real_now)
                with mock.patch("django.utils.timezone.now", self.clock):
                    rows = self._replay(plan, users)
                    self.clock.at = None  # "now" from here on = the run's now
                    self._finish(rows, users, volunteers)
                after = snapshot()
                problems, created = compare(before, after, self.demo_ids)
                problems += check_new_rows(created, self.demo_ids, WINDOW_START, self.real_now)
                if self.stats["external_attempts"]:
                    problems.append(f"external send attempted: {sorted(set(self.stats['external_attempts']))}")
                if User.objects.count() != user_count_before:
                    problems.append("the number of accounts changed")
                if problems:
                    raise CommandError("Rolled back — the post-run check failed:\n  " + "\n  ".join(problems[:40]))
                problems = self._rating_check(users)
                if problems:
                    raise CommandError("Rolled back — the rating check failed:\n  " + "\n  ".join(problems))
                profiles_after = self._profile_fields()
                manifest = self._manifest(created, profiles_before, profiles_after, n, excluded)
                manifest["date_joined"] = {str(uid): {"before": joined_before[uid].isoformat(),
                                                      "after": self.joined[uid].isoformat()}
                                           for uid in sorted(self.joined)}
                self._report(created, plan, users, excluded, user_count_before)
                if dry:
                    raise _DryRun()
                tmp = self.manifest_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
        except _DryRun:
            self.stdout.write(self.style.WARNING("DRY RUN — everything above was rolled back, nothing written."))
            return
        except Exception:
            tmp = self.manifest_path.with_suffix(".json.tmp")
            if tmp.exists() and not self.manifest_path.exists():
                tmp.unlink()
            raise
        os.replace(self.manifest_path.with_suffix(".json.tmp"), self.manifest_path)
        from accounts.models import clear_rating_cache
        from myapp.services import activity
        clear_rating_cache()  # a visitor may have rebuilt it from pre-commit data
        today = self.real_now.astimezone(TZ).date()
        cache.delete_many([activity.cache_key(uid, today) for uid in self.demo_ids])  # profile calendars
        self.stdout.write(self.style.SUCCESS(f"Committed. Manifest: {self.manifest_path}"))
        self.stdout.write(f"Accounts before {user_count_before}, after {User.objects.count()}.")

    def _profile_fields(self):
        from accounts.models import Profile

        fields = ["rating", "review_avg", "review_count", "availability_status"]
        return {row["user_id"]: {k: (str(row[k]) if row[k] is not None else None) for k in fields}
                for row in Profile.objects.filter(user_id__in=self.demo_ids).values("user_id", *fields)}

    def _replay(self, plan, users):
        from myapp.forms import HelpRequestForm
        from myapp.models import ChatMessage, HelpRequest, TaskReview
        from myapp.services import cancellation, chat, feedback, photo_reports, task_actions
        from myapp.services.ai import actions

        events = sorted(((t, order, r.seq, kind, payload) for r in plan for t, order, kind, payload in r.events),
                        key=lambda e: (e[0], e[1], e[2]))
        by_seq = {r.seq: r for r in plan}
        rows = {}
        refused = self.stats["refused"]
        for at, _order, seq, kind, payload in events:
            if at > self.real_now:
                continue
            r = by_seq[seq]
            self.clock.at = at
            if kind == "create":
                form = HelpRequestForm(data={
                    "help_type": r.help_type, "priority": r.priority, "description": r.description,
                    "address": r.address, "phone": r.phone, "latitude": r.lat, "longitude": r.lng,
                })
                if not form.is_valid():
                    raise CommandError(f"request form invalid: {form.errors.as_json()}")
                task = task_actions.create_request(users[r.client], form)
                HelpRequest.objects.filter(pk=task.pk).update(is_demo=True)
                task.is_demo = True
                rows[seq] = task
                continue
            task = rows.get(seq)
            if task is None:
                continue
            task.refresh_from_db()
            client = users[r.client]
            volunteer = users.get(r.volunteer)
            if kind == "cancel":
                if not cancellation.cancel_request(task, client, payload).ok:
                    refused["cancel"] += 1
            elif kind == "accept":
                if not task_actions.accept(volunteer, task).ok:
                    refused["accept"] += 1
            elif kind == "stage":
                if task.status == "active" and task.volunteer_id == volunteer.pk:
                    order = HelpRequest.WORK_STAGE_ORDER
                    if order.index(payload) > order.index(task.work_stage):
                        actions.advance_work_stage_for(volunteer, task, payload)
            elif kind == "chat":
                who, text, i = payload
                sender = volunteer if who == "v" else client
                if not chat.send_message(sender, task, text=text, client_msg_id=f"demo-{task.pk}-{i}").ok:
                    refused["chat"] += 1
            elif kind == "complete":
                if task.status == "active" and task.volunteer_id == volunteer.pk:
                    last = ChatMessage.objects.filter(task=task).order_by("-id").values_list("id", flat=True).first()
                    if task_actions.complete(volunteer, task).ok:
                        # No photo report on demo requests: the reward is released
                        # through the same path an approval uses (demo requests only).
                        photo_reports.credit_task(task)
                        task.refresh_from_db()
                        if last:
                            newest = ChatMessage.objects.filter(task=task).order_by("-id").values_list("id", flat=True).first()
                            chat.mark_read(volunteer, task, newest)
                            chat.mark_read(client, task, newest)
                    else:
                        refused["complete"] += 1
            elif kind == "review":
                if task.status == "completed" and not TaskReview.objects.filter(task=task).exists():
                    _delay, stars, comment, tags = r.review
                    if not feedback.submit_review(client, task, stars=stars, comment=comment, tags=tags).ok:
                        refused["review"] += 1
        # Open chats: the volunteer has read what was written so far.
        self.clock.at = None
        for task in rows.values():
            task.refresh_from_db()
            if task.status == "active":
                newest = ChatMessage.objects.filter(task=task).order_by("-id").values_list("id", flat=True).first()
                if newest:
                    chat.mark_read(users[task.volunteer_id], task, newest)
        return rows

    def _finish(self, rows, users, volunteers):
        """At the real now: cron-sweep guards, read notifications, activity
        days, badges recalculated from the data, availability, rating cache."""
        from accounts.models import clear_rating_cache
        from myapp.models import HelpRequest, Notification, UserDailyActivity
        from myapp.services import achievements, presence

        ids = [t.pk for t in rows.values()]
        # The overdue / stale sweeps must never alert staff about a demo request.
        HelpRequest.objects.filter(pk__in=ids, status="active").update(alarm_sent=True)
        HelpRequest.objects.filter(pk__in=ids, status="pending").update(stale_alert_sent=True)

        # Older notifications have been read (a moment after they arrived).
        rng = random.Random(f"{SEED}:read")
        cutoff = self.real_now - datetime.timedelta(days=2)
        for n in Notification.objects.filter(recipient_id__in=self.demo_ids, read_at__isnull=True,
                                             created_at__lt=cutoff, created_at__gte=WINDOW_START).order_by("pk"):
            if rng.random() < 0.9:
                read = min(n.created_at + datetime.timedelta(minutes=rng.randint(5, 1200)), self.real_now)
                Notification.objects.filter(pk=n.pk).update(read_at=read)

        self.clock.at = None
        for user in volunteers:
            achievements.evaluate(user, send_notifications=False)  # from the data, silently
            presence.recompute(user)
        self._daily_activity(ids)
        clear_rating_cache()

    def _daily_activity(self, task_ids):
        """One ``UserDailyActivity`` row per demo account per local day it acted
        (requests, accepts, stages ≈ chats, completions, reviews, chat lines),
        plus a few page views; never on a day that already has a row."""
        from myapp.models import ChatMessage, HelpRequest, TaskReview, UserDailyActivity

        per_day = Counter()

        def add(user_id, moment, k=1):
            if user_id in self.demo_ids and moment is not None:
                per_day[(user_id, moment.astimezone(TZ).date())] += k

        for t in HelpRequest.objects.filter(pk__in=task_ids):
            add(t.client_id, t.created_at)
            add(t.volunteer_id, t.accepted_at)
            add(t.volunteer_id, t.completed_at, 2)
            if t.cancelled_at:
                add(t.client_id, t.cancelled_at)
        for review in TaskReview.objects.filter(task_id__in=task_ids):
            add(review.client_id, review.created_at)
        for m in ChatMessage.objects.filter(task_id__in=task_ids, sender__isnull=False):
            add(m.sender_id, m.created_at)
        existing = set(UserDailyActivity.objects.filter(user_id__in=self.demo_ids).values_list("user_id", "date"))
        rng = random.Random(f"{SEED}:activity")
        today = self.real_now.astimezone(TZ).date()
        create = []
        for (user_id, day), count in sorted(per_day.items()):
            if (user_id, day) in existing or day < WINDOW_START.date() or day > today:
                continue
            create.append(UserDailyActivity(user_id=user_id, date=day, actions_count=count + rng.randint(1, 4),
                                            is_demo=True, source="demo"))
        UserDailyActivity.objects.bulk_create(create, batch_size=500)
        self._visits(task_ids)

    def _visits(self, task_ids):
        """Site visits on days without other activity (``plan_visits``), from
        max(1 Aug, registration) to today. Returns the rows created."""
        from myapp.models import HelpRequest, UserDailyActivity

        today = self.real_now.astimezone(TZ).date()
        joined = dict(User.objects.filter(pk__in=self.demo_ids).values_list("pk", "date_joined"))
        windows = {uid: max(WINDOW_START.date(), joined[uid].astimezone(TZ).date()) for uid in self.demo_ids}
        event_days = defaultdict(set)
        for uid, day in UserDailyActivity.objects.filter(user_id__in=self.demo_ids).values_list("user_id", "date"):
            event_days[uid].add(day)
        tasks = HelpRequest.objects.filter(pk__in=task_ids)
        done = Counter(tasks.filter(status="completed").values_list("volunteer_id", flat=True))
        done.update(tasks.values_list("client_id", flat=True))
        roles = dict(User.objects.filter(pk__in=self.demo_ids).values_list("pk", "is_volunteer"))
        photo = demo_photo_ids(self.demo_ids)
        bands = activity_bands(roles, photo, done)
        heavy = frozenset(uid for uid in photo if roles.get(uid))
        planned = plan_visits(event_days, windows, bands, today, heavy)
        # Photo volunteers: the darkest squares are in the current month — if an
        # earlier day still has the most actions, one of this month's new visit
        # days gets one more than that.
        existing = defaultdict(dict)
        for uid, day, n in UserDailyActivity.objects.filter(user_id__in=heavy).values_list("user_id", "date", "actions_count"):
            existing[uid][day] = n
        for uid in heavy:
            days = {**existing[uid], **planned.get(uid, {})}
            if not days:
                continue
            top = max(days.values())
            this_month = {d: n for d, n in days.items() if (d.year, d.month) == (today.year, today.month)}
            if this_month and max(this_month.values()) < top:
                new_days = [d for d in planned.get(uid, {}) if (d.year, d.month) == (today.year, today.month)]
                if new_days:
                    planned[uid][max(new_days, key=lambda d: planned[uid][d])] = top + 1
        rows = [UserDailyActivity(user_id=uid, date=day, actions_count=n, is_demo=True, source="demo")
                for uid, days in planned.items() for day, n in days.items()]
        UserDailyActivity.objects.bulk_create(rows, batch_size=500)
        return rows

    def _rating_check(self, users):
        """With photo volunteers: no volunteer without a photo (demo or not)
        ranks above a demo volunteer with a photo — the formula is untouched,
        the plan has to get there."""
        from myapp.services import rating

        photo_vol = {uid for uid in self.with_photo if users.get(uid) and users[uid].is_volunteer}
        if not photo_vol:
            return []
        ranked = list(rating.ranked_queryset())
        last_photo = max(i for i, u in enumerate(ranked) if u.pk in photo_vol)
        intruders = [f"#{u.pk} (place {i + 1}, score {u.score}, no photo)"
                     for i, u in enumerate(ranked[:last_photo + 1]) if not u.has_photo]
        return [f"volunteers without a photo above a demo volunteer with one: {', '.join(intruders)}"] if intruders else []

    def _share_report(self, task_ids):
        """Share of active days per account since max(1 Aug, registration), by tier."""
        from myapp.models import HelpRequest, UserDailyActivity

        today = self.real_now.astimezone(TZ).date()
        joined = dict(User.objects.filter(pk__in=self.demo_ids).values_list("pk", "date_joined"))
        roles = dict(User.objects.filter(pk__in=self.demo_ids).values_list("pk", "is_volunteer"))
        tasks = HelpRequest.objects.filter(pk__in=task_ids)
        done = Counter(tasks.filter(status="completed").values_list("volunteer_id", flat=True))
        done.update(tasks.values_list("client_id", flat=True))
        days = defaultdict(set)
        for uid, day in UserDailyActivity.objects.filter(user_id__in=self.demo_ids).values_list("user_id", "date"):
            days[uid].add(day)
        photo = demo_photo_ids(self.demo_ids)
        bands = activity_bands(roles, photo, done)
        labels = {band: f"{'with' if photo_ else 'without'} a photo" for (role, photo_), band in PHOTO_SHARES.items()}
        groups = defaultdict(list)
        for uid in self.demo_ids:
            first = max(WINDOW_START.date(), joined[uid].astimezone(TZ).date())
            total = (today - first).days + 1
            share = len({d for d in days[uid] if first <= d <= today}) / total if total > 0 else 0
            band = bands[uid]
            groups[("volunteer" if roles[uid] else "client", band)].append(share)
        self.shares = {}
        for (role, band), values in sorted(groups.items(), key=lambda kv: (kv[0][0], -kv[0][1][0])):
            values.sort()
            who = f"{role}s {labels[band]}" if photo and band in labels else f"{role}s"
            self.stdout.write(f"  active-day share, {who} (band {int(band[0]*100)}–{int(band[1]*100)} %): "
                              f"{len(values)} accounts, min {values[0]:.0%}, median {values[len(values)//2]:.0%}, "
                              f"max {values[-1]:.0%}")
            self.shares[(role, band)] = values

    def _fill(self, dry):
        """--fill-activity: add the site-visit days of an existing run."""
        from accounts.models import clear_rating_cache
        from myapp.services import activity

        if not self.manifest_path.exists():
            raise CommandError(f"No manifest at {self.manifest_path} — run the seed first.")
        manifest = json.loads(self.manifest_path.read_text())
        if manifest.get("command") != "seed_demo_activity" or manifest.get("version") != MANIFEST_VERSION:
            raise CommandError("Not a seed_demo_activity manifest.")
        self.demo_ids = set(manifest["demo_user_ids"]) & set(demo_accounts().values_list("pk", flat=True))
        task_ids = manifest["rows"].get("myapp.HelpRequest", [])
        stats = {"notify_rows": 0, "notify_dropped": 0, "notify_before_joined": 0, "ws_suppressed": 0,
                 "external_attempts": [], "refused": Counter()}
        user_count = User.objects.count()
        tmp = self.manifest_path.with_suffix(".json.tmp")
        try:
            with quiet(self.demo_ids, stats), info_logs_off(), transaction.atomic():
                if connection.vendor == "postgresql":
                    with connection.cursor() as cur:
                        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                before = snapshot()
                rows = self._visits(task_ids)
                after = snapshot()
                problems, created = compare(before, after, self.demo_ids)
                if set(created) - {"myapp.UserDailyActivity"}:
                    problems.append(f"unexpected new rows: {sorted(set(created) - {'myapp.UserDailyActivity'})}")
                problems += check_new_rows(created, self.demo_ids, WINDOW_START, self.real_now)
                joined = dict(User.objects.filter(pk__in=self.demo_ids).values_list("pk", "date_joined"))
                early = [r.pk for r in rows if r.date < joined[r.user_id].astimezone(TZ).date()]
                if early:
                    problems.append(f"visit days before registration: {early[:10]}")
                if User.objects.count() != user_count:
                    problems.append("the number of accounts changed")
                if stats["external_attempts"]:
                    problems.append(f"external send attempted: {stats['external_attempts']}")
                if problems:
                    raise CommandError("Rolled back — the post-fill check failed:\n  " + "\n  ".join(problems[:40]))
                new_ids = created.get("myapp.UserDailyActivity", [])
                self.stdout.write(f"--fill-activity as of {self.real_now.astimezone(TZ):%Y-%m-%d %H:%M}: "
                                  f"{len(new_ids)} visit day(s) for {len({r.user_id for r in rows})} demo account(s); "
                                  f"accounts {user_count} → {User.objects.count()}")
                self._share_report(task_ids)
                if dry:
                    raise _DryRun()
                manifest["rows"]["myapp.UserDailyActivity"] = sorted(
                    set(manifest["rows"].get("myapp.UserDailyActivity", [])) | set(new_ids))
                manifest.setdefault("activity_fill", []).append(
                    {"at": timezone.now().isoformat(), "as_of": self.real_now.isoformat(), "rows": len(new_ids),
                     "git": git_commit()})
                tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
        except _DryRun:
            self.stdout.write(self.style.WARNING("DRY RUN — the visit days above were rolled back, nothing written."))
            return
        except Exception:
            if tmp.exists():
                tmp.unlink()
            raise
        os.replace(tmp, self.manifest_path)
        today = self.real_now.astimezone(TZ).date()
        cache.delete_many([activity.cache_key(uid, today) for uid in self.demo_ids])
        clear_rating_cache()
        self.stdout.write(self.style.SUCCESS(f"Committed. Manifest updated: {self.manifest_path}"))

    def _manifest(self, created, before, after, n, excluded):
        return {
            "version": MANIFEST_VERSION,
            "command": "seed_demo_activity",
            "created_at": timezone.now().isoformat(),
            "as_of": self.real_now.isoformat(),
            "window_start": WINDOW_START.isoformat(),
            "seed": SEED, "requests": n, "git": git_commit(),
            "demo_email_suffix": DEMO_EMAIL_SUFFIX,
            "demo_user_ids": sorted(self.demo_ids),
            "excluded_user_ids": excluded,
            "rows": {label: created.get(label, []) for label in MANIFEST_MODELS},
            "profiles": {str(uid): {"before": before.get(uid), "after": after.get(uid)}
                         for uid in sorted(self.demo_ids) if before.get(uid) != after.get(uid)},
        }

    # ---------------------------------------------------------------- report

    def _report(self, created, plan, users, excluded, user_count_before):
        from myapp.models import ChatMessage, HelpRequest, Notification, TaskReview, UserAchievement, UserDailyActivity

        out = self.stdout.write
        task_ids = created.get("myapp.HelpRequest", [])
        tasks = list(HelpRequest.objects.filter(pk__in=task_ids))
        volunteers = sum(1 for u in users.values() if u.is_volunteer)
        clients = sum(1 for u in users.values() if u.is_client)
        out(self.style.MIGRATE_HEADING(f"seed_demo_activity — as of {self.real_now.astimezone(TZ):%Y-%m-%d %H:%M} "
                                       f"Asia/Dushanbe, window from {WINDOW_START:%Y-%m-%d}"))
        out(f"Demo accounts (@khayrkhoh.invalid): {len(users)} — volunteers {volunteers}, clients {clients}; "
            f"left out (party of an existing request): {len(excluded)} {excluded or ''}")
        out(f"Accounts in the database: {user_count_before} (unchanged — checked)")
        status = Counter(t.status for t in tasks)
        out(f"Help requests: {len(tasks)} — " + ", ".join(f"{k} {status.get(k, 0)}"
                                                           for k in ("completed", "active", "pending", "cancelled")))
        months = Counter(t.created_at.astimezone(TZ).strftime("%Y-%m") for t in tasks)
        out("  by month created: " + ", ".join(f"{m} {c}" for m, c in sorted(months.items())))
        out("  by help type: " + ", ".join(f"{k} {v}" for k, v in Counter(t.help_type for t in tasks).most_common()))
        out("  by language: " + ", ".join(f"{k} {v}" for k, v in Counter(r.lang for r in plan).most_common()))
        reviews = list(TaskReview.objects.filter(pk__in=created.get("myapp.TaskReview", [])))
        if reviews:
            avg = sum(r.stars for r in reviews) / len(reviews)
            out(f"Reviews (client → volunteer): {len(reviews)}, average {avg:.2f}, "
                + ", ".join(f"{s}★ {c}" for s, c in sorted(Counter(r.stars for r in reviews).items(), reverse=True))
                + f"; show_on_landing set: {sum(r.show_on_landing for r in reviews)}")
        msgs = list(ChatMessage.objects.filter(pk__in=created.get("myapp.ChatMessage", [])))
        flagged = sum(1 for m in msgs if m.flags)
        out(f"Chat messages: {len(msgs)} (system {sum(1 for m in msgs if m.sender_id is None)}, "
            f"flagged {flagged}) in {len({m.task_id for m in msgs})} chats; read cursors: "
            f"{len(created.get('myapp.ChatReadState', []))}")
        notes = Notification.objects.filter(pk__in=created.get("myapp.Notification", []))
        out(f"In-app notifications (DB rows only): {notes.count()} — "
            + ", ".join(f"{k} {v}" for k, v in Counter(notes.values_list("type", flat=True)).most_common()))
        out(f"  not written: for protected accounts {self.stats['notify_dropped']}, dated before the recipient's "
            f"registration {self.stats['notify_before_joined']}; WebSocket events suppressed: "
            f"{self.stats['ws_suppressed']}; external sends attempted: {len(self.stats['external_attempts'])}")
        badges = UserAchievement.objects.filter(pk__in=created.get("myapp.UserAchievement", [])).select_related("achievement")
        out(f"Achievements awarded from the data: {badges.count()} — "
            + ", ".join(f"{k} {v}" for k, v in Counter(b.achievement.code for b in badges).most_common()))
        out(f"Daily activity rows: {len(created.get('myapp.UserDailyActivity', []))}")
        self._share_report(task_ids)
        joined = sorted(self.joined.values())
        if joined:
            out(f"Registration dates moved: {len(joined)} accounts, {joined[0].astimezone(TZ):%Y-%m-%d} … "
                f"{joined[-1].astimezone(TZ):%Y-%m-%d} (each ≥ 1 day before its first demo activity)")
        if self.stats["refused"]:
            out(self.style.WARNING("Steps a service refused (skipped): " + ", ".join(
                f"{k} {v}" for k, v in self.stats["refused"].items())))

        # Rating from the created data (the same ordering the public page uses).
        from myapp.services import rating
        top = list(rating.ranked_queryset()[:15])
        photo_vol = sorted(uid for uid in self.with_photo if users.get(uid) and users[uid].is_volunteer)
        photo_cli = sorted(uid for uid in self.with_photo if users.get(uid) and users[uid].is_client)
        out(f"Demo accounts with a profile photo: volunteers {len(photo_vol)} of {volunteers} {photo_vol or ''}, "
            f"clients {len(photo_cli)} of {clients} {photo_cli or ''}")
        out("Rating top 15 (computed, all accounts):")
        for place, u in enumerate(top, start=1):
            mark = "photo " if u.has_photo else "no photo"
            who = "demo" if u.pk in self.demo_ids else "not demo"
            out(f"  {place:>2}. #{u.pk:<4} {u.username[:26]:26} {u.completed:>2} done  score {u.score:>3}  "
                f"{mark}  {who}")

        done = Counter(t.volunteer_id for t in tasks if t.status == "completed")
        accepted = Counter(t.volunteer_id for t in tasks if t.volunteer_id)
        asked = Counter(t.client_id for t in tasks)
        rev_given = Counter(r.client_id for r in reviews)
        rev_got = Counter(r.volunteer_id for r in reviews)
        sent = Counter(m.sender_id for m in msgs if m.sender_id)
        got_notes = Counter(notes.values_list("recipient_id", flat=True))
        got_badges = Counter(b.user_id for b in badges)
        act = Counter(UserDailyActivity.objects.filter(pk__in=created.get("myapp.UserDailyActivity", []))
                      .values_list("user_id", flat=True))
        busy_vol = sum(1 for u in users.values() if u.is_volunteer and accepted.get(u.pk))
        out(f"Volunteers with at least one request: {busy_vol} of {volunteers}; most active: " + ", ".join(
            f"#{uid} {c}" for uid, c in done.most_common(5)))
        if self.options["per_user"]:
            out("")
            out(f"{'id':>5} {'role':9} {'username':28} {'req':>4} {'acc':>4} {'done':>4} {'rev+':>4} {'rev-':>4} "
                f"{'msgs':>4} {'notif':>5} {'badge':>5} {'days':>4}")
            for uid, u in sorted(users.items()):
                role = "volunteer" if u.is_volunteer else "client"
                out(f"{uid:>5} {role:9} {u.username[:28]:28} {asked.get(uid, 0):>4} {accepted.get(uid, 0):>4} "
                    f"{done.get(uid, 0):>4} {rev_given.get(uid, 0):>4} {rev_got.get(uid, 0):>4} {sent.get(uid, 0):>4} "
                    f"{got_notes.get(uid, 0):>5} {got_badges.get(uid, 0):>5} {act.get(uid, 0):>4}")

    # ----------------------------------------------------------------- purge

    def _purge(self, dry, finalize=True):
        """``finalize=False``: inside the --reset dry run — no cache clear, the
        manifest stays where it is."""
        from django.db.models.deletion import Collector

        from accounts.models import Profile, clear_rating_cache
        from myapp.models import HelpRequest
        from myapp.services import feedback, presence

        if not self.manifest_path.exists():
            if self.options["reset"]:
                self.stdout.write(f"No manifest at {self.manifest_path} — nothing to purge.")
                return
            raise CommandError(f"No manifest at {self.manifest_path} — nothing to purge.")
        manifest = json.loads(self.manifest_path.read_text())
        if manifest.get("command") != "seed_demo_activity" or manifest.get("version") != MANIFEST_VERSION:
            raise CommandError("Not a seed_demo_activity manifest.")
        demo_ids = set(manifest["demo_user_ids"])
        current_demo = set(demo_accounts().values_list("pk", flat=True))
        rows = manifest["rows"]
        self.demo_ids = demo_ids
        stats = {"notify_rows": 0, "notify_dropped": 0, "notify_before_joined": 0, "ws_suppressed": 0, "external_attempts": [],
                 "refused": Counter()}
        user_count = User.objects.count()
        try:
            with quiet(demo_ids, stats), info_logs_off(), transaction.atomic():
                for label in MANIFEST_MODELS:
                    model = apps.get_model(label)
                    pks = rows.get(label, [])
                    qs = model._base_manager.filter(pk__in=pks)
                    if label == "myapp.HelpRequest":
                        bad = qs.exclude(is_demo=True, client_id__in=demo_ids)
                        if bad.exists():
                            raise CommandError(f"Refusing: manifest requests that are not demo rows of demo clients: "
                                               f"{list(bad.values_list('pk', flat=True)[:10])}")
                listed = {label: set(rows.get(label, [])) for label in MANIFEST_MODELS}
                collector = Collector(using=connection.alias)
                found = {}
                for label in MANIFEST_MODELS:
                    objs = list(apps.get_model(label)._base_manager.filter(pk__in=listed[label]))
                    found[label] = len(objs)
                    if objs:
                        collector.collect(objs)
                outside = []
                for model, instances in collector.data.items():
                    label = _label(model)
                    extra = {o.pk for o in instances} - listed.get(label, set())
                    if extra and label not in PURGE_EXTRA_CASCADE:
                        outside.append(f"{label} {sorted(extra)[:10]}")
                for qs in collector.fast_deletes:
                    label = _label(qs.model)
                    extra = set(qs.values_list("pk", flat=True)) - listed.get(label, set())
                    if extra and label not in PURGE_EXTRA_CASCADE:
                        outside.append(f"{label} {sorted(extra)[:10]}")
                for (fld, _value), groups in collector.field_updates.items():
                    for group in groups:
                        pks = set(group.values_list("pk", flat=True)) if hasattr(group, "values_list") else \
                            {o.pk for o in group}
                        if pks:
                            outside.append(f"{_label(fld.model)}.{fld.name} would be changed on {sorted(pks)[:10]}")
                if outside:
                    raise CommandError("Refusing to purge — the deletion would also touch rows the manifest does not "
                                       "list:\n  " + "\n  ".join(outside))
                before = snapshot()
                deleted = collector.delete()
                # Derived profile fields: points back by what the seed added;
                # review stats recomputed from what remains; availability recomputed.
                for uid, change in manifest.get("profiles", {}).items():
                    uid = int(uid)
                    b, a = change.get("before") or {}, change.get("after") or {}
                    profile = Profile.objects.filter(user_id=uid).first()
                    if profile is None:
                        continue
                    added = int(a.get("rating") or 0) - int(b.get("rating") or 0)
                    Profile.objects.filter(pk=profile.pk).update(rating=max(0, profile.rating - added))
                restored, drifted = 0, []
                for uid, change in manifest.get("date_joined", {}).items():
                    done = User.objects.filter(
                        pk=int(uid), date_joined=datetime.datetime.fromisoformat(change["after"])
                    ).update(date_joined=datetime.datetime.fromisoformat(change["before"]))
                    restored += done
                    if not done:
                        drifted.append(int(uid))
                if manifest.get("date_joined"):
                    self.stdout.write(f"  date_joined restored: {restored}" + (
                        f"; changed since the run, left as is: {drifted}" if drifted else ""))
                for uid in sorted(demo_ids & current_demo):
                    user = User.objects.get(pk=uid)
                    if user.is_volunteer:
                        feedback.recompute_volunteer_stats(user)
                        presence.recompute(user)
                if User.objects.count() != user_count:
                    raise CommandError("Rolled back: the number of accounts changed.")
                problems = compare_purge(before, snapshot(), listed, demo_ids)
                if problems:
                    raise CommandError("Rolled back — the post-purge check failed:\n  " + "\n  ".join(problems[:40]))
                if stats["external_attempts"]:
                    raise CommandError(f"Rolled back: external send attempted {stats['external_attempts']}")
                self.stdout.write("Purge: " + ", ".join(f"{label.split('.')[1]} {found[label]}/{len(listed[label])}"
                                                        for label in MANIFEST_MODELS)
                                  + " (found / listed; the rest were already gone, e.g. pruned notifications)")
                self.stdout.write(f"  deleted in total: {deleted[0]} row(s) {dict(deleted[1])}")
                if dry:
                    raise _DryRun()
        except _DryRun:
            self.stdout.write(self.style.WARNING("DRY RUN — purge rolled back, nothing deleted."))
            return
        if not finalize:
            return
        clear_rating_cache()
        done_path = self.manifest_path.with_name(
            f"{self.manifest_path.stem}.purged-{timezone.now():%Y%m%d-%H%M%S}.json")
        os.replace(self.manifest_path, done_path)
        self.stdout.write(self.style.SUCCESS(f"Purged. Manifest moved to {done_path}"))
