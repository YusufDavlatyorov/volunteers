import shutil
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import Profile
from myapp.models import Broadcast, EmergencyReport, Event, HelpRequest, PhotoReport


User = get_user_model()
PASSWORD = "Volunteer2026!"


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

# Real, licensed photographs bundled under static/images/ (see static/images/CREDITS.md).
# Reused across the demo photo reports below instead of generating fake placeholder art.
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


class Command(BaseCommand):
    help = "Заполняет проект демонстрационными пользователями, акциями, запросами и фотоотчетами."

    def handle(self, *args, **options):
        settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
        self.media_root = Path(settings.MEDIA_ROOT)
        self.static_images_root = Path(settings.BASE_DIR) / "static" / "images"
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
        self._create_emergencies(requests, admin)

        self.stdout.write(self.style.SUCCESS("Demo data ready."))
        self.stdout.write(f"Users: {User.objects.count()} | Events: {Event.objects.count()} | Requests: {HelpRequest.objects.count()} | Reports: {PhotoReport.objects.count()} | Emergencies: {EmergencyReport.objects.count()}")
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

        # No avatar image is generated here: demo users show the same initials
        # fallback (see .avatar-fallback in style.css) that real users without an
        # uploaded photo get. This avoids fake generated "person" avatars. Explicitly
        # clear the field too, so re-running this command on an existing dev database
        # drops any stale reference to the old generated SVG avatars.
        profile, _ = Profile.objects.get_or_create(user=user)
        profile.full_name = full_name
        profile.age = age or 30
        profile.bio = bio or "Участник Generation Connect. Готов помогать по своему региону и быстро отвечать на запросы."
        profile.rating = self._rating_for(username, is_volunteer)
        profile.image = ""
        # Give volunteers and clients a location near their region centre so the
        # operations map is populated. Curators/admin are coordinators, not
        # field staff — no location.
        if is_volunteer or is_client:
            profile.latitude, profile.longitude = _coords_for(region, username)
            profile.location_updated_at = timezone.now()
        if is_volunteer:
            profile.availability_status = _AVAILABILITY_CYCLE[
                sum(ord(c) for c in username) % len(_AVAILABILITY_CYCLE)
            ]
            # A deterministic 2-3 skill spread so the CRM matching shows real
            # skill fit rather than everyone being "no info".
            h = sum(ord(c) for c in username)
            profile.skills = sorted({_HELP_TYPES[(h + i) % len(_HELP_TYPES)] for i in range(3)})
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
        # idx 1 (active medical) -> emergency + overdue; idx 5,6 -> high.
        priority_by_idx = {1: "emergency", 5: "high", 6: "high"}
        result = []
        for idx, (help_type, description, address, region, status) in enumerate(request_data):
            client = clients[idx % len(clients)]
            volunteer = volunteers[idx % len(volunteers)] if status in {"active", "completed"} else None
            priority = priority_by_idx.get(idx, "normal")
            accepted_at = None
            if volunteer:
                # Push the emergency active task past the 3h overdue threshold.
                hours_ago = 5 if idx == 1 else idx + 1
                accepted_at = timezone.now() - timedelta(hours=hours_ago)
            lat, lng = _coords_for(region, f"{client.username}-{description}")
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
                    "priority": priority,
                    "is_urgent": priority != "normal",
                    "latitude": lat,
                    "longitude": lng,
                    "accepted_at": accepted_at,
                    "completed_at": timezone.now() - timedelta(days=idx) if status == "completed" else None,
                },
            )
            result.append(item)

        # Make the pending set deterministic for the stale-request demo: exactly
        # one request sits past STALE_PENDING_THRESHOLD (48h), the rest are fresh.
        # created_at is auto_now_add, so it must be set with a raw UPDATE.
        pending = [r for r in result if r.status == "pending"]
        for offset, task in enumerate(pending):
            new_created = (
                timezone.now() - timedelta(days=3) if offset == 0
                else timezone.now() - timedelta(hours=offset + 1)
            )
            HelpRequest.objects.filter(pk=task.pk).update(
                created_at=new_created, stale_alert_sent=False
            )
        return result

    def _create_emergencies(self, requests, admin):
        """One open + one resolved SOS on active tasks, so the CRM isn't empty."""
        active = [r for r in requests if r.status == "active" and r.volunteer]
        if not active:
            return
        open_task = active[0]
        EmergencyReport.objects.get_or_create(
            help_request=open_task,
            volunteer=open_task.volunteer,
            status=EmergencyReport.STATUS_OPEN,
            defaults={
                "reason": "Клиент не открывает дверь, соседи говорят о шуме внутри.",
                "region": open_task.region,
                "latitude": open_task.latitude,
                "longitude": open_task.longitude,
                "notified_at": timezone.now(),
            },
        )
        if len(active) > 1:
            done_task = active[1]
            EmergencyReport.objects.get_or_create(
                help_request=done_task,
                volunteer=done_task.volunteer,
                status=EmergencyReport.STATUS_RESOLVED,
                defaults={
                    "reason": "Плохое самочувствие клиента.",
                    "region": done_task.region,
                    "notified_at": timezone.now() - timedelta(hours=2),
                    "acknowledged_at": timezone.now() - timedelta(hours=2),
                    "acknowledged_by": admin,
                    "resolved_at": timezone.now() - timedelta(hours=1),
                    "resolved_by": admin,
                    "resolution_note": "Вызвали 103, клиент осмотрен, всё в порядке.",
                },
            )

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
            source = self.static_images_root / REPORT_PHOTOS[idx % len(REPORT_PHOTOS)]
            image_name = f"reports/report_{idx + 1}{source.suffix}"
            shutil.copyfile(source, self.media_root / image_name)
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
