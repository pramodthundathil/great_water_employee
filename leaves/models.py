from django.db import models, transaction
from django.utils import timezone
from django.core.exceptions import ValidationError
from decimal import Decimal
import uuid


class LeaveApplicationStatus(models.TextChoices):
    PENDING_LEAD_RECOMMENDATION = "PENDING_LEAD_RECOMMENDATION", "Pending Lead Recommendation"
    PENDING_HR_VERIFICATION = "PENDING_HR_VERIFICATION", "Pending HR Verification"
    PENDING_DIRECTOR_APPROVAL = "PENDING_DIRECTOR_APPROVAL", "Pending Director Approval"
    APPROVED = "APPROVED", "Approved"
    REJECTED_BY_LEAD = "REJECTED_BY_LEAD", "Rejected by Lead / Manager"
    REJECTED_BY_HR = "REJECTED_BY_HR", "Rejected by HR Coordinator"
    REJECTED_BY_DIRECTOR = "REJECTED_BY_DIRECTOR", "Rejected by Director"
    WITHDRAWN = "WITHDRAWN", "Withdrawn"


class LeaveLedgerEntryType(models.TextChoices):
    OPENING_BALANCE = "OPENING_BALANCE", "Opening Balance"
    ACCRUAL_CREDIT = "ACCRUAL_CREDIT", "Earned Leave Accrual Credit"
    LEAVE_DEDUCTION = "LEAVE_DEDUCTION", "Approved Leave Deduction"
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT", "HR Manual Adjustment"
    CARRY_FORWARD = "CARRY_FORWARD", "Yearly Carry Forward"
    LAPSED = "LAPSED", "Lapsed / Expired"


class HalfDayPeriod(models.TextChoices):
    NONE = "NONE", "Full Day"
    FIRST_HALF = "FIRST_HALF", "First Half (Morning)"
    SECOND_HALF = "SECOND_HALF", "Second Half (Afternoon)"


class LeaveType(models.Model):
    organization = models.ForeignKey(
        "organization.Organization",
        on_delete=models.CASCADE,
        related_name="leave_types"
    )
    name = models.CharField(max_length=50)  # e.g., Earned Leave, Casual Leave, Sick Leave, Unpaid Leave
    code = models.CharField(max_length=20, unique=True)  # EL, CL, SL, LWP
    is_paid = models.BooleanField(default=True)
    is_accrual_based = models.BooleanField(default=False, help_text="True for Earned Leave based on service length")
    requires_attachment = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Leave Type"
        verbose_name_plural = "Leave Types"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"


class LeavePolicy(models.Model):
    leave_type = models.ForeignKey(
        LeaveType,
        on_delete=models.CASCADE,
        related_name="policies"
    )
    name = models.CharField(max_length=100)
    yearly_entitlement = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("18.00"))
    accrual_frequency = models.CharField(
        max_length=20,
        choices=[("MONTHLY", "Monthly"), ("YEARLY", "Yearly"), ("NONE", "None")],
        default="MONTHLY"
    )
    monthly_accrual_rate = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal("1.50"))
    max_carry_forward = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0.00"))
    probation_period_months = models.PositiveIntegerField(default=0)
    effective_date = models.DateField(default=timezone.now)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Leave Policy"
        verbose_name_plural = "Leave Policies"

    def __str__(self):
        return f"{self.name} - {self.leave_type.code} ({self.yearly_entitlement} days/yr)"


class LeaveAccrualRun(models.Model):
    """
    Prevents duplicate credits for the same employee, policy, and period key.
    """
    employee = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="accrual_runs"
    )
    policy = models.ForeignKey(LeavePolicy, on_delete=models.PROTECT)
    period_key = models.CharField(max_length=20, db_index=True)  # e.g., '2026-10'
    days_credited = models.DecimalField(max_digits=4, decimal_places=2)
    executed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Leave Accrual Run"
        verbose_name_plural = "Leave Accrual Runs"
        unique_together = ("employee", "policy", "period_key")

    def __str__(self):
        return f"{self.employee.employee_id} - {self.period_key} (+{self.days_credited} days)"


