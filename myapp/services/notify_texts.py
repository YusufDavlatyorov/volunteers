"""Notification text tables (Feature Pack v2, feature 11) — data only, no imports.

Two separate tables, on purpose:

* ``CHANNEL_TEXTS`` — the server-rendered email/Telegram subject + body per type
  and language (``Profile.language``: ru / tj / en). These may contain contact
  details (a volunteer needs the client's phone in the "accepted" email) — they
  go only to the addressed recipient over email/Telegram, never into a
  ``Notification`` row or a WebSocket payload. The **RU strings reproduce the
  pre-Feature-Pack Russian subjects/bodies exactly** (tests lock several).
* ``INAPP_TEXTS`` — the in-app (bell / notifications page / toast) title + body
  per type, rendered client-side by ``static/js/i18n.js`` from the keys
  ``ntf.<type>[.<variant>].title|body`` with the row's safe ``params``. The same
  table is the server-side fallback text and the source of the i18n.js keys.

Rendering rules (``render()`` below): a context dict (params + channel-only
extras) → ``maps`` fill derived names from codes (``region`` → ``region_name``
in the recipient's language; on a miss the call-site value is kept) →
``None`` values take the per-type ``fallbacks`` or "—" → ``str.format_map``
with missing keys rendered as "—" (a text never raises).
"""

LANGS = ("ru", "tj", "en")
DEFAULT_LANG = "ru"

# Choice labels per language. RU equals the model choices exactly, so the RU
# renders match the old get_*_display() output.
LABELS = {
    "region": {
        "ru": {"dushanbe": "Душанбе", "sogd": "Согд", "khatlon": "Хатлон", "gbao": "ГБАО", "rrp": "РРП"},
        "tj": {"dushanbe": "Душанбе", "sogd": "Суғд", "khatlon": "Хатлон", "gbao": "ВМКБ", "rrp": "НТМ"},
        "en": {"dushanbe": "Dushanbe", "sogd": "Sughd", "khatlon": "Khatlon", "gbao": "GBAO", "rrp": "RRS"},
    },
    "help_type": {
        "ru": {"medical": "Медицинская помощь", "grocery": "Продукты и покупки", "transport": "Транспорт",
               "household": "Домашние дела", "emotional": "Общение и поддержка", "documents": "Документы",
               "other": "Другое"},
        "tj": {"medical": "Ёрии тиббӣ", "grocery": "Маҳсулот ва харид", "transport": "Нақлиёт",
               "household": "Корҳои хона", "emotional": "Суҳбат ва дастгирӣ", "documents": "Ҳуҷҷатҳо",
               "other": "Дигар"},
        "en": {"medical": "Medical help", "grocery": "Groceries and shopping", "transport": "Transport",
               "household": "Household chores", "emotional": "Company and support", "documents": "Documents",
               "other": "Other"},
    },
    "task_status": {
        "ru": {"pending": "Свободный", "active": "В работе", "completed": "Завершен", "cancelled": "Отменен"},
        "tj": {"pending": "Озод", "active": "Дар иҷро", "completed": "Анҷомёфта", "cancelled": "Бекоршуда"},
        "en": {"pending": "Open", "active": "In progress", "completed": "Completed", "cancelled": "Cancelled"},
    },
    "donation_status": {
        "ru": {"pending": "Ожидает проверки", "approved": "Одобрено", "ready": "Готово к передаче",
               "received": "Получено", "distributed": "Распределено", "cancelled": "Отменено"},
        "tj": {"pending": "Дар интизори санҷиш", "approved": "Тасдиқ шуд", "ready": "Барои супоридан омода",
               "received": "Қабул шуд", "distributed": "Тақсим шуд", "cancelled": "Бекор шуд"},
        "en": {"pending": "Awaiting review", "approved": "Approved", "ready": "Ready for handover",
               "received": "Received", "distributed": "Distributed", "cancelled": "Cancelled"},
    },
    "pet_type": {
        "ru": {"lost": "Потерян", "found": "Найден"},
        "tj": {"lost": "Гумшуда", "found": "Ёфтшуда"},
        "en": {"lost": "Lost", "found": "Found"},
    },
    "pet_species": {
        "ru": {"dog": "Собака", "cat": "Кошка", "bird": "Птица", "other": "Другое"},
        "tj": {"dog": "Саг", "cat": "Гурба", "bird": "Парранда", "other": "Дигар"},
        "en": {"dog": "Dog", "cat": "Cat", "bird": "Bird", "other": "Other"},
    },
    # Badge titles (milestone 4) — also the ach.<code>.title i18n keys
    # (services/achievements.py::ui_i18n_keys).
    "achievement": {
        "ru": {"first_help": "Первая помощь", "tasks_5": "Пять добрых дел", "tasks_25": "Опора района",
               "high_rating": "Высокий рейтинг", "regular": "Регулярность", "long_term": "Давний друг",
               "tasks_10": "Надёжная опора", "tasks_50": "Сердце района",
               "tasks_100": "Сто добрых дел", "kind_words": "Тёплые слова", "trusted": "Доверие клиентов",
               "steady_helper": "Верный помощник", "year_together": "Год рядом",
               "versatile": "Мастер на все руки", "donation_courier": "Курьер добра",
               "pet_friend": "Друг хвостатых", "mentor": "Наставник"},
        "tj": {"first_help": "Кумаки аввал", "tasks_5": "Панҷ кори нек", "tasks_25": "Такягоҳи маҳалла",
               "high_rating": "Рейтинги баланд", "regular": "Мунтазамӣ", "long_term": "Дӯсти дерина",
               "tasks_10": "Такягоҳи боэътимод", "tasks_50": "Дили маҳалла",
               "tasks_100": "Сад кори нек", "kind_words": "Суханони гарм", "trusted": "Эътимоди мизоҷон",
               "steady_helper": "Ёвари содиқ", "year_together": "Як сол дар канор",
               "versatile": "Устои ҳамакора", "donation_courier": "Қосиди некӣ",
               "pet_friend": "Дӯсти ҳайвонот", "mentor": "Мураббӣ"},
        "en": {"first_help": "First help", "tasks_5": "Five good deeds", "tasks_25": "Pillar of the neighbourhood",
               "high_rating": "High rating", "regular": "Regular helper", "long_term": "Long-time friend",
               "tasks_10": "Reliable support", "tasks_50": "Heart of the neighbourhood",
               "tasks_100": "A hundred good deeds", "kind_words": "Kind words", "trusted": "Clients' trust",
               "steady_helper": "Steady helper", "year_together": "A year by their side",
               "versatile": "Jack of all trades", "donation_courier": "Courier of kindness",
               "pet_friend": "Friend of pets", "mentor": "Mentor"},
    },
}

_REGION = ("region", "region")
_HELP_TYPE = ("help_type", "help_type")

