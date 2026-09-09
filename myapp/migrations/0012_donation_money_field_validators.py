"""Stage 7: add field-level Min/Max validators to Donation.amount and
Donation.quantity.

Schema no-op — the column types are unchanged. This only records that the
money/quantity bounds (previously enforced only in Donation.clean() and the
service/form layer) now also run as field validators, so the rule holds on any
full_clean() path, including a straight-through Django-admin edit.
"""

from decimal import Decimal

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('myapp', '0011_petreport'),
    ]

    operations = [
        migrations.AlterField(
            model_name='donation',
            name='amount',
            field=models.DecimalField(
                decimal_places=2,
                max_digits=12,
                validators=[
                    django.core.validators.MinValueValidator(Decimal('0.01')),
                    django.core.validators.MaxValueValidator(Decimal('1000000.00')),
                ],
            ),
        ),
        migrations.AlterField(
            model_name='donation',
            name='quantity',
            field=models.PositiveIntegerField(
                default=1,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(999),
                ],
            ),
        ),
    ]