class LeaveLedgerEntry(models.Model):
    """
    Immutable transaction ledger for all balance movements.
    """
    employee = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="leave_ledger_entries"
    )
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT)
    entry_type = models.CharField(
        max_length=30,
        choices=LeaveLedgerEntryType.choices
    )
    transaction_days = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text="Positive for credits, negative for debits"
    )
    balance_after = models.DecimalField(max_digits=6, decimal_places=2)
    reference_note = models.CharField(max_length=255)
    created_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_ledger_entries"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Leave Ledger Entry"
        verbose_name_plural = "Leave Ledger Entries"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.employee.employee_id} | {self.leave_type.code} | {self.entry_type} | {self.transaction_days:+f} (Bal: {self.balance_after})"

    @classmethod
    def get_current_balance(cls, employee, leave_type):
        latest = cls.objects.filter(employee=employee, leave_type=leave_type).order_by("-created_at", "-id").first()
        return latest.balance_after if latest else Decimal("0.00")


class LeaveApplication(models.Model):
    application_number = models.CharField(max_length=50, unique=True, db_index=True)
    employee = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="leave_applications"
    )
    branch = models.ForeignKey(
        "organization.Branch",
        on_delete=models.PROTECT,
        related_name="leave_applications"
    )
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT)
    start_date = models.DateField(db_index=True)
    end_date = models.DateField(db_index=True)
    is_half_day = models.BooleanField(default=False)
    half_day_period = models.CharField(
        max_length=20,
        choices=HalfDayPeriod.choices,
        default=HalfDayPeriod.NONE
    )
    number_of_days = models.DecimalField(max_digits=4, decimal_places=2)
    reason = models.TextField()
    attachment = models.FileField(upload_to="leaves/%Y/%m/", null=True, blank=True)
    status = models.CharField(
        max_length=35,
        choices=LeaveApplicationStatus.choices,
        default=LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION,
        db_index=True
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Leave Application"
        verbose_name_plural = "Leave Applications"
        ordering = ["-submitted_at"]
        indexes = [
            models.Index(fields=["branch", "status"]),
            models.Index(fields=["employee", "status"]),
            models.Index(fields=["start_date", "end_date"]),
        ]

    def __str__(self):
        return f"{self.application_number} - {self.employee.employee_id} ({self.number_of_days} days)"

    def save(self, *args, **kwargs):
        if not self.application_number:
            date_prefix = timezone.now().strftime("%Y%m")
            unique_part = uuid.uuid4().hex[:6].upper()
            self.application_number = f"LV-{date_prefix}-{unique_part}"
        super().save(*args, **kwargs)

    @transaction.atomic
    def lead_review(self, approver, action, remarks=""):
        """
        Stage 1: Reporting Lead / Manager recommendation.
        """
        if self.status != LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION:
            raise ValidationError("Leave application is not awaiting Lead recommendation.")
        if approver == self.employee and not approver.is_superuser:
            raise ValidationError("You cannot recommend your own leave application.")

        if action == "RECOMMENDED":
            self.status = LeaveApplicationStatus.PENDING_HR_VERIFICATION
            LeaveApproval.objects.create(
                application=self,
                stage="LEAD_MANAGER",
                approver=approver,
                action="RECOMMENDED",
                remarks=remarks or "Recommended to HR Coordinator"
            )
        elif action == "REJECTED":
            if not remarks:
                raise ValidationError("Remarks are mandatory when returning/rejecting leave.")
            self.status = LeaveApplicationStatus.REJECTED_BY_LEAD
            LeaveApproval.objects.create(
                application=self,
                stage="LEAD_MANAGER",
                approver=approver,
                action="REJECTED",
                remarks=remarks
            )
        else:
            raise ValidationError(f"Invalid lead action: {action}")
        self.save()

    @transaction.atomic
    def hr_review(self, approver, action, remarks=""):
        """
        Stage 2: HR Coordinator policy verification.
        """
        if self.status != LeaveApplicationStatus.PENDING_HR_VERIFICATION:
            raise ValidationError("Leave application is not awaiting HR verification.")
        if approver == self.employee and not approver.is_superuser:
            raise ValidationError("You cannot verify your own leave application. Another HR Coordinator must verify it.")

        if action == "VERIFIED":
            self.status = LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL
            LeaveApproval.objects.create(
                application=self,
                stage="HR_COORDINATOR",
                approver=approver,
                action="VERIFIED",
                remarks=remarks or "Verified against leave policy and balance"
            )
        elif action == "REJECTED":
            if not remarks:
                raise ValidationError("Remarks are mandatory when rejecting leave.")
            self.status = LeaveApplicationStatus.REJECTED_BY_HR
            LeaveApproval.objects.create(
                application=self,
                stage="HR_COORDINATOR",
                approver=approver,
                action="REJECTED",
                remarks=remarks
            )
        else:
            raise ValidationError(f"Invalid HR action: {action}")
        self.save()

    @transaction.atomic
    def director_review(self, approver, action, remarks=""):
        """
        Stage 3: Director final approval or rejection.
        CRITICAL: Balance is deducted ONLY upon Director approval!
        """
        if self.status != LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL:
            raise ValidationError("Leave application is not awaiting Director approval.")

        if action == "APPROVED":
            self.status = LeaveApplicationStatus.APPROVED
            LeaveApproval.objects.create(
                application=self,
                stage="DIRECTOR",
                approver=approver,
                action="APPROVED",
                remarks=remarks or "Final approval granted"
            )

            # Deduct from official balance in the ledger (paid leave types only)
            from leaves.services import deduct_leave_from_ledger
            deduct_leave_from_ledger(self, approver)

        elif action == "REJECTED":
            if not remarks:
                raise ValidationError("Remarks are mandatory when rejecting leave.")
            self.status = LeaveApplicationStatus.REJECTED_BY_DIRECTOR
            LeaveApproval.objects.create(
                application=self,
                stage="DIRECTOR",
                approver=approver,
                action="REJECTED",
                remarks=remarks
            )
        else:
            raise ValidationError(f"Invalid Director action: {action}")
        self.save()

    @transaction.atomic
    def withdraw(self, user):
        if self.employee != user and not user.is_superuser:
            raise ValidationError("Only the employee can withdraw their application.")
        if self.status in [LeaveApplicationStatus.APPROVED, LeaveApplicationStatus.WITHDRAWN]:
            raise ValidationError(f"Cannot withdraw leave application in status {self.status}.")
        self.status = LeaveApplicationStatus.WITHDRAWN
        self.save()


class LeaveApproval(models.Model):
    application = models.ForeignKey(
        LeaveApplication,
        on_delete=models.CASCADE,
        related_name="approval_history"
    )
    stage = models.CharField(max_length=30)  # LEAD_MANAGER, HR_COORDINATOR, DIRECTOR
    approver = models.ForeignKey(
        "accounts.User",
        on_delete=models.PROTECT,
        related_name="leave_approvals"
    )
    action = models.CharField(max_length=30)  # RECOMMENDED, VERIFIED, APPROVED, REJECTED
    remarks = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Leave Approval"
        verbose_name_plural = "Leave Approvals"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.application.application_number} - {self.stage} - {self.action} by {self.approver.employee_id}"


class HolidayCalendar(models.Model):
    organization = models.ForeignKey(
        "organization.Organization",
        on_delete=models.CASCADE,
        related_name="holiday_calendars"
    )
    branch = models.ForeignKey(
        "organization.Branch",
        on_delete=models.CASCADE,
        related_name="holiday_calendars",
        null=True,
        blank=True,
        help_text="Leave blank if observed across all branches"
    )
    name = models.CharField(max_length=100)
    year = models.PositiveIntegerField(default=2026)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Holiday Calendar"
        verbose_name_plural = "Holiday Calendars"

    def __str__(self):
        branch_str = self.branch.code if self.branch else "All Branches"
        return f"{self.name} ({self.year}) - {branch_str}"


class Holiday(models.Model):
    calendar = models.ForeignKey(
        HolidayCalendar,
        on_delete=models.CASCADE,
        related_name="holidays"
    )
    date = models.DateField(db_index=True)
    name = models.CharField(max_length=100)
    is_optional = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Holiday"
        verbose_name_plural = "Holidays"
        ordering = ["date"]

    def __str__(self):
        return f"{self.date} - {self.name}"
