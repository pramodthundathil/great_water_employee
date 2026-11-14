"""Expense claim rules shared by the web portal (and available to the API)."""
from django.db import transaction
from django.db.models import ProtectedError

from expenses.models import ExpenseClaim, ExpenseClaimStatus

# Approved / paid claims are financial records and are never deletable.
LOCKED_STATUSES = (ExpenseClaimStatus.APPROVED_BY_DIRECTOR, ExpenseClaimStatus.PAID)
OWNER_DELETABLE = (
    ExpenseClaimStatus.DRAFT,
    ExpenseClaimStatus.WITHDRAWN,
    ExpenseClaimStatus.REJECTED_BY_ADMIN,
    ExpenseClaimStatus.REJECTED_BY_DIRECTOR,
)


def claim_delete_block_reason(user, claim):
    """Why `user` may not delete `claim`, or None when allowed."""
    if claim.status in LOCKED_STATUSES:
        return "approved / paid claims are kept as financial records"
    if user.is_superuser or user.is_director:
        if claim.employee_id == user.id or user.can_access_branch(claim.branch):
            return None
        return "outside your branch scope"
    if claim.employee_id == user.id:
        if claim.status in OWNER_DELETABLE or claim.can_withdraw:
            return None
        return "already under review"
    return "only the claim owner or the Director can delete it"


def can_bulk_delete_claims(user):
    """Everyone can delete at least their own eligible claims."""
    return user.is_authenticated


def bulk_delete_claims(user, ids):
    """Returns {deleted: [claim_number], skipped: [(claim_number, reason)]}."""
    clean_ids = [int(i) for i in ids if str(i).isdigit()]
    result = {"deleted": [], "skipped": []}
    for claim in ExpenseClaim.objects.filter(pk__in=clean_ids).select_related("branch"):
        reason = claim_delete_block_reason(user, claim)
        if reason:
            result["skipped"].append((claim.claim_number, reason))
            continue
        number = claim.claim_number
        try:
            files = [att.file for att in claim.attachments.all() if att.file]
            with transaction.atomic():
                claim.delete()
            for f in files:  # only remove stored receipts once the DB delete succeeded
                try:
                    f.storage.delete(f.name)
                except OSError:
                    pass
            result["deleted"].append(number)
        except ProtectedError:
            result["skipped"].append((number, "linked to a payment record"))
    return result
