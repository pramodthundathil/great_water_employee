from django.db import models, transaction
from django.utils import timezone
from django.core.exceptions import ValidationError
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation


class ExpenseClaimStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    UNDER_ADMIN_REVIEW = "UNDER_ADMIN_REVIEW", "Under Admin Review"
    UNDER_DIRECTOR_REVIEW = "UNDER_DIRECTOR_REVIEW", "Under Director Review"
    APPROVED_BY_DIRECTOR = "APPROVED_BY_DIRECTOR", "Approved (Awaiting Payment)"
    PAID = "PAID", "Paid / Reimbursed"
    REJECTED_BY_ADMIN = "REJECTED_BY_ADMIN", "Rejected by Admin Manager"
    REJECTED_BY_DIRECTOR = "REJECTED_BY_DIRECTOR", "Rejected by Director"
    WITHDRAWN = "WITHDRAWN", "Withdrawn"


class PaymentMode(models.TextChoices):
    BANK_TRANSFER = "BANK_TRANSFER", "Bank Transfer"
    CHEQUE = "CHEQUE", "Cheque"
    CASH = "CASH", "Cash"
    CORPORATE_CARD = "CORPORATE_CARD", "Corporate Card"


# Audit-trail entry written when the Admin Manager disburses a reimbursement.
PAYMENT_STAGE = "PAYMENT"
PAYMENT_ACTION = "PAID"


def payment_trail_remarks(payment):
    """One-line summary of a disbursement for the claim's audit trail."""
    parts = [f"Disbursed {payment.claim.amount:,.2f} via {payment.get_payment_mode_display()}"]
    if payment.payment_reference:
        parts.append(f"Ref {payment.payment_reference}")
    paid_on = payment.payment_date
    if isinstance(paid_on, str):  # views pass the raw form / JSON value through
        try:
            paid_on = date.fromisoformat(paid_on[:10])
        except ValueError:
            pass
    parts.append(f"Paid on {paid_on:%d %b %Y}" if hasattr(paid_on, "strftime") else f"Paid on {paid_on}")
    text = " · ".join(parts)
    if payment.remarks:
        text += f" — {payment.remarks}"
    return text


def review_message(claim, recorded):
    """Human-readable outcome of an expense review, shared by the API and the web portal."""
    if recorded == "REJECTED":
        return f"Claim {claim.claim_number} rejected."
    if recorded == "PARTIALLY_APPROVED":
        return (f"Claim {claim.claim_number} partially approved: "
                f"{claim.amount:,.2f} of {claim.claimed_amount:,.2f} claimed.")
    return f"Claim {claim.claim_number} approved for {claim.amount:,.2f}."


class ExpenseCategory(models.Model):
    organization = models.ForeignKey(
        "organization.Organization",
        on_delete=models.CASCADE,
        related_name="expense_categories"
    )
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=50, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Expense Category"
        verbose_name_plural = "Expense Categories"
        ordering = ["name"]

    def __str__(self):
        return self.name