# type -> {"maps": {target: (source_key, LABELS name | {lang: {code: text}})},
#          "fallbacks": {lang: {key: text}}, "ru"|"tj"|"en": (subject, body)}
CHANNEL_TEXTS = {
    "task_new_in_region": {
        "maps": {"region_name": _REGION},
        "ru": ("Новый запрос помощи", "Новый запрос в регионе {region_name}: {description}"),
        "tj": ("Дархости нави кумак", "Дархости нав дар минтақаи {region_name}: {description}"),
        "en": ("New help request", "New request in {region_name}: {description}"),
    },
    "task_accepted": {
        "ru": ("Запрос принят волонтером",
               "Запрос #{task_id} принят.\nКлиент: {client_name}, телефон: {phone}\n"
               "Волонтер: {volunteer_name}\nАдрес: {address}"),
        "tj": ("Дархост аз ҷониби ихтиёрӣ қабул шуд",
               "Дархости #{task_id} қабул шуд.\nМуштарӣ: {client_name}, телефон: {phone}\n"
               "Ихтиёрӣ: {volunteer_name}\nСуроға: {address}"),
        "en": ("Request accepted by a volunteer",
               "Request #{task_id} has been accepted.\nClient: {client_name}, phone: {phone}\n"
               "Volunteer: {volunteer_name}\nAddress: {address}"),
    },
    "task_assigned": {
        "maps": {"help_type_name": _HELP_TYPE},
        "ru": ("Вам назначен запрос помощи",
               "Куратор назначил вам запрос #{task_id} ({help_type_name}).\n"
               "Клиент: {client_name}, телефон: {phone}\nАдрес: {address}"),
        "tj": ("Ба шумо дархости кумак таъин шуд",
               "Куратор ба шумо дархости #{task_id} ({help_type_name})-ро таъин кард.\n"
               "Муштарӣ: {client_name}, телефон: {phone}\nСуроға: {address}"),
        "en": ("A help request has been assigned",
               "A curator assigned request #{task_id} ({help_type_name}).\n"
               "Client: {client_name}, phone: {phone}\nAddress: {address}"),
        # Milestone 2: a curator manually assigned a volunteer who has
        # do-not-disturb on (owner note §12). Same maps/fallbacks as the base.
        "variants": {
            "urgent_override": {
                "ru": ("Срочное назначение",
                       "Куратор срочно назначил вам запрос #{task_id} ({help_type_name}), "
                       "хотя у вас включён режим «не беспокоить».\n"
                       "Клиент: {client_name}, телефон: {phone}\nАдрес: {address}"),
                "tj": ("Таъиноти фаврӣ",
                       "Куратор ба шумо дархости #{task_id} ({help_type_name})-ро фавран таъин кард, "
                       "гарчанде ки реҷаи «халал нарасонед» фаъол аст.\n"
                       "Муштарӣ: {client_name}, телефон: {phone}\nСуроға: {address}"),
                "en": ("Urgent assignment",
                       "A curator urgently assigned request #{task_id} ({help_type_name}) to you, "
                       "even though your do-not-disturb mode is on.\n"
                       "Client: {client_name}, phone: {phone}\nAddress: {address}"),
            },
        },
    },
    "task_stage_changed": {
        "maps": {
            "stage_subject": ("work_stage", {
                "ru": {"en_route": "Волонтёр выехал к вам", "arrived": "Волонтёр на месте",
                       "in_progress": "Волонтёр приступил к работе", "assigned": "Волонтёр назначен"},
                "tj": {"en_route": "Ихтиёрӣ ба назди шумо баромад", "arrived": "Ихтиёрӣ омад",
                       "in_progress": "Ихтиёрӣ ба кор шурӯъ кард", "assigned": "Ихтиёрӣ таъин шуд"},
                "en": {"en_route": "Your volunteer is on the way", "arrived": "Your volunteer has arrived",
                       "in_progress": "Your volunteer has started", "assigned": "A volunteer is assigned"},
            }),
            "stage_lower": ("work_stage", {
                "ru": {"assigned": "назначен", "en_route": "в пути", "arrived": "на месте", "in_progress": "выполняется"},
                "tj": {"assigned": "таъин шуд", "en_route": "дар роҳ", "arrived": "дар ҷой", "in_progress": "иҷро мешавад"},
                "en": {"assigned": "assigned", "en_route": "on the way", "arrived": "arrived", "in_progress": "in progress"},
            }),
        },
        "ru": ("{stage_subject}", "Запрос #{task_id}: {stage_lower}."),
        "tj": ("{stage_subject}", "Дархости #{task_id}: {stage_lower}."),
        "en": ("{stage_subject}", "Request #{task_id}: {stage_lower}."),
    },
    "task_completed": {
        "ru": ("Запрос выполнен", "Ваш запрос #{task_id} отмечен как выполненный. Спасибо!"),
        "tj": ("Дархост иҷро шуд", "Дархости шумо #{task_id} ҳамчун иҷрошуда қайд карда шуд. Ташаккур!"),
        "en": ("Request completed", "Your request #{task_id} has been marked as completed. Thank you!"),
    },
    "task_cancelled": {
        "maps": {"help_type_name": _HELP_TYPE},
        "ru": ("Запрос отменён", "Запрос #{task_id} ({help_type_name}) отменён. Причина: {reason}"),
        "tj": ("Дархост бекор шуд", "Дархости #{task_id} ({help_type_name}) бекор карда шуд. Сабаб: {reason}"),
        "en": ("Request cancelled", "Request #{task_id} ({help_type_name}) has been cancelled. Reason: {reason}"),
    },
    "sos_new": {
        "maps": {"region_name": _REGION, "help_type_name": _HELP_TYPE},
        "fallbacks": {"ru": {"location": "не указано"}, "tj": {"location": "нишон дода нашудааст"},
                      "en": {"location": "not specified"}},
        "ru": ("⚠️ SOS: волонтёр сообщил об опасности",
               "Волонтёр {volunteer_name} сообщил об опасности во время работы.\n\n"
               "Запрос #{task_id} — {help_type_name}\n"
               "Клиент: {client_name}, телефон: {phone}\n"
               "Регион: {region_name}\n"
               "Адрес: {address}\n"
               "Местоположение сигнала: {location}\n"
               "Причина: {reason}\n"
               "Время: {time}\n"),
        "tj": ("⚠️ SOS: ихтиёрӣ дар бораи хатар хабар дод",
               "Ихтиёрӣ {volunteer_name} ҳангоми кор дар бораи хатар хабар дод.\n\n"
               "Дархости #{task_id} — {help_type_name}\n"
               "Муштарӣ: {client_name}, телефон: {phone}\n"
               "Минтақа: {region_name}\n"
               "Суроға: {address}\n"
               "Ҷойгиршавии сигнал: {location}\n"
               "Сабаб: {reason}\n"
               "Вақт: {time}\n"),
        "en": ("⚠️ SOS: a volunteer reported danger",
               "Volunteer {volunteer_name} reported danger while on a task.\n\n"
               "Request #{task_id} — {help_type_name}\n"
               "Client: {client_name}, phone: {phone}\n"
               "Region: {region_name}\n"
               "Address: {address}\n"
               "Signal location: {location}\n"
               "Reason: {reason}\n"
               "Time: {time}\n"),
    },
    "sos_nearby": {
        "maps": {"region_name": _REGION},
        "ru": ("⚠️ SOS рядом с вами",
               "Волонтёр в регионе {region_name} подал сигнал опасности. "
               "Откройте карту на сайте, чтобы увидеть, где он, — точка видна, пока сигнал активен."),
        "tj": ("⚠️ SOS дар наздикии шумо",
               "Ихтиёрӣ дар минтақаи {region_name} сигнали хатар дод. "
               "Харитаро дар сайт кушоед, то бинед, ки ӯ дар куҷост, — нуқта то фаъол будани сигнал намоён аст."),
        "en": ("⚠️ SOS near you",
               "A volunteer in {region_name} raised a danger alert. "
               "Open the map on the site to see where — the point is visible while the alert is active."),
    },
    "sos_update": {
        "maps": {"phrase": ("action", {
            "ru": {"acknowledged": "принят координатором", "resolved": "закрыт координатором",
                   "cancelled": "отменён координатором"},
            "tj": {"acknowledged": "аз ҷониби куратор қабул шуд", "resolved": "аз ҷониби куратор пӯшида шуд",
                   "cancelled": "аз ҷониби куратор бекор шуд"},
            "en": {"acknowledged": "was acknowledged by a coordinator", "resolved": "was closed by a coordinator",
                   "cancelled": "was cancelled by a coordinator"},
        })},
        "ru": ("Сигнал опасности: обновление", "Ваш сигнал по запросу #{task_id} {phrase}."),
        "tj": ("Сигнали хатар: навсозӣ", "Сигнали шумо оид ба дархости #{task_id} {phrase}."),
        "en": ("Danger alert: update", "Your alert on request #{task_id} {phrase}."),
    },
    "application_submitted": {
        "maps": {
            "region_name": _REGION,
            "subject_v": ("kind", {
                "ru": {"new": "Новая заявка волонтера", "again": "Повторная заявка волонтера"},
                "tj": {"new": "Аризаи нави ихтиёрӣ", "again": "Аризаи такрории ихтиёрӣ"},
                "en": {"new": "New volunteer application", "again": "Volunteer application resubmitted"},
            }),
            "body_v": ("kind", {
                "ru": {"new": "Пользователь {username} подал заявку на волонтерство в регионе {region_name}.",
                       "again": "Пользователь {username} подал заявку на волонтерство повторно."},
                "tj": {"new": "Корбар {username} дар минтақаи {region_name} барои ихтиёрӣ шудан ариза дод.",
                       "again": "Корбар {username} барои ихтиёрӣ шудан аризаи такрорӣ дод."},
                "en": {"new": "User {username} applied to volunteer in {region_name}.",
                       "again": "User {username} re-applied to volunteer."},
            }),
        },
        "ru": ("{subject_v}", "{body_v}"),
        "tj": ("{subject_v}", "{body_v}"),
        "en": ("{subject_v}", "{body_v}"),
    },
    "application_approved": {
        "ru": ("Заявка на волонтерство одобрена",
               "Поздравляем! Ваша заявка на волонтерство одобрена. Теперь вам доступен список запросов."),
        "tj": ("Аризаи ихтиёрӣ тасдиқ шуд",
               "Табрик! Аризаи шумо барои ихтиёрӣ шудан тасдиқ шуд. Акнун рӯйхати дархостҳо барои шумо дастрас аст."),
        "en": ("Volunteer application approved",
               "Congratulations! Your volunteer application has been approved. The request list is now open to you."),
    },
    "application_rejected": {
        "ru": ("Заявка на волонтерство отклонена", "Ваша заявка на волонтерство отклонена администратором."),
        "tj": ("Аризаи ихтиёрӣ рад шуд", "Аризаи шумо барои ихтиёрӣ шудан аз ҷониби маъмур рад карда шуд."),
        "en": ("Volunteer application declined", "Your volunteer application was declined by an administrator."),
    },
    "donation_new": {
        "ru": ("Новое предложение помощи",
               "Предложение #{donation_id}: {item_label} ×{quantity} от {donor_label}. Ожидает проверки координатора."),
        "tj": ("Пешниҳоди нави кумак",
               "Пешниҳоди #{donation_id}: {item_label} ×{quantity} аз {donor_label}. Интизори санҷиши ҳамоҳангсоз."),
        "en": ("New offer of help",
               "Offer #{donation_id}: {item_label} ×{quantity} from {donor_label}. Awaiting coordinator review."),
    },
    "photo_report_approved": {
        "ru": ("Фотоотчёт одобрен", "Ваш фотоотчёт «{title}» одобрен координатором. Спасибо за помощь!"),
        "tj": ("Фотоҳисобот тасдиқ шуд", "Фотоҳисоботи шумо «{title}» аз ҷониби ҳамоҳангсоз тасдиқ шуд. Ташаккур барои кумак!"),
        "en": ("Photo report approved", "Your photo report “{title}” was approved by a coordinator. Thank you for helping!"),
    },
    "photo_report_rejected": {
        "ru": ("Фотоотчёт отклонён",
               "Ваш фотоотчёт «{title}» отклонён координатором.\nПричина: {message}\nЗагрузите новый фотоотчёт на сайте."),
        "tj": ("Фотоҳисобот рад шуд",
               "Фотоҳисоботи шумо «{title}» аз ҷониби ҳамоҳангсоз рад шуд.\nСабаб: {message}\nДар сайт фотоҳисоботи нав бор кунед."),
        "en": ("Photo report rejected",
               "Your photo report “{title}” was rejected by a coordinator.\nReason: {message}\nPlease upload a new photo report on the website."),
    },
    "photo_report_deleted": {
        "ru": ("Фотоотчёт удалён",
               "Ваш фотоотчёт «{title}» удалён администратором. Если за него были начислены баллы, они отменены — "
               "загрузите новый фотоотчёт на сайте."),
        "tj": ("Фотоҳисобот нест карда шуд",
               "Фотоҳисоботи шумо «{title}» аз ҷониби маъмур нест карда шуд. Агар барои он хол дода шуда бошад, "
               "он бекор шуд — дар сайт фотоҳисоботи нав бор кунед."),
        "en": ("Photo report deleted",
               "Your photo report “{title}” was deleted by an administrator. Any points it earned were withdrawn — "
               "please upload a new photo report on the website."),
        # A bulk delete: one message per volunteer, with the count.
        "variants": {
            "many": {
                "ru": ("Фотоотчёты удалены",
                       "Администратор удалил ваши фотоотчёты: {count}. Если за них были начислены баллы, они отменены — "
                       "загрузите новые фотоотчёты на сайте."),
                "tj": ("Фотоҳисоботҳо нест карда шуданд",
                       "Маъмур фотоҳисоботҳои шуморо нест кард: {count}. Агар барои онҳо хол дода шуда бошад, "
                       "он бекор шуд — дар сайт фотоҳисоботҳои нав бор кунед."),
                "en": ("Photo reports deleted",
                       "An administrator deleted your photo reports: {count}. Any points they earned were withdrawn — "
                       "please upload new photo reports on the website."),
            },
        },
    },
    "donation_assigned": {
        "ru": ("Вам назначено предложение помощи",
               "Предложение #{donation_id}: {item_label} ×{quantity}. Откройте детали, чтобы согласовать передачу."),
        "tj": ("Ба шумо пешниҳоди кумак таъин шуд",
               "Пешниҳоди #{donation_id}: {item_label} ×{quantity}. Барои мувофиқа кардани супоридан тафсилотро кушоед."),
        "en": ("An offer of help was assigned to you",
               "Offer #{donation_id}: {item_label} ×{quantity}. Open the details to arrange the handover."),
    },
    "donation_status": {
        "maps": {"status_name": ("status", "donation_status")},
        "ru": ("Статус предложения обновлён", "Ваше предложение #{donation_id} теперь: {status_name}."),
        "tj": ("Ҳолати пешниҳод нав шуд", "Пешниҳоди шумо #{donation_id} акнун: {status_name}."),
        "en": ("Offer status updated", "Your offer #{donation_id} is now: {status_name}."),
    },
    "pet_new": {
        "maps": {"type_name": ("report_type", "pet_type"), "region_name": _REGION},
        "ru": ("Новое объявление о животном",
               "{type_name}: {display_name}{breed_part}.\nРегион: {region_name}\nОписание: {description}\nАвтор: {author_name}"),
        "tj": ("Эълони нав дар бораи ҳайвон",
               "{type_name}: {display_name}{breed_part}.\nМинтақа: {region_name}\nТавсиф: {description}\nМуаллиф: {author_name}"),
        "en": ("New pet report",
               "{type_name}: {display_name}{breed_part}.\nRegion: {region_name}\nDescription: {description}\nAuthor: {author_name}"),
    },
    "pet_match": {
        "maps": {
            "type_word": ("report_type", {
                "ru": {"lost": "потерянном", "found": "найденном"},
                "tj": {"lost": "гумшуда", "found": "ёфтшуда"},
                "en": {"lost": "lost", "found": "found"},
            }),
            "species_name": ("species", "pet_species"),
            "region_name": _REGION,
        },
        "fallbacks": {"ru": {"region_name": "регион не указан", "type_word": ""},
                      "tj": {"region_name": "минтақа нишон дода нашудааст", "type_word": ""},
                      "en": {"region_name": "region not set", "type_word": ""}},
        "ru": ("Возможное совпадение по вашему объявлению",
               "По вашему объявлению #{other_id} появилось новое объявление о {type_word} животном "
               "(#{pet_id}, {species_name}, {region_name}). Проверьте, не совпадают ли они."),
        "tj": ("Мувофиқати эҳтимолӣ бо эълони шумо",
               "Ба эълони шумо #{other_id} эълони нав дар бораи ҳайвони {type_word} пайдо шуд "
               "(#{pet_id}, {species_name}, {region_name}). Санҷед, ки онҳо мувофиқ меоянд ё не."),
        "en": ("Possible match for your report",
               "A new report about a {type_word} animal matches your report #{other_id} "
               "(#{pet_id}, {species_name}, {region_name}). Check whether they are the same."),
    },
    "pet_status": {
        "maps": {"phrase": ("status", {
            "ru": {"open": "снова открыто", "matched": "отмечено как возможное совпадение",
                   "resolved": "закрыто: питомец воссоединён с хозяином", "closed": "закрыто"},
            "tj": {"open": "дубора кушода шуд", "matched": "ҳамчун мувофиқати эҳтимолӣ қайд шуд",
                   "resolved": "пӯшида шуд: ҳайвон ба соҳибаш баргардонида шуд", "closed": "пӯшида шуд"},
            "en": {"open": "is open again", "matched": "was marked as a possible match",
                   "resolved": "is closed: the pet is back with its owner", "closed": "is closed"},
        })},
        "ru": ("Статус вашего объявления обновлён", "Ваше объявление #{pet_id} ({display_name}) {phrase}."),
        "tj": ("Ҳолати эълони шумо нав шуд", "Эълони шумо #{pet_id} ({display_name}) {phrase}."),
        "en": ("Your report status changed", "Your report #{pet_id} ({display_name}) {phrase}."),
    },
    "broadcast": {
        # Free text from staff — always passed as subject/message overrides.
        "ru": ("{subject}", "{message}"),
        "tj": ("{subject}", "{message}"),
        "en": ("{subject}", "{message}"),
    },
    "event_new": {
        "ru": ("Акция: {title}", "{description}\nДата: {date}"),
        "tj": ("Чорабинӣ: {title}", "{description}\nСана: {date}"),
        "en": ("Event: {title}", "{description}\nDate: {date}"),
    },
    "task_overdue": {
        "ru": ("Просроченный запрос", "Запрос #{task_id} в работе больше 3 часов. Волонтер: {volunteer_str}"),
        "tj": ("Дархости мӯҳлаташ гузашта", "Дархости #{task_id} зиёда аз 3 соат дар иҷро аст. Ихтиёрӣ: {volunteer_str}"),
        "en": ("Overdue request", "Request #{task_id} has been in progress for over 3 hours. Volunteer: {volunteer_str}"),
    },
    "task_stale": {
        "maps": {"region_name": _REGION, "help_type_name": _HELP_TYPE},
        "ru": ("Запрос долго ждёт волонтёра",
               "Запрос #{task_id} ({help_type_name}) в регионе {region_name} ждёт волонтёра уже {hours} ч. "
               "и до сих пор не принят. Клиент: {client_name}, телефон: {phone}."),
        "tj": ("Дархост муддати дароз ихтиёриро интизор аст",
               "Дархости #{task_id} ({help_type_name}) дар минтақаи {region_name} {hours} соат боз ихтиёриро "
               "интизор аст ва то ҳол қабул нашудааст. Муштарӣ: {client_name}, телефон: {phone}."),
        "en": ("Request waiting for a volunteer",
               "Request #{task_id} ({help_type_name}) in {region_name} has waited {hours} h "
               "and is still not accepted. Client: {client_name}, phone: {phone}."),
    },
    "task_escalated": {
        "maps": {"region_name": _REGION, "help_type_name": _HELP_TYPE, "status_name": ("status", "task_status")},
        "ru": ("Запрос передан на рассмотрение",
               "Запрос #{task_id} ({help_type_name}) требует внимания координатора.\n"
               "Статус: {status_name}\nРегион: {region_name}\nКлиент: {client_name}\n"
               "Волонтёр: {volunteer_name}\nЭскалацию инициировал: {actor_name} ({actor_role})\n"
               "Комментарий: {note}"),
        "tj": ("Дархост барои баррасӣ супорида шуд",
               "Дархости #{task_id} ({help_type_name}) диққати ҳамоҳангсозро талаб мекунад.\n"
               "Ҳолат: {status_name}\nМинтақа: {region_name}\nМуштарӣ: {client_name}\n"
               "Ихтиёрӣ: {volunteer_name}\nТашаббускор: {actor_name} ({actor_role})\n"
               "Шарҳ: {note}"),
        "en": ("Request escalated for review",
               "Request #{task_id} ({help_type_name}) needs a coordinator's attention.\n"
               "Status: {status_name}\nRegion: {region_name}\nClient: {client_name}\n"
               "Volunteer: {volunteer_name}\nEscalated by: {actor_name} ({actor_role})\n"
               "Comment: {note}"),
    },
    "volunteer_pinged": {
        "maps": {"region_name": _REGION, "help_type_name": _HELP_TYPE},
        "ru": ("Рекомендованный запрос помощи",
               "Куратор рекомендует вам запрос #{task_id} ({help_type_name}) в регионе {region_name}.\n"
               "Это рекомендация, а не назначение — запрос остаётся свободным, пока вы сами его не примете."),
        "tj": ("Дархости тавсияшуда",
               "Куратор ба шумо дархости #{task_id} ({help_type_name})-ро дар минтақаи {region_name} тавсия медиҳад.\n"
               "Ин тавсия аст, на таъинот — дархост то он даме ки худатон қабул накунед, озод мемонад."),
        "en": ("Recommended help request",
               "A curator recommends request #{task_id} ({help_type_name}) in {region_name} to you.\n"
               "This is a recommendation, not an assignment — the request stays open until you accept it yourself."),
    },
    "tracking_stopped": {
        "ru": ("Волонтёр остановил передачу местоположения",
               "Волонтёр прекратил передачу своего местоположения по запросу #{task_id}."),
        "tj": ("Ихтиёрӣ интиқоли ҷойгиршавиро қатъ кард",
               "Ихтиёрӣ интиқоли ҷойгиршавии худро оид ба дархости #{task_id} қатъ кард."),
        "en": ("Volunteer stopped sharing location",
               "The volunteer stopped sharing their location on request #{task_id}."),
    },
    # Chat (milestone 3). ``sender_display`` / ``preview`` come from notify(extra=)
    # — the message text reaches only the addressed party's email/Telegram.
    "chat_message": {
        "fallbacks": {"ru": {"preview": "[фото]"}, "tj": {"preview": "[сурат]"}, "en": {"preview": "[photo]"}},
        "ru": ("Новое сообщение в чате", "Новое сообщение по запросу #{task_id} от {sender_display}:\n{preview}"),
        "tj": ("Паёми нав дар чат", "Паёми нав оид ба дархости #{task_id} аз {sender_display}:\n{preview}"),
        "en": ("New chat message", "New message about request #{task_id} from {sender_display}:\n{preview}"),
    },
    "chat_reported": {
        "ru": ("Жалоба на сообщение в чате",
               "Участник чата по запросу #{task_id} пожаловался на сообщение #{message_id}. Проверьте чат."),
        "tj": ("Шикоят аз паём дар чат",
               "Иштирокчии чат оид ба дархости #{task_id} аз паёми #{message_id} шикоят кард. Чатро санҷед."),
        "en": ("Chat message reported",
               "A chat participant on request #{task_id} reported message #{message_id}. Please review the chat."),
    },
    "chat_flagged": {
        "ru": ("Сообщение в чате стоит проверить",
               "В чате запроса #{task_id} есть сообщение, которое стоит проверить (#{message_id}). Сообщение доставлено как обычно."),
        "tj": ("Паём дар чатро бояд санҷид",
               "Дар чати дархости #{task_id} паёме ҳаст, ки бояд санҷид (#{message_id}). Паём ба таври муқаррарӣ расонида шуд."),
        "en": ("A chat message may need a look",
               "The chat of request #{task_id} has a message worth checking (#{message_id}). It was delivered as usual."),
    },
    "community_report": {
        "ru": ("Жалоба в сообществе волонтёров", "Участник сообщества пожаловался на сообщение. Откройте ссылку и проверьте."),
        "tj": ("Шикоят дар ҷамъияти ихтиёриён", "Иштирокчии ҷамъият аз паём шикоят кард. Истинодро кушоед ва санҷед."),
        "en": ("Report in the volunteer community", "A community member reported a message. Open the link and review it."),
    },
    "review_received": {
        "ru": ("Новый отзыв", "Вы получили новый отзыв по запросу #{task_id}."),
        "tj": ("Фикри нав", "Шумо оид ба дархости #{task_id} фикри нав гирифтед."),
        "en": ("New review", "You received a new review for request #{task_id}."),
    },
    "review_low": {
        "ru": ("Низкая оценка волонтёра",
               "Клиент оценил помощь по запросу #{task_id} на {stars} из 5 (волонтёр: {volunteer_name}). Проверьте отзыв."),
        "tj": ("Баҳои пасти ихтиёрӣ",
               "Мизоҷ кумакро оид ба дархости #{task_id} бо {stars} аз 5 баҳо дод (ихтиёрӣ: {volunteer_name}). Фикрро санҷед."),
        "en": ("Low volunteer rating",
               "The client rated the help on request #{task_id} {stars} out of 5 (volunteer: {volunteer_name}). Please review it."),
    },
    "review_request": {
        "ru": ("Оцените помощь волонтёра", "Оцените помощь волонтёра по запросу #{task_id}."),
        "tj": ("Кумаки ихтиёриро баҳо диҳед", "Кумаки ихтиёриро оид ба дархости #{task_id} баҳо диҳед."),
        "en": ("Rate the volunteer's help", "Please rate the volunteer's help with request #{task_id}."),
    },
    "location_stale": {
        "ru": ("Обновите местоположение",
               "Ваше сохранённое местоположение устарело — обновите его в профиле, по нему подбираются запросы."),
        "tj": ("Ҷойгиршавиро навсозӣ кунед",
               "Ҷойгиршавии нигоҳдоштаи шумо кӯҳна шудааст — онро дар профил навсозӣ кунед, дархостҳо аз рӯи он интихоб мешаванд."),
        "en": ("Update your location",
               "Your saved location is out of date — update it in your profile; requests are matched by it."),
    },
    "achievement_unlocked": {
        "maps": {"achievement": ("achievement_code", "achievement")},
        "ru": ("Новое достижение", "Вы получили новое достижение: {achievement}."),
        "tj": ("Дастоварди нав", "Шумо дастоварди нав гирифтед: {achievement}."),
        "en": ("New achievement", "You unlocked a new achievement: {achievement}."),
    },
}

