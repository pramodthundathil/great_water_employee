"""
Add the missing "Disbursed" audit-trail entry for claims that were paid before
record_payment() started writing one. Idempotent: skips claims that already have it.
"""
from datetime import date

from django.db import migrations

PAYMENT_STAGE = "PAYMENT"
PAYMENT_ACTION = "PAID"
MODE_LABELS = {
    "BANK_TRANSFER": "Bank Transfer",
    "CHEQUE": "Cheque",
    "CASH": "Cash",
    "CORPORATE_CARD": "Corporate Card",
}


def _remarks(p):
    parts = [f"Disbursed via {MODE_LABELS.get(p.payment_mode, p.payment_mode)}"]
    if p.payment_reference:
        parts.append(f"Ref {p.payment_reference}")
    if isinstance(p.payment_date, date):
        parts.append(f"Paid on {p.payment_date:%d %b %Y}")
    text = " · ".join(parts)
    if p.remarks:
        text += f" — {p.remarks}"
    return text


def backfill(apps, schema_editor):
    Payment = apps.get_model("expenses", "ReimbursementPayment")
    Approval = apps.get_model("expenses", "ExpenseApproval")
    for p in Payment.objects.select_related("claim").iterator():
        if Approval.objects.filter(claim_id=p.claim_id, stage=PAYMENT_STAGE).exists():
            continue
        entry = Approval.objects.create(
            claim_id=p.claim_id, stage=PAYMENT_STAGE, approver_id=p.recorded_by_id,
            action=PAYMENT_ACTION, remarks=_remarks(p),
        )
        # created_at is auto_now_add; date the entry to when the payment was actually recorded.
        Approval.objects.filter(pk=entry.pk).update(created_at=p.created_at)


def remove(apps, schema_editor):
    apps.get_model("expenses", "ExpenseApproval").objects.filter(stage=PAYMENT_STAGE).delete()


class Migration(migrations.Migration):
    dependencies = [("expenses", "0001_initial")]
    operations = [migrations.RunPython(backfill, remove)]
