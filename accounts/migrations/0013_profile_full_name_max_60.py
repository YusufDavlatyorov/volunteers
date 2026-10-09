"""Profile.full_name: a 60-character column (was 255 + a 60-char validator).

Shortening a column is the one step here that could lose data: PostgreSQL
refuses ``ALTER ... TYPE varchar(60)`` while a longer value exists, SQLite
would silently keep it. So the migration checks first and stops with a clear
message (count + profile ids, never the names) instead of truncating anything —
shorten those names by hand, then run ``migrate`` again.
"""

from django.db import migrations, models
from django.db.models.functions import Length

LIMIT = 60


def refuse_longer_names(apps, schema_editor):
    Profile = apps.get_model("accounts", "Profile")
    too_long = Profile.objects.annotate(n=Length("full_name")).filter(n__gt=LIMIT)
    count = too_long.count()
    if count:
        ids = list(too_long.order_by("pk").values_list("pk", flat=True)[:50])
        raise RuntimeError(
            f"accounts 0013: {count} profile(s) have a full name longer than {LIMIT} "
            f"characters (profile ids: {ids}). Shorten them first; nothing was changed."
        )


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0012_image_variants"),
    ]

    operations = [
        migrations.RunPython(refuse_longer_names, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="profile",
            name="full_name",
            field=models.CharField(blank=True, max_length=60),
        ),
    ]
