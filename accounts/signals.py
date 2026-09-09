import logging

from django.db.models.signals import post_save
from django.dispatch import receiver
from django.core.mail import send_mail
from django.conf import settings
from .models import Users, Profile

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Users)
def create_user_profile(sender, instance, created, **kwargs):
    """Создаём профиль при регистрации"""
    if created:
        Profile.objects.get_or_create(
            user=instance,
            defaults={'rating': 0}
        )


@receiver(post_save, sender=Users)
def send_welcome_email(sender, instance, created, **kwargs):
    """Приветственное письмо"""
    if created and instance.email:
        try:
            send_mail(
                subject='🎉 Добро пожаловать в Generation Connect!',
                message=f'''
Здравствуйте, {instance.username}!

Вы успешно зарегистрировались в Generation Connect.
Ваша роль: {instance.role_display}
Ваш регион: {instance.get_region_display() if instance.region else "Не указан"}

С уважением,
Команда Generation Connect
                ''',
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[instance.email],
                fail_silently=True,
            )
        except Exception as e:
            logger.warning("Welcome email failed for %s: %s", instance.email, e)