# In-app texts (i18n.js keys ntf.<type>[.<variant>].title / .body). Placeholders
# may only use the SAFE params notify() stores on the row (ids, codes, display
# names) — never contact details.
INAPP_TEXTS = {
    "task_new_in_region": {
        "en": ("New request in your region", "Request #{task_id} needs a volunteer."),
        "ru": ("Новый запрос в вашем регионе", "Запросу #{task_id} нужен волонтёр."),
        "tj": ("Дархости нав дар минтақаи шумо", "Ба дархости #{task_id} ихтиёрӣ лозим аст."),
    },
    "task_accepted": {
        "en": ("Request accepted", "Request #{task_id} was accepted by {volunteer_name}."),
        "ru": ("Запрос принят", "Запрос #{task_id} принял волонтёр {volunteer_name}."),
        "tj": ("Дархост қабул шуд", "Дархости #{task_id}-ро ихтиёрӣ {volunteer_name} қабул кард."),
    },
    "task_assigned": {
        "variants": {
            "urgent_override": {
                "en": ("Urgent assignment",
                       "A curator urgently assigned request #{task_id} to you, even though your "
                       "do-not-disturb mode is on."),
                "ru": ("Срочное назначение",
                       "Куратор срочно назначил вам запрос #{task_id}, хотя у вас включён режим "
                       "«не беспокоить»."),
                "tj": ("Таъиноти фаврӣ",
                       "Куратор ба шумо дархости #{task_id}-ро фавран таъин кард, гарчанде ки "
                       "реҷаи «халал нарасонед» фаъол аст."),
            },
        },
        "en": ("Request assigned", "Request #{task_id} was assigned by a curator."),
        "ru": ("Запрос назначен", "Куратор назначил запрос #{task_id}."),
        "tj": ("Дархост таъин шуд", "Куратор дархости #{task_id}-ро таъин кард."),
    },
    "task_stage_changed": {
        "variants": {
            "en_route": {
                "en": ("Volunteer on the way", "Request #{task_id}: the volunteer is on the way."),
                "ru": ("Волонтёр в пути", "Запрос #{task_id}: волонтёр выехал к вам."),
                "tj": ("Ихтиёрӣ дар роҳ", "Дархости #{task_id}: ихтиёрӣ ба назди шумо баромад."),
            },
            "arrived": {
                "en": ("Volunteer has arrived", "Request #{task_id}: the volunteer is at your door."),
                "ru": ("Волонтёр на месте", "Запрос #{task_id}: волонтёр на месте."),
                "tj": ("Ихтиёрӣ омад", "Дархости #{task_id}: ихтиёрӣ дар ҷой аст."),
            },
        },
        "en": ("Request update", "Request #{task_id}: the work stage changed."),
        "ru": ("Обновление запроса", "Запрос #{task_id}: этап работы изменён."),
        "tj": ("Навсозии дархост", "Дархости #{task_id}: марҳилаи кор иваз шуд."),
    },
    "task_completed": {
        "en": ("Request completed", "Your request #{task_id} is completed. Thank you!"),
        "ru": ("Запрос выполнен", "Ваш запрос #{task_id} выполнен. Спасибо!"),
        "tj": ("Дархост иҷро шуд", "Дархости шумо #{task_id} иҷро шуд. Ташаккур!"),
    },
    "task_cancelled": {
        "en": ("Request cancelled", "Request #{task_id} has been cancelled."),
        "ru": ("Запрос отменён", "Запрос #{task_id} отменён."),
        "tj": ("Дархост бекор шуд", "Дархости #{task_id} бекор карда шуд."),
    },
    "sos_new": {
        "en": ("SOS: volunteer in danger", "Volunteer {volunteer_name} raised an alert on request #{task_id}."),
        "ru": ("SOS: волонтёр в опасности", "Волонтёр {volunteer_name} подал сигнал по запросу #{task_id}."),
        "tj": ("SOS: ихтиёрӣ дар хатар", "Ихтиёрӣ {volunteer_name} оид ба дархости #{task_id} сигнал дод."),
    },
    "sos_nearby": {
        "en": ("SOS in your region", "A volunteer nearby needs help. Open the map to see where."),
        "ru": ("SOS в вашем регионе", "Волонтёру рядом с вами нужна помощь. Откройте карту, чтобы увидеть, где."),
        "tj": ("SOS дар минтақаи шумо", "Ба ихтиёрии наздики шумо кумак лозим аст. Харитаро кушоед, то бинед, ки дар куҷо."),
    },
    "sos_update": {
        "variants": {
            "acknowledged": {
                "en": ("Alert acknowledged", "Your alert on request #{task_id} was acknowledged."),
                "ru": ("Сигнал принят", "Ваш сигнал по запросу #{task_id} принят координатором."),
                "tj": ("Сигнал қабул шуд", "Сигнали шумо оид ба дархости #{task_id} қабул шуд."),
            },
            "resolved": {
                "en": ("Alert closed", "Your alert on request #{task_id} was closed."),
                "ru": ("Сигнал закрыт", "Ваш сигнал по запросу #{task_id} закрыт координатором."),
                "tj": ("Сигнал пӯшида шуд", "Сигнали шумо оид ба дархости #{task_id} пӯшида шуд."),
            },
            "cancelled": {
                "en": ("Alert cancelled", "Your alert on request #{task_id} was cancelled."),
                "ru": ("Сигнал отменён", "Ваш сигнал по запросу #{task_id} отменён координатором."),
                "tj": ("Сигнал бекор шуд", "Сигнали шумо оид ба дархости #{task_id} бекор шуд."),
            },
        },
        "en": ("Alert update", "Your alert on request #{task_id} was updated."),
        "ru": ("Обновление сигнала", "Ваш сигнал по запросу #{task_id} обновлён."),
        "tj": ("Навсозии сигнал", "Сигнали шумо оид ба дархости #{task_id} нав шуд."),
    },
    "application_submitted": {
        "variants": {
            "again": {
                "en": ("Application resubmitted", "{username} re-applied to volunteer."),
                "ru": ("Повторная заявка", "{username} повторно подал заявку на волонтерство."),
                "tj": ("Аризаи такрорӣ", "{username} аризаи такрорӣ барои ихтиёрӣ шудан дод."),
            },
        },
        "en": ("New volunteer application", "{username} applied to volunteer."),
        "ru": ("Новая заявка волонтера", "{username} подал заявку на волонтерство."),
        "tj": ("Аризаи нави ихтиёрӣ", "{username} барои ихтиёрӣ шудан ариза дод."),
    },
    "application_approved": {
        "en": ("Application approved", "Welcome aboard! The request list is now open to you."),
        "ru": ("Заявка одобрена", "Добро пожаловать! Теперь вам доступен список запросов."),
        "tj": ("Ариза тасдиқ шуд", "Хуш омадед! Акнун рӯйхати дархостҳо барои шумо дастрас аст."),
    },
    "application_rejected": {
        "en": ("Application declined", "Your volunteer application was declined."),
        "ru": ("Заявка отклонена", "Ваша заявка на волонтерство отклонена."),
        "tj": ("Ариза рад шуд", "Аризаи шумо барои ихтиёрӣ шудан рад карда шуд."),
    },
    "donation_new": {
        "en": ("New offer of help", "Offer #{donation_id} is waiting for review."),
        "ru": ("Новое предложение помощи", "Предложение #{donation_id} ожидает проверки."),
        "tj": ("Пешниҳоди нави кумак", "Пешниҳоди #{donation_id} интизори санҷиш аст."),
    },
    "photo_report_approved": {
        "en": ("Photo report approved", "“{title}” was approved by a coordinator."),
        "ru": ("Фотоотчёт одобрен", "«{title}» одобрен координатором."),
        "tj": ("Фотоҳисобот тасдиқ шуд", "«{title}» аз ҷониби ҳамоҳангсоз тасдиқ шуд."),
    },
    "photo_report_rejected": {
        "en": ("Photo report rejected", "“{title}”: {message}. Upload a new report."),
        "ru": ("Фотоотчёт отклонён", "«{title}»: {message}. Загрузите новый отчёт."),
        "tj": ("Фотоҳисобот рад шуд", "«{title}»: {message}. Фотоҳисоботи нав бор кунед."),
    },
    "photo_report_deleted": {
        "en": ("Photo report deleted", "“{title}” was deleted by an administrator. Upload a new report."),
        "ru": ("Фотоотчёт удалён", "«{title}» удалён администратором. Загрузите новый отчёт."),
        "tj": ("Фотоҳисобот нест карда шуд", "«{title}» аз ҷониби маъмур нест карда шуд. Фотоҳисоботи нав бор кунед."),
        "variants": {
            "many": {
                "en": ("Photo reports deleted", "An administrator deleted your photo reports: {count}. Upload new reports."),
                "ru": ("Фотоотчёты удалены", "Администратор удалил ваши фотоотчёты: {count}. Загрузите новые отчёты."),
                "tj": ("Фотоҳисоботҳо нест карда шуданд", "Маъмур фотоҳисоботҳои шуморо нест кард: {count}. Фотоҳисоботҳои нав бор кунед."),
            },
        },
    },
    "donation_assigned": {
        "en": ("Offer assigned to you", "Offer #{donation_id}: arrange the handover."),
        "ru": ("Вам назначено предложение", "Предложение #{donation_id}: согласуйте передачу."),
        "tj": ("Ба шумо пешниҳод таъин шуд", "Пешниҳоди #{donation_id}: супориданро мувофиқа кунед."),
    },
    "donation_status": {
        "variants": {
            "approved": {
                "en": ("Offer approved", "Your offer #{donation_id} was approved."),
                "ru": ("Предложение одобрено", "Ваше предложение #{donation_id} одобрено."),
                "tj": ("Пешниҳод тасдиқ шуд", "Пешниҳоди шумо #{donation_id} тасдиқ шуд."),
            },
            "ready": {
                "en": ("Offer ready", "Your offer #{donation_id} is ready for handover."),
                "ru": ("Готово к передаче", "Ваше предложение #{donation_id} готово к передаче."),
                "tj": ("Барои супоридан омода", "Пешниҳоди шумо #{donation_id} барои супоридан омода аст."),
            },
            "received": {
                "en": ("Offer received", "Your offer #{donation_id} was received. Thank you!"),
                "ru": ("Предложение получено", "Ваше предложение #{donation_id} получено. Спасибо!"),
                "tj": ("Пешниҳод қабул шуд", "Пешниҳоди шумо #{donation_id} қабул шуд. Ташаккур!"),
            },
            "distributed": {
                "en": ("Offer distributed", "Your offer #{donation_id} reached people in need."),
                "ru": ("Предложение распределено", "Ваше предложение #{donation_id} передано нуждающимся."),
                "tj": ("Пешниҳод тақсим шуд", "Пешниҳоди шумо #{donation_id} ба ниёзмандон расид."),
            },
            "cancelled": {
                "en": ("Offer cancelled", "Your offer #{donation_id} was cancelled."),
                "ru": ("Предложение отменено", "Ваше предложение #{donation_id} отменено."),
                "tj": ("Пешниҳод бекор шуд", "Пешниҳоди шумо #{donation_id} бекор шуд."),
            },
        },
        "en": ("Offer status updated", "Your offer #{donation_id} has a new status."),
        "ru": ("Статус предложения обновлён", "У вашего предложения #{donation_id} новый статус."),
        "tj": ("Ҳолати пешниҳод нав шуд", "Пешниҳоди шумо #{donation_id} ҳолати нав дорад."),
    },
    "pet_new": {
        "en": ("New pet report", "Pet report #{pet_id} was posted."),
        "ru": ("Новое объявление о животном", "Опубликовано объявление #{pet_id}."),
        "tj": ("Эълони нав дар бораи ҳайвон", "Эълони #{pet_id} нашр шуд."),
    },
    "pet_match": {
        "en": ("Possible match", "Report #{pet_id} may match your report #{other_id}."),
        "ru": ("Возможное совпадение", "Объявление #{pet_id} может совпадать с вашим #{other_id}."),
        "tj": ("Мувофиқати эҳтимолӣ", "Эълони #{pet_id} шояд бо эълони шумо #{other_id} мувофиқ ояд."),
    },
    "pet_status": {
        "variants": {
            "matched": {
                "en": ("Possible match marked", "Your report #{pet_id} was marked as a possible match."),
                "ru": ("Отмечено совпадение", "Ваше объявление #{pet_id} отмечено как возможное совпадение."),
                "tj": ("Мувофиқат қайд шуд", "Эълони шумо #{pet_id} ҳамчун мувофиқати эҳтимолӣ қайд шуд."),
            },
            "resolved": {
                "en": ("Reunited!", "Your report #{pet_id} is closed: the pet is home."),
                "ru": ("Питомец дома!", "Ваше объявление #{pet_id} закрыто: питомец вернулся."),
                "tj": ("Ҳайвон ба хона баргашт!", "Эълони шумо #{pet_id} пӯшида шуд: ҳайвон баргашт."),
            },
        },
        "en": ("Report status updated", "Your report #{pet_id} has a new status."),
        "ru": ("Статус объявления обновлён", "У вашего объявления #{pet_id} новый статус."),
        "tj": ("Ҳолати эълон нав шуд", "Эълони шумо #{pet_id} ҳолати нав дорад."),
    },
    "broadcast": {
        "en": ("{subject}", "{message}"),
        "ru": ("{subject}", "{message}"),
        "tj": ("{subject}", "{message}"),
    },
    "event_new": {
        "en": ("New event", "{title} — {date} {time}"),
        "ru": ("Новая акция", "{title} — {date} {time}"),
        "tj": ("Чорабинии нав", "{title} — {date} {time}"),
    },
    "task_overdue": {
        "en": ("Overdue request", "Request #{task_id} has been in progress for over 3 hours."),
        "ru": ("Просроченный запрос", "Запрос #{task_id} в работе больше 3 часов."),
        "tj": ("Дархости мӯҳлаташ гузашта", "Дархости #{task_id} зиёда аз 3 соат дар иҷро аст."),
    },
    "task_stale": {
        "en": ("Request waiting too long", "Request #{task_id} has waited {hours} h for a volunteer."),
        "ru": ("Запрос долго ждёт", "Запрос #{task_id} ждёт волонтёра уже {hours} ч."),
        "tj": ("Дархост дароз интизор аст", "Дархости #{task_id} {hours} соат ихтиёриро интизор аст."),
    },
    "task_escalated": {
        "en": ("Request escalated", "Request #{task_id} needs a coordinator's attention."),
        "ru": ("Запрос передан на рассмотрение", "Запрос #{task_id} требует внимания координатора."),
        "tj": ("Дархост барои баррасӣ", "Дархости #{task_id} диққати ҳамоҳангсозро талаб мекунад."),
    },
    "volunteer_pinged": {
        "en": ("Recommended request", "A curator recommends request #{task_id} to you."),
        "ru": ("Рекомендованный запрос", "Куратор рекомендует вам запрос #{task_id}."),
        "tj": ("Дархости тавсияшуда", "Куратор ба шумо дархости #{task_id}-ро тавсия медиҳад."),
    },
    "tracking_stopped": {
        "en": ("Location sharing stopped", "The volunteer stopped sharing their location on request #{task_id}."),
        "ru": ("Передача местоположения остановлена",
               "Волонтёр прекратил передачу местоположения по запросу #{task_id}."),
        "tj": ("Интиқоли ҷойгиршавӣ қатъ шуд",
               "Ихтиёрӣ интиқоли ҷойгиршавиро оид ба дархости #{task_id} қатъ кард."),
    },
    "chat_message": {
        "en": ("New message", "{sender_display}: new message about request #{task_id}."),
        "ru": ("Новое сообщение", "{sender_display}: новое сообщение по запросу #{task_id}."),
        "tj": ("Паёми нав", "{sender_display}: паёми нав оид ба дархости #{task_id}."),
        "variants": {
            # a curator wrote — the parties never see the curator's name
            "staff": {
                "en": ("Message from a curator", "A curator wrote in the chat about request #{task_id}."),
                "ru": ("Сообщение от куратора", "Куратор написал в чате по запросу #{task_id}."),
                "tj": ("Паём аз куратор", "Куратор дар чат оид ба дархости #{task_id} навишт."),
            },
        },
    },
    "chat_reported": {
        "en": ("Chat message reported", "A message in the chat of request #{task_id} was reported."),
        "ru": ("Жалоба на сообщение", "На сообщение в чате запроса #{task_id} поступила жалоба."),
        "tj": ("Шикоят аз паём", "Аз паём дар чати дархости #{task_id} шикоят расид."),
    },
    "chat_flagged": {
        "en": ("Chat: worth a look", "A message in the chat of request #{task_id} may need a look."),
        "ru": ("Чат: стоит проверить", "Сообщение в чате запроса #{task_id} стоит проверить."),
        "tj": ("Чат: бояд санҷид", "Паём дар чати дархости #{task_id}-ро бояд санҷид."),
        "variants": {
            "sms_code": {
                "en": ("Chat: an SMS code is mentioned", "Request #{task_id}: a chat message mentions a code from an SMS or a PIN."),
                "ru": ("Чат: упоминается код из СМС", "Запрос #{task_id}: в чате упоминается код из СМС или ПИН-код."),
                "tj": ("Чат: коди SMS зикр шудааст", "Дархости #{task_id}: дар чат коди SMS ё рамз зикр шудааст."),
            },
            "money": {
                "en": ("Chat: money is mentioned", "Request #{task_id}: a chat message mentions money, a transfer or a bank card."),
                "ru": ("Чат: упоминаются деньги", "Запрос #{task_id}: в чате упоминаются деньги, перевод или банковская карта."),
                "tj": ("Чат: пул зикр шудааст", "Дархости #{task_id}: дар чат пул, интиқол ё корти бонкӣ зикр шудааст."),
            },
            "off_platform": {
                "en": ("Chat: another messenger is suggested", "Request #{task_id}: a chat message suggests moving to another messenger."),
                "ru": ("Чат: предлагают другой мессенджер", "Запрос #{task_id}: в чате предлагают перейти в другой мессенджер."),
                "tj": ("Чат: мессенҷери дигар пешниҳод мешавад", "Дархости #{task_id}: дар чат гузаштан ба мессенҷери дигарро пешниҳод мекунанд."),
            },
            "phone": {
                "en": ("Chat: a phone number was shared", "Request #{task_id}: a chat message contains a phone number."),
                "ru": ("Чат: отправлен номер телефона", "Запрос #{task_id}: в сообщении чата есть номер телефона."),
                "tj": ("Чат: рақами телефон фиристода шуд", "Дархости #{task_id}: дар паёми чат рақами телефон ҳаст."),
            },
        },
    },
    "community_report": {
        "en": ("Community report", "A message in the volunteer community was reported."),
        "ru": ("Жалоба в сообществе", "В сообществе волонтёров пожаловались на сообщение."),
        "tj": ("Шикоят дар ҷамъият", "Дар ҷамъияти ихтиёриён аз паём шикоят карданд."),
    },
    "review_received": {
        "en": ("New review", "You received a review for request #{task_id}."),
        "ru": ("Новый отзыв", "Вы получили отзыв по запросу #{task_id}."),
        "tj": ("Фикри нав", "Шумо оид ба дархости #{task_id} фикр гирифтед."),
    },
    "review_low": {
        "en": ("Low rating", "Request #{task_id}: {stars}/5 for {volunteer_name}."),
        "ru": ("Низкая оценка", "Запрос #{task_id}: {stars}/5 для волонтёра {volunteer_name}."),
        "tj": ("Баҳои паст", "Дархости #{task_id}: {stars}/5 барои ихтиёрӣ {volunteer_name}."),
    },
    "review_request": {
        "en": ("Rate the help", "Please rate the volunteer's help with request #{task_id}."),
        "ru": ("Оцените помощь", "Оцените помощь волонтёра по запросу #{task_id}."),
        "tj": ("Кумакро баҳо диҳед", "Кумаки ихтиёриро оид ба дархости #{task_id} баҳо диҳед."),
    },
    "location_stale": {
        "en": ("Update your location",
               "Your saved location is out of date — update it in your profile; requests are matched by it."),
        "ru": ("Обновите местоположение",
               "Ваше сохранённое местоположение устарело — обновите его в профиле, по нему подбираются запросы."),
        "tj": ("Ҷойгиршавиро навсозӣ кунед",
               "Ҷойгиршавии нигоҳдоштаи шумо кӯҳна шудааст — онро дар профил навсозӣ кунед, дархостҳо аз рӯи он интихоб мешаванд."),
    },
    "achievement_unlocked": {
        "en": ("New achievement", "You unlocked a new badge!"),
        "ru": ("Новое достижение", "Вы получили новый значок!"),
        "tj": ("Дастоварди нав", "Шумо нишони нав гирифтед!"),
    },
}

