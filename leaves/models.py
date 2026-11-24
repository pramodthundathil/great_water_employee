from datetime import date
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


APPROVAL_STAGES = ["LEAD_MANAGER", "HR_COORDINATOR", "DIRECTOR"]

STAGE_PENDING_STATUS = {
    "LEAD_MANAGER": LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION,
    "HR_COORDINATOR": LeaveApplicationStatus.PENDING_HR_VERIFICATION,
    "DIRECTOR": LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL,
}

STAGE_LABELS = {
    "LEAD_MANAGER": "Lead / Manager",
    "HR_COORDINATOR": "HR Coordinator",
    "DIRECTOR": "Director",
    "EMPLOYEE": "Employee",
}


def initial_approval_stage(employee):
    """
    First approval stage for a leave raised by `employee`:
      Lead / Manager or Admin Manager -> straight to HR Coordinator
      HR Coordinator                  -> straight to Director
      Everyone else                   -> reporting Lead / Manager
    """
    role = getattr(employee, "role", "")
    if role == "HR_COORDINATOR":
        return "DIRECTOR"
    if role in ("LEAD_MANAGER", "ADMIN_MANAGER"):
        return "HR_COORDINATOR"
    return "LEAD_MANAGER"


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
    RETURNED = "RETURNED", "Returned to earlier stage"
    AMENDED = "AMENDED", "Leave type / dates changed by approver"
    APPROVAL_REVOKED = "APPROVAL_REVOKED", "Approval revoked by Director"
    VERIFICATION_REVERTED = "VERIFICATION_REVERTED", "HR verification reverted"