class ExpenseClaim(models.Model):
    claim_number = models.CharField(max_length=50, unique=True, db_index=True)
    employee = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="expense_claims"
    )
    branch = models.ForeignKey(
        "organization.Branch",
        on_delete=models.PROTECT,
        related_name="expense_claims"
    )
    category = models.ForeignKey(
        ExpenseCategory,
        on_delete=models.PROTECT,
        related_name="expense_claims"
    )
    expense_date = models.DateField(db_index=True)
    amount = models.DecimalField(
        max_digits=12, decimal_places=2,
        help_text="Current payable amount. Reduced when an approver grants only part of the claim."
    )
    requested_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Amount originally claimed by the employee (kept when a partial amount is approved)."
    )
    description = models.TextField()
    project_reference = models.CharField(max_length=100, blank=True)
    status = models.CharField(
        max_length=35,
        choices=ExpenseClaimStatus.choices,
        default=ExpenseClaimStatus.DRAFT,
        db_index=True
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Expense Claim"
        verbose_name_plural = "Expense Claims"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["employee", "status"]),
            models.Index(fields=["expense_date"]),
        ]

    def __str__(self):
        return f"{self.claim_number} - {self.employee.employee_id} ({self.amount})"

    def save(self, *args, **kwargs):
        if not self.claim_number:
            date_prefix = timezone.now().strftime("%Y%m")
            unique_part = uuid.uuid4().hex[:6].upper()
            self.claim_number = f"EXP-{date_prefix}-{unique_part}"
        if self.requested_amount is None and self.amount is not None:
            self.requested_amount = self.amount
        super().save(*args, **kwargs)

    # ------------------------------------------------------------------
    # Amount helpers
    # ------------------------------------------------------------------
    @property
    def claimed_amount(self):
        """What the employee asked for."""
        return self.requested_amount if self.requested_amount is not None else self.amount

    @property
    def is_partially_approved(self):
        return self.requested_amount is not None and self.amount < self.requested_amount

    def set_claimed_amount(self, value):
        """Employee edit: the claimed and payable amounts move together."""
        self.amount = value
        self.requested_amount = value

    @staticmethod
    def first_stage_status(employee):
        """
        Admin Managers' own claims skip the Admin Manager stage and go straight to the Director;
        everyone else starts with Admin Manager review.
        """
        if getattr(employee, "role", "") == "ADMIN_MANAGER":
            return ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW
        return ExpenseClaimStatus.UNDER_ADMIN_REVIEW

    @property
    def skips_admin_stage(self):
        return self.first_stage_status(self.employee) == ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW

    @transaction.atomic
    def submit(self):
        """
        Submits claim. Proof receipt is mandatory before submission.
        """
        if self.status not in [ExpenseClaimStatus.DRAFT, ExpenseClaimStatus.REJECTED_BY_ADMIN, ExpenseClaimStatus.REJECTED_BY_DIRECTOR]:
            raise ValidationError(f"Claim in status {self.status} cannot be submitted.")

        if not self.attachments.exists():
            raise ValidationError("At least one supporting receipt/proof is mandatory before submission.")

        # A resubmitted claim is reviewed again for the full amount the employee claimed.
        self.amount = self.claimed_amount
        self.requested_amount = self.amount
        self.status = self.first_stage_status(self.employee)
        self.submitted_at = timezone.now()
        self.save()

    @transaction.atomic
    def withdraw(self, user):
        """
        Allows employee to withdraw eligible claim (before any reviewer has acted on it).
        """
        if self.employee != user and not user.is_superuser:
            raise ValidationError("Only the claim owner can withdraw this claim.")
        if not self.can_withdraw:
            raise ValidationError(f"Claims in status {self.status} cannot be withdrawn.")
        self.status = ExpenseClaimStatus.WITHDRAWN
        self.save()

    @property
    def can_withdraw(self):
        if self.status in [ExpenseClaimStatus.DRAFT, ExpenseClaimStatus.UNDER_ADMIN_REVIEW]:
            return True
        # Admin Manager's own claim: Director review is its first stage.
        return self.status == ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW and self.skips_admin_stage

    def _parse_approved_amount(self, approved_amount):
        if approved_amount in (None, ""):
            return None
        try:
            value = Decimal(str(approved_amount)).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            raise ValidationError("Approved amount must be a number.")
        if value <= 0:
            raise ValidationError("Approved amount must be greater than zero. Reject the claim instead.")
        if value > self.amount:
            raise ValidationError(
                f"Approved amount cannot exceed the amount currently under review ({self.amount:,.2f})."
            )
        return value

    def _review(self, *, stage, expected_status, wrong_status_msg, approver, action, remarks,
                approved_amount, next_status, rejected_status, default_remarks):
        if self.status != expected_status:
            raise ValidationError(wrong_status_msg)
        if approver == self.employee and not approver.is_superuser:
            raise ValidationError("You cannot review your own expense claim.")

        action = (action or "").upper()
        if action == "PARTIALLY_APPROVED":
            if approved_amount in (None, ""):
                raise ValidationError("Enter the approved amount for a partial approval.")
            action = "APPROVED"

        if action == "APPROVED":
            value = self._parse_approved_amount(approved_amount)
            partial = value is not None and value < self.amount
            if partial and not (remarks or "").strip():
                raise ValidationError("Remarks are mandatory when approving less than the amount claimed.")
            if partial:
                self.amount = value
            recorded = "PARTIALLY_APPROVED" if partial else "APPROVED"
            ExpenseApproval.objects.create(
                claim=self, stage=stage, approver=approver, action=recorded,
                remarks=remarks or default_remarks,
                approved_amount=self.amount if partial else None,
            )
            self.status = next_status
        elif action == "REJECTED":
            if not (remarks or "").strip():
                raise ValidationError("Remarks are mandatory when rejecting a claim.")
            recorded = "REJECTED"
            ExpenseApproval.objects.create(claim=self, stage=stage, approver=approver, action="REJECTED", remarks=remarks)
            self.status = rejected_status
        else:
            raise ValidationError(f"Invalid review action: {action}")
        self.save()
        return recorded

    @transaction.atomic
    def admin_review(self, approver, action, remarks="", approved_amount=None):
        """
        Stage 1: Admin Manager review (full or partial amount).
        """
        return self._review(
            stage="ADMIN_MANAGER",
            expected_status=ExpenseClaimStatus.UNDER_ADMIN_REVIEW,
            wrong_status_msg="Claim is not currently under Admin Review.",
            approver=approver, action=action, remarks=remarks, approved_amount=approved_amount,
            next_status=ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW,
            rejected_status=ExpenseClaimStatus.REJECTED_BY_ADMIN,
            default_remarks="Approved for Director review",
        )

    @transaction.atomic
    def director_review(self, approver, action, remarks="", approved_amount=None):
        """
        Stage 2: Director final approval (full or partial amount) or rejection.
        """
        return self._review(
            stage="DIRECTOR",
            expected_status=ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW,
            wrong_status_msg="Claim is not currently under Director Review.",
            approver=approver, action=action, remarks=remarks, approved_amount=approved_amount,
            next_status=ExpenseClaimStatus.APPROVED_BY_DIRECTOR,
            rejected_status=ExpenseClaimStatus.REJECTED_BY_DIRECTOR,
            default_remarks="Final approval granted",
        )

    @transaction.atomic
    def record_payment(self, recorded_by, payment_date, payment_mode, payment_reference, remarks=""):
        """
        Stage 3: Admin Manager records payment for Director-approved claim.
        """
        if self.status != ExpenseClaimStatus.APPROVED_BY_DIRECTOR:
            raise ValidationError("Only claims with final Director approval are eligible for reimbursement payment.")

        if hasattr(self, "payment_record") and self.payment_record:
            raise ValidationError("Payment has already been recorded for this claim.")

        payment = ReimbursementPayment.objects.create(
            claim=self,
            recorded_by=recorded_by,
            payment_date=payment_date,
            payment_mode=payment_mode,
            payment_reference=payment_reference,
            remarks=remarks
        )
        # Disbursement is part of the claim's audit trail, alongside the approvals.
        ExpenseApproval.objects.create(
            claim=self,
            stage=PAYMENT_STAGE,
            approver=recorded_by,
            action=PAYMENT_ACTION,
            remarks=payment_trail_remarks(payment),
        )
        self.status = ExpenseClaimStatus.PAID
        self.save()
        return payment