NONE_TEXT = "—"


class _SafeDict(dict):
    def __missing__(self, key):
        return NONE_TEXT


def _norm_lang(lang):
    return lang if lang in LANGS else DEFAULT_LANG


def _apply_maps(entry, ctx, lang):
    for target, (source, table) in (entry.get("maps") or {}).items():
        labels = LABELS[table][lang] if isinstance(table, str) else table.get(lang, {})
        code = ctx.get(source)
        if code is not None and code in labels:
            ctx[target] = labels[code]
        else:
            ctx.setdefault(target, None)


def _fill_none(entry, ctx, lang):
    fallbacks = (entry.get("fallbacks") or {}).get(lang, {})
    for key, value in list(ctx.items()):
        if value is None or value == "" and key in fallbacks:
            ctx[key] = fallbacks.get(key, NONE_TEXT)


def _fmt(template, ctx):
    try:
        return template.format_map(ctx)
    except (ValueError, IndexError, AttributeError):  # a stray "{" in a template
        return template


def render_channel(type_, lang, context, variant=None):
    """(subject, body) for email/Telegram in ``lang``. Never raises.

    ``variant`` picks ``entry["variants"][variant]`` when the type defines it
    (the base entry's ``maps`` / ``fallbacks`` still apply); otherwise the
    base text is used, so an unknown variant degrades to the plain message."""
    lang = _norm_lang(lang)
    entry = CHANNEL_TEXTS.get(type_) or {}
    texts = (entry.get("variants") or {}).get(variant) if variant else None
    texts = texts or entry
    pair = texts.get(lang) or texts.get(DEFAULT_LANG)
    if not pair:
        return "KhayrKhoh", ""
    ctx = _SafeDict(context or {})
    _apply_maps(entry, ctx, lang)
    _fill_none(entry, ctx, lang)
    # Mapped values may themselves hold placeholders (application_submitted).
    for target in (entry.get("maps") or {}):
        if isinstance(ctx.get(target), str) and "{" in ctx[target]:
            ctx[target] = _fmt(ctx[target], ctx)
    return _fmt(pair[0], ctx), _fmt(pair[1], ctx)


