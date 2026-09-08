# Stage 9 — replace the money-donation model with in-kind (material) assistance.
#
# The old Donation/Product foundation modelled a cash flow (amount, currency,
# unit_price_snapshot, Product.price). Generation Connect coordinates *physical
# goods* instead, so the money columns are removed and an offer-of-goods field
# set + a physical-handover lifecycle take their place.
#
# No rows are deleted. Existing (demo-only) donations are kept: legacy statuses
# are remapped (confirmed -> approved, fulfilled -> distributed) and their
# timestamps carried across before the old columns drop.

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

REGION_CHOICES = [
    ('dushanbe', 'Душанбе'), ('sogd', 'Согд'), ('khatlon', 'Хатлон'),
    ('gbao', 'ГБАО'), ('rrp', 'РРП'),
]
CATEGORY_CHOICES = [
    ('food', 'Продукты питания'), ('bakery', 'Хлеб и выпечка'), ('clothing', 'Одежда'),
    ('footwear', 'Обувь'), ('hygiene', 'Гигиена'), ('medicine', 'Медикаменты'),
    ('school', 'Школьные принадлежности'), ('blankets', 'Пледы и постельное'),
    ('water', 'Питьевая вода'), ('household', 'Хозяйственные товары'), ('other', 'Другое'),
]
DONOR_TYPE_CHOICES = [('individual', 'Частное лицо'), ('business', 'Организация / бизнес')]
FULFILMENT_CHOICES = [('pickup', 'Забрать у донора'), ('dropoff', 'Донор привезёт сам')]
STATUS_CHOICES = [
    ('pending', 'Ожидает проверки'), ('approved', 'Одобрено'), ('ready', 'Готово к передаче'),
    ('received', 'Получено'), ('distributed', 'Распределено'), ('cancelled', 'Отменено'),
]


def remap_legacy_offers(apps, schema_editor):
    Donation = apps.get_model('myapp', 'Donation')
    for offer in Donation.objects.all():
        changed = False
        if offer.status == 'confirmed':
            offer.status = 'approved'
            changed = True
        elif offer.status == 'fulfilled':
            offer.status = 'distributed'
            changed = True
        if offer.confirmed_at and not offer.approved_at:
            offer.approved_at = offer.confirmed_at
            changed = True
        if offer.fulfilled_at and not offer.distributed_at:
            offer.distributed_at = offer.fulfilled_at
            changed = True
        if offer.product_id and offer.category == 'other':
            offer.category = offer.product.category
            changed = True
        if changed:
            offer.save(update_fields=['status', 'approved_at', 'distributed_at', 'category'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0012_donation_money_field_validators'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='product',
            options={'ordering': ['name'], 'verbose_name': 'Нужный товар', 'verbose_name_plural': 'Нужные товары'},
        ),
        migrations.AlterModelOptions(
            name='donation',
            options={'ordering': ['-created_at'], 'verbose_name': 'Предложение помощи', 'verbose_name_plural': 'Предложения помощи'},
        ),

        # --- Product: needed-items catalogue, no price ---
        migrations.AddField(
            model_name='product',
            name='category',
            field=models.CharField(choices=CATEGORY_CHOICES, default='other', max_length=20),
        ),
        migrations.AddField(
            model_name='product',
            name='unit',
            field=models.CharField(blank=True, max_length=40),
        ),

        # --- Donation: an offer of physical goods ---
        migrations.AddField(
            model_name='donation',
            name='donor_type',
            field=models.CharField(choices=DONOR_TYPE_CHOICES, default='individual', max_length=20),
        ),
        migrations.AddField(
            model_name='donation',
            name='organization_name',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name='donation',
            name='item_name',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name='donation',
            name='category',
            field=models.CharField(choices=CATEGORY_CHOICES, default='other', max_length=20),
        ),
        migrations.AddField(
            model_name='donation',
            name='unit',
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name='donation',
            name='description',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='donation',
            name='fulfilment',
            field=models.CharField(choices=FULFILMENT_CHOICES, default='pickup', max_length=20),
        ),
        migrations.AddField(
            model_name='donation',
            name='location',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='donation',
            name='region',
            field=models.CharField(blank=True, choices=REGION_CHOICES, max_length=100),
        ),
        migrations.AddField(
            model_name='donation',
            name='assigned_volunteer',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='assigned_donations', to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name='donation',
            name='approved_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='donation',
            name='ready_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='donation',
            name='received_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='donation',
            name='distributed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='donation',
            name='status',
            field=models.CharField(choices=STATUS_CHOICES, db_index=True, default='pending', max_length=20),
        ),
        migrations.AlterField(
            model_name='donation',
            name='quantity',
            field=models.PositiveIntegerField(
                default=1,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(100000),
                ],
            ),
        ),

        # --- carry legacy rows across, then drop the money columns ---
        migrations.RunPython(remap_legacy_offers, noop),

        migrations.RemoveField(model_name='product', name='price'),
        migrations.RemoveField(model_name='product', name='currency'),
        migrations.RemoveField(model_name='donation', name='amount'),
        migrations.RemoveField(model_name='donation', name='currency'),
        migrations.RemoveField(model_name='donation', name='unit_price_snapshot'),
        migrations.RemoveField(model_name='donation', name='confirmed_at'),
        migrations.RemoveField(model_name='donation', name='fulfilled_at'),
    ]
