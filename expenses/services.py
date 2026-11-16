"""Expense claim rules shared by the web portal (and available to the API)."""
from django.db import transaction
from django.db.models import ProtectedError

from expenses.models import PAYMENT_STAGE, ExpenseApproval, ExpenseClaim, ExpenseClaimStatus

# Audit-trail action written when a recorded disbursement is deleted (voided).
PAYMENT_VOID_ACTION = "PAYMENT_VOIDED"

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


def _delete_claims(user, ids, block_reason):
    """Delete each claim `block_reason(user, claim)` allows. Returns {deleted: [number], skipped: [(number, reason)]}."""
    clean_ids = [int(i) for i in ids if str(i).isdigit()]
    result = {"deleted": [], "skipped": []}
    for claim in ExpenseClaim.objects.filter(pk__in=clean_ids).select_related("branch"):
        reason = block_reason(user, claim)
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


def bulk_delete_claims(user, ids):
    """Returns {deleted: [claim_number], skipped: [(claim_number, reason)]}."""
    return _delete_claims(user, ids, claim_delete_block_reason)


# ---- Reimbursements desk -------------------------------------------------------------------

def approved_claim_delete_block_reason(user, claim):
    """
    Director-approved claims that have not been paid yet are normally locked. The Director (who gave
    final approval) may still remove one from the payment queue; nobody may delete a paid claim.
    """
    if claim.status != ExpenseClaimStatus.APPROVED_BY_DIRECTOR:
        return "not awaiting disbursement"
    if not (user.is_director or user.is_superuser):
        return "only the Director can delete an approved claim"
    if not (user.is_superuser or user.can_manage_expenses_for(claim.branch)):
        return "outside your branch scope"
    return None


def bulk_delete_approved_claims(user, ids):
    """Delete Director-approved, unpaid claims. Same result shape as bulk_delete_claims."""
    return _delete_claims(user, ids, approved_claim_delete_block_reason)


def payment_void_block_reason(user, claim):
    """Why `user` may not delete the payment record of `claim`, or None when allowed."""
    if claim.status != ExpenseClaimStatus.PAID or not hasattr(claim, "payment_record"):
        return "no payment recorded"
    if not (user.is_admin_manager or user.is_superuser):
        return "only an Admin Manager can delete payment records"
    if not (user.is_superuser or user.can_manage_expenses_for(claim.branch)):
        return "outside your branch scope"
    if claim.employee_id == user.id and not user.is_superuser:
        return "another Admin Manager must handle your own claim"
    return None


def bulk_void_payments(user, ids):
    """
    Delete the payment records of paid claims, returning each claim to the disbursement queue.
    The claim and its approvals are kept; the audit trail records what was voided and by whom.
    Returns {voided: [claim_number], skipped: [(claim_number, reason)]}.
    """
    clean_ids = [int(i) for i in ids if str(i).isdigit()]
    result = {"voided": [], "skipped": []}
    claims = ExpenseClaim.objects.filter(pk__in=clean_ids).select_related("branch", "payment_record")
    for claim in claims:
        reason = payment_void_block_reason(user, claim)
        if reason:
            result["skipped"].append((claim.claim_number, reason))
            continue
        payment = claim.payment_record
        summary = (f"Payment record deleted (was {payment.get_payment_mode_display()}, "
                   f"Ref {payment.payment_reference}, paid on {payment.payment_date:%d %b %Y}). "
                   "Claim returned to the disbursement queue.")
        with transaction.atomic():
            payment.delete()
            ExpenseApproval.objects.create(
                claim=claim, stage=PAYMENT_STAGE, approver=user, action=PAYMENT_VOID_ACTION, remarks=summary,
            )
            claim.status = ExpenseClaimStatus.APPROVED_BY_DIRECTOR
            claim.save(update_fields=["status", "updated_at"])
        result["voided"].append(claim.claim_number)
    return result