# Telegram-only endings (the main bot understands these commands).
TELEGRAM_SUFFIX = {
    "chat_message": {
        "ru": "\n\n↩️ Ответить прямо здесь: /chat_{task_id}",
        "tj": "\n\n↩️ Ҳамин ҷо ҷавоб диҳед: /chat_{task_id}",
        "en": "\n\n↩️ Reply right here: /chat_{task_id}",
    },
}


def telegram_suffix(type_, lang, context) -> str:
    entry = TELEGRAM_SUFFIX.get(type_)
    if not entry or not (context or {}).get("task_id"):
        return ""
    return _fmt(entry.get(_norm_lang(lang)) or entry[DEFAULT_LANG], _SafeDict(context))


def inapp_keys(type_, variant=None):
    """(title_key, body_key) for a type (+ optional variant known to INAPP_TEXTS)."""
    entry = INAPP_TEXTS.get(type_) or {}
    if variant and variant in (entry.get("variants") or {}):
        base = f"ntf.{type_}.{variant}"
    else:
        base = f"ntf.{type_}"
    return f"{base}.title", f"{base}.body"


def render_inapp(title_key, body_key, params, lang):
    """Server-side fallback of the client-rendered in-app text (no-JS, emails
    of the page). Mirrors what i18n.js shows for the same key + params."""
    lang = _norm_lang(lang)
    table = inapp_key_table(lang)
    ctx = _SafeDict({k: v for k, v in (params or {}).items() if v is not None})
    return _fmt(table.get(title_key, ""), ctx), _fmt(table.get(body_key, ""), ctx)


