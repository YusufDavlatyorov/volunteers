from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import Profile
from myapp.models import Broadcast, Event, HelpRequest, PhotoReport


User = get_user_model()
PASSWORD = "Volunteer2026!"


REGIONS = ["dushanbe", "sogd", "khatlon", "gbao", "rrp"]


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


class Command(BaseCommand):
    help = "Заполняет проект демонстрационными пользователями, акциями, запросами и фотоотчетами."

    def handle(self, *args, **options):
        settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
        self.media_root = Path("media")
        (self.media_root / "avatars").mkdir(parents=True, exist_ok=True)
        (self.media_root / "reports").mkdir(parents=True, exist_ok=True)

        admin = self._upsert_user("admin_demo", "Админ Generation", "admin@gc.local", "dushanbe", is_superuser=True)
        curators = [
            self._upsert_user(username, full_name, f"{username}@gc.local", region, is_curator=True)
            for username, full_name, region in CURATORS
        ]
        volunteers = [
            self._upsert_user(username, full_name, f"{username}@gc.local", region, is_volunteer=True, bio=bio, age=age)
            for username, full_name, region, bio, age in VOLUNTEERS
        ]
        clients = [
            self._upsert_user(username, full_name, f"{username}@gc.local", region, is_client=True, age=age)
            for username, full_name, region, age in CLIENTS
        ]

        events = self._create_events(curators)
        requests = self._create_requests(clients, volunteers)
        self._create_broadcasts(admin, curators)
        self._create_reports(volunteers, events, requests)

        self.stdout.write(self.style.SUCCESS("Demo data ready."))
        self.stdout.write(f"Users: {User.objects.count()} | Events: {Event.objects.count()} | Requests: {HelpRequest.objects.count()} | Reports: {PhotoReport.objects.count()}")
        self.stdout.write(f"Demo password for all demo users: {PASSWORD}")

    def _upsert_user(self, username, full_name, email, region, is_superuser=False, is_curator=False, is_volunteer=False, is_client=False, bio="", age=None):
        user, _ = User.objects.update_or_create(
            username=username,
            defaults={
                "email": email,
                "region": region,
                "is_superuser": is_superuser,
                "is_staff": is_superuser,
                "is_curator": is_curator,
                "is_volunteer": is_volunteer,
                "is_client": is_client,
                "is_active": True,
                "is_email_verified": True,
            },
        )
        user.set_password(PASSWORD)
        user.save()

        avatar_name = f"avatars/{username}.svg"
        self._write_svg(self.media_root / avatar_name, full_name, region, "avatar")
        profile, _ = Profile.objects.get_or_create(user=user)
        profile.full_name = full_name
        profile.age = age or 30
        profile.bio = bio or "Участник Generation Connect. Готов помогать по своему региону и быстро отвечать на запросы."
        profile.rating = self._rating_for(username, is_volunteer)
        profile.image = avatar_name
        profile.save()
        return user

    def _rating_for(self, username, is_volunteer):
        if not is_volunteer:
            return 0
        return 18 + (sum(ord(char) for char in username) % 72)

    def _create_events(self, curators):
        data = [
            ("Субботник у городского парка", "Команда убирает аллею, красит лавочки и помогает пожилым жителям района.", "dushanbe"),
            ("Посадка деревьев", "Волонтеры высаживают молодые деревья рядом с домом ветеранов.", "sogd"),
            ("Теплый визит", "Кураторы собирают волонтеров для общения, доставки продуктов и проверки бытовых нужд.", "khatlon"),
            ("День цифровой помощи", "Обучаем пожилых людей пользоваться телефоном, мессенджерами и онлайн-услугами.", "rrp"),
            ("Региональная акция заботы", "Общая акция для всех регионов с фотоотчетами и рейтингом участников.", "all"),
        ]
        events = []
        for idx, (title, description, region) in enumerate(data):
            event, _ = Event.objects.update_or_create(
                title=title,
                defaults={
                    "curator": curators[idx % len(curators)],
                    "description": description,
                    "region": region,
                    "date": timezone.now() + timedelta(days=idx + 2),
                    "notifications_sent": True,
                },
            )
            events.append(event)
        return events

    def _create_requests(self, clients, volunteers):
        request_data = [
            ("grocery", "Купить продукты на неделю и занести домой.", "ул. Рудаки 12", "dushanbe", "completed"),
            ("medical", "Сопроводить в поликлинику утром.", "пр. Исмоили Сомони 44", "sogd", "active"),
            ("household", "Помочь переставить мебель и проверить лампочки.", "ул. Бохтар 7", "khatlon", "pending"),
            ("documents", "Помочь заполнить документы и отправить заявление.", "ул. Ленина 19", "gbao", "completed"),
            ("emotional", "Прийти поговорить и помочь настроить телефон.", "мкр. 82, дом 5", "rrp", "pending"),
            ("transport", "Отвезти на рынок и помочь донести сумки.", "ул. Айни 3", "dushanbe", "active"),
            ("grocery", "Купить лекарства по списку и хлеб.", "ул. Сино 15", "sogd", "pending"),
            ("other", "Помочь подготовиться к семейному мероприятию.", "ул. Вахдат 21", "khatlon", "completed"),
        ]
        result = []
        for idx, (help_type, description, address, region, status) in enumerate(request_data):
            client = clients[idx % len(clients)]
            volunteer = volunteers[idx % len(volunteers)] if status in {"active", "completed"} else None
            item, _ = HelpRequest.objects.update_or_create(
                client=client,
                description=description,
                defaults={
                    "volunteer": volunteer,
                    "help_type": help_type,
                    "address": address,
                    "phone": f"+992 90 10{idx:02d} {idx:04d}",
                    "region": region,
                    "status": status,
                    "is_urgent": idx in {1, 5, 6},
                    "accepted_at": timezone.now() - timedelta(hours=idx + 1) if volunteer else None,
                    "completed_at": timezone.now() - timedelta(days=idx) if status == "completed" else None,
                },
            )
            result.append(item)
        return result

    def _create_broadcasts(self, admin, curators):
        Broadcast.objects.update_or_create(
            subject="Большой субботник в эту субботу",
            defaults={
                "sender": curators[0],
                "message": "Берем перчатки, воду и хорошее настроение. Сбор в 09:00 у парка.",
                "region": "dushanbe",
                "sent_count": 12,
            },
        )
        Broadcast.objects.update_or_create(
            subject="Акция помощи пожилым людям",
            defaults={
                "sender": admin,
                "message": "На этой неделе проверяем заявки, помогаем с продуктами и делаем фотоотчеты.",
                "region": "all",
                "sent_count": 25,
            },
        )

    def _create_reports(self, volunteers, events, requests):
        data = [
            ("После субботника", "Команда привела в порядок аллею и собрала 18 мешков мусора.", "dushanbe"),
            ("Доставка продуктов", "Волонтер доставил продукты и помог разобрать покупки дома.", "sogd"),
            ("Новые деревья", "Участники посадили деревья возле дома ветеранов.", "khatlon"),
            ("Цифровая помощь", "Пожилые жители научились отправлять сообщения и звонить по видео.", "rrp"),
            ("Помощь с документами", "Волонтер помог клиенту подготовить заявление и копии документов.", "gbao"),
            ("Теплый визит", "Команда провела вечер общения и проверила бытовые нужды.", "dushanbe"),
            ("Маршрут заботы", "Волонтеры закрыли несколько адресов за один день.", "sogd"),
            ("Акция региона", "Куратор собрал отчет по помощи пожилым людям.", "khatlon"),
        ]
        for idx, (title, description, region) in enumerate(data):
            image_name = f"reports/report_{idx + 1}.svg"
            self._write_svg(self.media_root / image_name, title, region, "report")
            PhotoReport.objects.update_or_create(
                title=title,
                defaults={
                    "author": volunteers[idx % len(volunteers)],
                    "description": description,
                    "image": image_name,
                    "region": region,
                    "event": events[idx % len(events)],
                    "help_request": requests[idx % len(requests)],
                },
            )

    def _write_svg(self, path, title, region, kind):
        colors = {
            "dushanbe": ("#2563eb", "#0f766e"),
            "sogd": ("#0f766e", "#f59e0b"),
            "khatlon": ("#dc2626", "#2563eb"),
            "gbao": ("#7c3aed", "#0f766e"),
            "rrp": ("#f59e0b", "#14213d"),
            "all": ("#2563eb", "#dc2626"),
        }
        c1, c2 = colors.get(region, ("#2563eb", "#0f766e"))
        initials = "".join(part[0] for part in title.split()[:2]).upper()
        label = "PHOTO REPORT" if kind == "report" else "PROFILE"
        path.write_text(
            f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800" viewBox="0 0 1200 800">
<defs>
<linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient>
</defs>
<rect width="1200" height="800" fill="url(#g)"/>
<circle cx="1010" cy="150" r="180" fill="rgba(255,255,255,.18)"/>
<circle cx="190" cy="680" r="240" fill="rgba(255,255,255,.14)"/>
<rect x="80" y="90" width="1040" height="620" rx="34" fill="rgba(255,255,255,.86)"/>
<text x="120" y="170" font-family="Arial" font-size="34" font-weight="700" fill="#607085">{label}</text>
<text x="120" y="390" font-family="Arial" font-size="170" font-weight="900" fill="{c1}">{initials}</text>
<text x="120" y="505" font-family="Arial" font-size="54" font-weight="800" fill="#14213d">{title}</text>
<text x="120" y="580" font-family="Arial" font-size="32" font-weight="700" fill="#607085">Generation Connect · {region}</text>
</svg>""",
            encoding="utf-8",
        )