class LeaveLedgerEntryType(models.TextChoices):
    OPENING_BALANCE = "OPENING_BALANCE", "Opening Balance"
    ACCRUAL_CREDIT = "ACCRUAL_CREDIT", "Earned Leave Accrual Credit"
    LEAVE_DEDUCTION = "LEAVE_DEDUCTION", "Approved Leave Deduction"
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT", "HR Manual Adjustment"
    CARRY_FORWARD = "CARRY_FORWARD", "Yearly Carry Forward"
    LAPSED = "LAPSED", "Lapsed / Expired"
    LEAVE_REVERSAL = "LEAVE_REVERSAL", "Approved Leave Reversal"


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
    branch = models.ForeignKey(
        "organization.Branch", on_delete=models.CASCADE, null=True, blank=True, related_name="leave_types",
        help_text="Leave blank if it applies to all branches",
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

    def is_available_to(self, employee):
        return self.branch_id is None or self.branch_id == employee.primary_branch_id

    @classmethod
    def available_for(cls, employee):
        """Active leave types an employee may apply for: organisation-wide plus their own branch's."""
        from organization.branch_config import available_in
        return available_in(employee.primary_branch_id, cls.objects.filter(is_active=True))


class LeavePolicy(models.Model):
    leave_type = models.ForeignKey(
        LeaveType,
        on_delete=models.CASCADE,
        related_name="policies"
    )
    branch = models.ForeignKey(
        "organization.Branch", on_delete=models.CASCADE, null=True, blank=True, related_name="leave_policies",
        help_text="Leave blank if it applies to all branches",
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

    def save(self, *args, assign_to_all=True, **kwargs):
        """
        New policies apply to all employees by default (admin, seed scripts, API).
        Pass assign_to_all=False to create a policy that will be scoped explicitly.
        """
        is_new = self._state.adding
        super().save(*args, **kwargs)
        if is_new and assign_to_all:
            LeavePolicyAssignment.objects.get_or_create(
                policy=self, scope=PolicyScope.ALL, branch=None, department=None, employee=None,
            )


class PolicyScope(models.TextChoices):
    ALL = "ALL", "All employees"
    BRANCH = "BRANCH", "Branch / location"
    DEPARTMENT = "DEPARTMENT", "Department"
    EMPLOYEE = "EMPLOYEE", "Individual employee"


# Higher number wins when several policies of the same leave type match one employee.
SCOPE_PRIORITY = {
    PolicyScope.EMPLOYEE: 4,
    PolicyScope.DEPARTMENT: 3,
    PolicyScope.BRANCH: 2,
    PolicyScope.ALL: 1,
}


class LeavePolicyAssignment(models.Model):
    """
    Who a LeavePolicy applies to. A policy with no assignments applies to nobody.
    For each employee and leave type exactly one policy is resolved, the most
    specific match winning: Individual > Department > Branch > All employees.
    """
    policy = models.ForeignKey(LeavePolicy, on_delete=models.CASCADE, related_name="assignments")
    scope = models.CharField(max_length=20, choices=PolicyScope.choices, db_index=True)
    branch = models.ForeignKey(
        "organization.Branch", on_delete=models.CASCADE, null=True, blank=True,
        related_name="leave_policy_assignments",
    )
    department = models.ForeignKey(
        "organization.Department", on_delete=models.CASCADE, null=True, blank=True,
        related_name="leave_policy_assignments",
    )
    employee = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, null=True, blank=True,
        related_name="leave_policy_assignments",
    )
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Leave Policy Assignment"
        verbose_name_plural = "Leave Policy Assignments"
        ordering = ["scope", "id"]
        # Duplicates are prevented in leaves.services.assign_policy (get_or_create),
        # since NULL-aware unique constraints are not portable across databases.

    @property
    def priority(self):
        """"All employees" of a branch-owned policy means that branch, so it ranks as a branch assignment."""
        if self.scope == PolicyScope.ALL and self.policy.branch_id:
            return SCOPE_PRIORITY[PolicyScope.BRANCH]
        return SCOPE_PRIORITY.get(self.scope, 0)

    @property
    def target_label(self):
        if self.scope == PolicyScope.BRANCH and self.branch_id:
            return self.branch.name
        if self.scope == PolicyScope.DEPARTMENT and self.department_id:
            return self.department.name
        if self.scope == PolicyScope.EMPLOYEE and self.employee_id:
            return f"{self.employee.full_name} ({self.employee.employee_id})"
        if self.policy.branch_id:
            return f"All {self.policy.branch.name} employees"
        return "All employees"

    def __str__(self):
        return f"{self.policy.name} → {self.get_scope_display()}: {self.target_label}"


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
    # First approval stage, decided by the applicant's role when the leave is submitted / resubmitted.
    starts_at_stage = models.CharField(max_length=20, default="LEAD_MANAGER")
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

    @property
    def skipped_stages(self):
        """Approval stages this application does not pass through because of who applied."""
        if self.starts_at_stage not in APPROVAL_STAGES:
            return []
        return APPROVAL_STAGES[:APPROVAL_STAGES.index(self.starts_at_stage)]

    @property
    def route_note(self):
        if self.starts_at_stage == "HR_COORDINATOR":
            return "Applied by a Lead / Manager or Admin Manager: Lead recommendation is not required, sent directly to HR Coordinator."
        if self.starts_at_stage == "DIRECTOR":
            return "Applied by an HR Coordinator: sent directly to the Director for approval."
        return ""

    @property
    def current_stage(self):
        """Short, user-facing description of where the application stands right now."""
        return {
            LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION: "Awaiting Lead / Manager",
            LeaveApplicationStatus.PENDING_HR_VERIFICATION: "Awaiting HR Coordinator",
            LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL: "Awaiting Director",
        }.get(self.status, self.display_status)

    def can_resubmit(self, user):
        return self.is_rejected and self.employee_id == user.id

    # ------------------------------------------------------------------
    # Workflow
    # ------------------------------------------------------------------
    def return_targets(self, stage):
        """
        Earlier approval stages a reviewer at `stage` may send this application back to
        (instead of rejecting it to the employee). Only stages the application really passes
        through count, and the Lead stage needs the employee to have a reporting lead.
        """
        if stage not in APPROVAL_STAGES:
            return []
        skipped = set(self.skipped_stages)
        has_lead = bool(getattr(getattr(self.employee, "profile", None), "reporting_lead_id", None))
        return [
            s for s in APPROVAL_STAGES[:APPROVAL_STAGES.index(stage)]
            if s not in skipped and (s != "LEAD_MANAGER" or has_lead)
        ]

    def _apply_amendment(self, leave_type, start_date, end_date, remarks):
        """
        Lets an approver change the leave type and / or the whole date range.
        Returns the field changes ({field: [old, new]}), or None when nothing changes.
        """
        new_type = leave_type or self.leave_type
        new_start = start_date or self.start_date
        new_end = end_date or self.end_date
        if new_type.id == self.leave_type_id and new_start == self.start_date and new_end == self.end_date:
            return None
        if new_type.id != self.leave_type_id and not new_type.is_available_to(self.employee):
            raise ValidationError(f"{new_type.name} is not available in this employee's branch.")
        if not new_type.is_active:
            raise ValidationError(f"{new_type.name} is not an active leave type.")
        if new_end < new_start:
            raise ValidationError("The new end date cannot be before the new start date.")
        if not remarks or not remarks.strip():
            raise ValidationError("Remarks are mandatory when changing the leave type or dates.")

        from leaves.services import calculate_leave_days, ACTIVE_LEAVE_STATUSES
        changes = {}
        half_day = self.is_half_day
        if half_day and new_start != new_end:
            half_day = False
            changes["is_half_day"] = [True, False]
        days = calculate_leave_days(self.employee.primary_branch or self.branch, new_start, new_end, half_day)
        if days <= 0:
            raise ValidationError("The new dates contain no working days.")
        clash = (LeaveApplication.objects
                 .filter(employee=self.employee, start_date__lte=new_end, end_date__gte=new_start,
                         status__in=ACTIVE_LEAVE_STATUSES)
                 .exclude(pk=self.pk).first())
        if clash:
            raise ValidationError(
                f"The new dates overlap with leave application {clash.application_number} "
                f"({clash.start_date:%d %b} to {clash.end_date:%d %b %Y}).")

        if new_type.id != self.leave_type_id:
            changes["leave_type"] = [self.leave_type.name, new_type.name]
        if new_start != self.start_date:
            changes["start_date"] = [self.start_date.isoformat(), new_start.isoformat()]
        if new_end != self.end_date:
            changes["end_date"] = [self.end_date.isoformat(), new_end.isoformat()]
        if days != self.number_of_days:
            changes["number_of_days"] = [fmt_days(self.number_of_days), fmt_days(days)]

        self.leave_type = new_type
        self.start_date = new_start
        self.end_date = new_end
        self.number_of_days = days
        if not half_day:
            self.is_half_day = False
            self.half_day_period = "NONE"
        return changes

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
                approved_end_date=None, amend_leave_type=None, amend_start_date=None, amend_end_date=None,
                return_to=None, source="SYSTEM", ip_address=None):
        if self.status != expected_status:
            raise ValidationError(wrong_status_msg)

        from leaves.services import log_leave_audit
        action = (action or "").upper()
        wants_partial = action == f"PARTIALLY_{positive_action}"
        if wants_partial:
            if approved_start_date is None or approved_end_date is None:
                raise ValidationError("Choose the approved start and end dates for a partial approval.")
            action = positive_action

        from_status = self.status
        changes = None
        self.last_amendment = None
        if action == positive_action:
            amendment = None
            if amend_leave_type is not None or amend_start_date is not None or amend_end_date is not None:
                amendment = self._apply_amendment(amend_leave_type, amend_start_date, amend_end_date, remarks)
            if amendment:
                # An amendment replaces the partial-approval range; it is logged as its own audit event.
                self.last_amendment = amendment
                self.save()
                log_leave_audit(self, approver, LeaveAuditAction.AMENDED, from_status=from_status, stage=stage,
                                remarks=remarks, changes=amendment, source=source, ip_address=ip_address)
                recorded_action = positive_action
            else:
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
                changes=amendment or {},
            )
        elif action == "RETURNED":
            targets = self.return_targets(stage)
            if not targets:
                raise ValidationError("There is no earlier approval stage to return this application to.")
            if return_to not in targets:
                raise ValidationError("Choose the stage to return this application to: "
                                      + ", ".join(STAGE_LABELS[t] for t in targets) + ".")
            if not remarks or not remarks.strip():
                raise ValidationError("Remarks are mandatory when returning leave to an earlier stage.")
            recorded_action = "RETURNED"
            self.status = STAGE_PENDING_STATUS[return_to]
            changes = {"returned_to": [STAGE_LABELS[stage], STAGE_LABELS[return_to]]}
            LeaveApproval.objects.create(
                application=self, stage=stage, approver=approver, action="RETURNED", remarks=remarks,
                returned_to=return_to, changes=changes,
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
        log_leave_audit(
            self, approver, recorded_action, from_status=from_status, stage=stage,
            remarks=remarks, changes=changes, source=source, ip_address=ip_address,
        )
        return recorded_action

    @transaction.atomic
    def lead_review(self, approver, action, remarks="", approved_start_date=None, approved_end_date=None,
                    source="SYSTEM", ip_address=None, **extra):
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
            source=source, ip_address=ip_address, **extra,
        )

    @transaction.atomic
    def hr_review(self, approver, action, remarks="", approved_start_date=None, approved_end_date=None,
                  source="SYSTEM", ip_address=None, **extra):
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
            source=source, ip_address=ip_address, **extra,
        )

    @transaction.atomic
    def director_review(self, approver, action, remarks="", approved_start_date=None, approved_end_date=None,
                        source="SYSTEM", ip_address=None, **extra):
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
            source=source, ip_address=ip_address, **extra,
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


    # ------------------------------------------------------------------
    # Corrections after a decision
    # ------------------------------------------------------------------
    def _check_corrector(self, user, remarks, what):
        if user.id == self.employee_id and not user.is_superuser:
            raise ValidationError(f"You cannot {what} on your own leave application.")
        if not remarks or not remarks.strip():
            raise ValidationError(f"Remarks are mandatory to {what}.")

    @transaction.atomic
    def revoke_approval(self, director, remarks="", source="SYSTEM", ip_address=None):
        """
        Director takes back an approved leave, also after its dates are over. The deducted days are
        credited back and the leave returns to Pending Director Approval, where the Director can
        approve it again (optionally with corrected dates) or reject it.
        """
        if self.status != LeaveApplicationStatus.APPROVED:
            raise ValidationError("Only an approved leave can be revoked.")
        if not director.is_director:
            raise ValidationError("Only a Director can revoke an approved leave.")
        self._check_corrector(director, remarks, "revoke the approval")

        from leaves.services import log_leave_audit, reverse_leave_deduction
        reverse_leave_deduction(self, director, note_prefix="Approval revoked for")
        from_status = self.status
        self.status = LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL
        self.save()
        LeaveApproval.objects.create(application=self, stage="DIRECTOR", approver=director,
                                     action="REVOKED", remarks=remarks.strip())
        log_leave_audit(self, director, LeaveAuditAction.APPROVAL_REVOKED, from_status=from_status,
                        stage="DIRECTOR", remarks=remarks, source=source, ip_address=ip_address)

    @transaction.atomic
    def revert_verification(self, hr_user, remarks="", source="SYSTEM", ip_address=None):
        """
        HR Coordinator takes back their verification while the leave still awaits the Director.
        Once the Director has decided, HR can no longer revert it.
        """
        if self.status == LeaveApplicationStatus.APPROVED:
            raise ValidationError("The Director has already approved this leave; HR verification can no longer be reverted.")
        if self.status != LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL:
            raise ValidationError("HR verification can only be reverted while the leave awaits the Director.")
        if "HR_COORDINATOR" in self.skipped_stages:
            raise ValidationError("This leave did not pass through HR verification.")
        if not hr_user.is_hr_coordinator:
            raise ValidationError("Only an HR Coordinator can revert HR verification.")
        self._check_corrector(hr_user, remarks, "revert the verification")

        from leaves.services import log_leave_audit
        from_status = self.status
        self.status = LeaveApplicationStatus.PENDING_HR_VERIFICATION
        self.save()
        LeaveApproval.objects.create(application=self, stage="HR_COORDINATOR", approver=hr_user,
                                     action="VERIFICATION_REVERTED", remarks=remarks.strip())
        log_leave_audit(self, hr_user, LeaveAuditAction.VERIFICATION_REVERTED, from_status=from_status,
                        stage="HR_COORDINATOR", remarks=remarks, source=source, ip_address=ip_address)

    @transaction.atomic
    def edit_dates(self, user, start_date, end_date, remarks="", leave_type=None, source="SYSTEM", ip_address=None):
        """
        Corrects the leave type / dates after a decision (past dates are allowed):
          Director       : an approved leave; the ledger is re-adjusted to the new days
          HR Coordinator : a verified leave still awaiting the Director (not once it is approved)
        Returns the field changes.
        """
        S = LeaveApplicationStatus
        if self.status == S.APPROVED:
            if not user.is_director:
                raise ValidationError("The Director has already approved this leave; only a Director can change it now.")
            stage = "DIRECTOR"
        elif self.status == S.PENDING_DIRECTOR_APPROVAL and "HR_COORDINATOR" not in self.skipped_stages:
            if not user.is_hr_coordinator:
                raise ValidationError("Only an HR Coordinator can change the dates at this stage.")
            stage = "HR_COORDINATOR"
        else:
            raise ValidationError("The dates of this leave cannot be changed at this stage.")
        self._check_corrector(user, remarks, "change the dates")

        old_type, old_days = self.leave_type, self.number_of_days
        changes = self._apply_amendment(leave_type, start_date, end_date, remarks)
        if not changes:
            raise ValidationError("Nothing to change: the leave type and dates are the same.")
        self.save()
        if self.status == S.APPROVED:
            from leaves.services import deduct_leave_from_ledger, reverse_leave_deduction
            reverse_leave_deduction(self, user, note_prefix="Dates changed for", leave_type=old_type, days=old_days)
            deduct_leave_from_ledger(self, user, note_prefix="Re-approved after date change")

        from leaves.services import log_leave_audit
        LeaveApproval.objects.create(application=self, stage=stage, approver=user, action="DATES_EDITED",
                                     remarks=remarks.strip(), changes=changes)
        log_leave_audit(self, user, LeaveAuditAction.AMENDED, from_status=self.status, stage=stage,
                        remarks=remarks, changes=changes, source=source, ip_address=ip_address)
        self.last_amendment = changes
        return changes


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
    # Set when an approver sent the application back to an earlier stage (action RETURNED).
    returned_to = models.CharField(max_length=30, blank=True)
    # Field changes made by this approver: leave type / date amendments, or {"returned_to": [from, to]}.
    changes = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Leave Approval"
        verbose_name_plural = "Leave Approvals"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.application.application_number} - {self.stage} - {self.action} by {self.approver.employee_id}"

    @property
    def action_label(self):
        if self.action == "RETURNED":
            return f"Returned to {STAGE_LABELS.get(self.returned_to, self.returned_to)}"
        special = {"REVOKED": "Approval revoked", "VERIFICATION_REVERTED": "Verification reverted",
                   "DATES_EDITED": "Dates changed"}
        if self.action in special:
            return special[self.action]
        return self.action.replace("_", " ").title()

    @property
    def change_rows(self):
        """[(label, old, new)] for amendments, for templates."""
        labels = {"leave_type": "Leave type", "start_date": "From", "end_date": "To",
                  "number_of_days": "Days", "is_half_day": "Half day"}
        def fmt(field, value):
            if field in ("start_date", "end_date") and isinstance(value, str):
                try:
                    return date.fromisoformat(value[:10]).strftime("%d %b %Y")
                except ValueError:
                    return value
            if isinstance(value, bool):
                return "Yes" if value else "No"
            return value
        return [(labels.get(k, k.replace("_", " ").capitalize()), fmt(k, v[0]), fmt(k, v[1]))
                for k, v in (self.changes or {}).items()
                if k != "returned_to" and isinstance(v, (list, tuple)) and len(v) == 2]

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