_KEY_TABLE_CACHE = {}


def inapp_key_table(lang):
    """Flat {i18n key: text} for one language — what goes into i18n.js."""
    lang = _norm_lang(lang)
    if lang not in _KEY_TABLE_CACHE:
        flat = {}
        for type_, entry in INAPP_TEXTS.items():
            title, body = entry[lang]
            flat[f"ntf.{type_}.title"] = title
            flat[f"ntf.{type_}.body"] = body
            for variant, texts in (entry.get("variants") or {}).items():
                vt, vb = texts[lang]
                flat[f"ntf.{type_}.{variant}.title"] = vt
                flat[f"ntf.{type_}.{variant}.body"] = vb
        _KEY_TABLE_CACHE[lang] = flat
    return _KEY_TABLE_CACHE[lang]


# Type names for the notifications page filter and the preferences matrix
# (i18n keys ntf.type.<code>). The EN text is the template fallback.
TYPE_LABELS = {
    "task_new_in_region": {"en": "New requests in my region", "ru": "Новые запросы в моём регионе", "tj": "Дархостҳои нав дар минтақаи ман"},
    "task_assigned": {"en": "Request assigned", "ru": "Назначение запроса", "tj": "Таъини дархост"},
    "task_accepted": {"en": "Request accepted", "ru": "Запрос принят", "tj": "Дархост қабул шуд"},
    "task_stage_changed": {"en": "Volunteer progress", "ru": "Ход работы волонтёра", "tj": "Рафти кори ихтиёрӣ"},
    "task_completed": {"en": "Request completed", "ru": "Запрос выполнен", "tj": "Дархост иҷро шуд"},
    "task_cancelled": {"en": "Request cancelled", "ru": "Запрос отменён", "tj": "Дархост бекор шуд"},
    "sos_new": {"en": "SOS alerts", "ru": "Сигналы SOS", "tj": "Сигналҳои SOS"},
    "sos_nearby": {"en": "SOS alerts in my region", "ru": "Сигналы SOS в моём регионе", "tj": "Сигналҳои SOS дар минтақаи ман"},
    "sos_update": {"en": "My SOS updates", "ru": "Обновления моего SOS", "tj": "Навсозиҳои SOS-и ман"},
    "application_submitted": {"en": "Volunteer applications", "ru": "Заявки волонтёров", "tj": "Аризаҳои ихтиёриён"},
    "application_approved": {"en": "Application approved", "ru": "Заявка одобрена", "tj": "Ариза тасдиқ шуд"},
    "application_rejected": {"en": "Application declined", "ru": "Заявка отклонена", "tj": "Ариза рад шуд"},
    "donation_new": {"en": "New offers of help", "ru": "Новые предложения помощи", "tj": "Пешниҳодҳои нави кумак"},
    "photo_report_approved": {"en": "Photo report approved", "ru": "Фотоотчёт одобрен", "tj": "Фотоҳисобот тасдиқ шуд"},
    "photo_report_rejected": {"en": "Photo report rejected", "ru": "Фотоотчёт отклонён", "tj": "Фотоҳисобот рад шуд"},
    "photo_report_deleted": {"en": "Photo report deleted", "ru": "Фотоотчёт удалён", "tj": "Фотоҳисобот нест карда шуд"},
    "donation_assigned": {"en": "Offers assigned to me", "ru": "Назначенные мне предложения", "tj": "Пешниҳодҳои ба ман таъиншуда"},
    "donation_status": {"en": "My offers' status", "ru": "Статус моих предложений", "tj": "Ҳолати пешниҳодҳои ман"},
    "pet_new": {"en": "New pet reports", "ru": "Новые объявления о животных", "tj": "Эълонҳои нав дар бораи ҳайвонҳо"},
    "pet_match": {"en": "Pet matches", "ru": "Совпадения по животным", "tj": "Мувофиқатҳо оид ба ҳайвонҳо"},
    "pet_status": {"en": "My pet reports", "ru": "Мои объявления о животных", "tj": "Эълонҳои ман дар бораи ҳайвонҳо"},
    "broadcast": {"en": "Broadcasts", "ru": "Рассылки", "tj": "Паёмҳои умумӣ"},
    "event_new": {"en": "Events", "ru": "Акции", "tj": "Чорабиниҳо"},
    "task_overdue": {"en": "Overdue requests", "ru": "Просроченные запросы", "tj": "Дархостҳои мӯҳлаташ гузашта"},
    "task_stale": {"en": "Requests waiting too long", "ru": "Долго ждущие запросы", "tj": "Дархостҳои дер интизоршуда"},
    "task_escalated": {"en": "Escalations", "ru": "Эскалации", "tj": "Эскалатсияҳо"},
    "volunteer_pinged": {"en": "Recommended requests", "ru": "Рекомендованные запросы", "tj": "Дархостҳои тавсияшуда"},
    "tracking_stopped": {"en": "Location sharing stopped", "ru": "Остановка передачи местоположения", "tj": "Қатъи интиқоли ҷойгиршавӣ"},
    "chat_message": {"en": "Chat messages", "ru": "Сообщения в чате", "tj": "Паёмҳои чат"},
    "chat_reported": {"en": "Chat reports", "ru": "Жалобы в чате", "tj": "Шикоятҳо дар чат"},
    "community_report": {"en": "Community reports", "ru": "Жалобы в сообществе", "tj": "Шикоятҳо дар ҷамъият"},
    "chat_flagged": {"en": "Chat safety flags", "ru": "Флаги безопасности в чате", "tj": "Аломатҳои бехатарӣ дар чат"},
    "review_received": {"en": "Reviews", "ru": "Отзывы", "tj": "Фикрҳо"},
    "review_low": {"en": "Low ratings", "ru": "Низкие оценки", "tj": "Баҳоҳои паст"},
    "review_request": {"en": "Requests to rate help", "ru": "Просьбы оценить помощь", "tj": "Дархостҳои баҳодиҳӣ"},
    "location_stale": {"en": "Location reminders", "ru": "Напоминания о местоположении", "tj": "Ёдраскуниҳо дар бораи ҷойгиршавӣ"},
    "achievement_unlocked": {"en": "Achievements", "ru": "Достижения", "tj": "Дастовардҳо"},
}

