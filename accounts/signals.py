import logging

from django.db.models.signals import post_save
from django.dispatch import receiver
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
        from myapp.notifications import queue_email

        try:
            queue_email(  # background delivery (myapp/tasks.py)
                instance.email,
                '🎉 Добро пожаловать в KhayrKhoh!',
                f'''
Здравствуйте, {instance.username}!

Вы успешно зарегистрировались в KhayrKhoh.
Ваша роль: {instance.role_display}
Ваш регион: {instance.get_region_display() if instance.region else "Не указан"}

С уважением,
Команда KhayrKhoh
                ''',
            )
        except Exception as e:
            logger.warning("Welcome email failed for %s: %s", instance.email, e)
