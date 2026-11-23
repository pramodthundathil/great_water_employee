from decimal import Decimal

from django.db import migrations


def forwards(apps, schema_editor):
    """Each existing claim gets its branch currency and one item line mirroring the claimed amount."""
    ExpenseClaim = apps.get_model("expenses", "ExpenseClaim")
    ExpenseItem = apps.get_model("expenses", "ExpenseItem")
    for claim in ExpenseClaim.objects.select_related("branch").iterator():
        if not claim.currency_code:
            claim.currency_code = claim.branch.currency_code
            claim.save(update_fields=["currency_code"])
        if ExpenseItem.objects.filter(claim_id=claim.id).exists():
            continue
        claimed = claim.requested_amount if claim.requested_amount is not None else claim.amount
        ExpenseItem.objects.create(
            claim_id=claim.id, position=0, category_id=claim.category_id, expense_date=claim.expense_date,
            description=(claim.description or "")[:255], currency_code=claim.currency_code,
            original_amount=claimed, exchange_rate=Decimal("1"), converted_amount=claimed,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("expenses", "0004_expenseclaim_currency_code_expenseitem"),
        ("organization", "0003_backfill_branch_currency"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