# Page/UI strings of the notifications + preferences pages (i18n keys).
UI_STRINGS = {
    "ntf.page_title": {"en": "Notifications", "ru": "Уведомления", "tj": "Огоҳиномаҳо"},
    "ntf.page_lede": {"en": "Everything the platform told you, newest first.", "ru": "Всё, о чём вам сообщала платформа, — сначала новые.", "tj": "Ҳама чизе, ки платформа ба шумо хабар дод, аввал навҳо."},
    "ntf.filter_type": {"en": "Type", "ru": "Тип", "tj": "Навъ"},
    "ntf.filter_all_types": {"en": "All types", "ru": "Все типы", "tj": "Ҳама навъҳо"},
    "ntf.filter_unread": {"en": "Unread only", "ru": "Только непрочитанные", "tj": "Танҳо нахондашуда"},
    "ntf.unread_count": {"en": "Unread: {count}", "ru": "Непрочитанных: {count}", "tj": "Нахондашуда: {count}"},
    "ntf.mark_all_read": {"en": "Mark all as read", "ru": "Отметить все как прочитанные", "tj": "Ҳамаро хондашуда қайд кардан"},
    "ntf.mark_read": {"en": "Mark as read", "ru": "Прочитано", "tj": "Хонда шуд"},
    "ntf.open": {"en": "Open", "ru": "Открыть", "tj": "Кушодан"},
    "ntf.new_badge": {"en": "New", "ru": "Новое", "tj": "Нав"},
    "ntf.empty_title": {"en": "No notifications", "ru": "Уведомлений нет", "tj": "Огоҳинома нест"},
    "ntf.empty_text": {"en": "When something needs your attention, it will appear here.", "ru": "Когда что-то потребует вашего внимания, оно появится здесь.", "tj": "Вақте ки чизе диққати шуморо талаб кунад, дар ин ҷо пайдо мешавад."},
    "ntf.preferences": {"en": "Notification settings", "ru": "Настройки уведомлений", "tj": "Танзимоти огоҳиномаҳо"},
    "ntf.prefs_lede": {"en": "Choose where each kind of notification reaches you. Notifications on the site are always on.", "ru": "Выберите, куда приходят уведомления каждого типа. Уведомления на сайте включены всегда.", "tj": "Интихоб кунед, ки ҳар навъи огоҳинома ба куҷо расад. Огоҳиномаҳо дар сайт ҳамеша фаъоланд."},
    "ntf.col_type": {"en": "Notification", "ru": "Уведомление", "tj": "Огоҳинома"},
    "ntf.channel.in_app": {"en": "On the site", "ru": "На сайте", "tj": "Дар сайт"},
    "ntf.channel.email": {"en": "Email", "ru": "Email", "tj": "Email"},
    "ntf.channel.telegram": {"en": "Telegram", "ru": "Telegram", "tj": "Telegram"},
    "ntf.prefs_locked": {"en": "Always on", "ru": "Всегда включено", "tj": "Ҳамеша фаъол"},
    "ntf.prefs_save": {"en": "Save settings", "ru": "Сохранить настройки", "tj": "Нигоҳ доштани танзимот"},
    "ntf.prefs_saved": {"en": "Notification settings saved.", "ru": "Настройки уведомлений сохранены.", "tj": "Танзимоти огоҳиномаҳо нигоҳ дошта шуд."},
    "ntf.back_to_list": {"en": "All notifications", "ru": "Все уведомления", "tj": "Ҳамаи огоҳиномаҳо"},
}


def all_i18n_keys(lang):
    """Every notification-related i18n key → text for one language (the
    snippet frontend-qa adds to static/js/i18n.js)."""
    lang = _norm_lang(lang)
    flat = dict(inapp_key_table(lang))
    flat.update({f"ntf.type.{code}": labels[lang] for code, labels in TYPE_LABELS.items()})
    flat.update({key: labels[lang] for key, labels in UI_STRINGS.items()})
    return flat
