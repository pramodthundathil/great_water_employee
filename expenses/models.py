from django.db import models, transaction
from django.utils import timezone
from django.core.exceptions import ValidationError
import uuid
from datetime import date


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
    parts = [f"Disbursed via {payment.get_payment_mode_display()}"]
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
    amount = models.DecimalField(max_digits=12, decimal_places=2)
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
        super().save(*args, **kwargs)

    @transaction.atomic
    def submit(self):
        """
        Submits claim. Proof receipt is mandatory before submission.
        """
        if self.status not in [ExpenseClaimStatus.DRAFT, ExpenseClaimStatus.REJECTED_BY_ADMIN, ExpenseClaimStatus.REJECTED_BY_DIRECTOR]:
            raise ValidationError(f"Claim in status {self.status} cannot be submitted.")
        
        if not self.attachments.exists():
            raise ValidationError("At least one supporting receipt/proof is mandatory before submission.")

        self.status = ExpenseClaimStatus.UNDER_ADMIN_REVIEW
        self.submitted_at = timezone.now()
        self.save()

    @transaction.atomic
    def withdraw(self, user):
        """
        Allows employee to withdraw eligible claim.
        """
        if self.employee != user and not user.is_superuser:
            raise ValidationError("Only the claim owner can withdraw this claim.")
        if self.status not in [ExpenseClaimStatus.DRAFT, ExpenseClaimStatus.UNDER_ADMIN_REVIEW]:
            raise ValidationError(f"Claims in status {self.status} cannot be withdrawn.")
        self.status = ExpenseClaimStatus.WITHDRAWN
        self.save()

    @transaction.atomic
    def admin_review(self, approver, action, remarks=""):
        """
        Stage 1: Admin Manager review.
        """
        if self.status != ExpenseClaimStatus.UNDER_ADMIN_REVIEW:
            raise ValidationError("Claim is not currently under Admin Review.")
        if approver == self.employee and not approver.is_superuser:
            raise ValidationError("You cannot review your own expense claim. Another Admin Manager must review it.")

        if action == "APPROVED":
            self.status = ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW
            ExpenseApproval.objects.create(
                claim=self,
                stage="ADMIN_MANAGER",
                approver=approver,
                action="APPROVED",
                remarks=remarks or "Approved for Director review"
            )
        elif action == "REJECTED":
            if not remarks:
                raise ValidationError("Remarks are mandatory when rejecting a claim.")
            self.status = ExpenseClaimStatus.REJECTED_BY_ADMIN
            ExpenseApproval.objects.create(
                claim=self,
                stage="ADMIN_MANAGER",
                approver=approver,
                action="REJECTED",
                remarks=remarks
            )
        else:
            raise ValidationError(f"Invalid review action: {action}")
        self.save()

    @transaction.atomic
    def director_review(self, approver, action, remarks=""):
        """
        Stage 2: Director final approval or rejection.
        """
        if self.status != ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW:
            raise ValidationError("Claim is not currently under Director Review.")

        if action == "APPROVED":
            self.status = ExpenseClaimStatus.APPROVED_BY_DIRECTOR
            ExpenseApproval.objects.create(
                claim=self,
                stage="DIRECTOR",
                approver=approver,
                action="APPROVED",
                remarks=remarks or "Final approval granted"
            )
        elif action == "REJECTED":
            if not remarks:
                raise ValidationError("Remarks are mandatory when rejecting a claim.")
            self.status = ExpenseClaimStatus.REJECTED_BY_DIRECTOR
            ExpenseApproval.objects.create(
                claim=self,
                stage="DIRECTOR",
                approver=approver,
                action="REJECTED",
                remarks=remarks
            )
        else:
            raise ValidationError(f"Invalid director action: {action}")
        self.save()

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
    action = models.CharField(max_length=30)  # APPROVED, REJECTED
    remarks = models.TextField(blank=True)
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
