"""Flash messages (django.contrib.messages) in the visitor's UI language.

Views write their confirmations in Russian (the server-rendered fallback, like
every other string). The UI language lives only in the browser (i18n.js), so a
known message is tagged with its i18n.js key — plus ``data-i18n-args`` for the
parts that vary — and i18n.js swaps the text in. An unknown message is simply
shown as written.

    {% load flash %}
    <span{% flash_i18n_attrs message %}>{{ message }}</span>

Every key here exists in all three languages (FlashI18nTests).
"""

import json
import re

from django import template
from django.utils.html import format_html

register = template.Library()

# (exact text or regex with named groups, i18n key). Regexes are anchored.
FLASH_KEYS = [
    # requests
    ("Запрос отправлен. Мы сообщили о нём волонтёрам вашего региона. "
     "Когда волонтёр примет запрос, вы получите уведомление.", "flash.request_created"),
    ("Запрос отправлен. Сейчас никого из волонтёров региона не удалось оповестить — "
     "запрос виден волонтёрам и координаторам, мы сообщим, когда его примут.", "flash.request_created_nobody"),
    ("Запрос отменён. Участникам отправлено уведомление.", "flash.request_cancelled"),
    ("Вы не можете отменить этот запрос.", "flash.cancel_forbidden"),
    ("Запрос уже закрыт или принят волонтёром.", "flash.cancel_not_open"),
    ("Укажите причину отмены.", "flash.cancel_reason"),
    ("Сначала завершите текущий запрос. Один волонтер работает с одним запросом.", "flash.accept_busy"),
    ("Этот запрос уже принят другим волонтером.", "flash.accept_taken"),
    ("Вы приняли запрос. Клиенту отправлено уведомление.", "flash.accepted"),
    ("Этот запрос уже завершён.", "flash.already_completed"),
    ("Запрос завершен, рейтинг обновлен.", "flash.completed"),
    ("Не удалось изменить этап задачи.", "flash.stage_failed"),
    (r"Этап обновлён: (?P<stage>.+)\.", "flash.stage_updated"),
    # access
    ("У вас нет доступа к этой странице", "flash.no_access"),
    ("У вас нет доступа к этому действию.", "flash.no_access_action"),
    # account
    ("Аккаунт создан. Заявка на волонтерство отправлена на рассмотрение администратору.", "flash.registered_volunteer"),
    ("Аккаунт создан. Проверьте email для подтверждения.", "flash.registered_client"),
    (r"Добро пожаловать, (?P<name>.+)!", "flash.welcome"),
    ("Вы вышли из системы", "flash.logged_out"),
    # Continue with Google (accounts/views.py google_*)
    ("Вход через Google отменён", "flash.google_cancelled"),
    ("Сессия входа через Google истекла. Попробуйте ещё раз.", "flash.google_expired"),
    ("Не удалось войти через Google. Попробуйте ещё раз.", "flash.google_failed"),
    ("Этот email уже привязан к другому Google-аккаунту. Войдите по паролю.", "flash.google_email_taken"),
    ("Аккаунт отключён. Обратитесь к администратору.", "flash.account_disabled"),
    ("Email этого Google-аккаунта не подтверждён. Зарегистрируйтесь обычным способом.", "flash.google_unverified"),
    ("Аккаунт создан.", "flash.registered_google"),
    ("Профиль обновлен", "flash.profile_saved"),
    ("Слишком много попыток. Попробуйте снова через несколько минут.", "flash.too_many"),
    ("Если email найден, ссылка отправлена.", "flash.reset_sent"),
    ("Ссылка недействительна или устарела", "flash.link_invalid"),
    ("Пароль изменен", "flash.password_changed"),
    ("Email подтвержден", "flash.email_confirmed"),
    ("Telegram отвязан от аккаунта.", "flash.telegram_unlinked"),
    ("Настройки уведомлений сохранены.", "flash.notif_saved"),
    # volunteer application
    ("Повторную заявку можно подать только после отклонения.", "flash.reapply_only_rejected"),
    ("Заявка отправлена повторно.", "flash.reapplied"),
    ("Заявка уже одобрена.", "flash.app_already_approved"),
    ("Заявка уже отклонена.", "flash.app_already_rejected"),
    (r"Заявка пользователя (?P<name>.+) одобрена\.", "flash.app_approved"),
    (r"Заявка пользователя (?P<name>.+) отклонена\.", "flash.app_rejected"),
    # staff tools
    (r"Проверено\. Просроченных запросов: (?P<n>\d+)\.", "flash.overdue_checked"),
    (r"Акция создана\. Уведомлений отправлено: (?P<n>\d+)\.", "flash.event_created"),
    (r"Сообщение отправлено\. Получателей: (?P<n>\d+)\.", "flash.broadcast_sent"),
    ("Фотоотчеты может публиковать только волонтер, куратор или админ.", "flash.reports_forbidden"),
    ("Фотоотчёт отправлен на проверку координатору.", "flash.report_added"),
    # photo-report review (myapp/views_photo_reports.py)
    ("Запрос завершён. Загрузите фотоотчёт — баллы начислятся после его проверки.", "flash.completed_report_pending"),
    ("Фотоотчёт одобрен.", "flash.report_approved"),
    ("Фотоотчёт отклонён, волонтёр получил уведомление.", "flash.report_rejected"),
    ("Укажите причину отклонения.", "flash.report_reason_required"),
    ("Этот фотоотчёт уже проверен.", "flash.report_already_reviewed"),
    ("Фотоотчёт удалён.", "flash.report_deleted"),
    ("Фотоотчёт удалён. Баллы за запрос отменены — он снова ждёт фотоотчёт.", "flash.report_deleted_reward"),
    ("Этот фотоотчёт уже удалён.", "flash.report_not_found"),
    (r"Удалено фотоотчётов: (?P<n>\d+)\. Баллы отменены по запросам: (?P<r>\d+)\.", "flash.reports_bulk_deleted"),
    ("Выберите хотя бы один фотоотчёт.", "flash.reports_bulk_none"),
    # emergency
    ("У вас уже есть открытый сигнал по этому запросу.", "flash.sos_exists"),
    ("Сигнал уже обрабатывается…", "flash.sos_processing"),
    ("Сигнал отправлен. Куратор и администратор уведомлены.", "flash.sos_sent"),
    ("Сигнал уже закрыт.", "flash.sos_closed"),
    ("Уведомление отправлено повторно.", "flash.sos_realerted"),
    ("Статус сигнала обновлён.", "flash.sos_updated"),
    ("Это действие недоступно для текущего статуса сигнала.", "flash.sos_bad_action"),
    ("Неизвестное действие.", "flash.unknown_action"),
    # donations
    ("Спасибо! Предложение записано и ожидает проверки координатором.", "flash.donation_created"),
    ("Спасибо! Пожертвование отправлено — координатор свяжется с вами.", "flash.donation_goods_created"),
    ("Волонтёр не найден.", "flash.volunteer_not_found"),
    ("Сейчас нельзя назначить волонтёра для этого предложения.", "flash.donation_cant_assign"),
    ("Волонтёр обновлён.", "flash.donation_volunteer_set"),
    ("Это действие недоступно для текущего статуса предложения.", "flash.donation_bad_action"),
    ("Статус предложения обновлён.", "flash.donation_updated"),
    # pets
    ("Объявление опубликовано. Координаторы уведомлены.", "flash.pet_published"),
    ("Закрытое объявление изменить нельзя.", "flash.pet_closed"),
    ("Объявление обновлено.", "flash.pet_updated"),
    ("Объявление удалено.", "flash.pet_deleted"),
    ("Это действие доступно только координаторам.", "flash.staff_only"),
    ("Это действие недоступно для текущего статуса объявления.", "flash.pet_bad_action"),
    ("Статус объявления обновлён.", "flash.pet_status_updated"),
    # reviews
    ("Оставить отзыв можно после завершения запроса.", "flash.review_after_completion"),
    ("Срок редактирования отзыва истёк.", "flash.review_edit_expired"),
    ("Спасибо! Ваш отзыв сохранён.", "flash.review_saved"),
    ("Отзыв обновлён.", "flash.review_updated"),
    ("Отзыв снова виден.", "flash.review_shown"),
    ("Отзыв скрыт.", "flash.review_hidden"),
]

_EXACT = {text: key for text, key in FLASH_KEYS if "(?P<" not in text}
_PATTERNS = [(re.compile(text + r"\Z"), key) for text, key in FLASH_KEYS if "(?P<" in text]


def flash_key(text):
    """(i18n key, args dict) for a known message, else (None, None)."""
    text = str(text)
    if text in _EXACT:
        return _EXACT[text], None
    for pattern, key in _PATTERNS:
        match = pattern.match(text)
        if match:
            return key, match.groupdict()
    return None, None


@register.simple_tag
def flash_i18n_attrs(message):
    key, args = flash_key(message)
    if not key:
        return ""
    if args:
        return format_html(' data-i18n="{}" data-i18n-args="{}"', key, json.dumps(args, ensure_ascii=False))
    return format_html(' data-i18n="{}"', key)


@register.filter
def flash_level(message):
    """'error' / 'warning' / 'success' / 'info' — the message's own level."""
    tags = str(getattr(message, "level_tag", "") or "")
    return tags if tags in ("error", "warning", "success", "info") else "info"