class ExpenseAttachment(models.Model):
    claim = models.ForeignKey(
        ExpenseClaim,
        on_delete=models.CASCADE,
        related_name="attachments"
    )
    file = models.FileField(upload_to="receipts/%Y/%m/")
    original_filename = models.CharField(max_length=255)
    file_type = models.CharField(max_length=100, blank=True)
    file_size = models.PositiveIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Expense Attachment"
        verbose_name_plural = "Expense Attachments"

    def __str__(self):
        return f"Attachment for {self.claim.claim_number}: {self.original_filename}"


class ExpenseApproval(models.Model):
    claim = models.ForeignKey(
        ExpenseClaim,
        on_delete=models.CASCADE,
        related_name="approval_history"
    )
    stage = models.CharField(max_length=30)  # ADMIN_MANAGER, DIRECTOR
    approver = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="expense_approvals"
    )
    action = models.CharField(max_length=30)  # APPROVED, PARTIALLY_APPROVED, REJECTED, PAID
    remarks = models.TextField(blank=True)
    approved_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Set when this approver granted less than the amount under review."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Expense Approval"
        verbose_name_plural = "Expense Approvals"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.claim.claim_number} - {self.stage} - {self.action} by {self.approver.employee_id}"


class ReimbursementPayment(models.Model):
    claim = models.OneToOneField(
        ExpenseClaim,
        on_delete=models.PROTECT,
        related_name="payment_record"
    )
    recorded_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="recorded_payments"
    )
    payment_date = models.DateField(db_index=True)
    payment_mode = models.CharField(
        max_length=30,
        choices=PaymentMode.choices,
        default=PaymentMode.BANK_TRANSFER
    )
    payment_reference = models.CharField(max_length=100, db_index=True)
    remarks = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Reimbursement Payment"
        verbose_name_plural = "Reimbursement Payments"

    def __str__(self):
        return f"Payment for {self.claim.claim_number}: Ref {self.payment_reference} ({self.payment_mode})"
