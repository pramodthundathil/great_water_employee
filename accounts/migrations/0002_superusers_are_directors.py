from django.db import migrations


def superusers_to_director(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.filter(is_superuser=True).update(role="DIRECTOR", is_global_scope=True, is_staff=True)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(superusers_to_director, migrations.RunPython.noop),
    ]
