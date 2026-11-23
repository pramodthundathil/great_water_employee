from django.db import migrations


def forwards(apps, schema_editor):
    from organization.currencies import default_currency_for_country, normalize_code
    Branch = apps.get_model("organization", "Branch")
    for b in Branch.objects.select_related("organization"):
        org_cur = normalize_code(getattr(b.organization, "currency_code", "")) or "USD"
        b.currency_code = default_currency_for_country(b.country, fallback=org_cur, city=b.city)
        b.save(update_fields=["currency_code"])


class Migration(migrations.Migration):
    dependencies = [("organization", "0002_branch_allowed_currencies_branch_currency_code")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
