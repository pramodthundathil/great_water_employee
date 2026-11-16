from django.db import migrations


def assign_existing_to_all(apps, schema_editor):
    """Before scoping existed every policy applied to everyone; keep that behaviour."""
    LeavePolicy = apps.get_model("leaves", "LeavePolicy")
    Assignment = apps.get_model("leaves", "LeavePolicyAssignment")
    for policy in LeavePolicy.objects.all():
        if not Assignment.objects.filter(policy=policy).exists():
            Assignment.objects.create(policy=policy, scope="ALL")


class Migration(migrations.Migration):

    dependencies = [
        ("leaves", "0005_leave_policy_assignment"),
    ]

    operations = [
        migrations.RunPython(assign_existing_to_all, migrations.RunPython.noop),
    ]
