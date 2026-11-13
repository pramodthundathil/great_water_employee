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


def fmt_days(value):
    """Decimal('3.00') -> '3', Decimal('2.50') -> '2.5'."""
    if value is None:
        return ""
    return format(Decimal(value).normalize(), "f")


REJECTED_LEAVE_STATUSES = [
    LeaveApplicationStatus.REJECTED_BY_LEAD,
    LeaveApplicationStatus.REJECTED_BY_HR,
    LeaveApplicationStatus.REJECTED_BY_DIRECTOR,
]


class LeaveAuditAction(models.TextChoices):
    CREATED = "CREATED", "Application Submitted"
    RECORDED_BY_HR = "RECORDED_BY_HR", "Recorded by HR"
    RECOMMENDED = "RECOMMENDED", "Recommended by Lead"
    PARTIALLY_RECOMMENDED = "PARTIALLY_RECOMMENDED", "Partially Recommended by Lead"
    VERIFIED = "VERIFIED", "Verified by HR"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED", "Partially Verified by HR"
    APPROVED = "APPROVED", "Approved by Director"
    PARTIALLY_APPROVED = "PARTIALLY_APPROVED", "Partially Approved by Director"
    REJECTED = "REJECTED", "Rejected"
    RESUBMITTED = "RESUBMITTED", "Resubmitted"
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
    # Original request, preserved when an approver grants only part of it.
    requested_start_date = models.DateField(null=True, blank=True)
    requested_end_date = models.DateField(null=True, blank=True)
    requested_number_of_days = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    resubmission_count = models.PositiveIntegerField(default=0)
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
        if self.requested_start_date is None:
            self.requested_start_date = self.start_date
        if self.requested_end_date is None:
            self.requested_end_date = self.end_date
        if self.requested_number_of_days is None:
            self.requested_number_of_days = self.number_of_days
        super().save(*args, **kwargs)

    # ------------------------------------------------------------------
    # Derived state
    # ------------------------------------------------------------------
    @property
    def is_rejected(self):
        return self.status in REJECTED_LEAVE_STATUSES

    @property
    def is_partially_approved(self):
        return (
            self.status == LeaveApplicationStatus.APPROVED
            and self.requested_number_of_days is not None
            and self.number_of_days < self.requested_number_of_days
        )

    @property
    def is_reduced(self):
        """True when any approver has narrowed the requested dates."""
        return self.requested_number_of_days is not None and self.number_of_days < self.requested_number_of_days

    @property
    def display_status(self):
        if self.is_partially_approved:
            return "Partially Approved"
        return self.get_status_display()

    def can_resubmit(self, user):
        return self.is_rejected and self.employee_id == user.id

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def _apply_partial(self, approved_start_date, approved_end_date, remarks):
        """
        Narrows the leave to a shorter date range inside the current one.
        Returns the field changes, or None when the full range is approved.
        """
        if approved_start_date is None and approved_end_date is None:
            return None
        if approved_start_date is None or approved_end_date is None:
            raise ValidationError("Both approved start and end dates are required for a partial approval.")
        if self.is_half_day:
            raise ValidationError("A half-day leave cannot be partially approved.")
        if approved_end_date < approved_start_date:
            raise ValidationError("Approved end date cannot be before the approved start date.")
        if approved_start_date < self.start_date or approved_end_date > self.end_date:
            raise ValidationError(
                f"Approved dates must fall within the current leave period "
                f"({self.start_date:%d %b %Y} to {self.end_date:%d %b %Y})."
            )
        if approved_start_date == self.start_date and approved_end_date == self.end_date:
            return None

        from leaves.services import calculate_leave_days
        days = calculate_leave_days(self.employee.primary_branch or self.branch, approved_start_date, approved_end_date)
        if days <= 0:
            raise ValidationError("The approved dates contain no working days.")
        if days >= self.number_of_days:
            return None
        if not remarks or not remarks.strip():
            raise ValidationError("Remarks are mandatory when approving fewer days than requested.")

        changes = {
            "start_date": [self.start_date.isoformat(), approved_start_date.isoformat()],
            "end_date": [self.end_date.isoformat(), approved_end_date.isoformat()],
            "number_of_days": [fmt_days(self.number_of_days), fmt_days(days)],
        }
        self.start_date = approved_start_date
        self.end_date = approved_end_date
        self.number_of_days = days
        return changes

    def _review(self, *, stage, expected_status, wrong_status_msg, approver, action, remarks,
                positive_action, next_status, rejected_status, approved_start_date=None,
                approved_end_date=None, source="SYSTEM", ip_address=None):
        if self.status != expected_status:
            raise ValidationError(wrong_status_msg)

        action = (action or "").upper()
        wants_partial = action == f"PARTIALLY_{positive_action}"
        if wants_partial:
            if approved_start_date is None or approved_end_date is None:
                raise ValidationError("Choose the approved start and end dates for a partial approval.")
            action = positive_action

        from_status = self.status
        changes = None
        if action == positive_action:
            changes = self._apply_partial(approved_start_date, approved_end_date, remarks)
            recorded_action = f"PARTIALLY_{positive_action}" if changes else positive_action
            self.status = next_status
            LeaveApproval.objects.create(
                application=self,
                stage=stage,
                approver=approver,
                action=recorded_action,
                remarks=remarks or "",
                approved_start_date=self.start_date if changes else None,
                approved_end_date=self.end_date if changes else None,
                approved_days=self.number_of_days if changes else None,
            )
        elif action == "REJECTED":
            if not remarks or not remarks.strip():
                raise ValidationError("Remarks are mandatory when returning/rejecting leave.")
            recorded_action = "REJECTED"
            self.status = rejected_status
            LeaveApproval.objects.create(
                application=self, stage=stage, approver=approver, action="REJECTED", remarks=remarks,
            )
        else:
            raise ValidationError(f"Invalid action: {action}")

        self.save()
        from leaves.services import log_leave_audit
        log_leave_audit(
            self, approver, recorded_action, from_status=from_status, stage=stage,
            remarks=remarks, changes=changes, source=source, ip_address=ip_address,
        )
        return recorded_action

    @transaction.atomic
    def lead_review(self, approver, action, remarks="", approved_start_date=None, approved_end_date=None,
                    source="SYSTEM", ip_address=None):
        """Stage 1: Reporting Lead / Manager recommendation (full or partial)."""
        if approver == self.employee and not approver.is_superuser:
            raise ValidationError("You cannot recommend your own leave application.")
        return self._review(
            stage="LEAD_MANAGER",
            expected_status=LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION,
            wrong_status_msg="Leave application is not awaiting Lead recommendation.",
            approver=approver, action=action, remarks=remarks,
            positive_action="RECOMMENDED",
            next_status=LeaveApplicationStatus.PENDING_HR_VERIFICATION,
            rejected_status=LeaveApplicationStatus.REJECTED_BY_LEAD,
            approved_start_date=approved_start_date, approved_end_date=approved_end_date,
            source=source, ip_address=ip_address,
        )

    @transaction.atomic
    def hr_review(self, approver, action, remarks="", approved_start_date=None, approved_end_date=None,
                  source="SYSTEM", ip_address=None):
        """Stage 2: HR Coordinator policy verification (full or partial)."""
        if approver == self.employee and not approver.is_superuser:
            raise ValidationError("You cannot verify your own leave application. Another HR Coordinator must verify it.")
        return self._review(
            stage="HR_COORDINATOR",
            expected_status=LeaveApplicationStatus.PENDING_HR_VERIFICATION,
            wrong_status_msg="Leave application is not awaiting HR verification.",
            approver=approver, action=action, remarks=remarks,
            positive_action="VERIFIED",
            next_status=LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL,
            rejected_status=LeaveApplicationStatus.REJECTED_BY_HR,
            approved_start_date=approved_start_date, approved_end_date=approved_end_date,
            source=source, ip_address=ip_address,
        )

    @transaction.atomic
    def director_review(self, approver, action, remarks="", approved_start_date=None, approved_end_date=None,
                        source="SYSTEM", ip_address=None):
        """
        Stage 3: Director final approval (full or partial) or rejection.
        CRITICAL: Balance is deducted ONLY upon Director approval, and only for the approved days.
        """
        recorded = self._review(
            stage="DIRECTOR",
            expected_status=LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL,
            wrong_status_msg="Leave application is not awaiting Director approval.",
            approver=approver, action=action, remarks=remarks,
            positive_action="APPROVED",
            next_status=LeaveApplicationStatus.APPROVED,
            rejected_status=LeaveApplicationStatus.REJECTED_BY_DIRECTOR,
            approved_start_date=approved_start_date, approved_end_date=approved_end_date,
            source=source, ip_address=ip_address,
        )
        if self.status == LeaveApplicationStatus.APPROVED:
            from leaves.services import deduct_leave_from_ledger
            deduct_leave_from_ledger(self, approver)
        return recorded

    @transaction.atomic
    def withdraw(self, user, source="SYSTEM", ip_address=None):
        if self.employee != user and not user.is_superuser:
            raise ValidationError("Only the employee can withdraw their application.")
        if self.status in [LeaveApplicationStatus.APPROVED, LeaveApplicationStatus.WITHDRAWN]:
            raise ValidationError(f"Cannot withdraw leave application in status {self.status}.")
        from_status = self.status
        self.status = LeaveApplicationStatus.WITHDRAWN
        self.save()
        from leaves.services import log_leave_audit
        log_leave_audit(self, user, LeaveAuditAction.WITHDRAWN, from_status=from_status, stage="EMPLOYEE",
                        source=source, ip_address=ip_address)


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
    action = models.CharField(max_length=30)  # RECOMMENDED, VERIFIED, APPROVED, PARTIALLY_*, REJECTED, RESUBMITTED
    remarks = models.TextField(blank=True)
    # Set only when the approver granted fewer days than were pending at their stage.
    approved_start_date = models.DateField(null=True, blank=True)
    approved_end_date = models.DateField(null=True, blank=True)
    approved_days = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Leave Approval"
        verbose_name_plural = "Leave Approvals"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.application.application_number} - {self.stage} - {self.action} by {self.approver.employee_id}"

    @property
    def action_label(self):
        return self.action.replace("_", " ").title()

    @property
    def stage_label(self):
        return {"LEAD_MANAGER": "Lead / Manager", "HR_COORDINATOR": "HR Coordinator",
                "DIRECTOR": "Director", "EMPLOYEE": "Employee"}.get(self.stage, self.stage)


class LeaveAuditLog(models.Model):
    """
    Append-only audit trail of every event on a leave application:
    submission, each review decision (full / partial / rejection), resubmission and withdrawal.
    """
    SOURCE_CHOICES = [("WEB", "Web Portal"), ("MOBILE", "Mobile App"), ("SYSTEM", "System")]

    application = models.ForeignKey(
        LeaveApplication,
        on_delete=models.CASCADE,
        related_name="audit_logs"
    )
    actor = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="leave_audit_logs"
    )
    action = models.CharField(max_length=30, choices=LeaveAuditAction.choices, db_index=True)
    stage = models.CharField(max_length=30, blank=True)
    from_status = models.CharField(max_length=35, blank=True)
    to_status = models.CharField(max_length=35, blank=True)
    remarks = models.TextField(blank=True)
    changes = models.JSONField(default=dict, blank=True, help_text="{field: [old, new]}")
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default="SYSTEM")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        verbose_name = "Leave Audit Log"
        verbose_name_plural = "Leave Audit Logs"
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.application.application_number} - {self.action} ({self.created_at:%Y-%m-%d %H:%M})"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Audit log entries are immutable.")
        super().save(*args, **kwargs)


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
