from decimal import Decimal
from datetime import date
from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from leaves.models import (
    LeaveType, LeavePolicy, LeaveAccrualRun, LeaveLedgerEntry,
    LeaveLedgerEntryType, LeaveApplication, LeaveApplicationStatus,
    LeaveAuditLog, LeaveAuditAction, LeaveApproval, fmt_days,
    initial_approval_stage, STAGE_PENDING_STATUS, STAGE_LABELS,
)


def get_employee_leave_balances(employee):
    """
    Computes comprehensive live balance breakdown for an employee across all active leave types:
    - Earned/Credited days
    - Taken days (Director approved)
    - Pending days (Awaiting recommendation, HR verification, or Director approval)
    - Available balance (official ledger balance)
    """
    leave_types = LeaveType.objects.filter(is_active=True)
    summary = []

    for lt in leave_types:
        current_balance = LeaveLedgerEntry.get_current_balance(employee, lt)

        # Approved leaves taken
        approved_apps = LeaveApplication.objects.filter(
            employee=employee,
            leave_type=lt,
            status=LeaveApplicationStatus.APPROVED
        )
        taken_days = sum((app.number_of_days for app in approved_apps), Decimal("0.00"))

        # Pending approval
        pending_apps = LeaveApplication.objects.filter(
            employee=employee,
            leave_type=lt,
            status__in=[
                LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION,
                LeaveApplicationStatus.PENDING_HR_VERIFICATION,
                LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL,
            ]
        )
        pending_days = sum((app.number_of_days for app in pending_apps), Decimal("0.00"))

        # Earned / Total credited lifetime (all positive ledger transactions)
        positive_entries = LeaveLedgerEntry.objects.filter(
            employee=employee,
            leave_type=lt,
            transaction_days__gt=0
        )
        total_credited = sum((e.transaction_days for e in positive_entries), Decimal("0.00"))

        summary.append({
            "leave_type": lt,
            "earned_days": total_credited,
            "taken_days": taken_days,
            "pending_days": pending_days,
            "available_balance": current_balance,
            "effective_remaining": current_balance - pending_days,
        })

    return summary


@transaction.atomic
def run_accrual_for_employee(employee, policy, period_key, as_of_date=None):
    """
    Runs idempotent earned-leave accrual for a specific employee and period.
    Prevents duplicate credits by checking LeaveAccrualRun.
    """
    if as_of_date is None:
        as_of_date = date.today()

    # 1. Idempotency check: Already processed for this period?
    if LeaveAccrualRun.objects.filter(employee=employee, policy=policy, period_key=period_key).exists():
        return None, "Already processed for this period key"

    # 2. Check employee joining date & probation
    profile = getattr(employee, "profile", None)
    if not profile or not profile.date_of_joining:
        return None, "Employee profile or joining date missing"

    doj = profile.date_of_joining
    if doj > as_of_date:
        return None, "Employee has not yet joined as of period date"

    # Probation check
    if policy.probation_period_months > 0:
        probation_end = doj + relativedelta(months=policy.probation_period_months)
        if as_of_date < probation_end:
            return None, f"Employee is in probation until {probation_end}"

    # Calculate days to credit
    days_to_credit = policy.monthly_accrual_rate

    # Calculate new running balance
    current_balance = LeaveLedgerEntry.get_current_balance(employee, policy.leave_type)
    new_balance = current_balance + days_to_credit

    # 3. Create Accrual Run record (Enforces unique constraint)
    accrual_run = LeaveAccrualRun.objects.create(
        employee=employee,
        policy=policy,
        period_key=period_key,
        days_credited=days_to_credit
    )

    # 4. Create Ledger Entry
    LeaveLedgerEntry.objects.create(
        employee=employee,
        leave_type=policy.leave_type,
        entry_type=LeaveLedgerEntryType.ACCRUAL_CREDIT,
        transaction_days=days_to_credit,
        balance_after=new_balance,
        reference_note=f"Monthly Accrual for {period_key} under policy '{policy.name}'",
        created_by=None  # System automated
    )

    return accrual_run, "Accrual processed successfully"


def run_batch_monthly_accrual(period_key=None, as_of_date=None):
    """
    Executes monthly accrual across all active employees for active accrual policies.
    """
    from accounts.models import User

    if as_of_date is None:
        as_of_date = date.today()
    if period_key is None:
        period_key = as_of_date.strftime("%Y-%m")

    policies = LeavePolicy.objects.filter(is_active=True, accrual_frequency="MONTHLY")
    employees = User.objects.filter(is_active=True, profile__isnull=False)

    results = {
        "period_key": period_key,
        "total_employees": employees.count(),
        "credited": 0,
        "skipped": 0,
        "details": [],
    }

    for policy in policies:
        for emp in employees:
            run, message = run_accrual_for_employee(emp, policy, period_key, as_of_date)
            if run:
                results["credited"] += 1
                results["details"].append(f"Credited {emp.employee_id}: +{run.days_credited} days")
            else:
                results["skipped"] += 1
                results["details"].append(f"Skipped {emp.employee_id}: {message}")

    return results


@transaction.atomic
def record_manual_leave_adjustment(employee, leave_type, days, remarks, hr_user):
    """
    Authorizes HR Coordinator to manually credit or debit leave with mandatory remarks.
    """
    if not remarks or not remarks.strip():
        raise ValidationError("Mandatory justification remarks are required for all manual balance adjustments.")

    days_dec = Decimal(str(days))
    if days_dec == Decimal("0.00"):
        raise ValidationError("Adjustment days cannot be zero.")

    current_bal = LeaveLedgerEntry.get_current_balance(employee, leave_type)
    new_bal = current_bal + days_dec

    if new_bal < Decimal("0.00"):
        raise ValidationError(f"Adjustment cannot result in negative balance. Current balance is {current_bal}.")

    entry = LeaveLedgerEntry.objects.create(
        employee=employee,
        leave_type=leave_type,
        entry_type=LeaveLedgerEntryType.MANUAL_ADJUSTMENT,
        transaction_days=days_dec,
        balance_after=new_bal,
        reference_note=f"Manual Adjustment by {hr_user.employee_id}: {remarks.strip()}",
        created_by=hr_user
    )
    return entry


# ---------------------------------------------------------------------------
# Leave request rules shared by the web portal and the mobile REST API
# ---------------------------------------------------------------------------

ACTIVE_LEAVE_STATUSES = [
    LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION,
    LeaveApplicationStatus.PENDING_HR_VERIFICATION,
    LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL,
    LeaveApplicationStatus.APPROVED,
]

PENDING_LEAVE_STATUSES = ACTIVE_LEAVE_STATUSES[:3]


def get_holiday_dates(branch, start_date, end_date):
    """Public (non-optional) holidays for the branch plus organisation-wide calendars."""
    from django.db.models import Q
    from leaves.models import Holiday

    qs = Holiday.objects.filter(
        date__gte=start_date,
        date__lte=end_date,
        is_optional=False,
        calendar__is_active=True,
    )
    if branch is not None:
        qs = qs.filter(Q(calendar__branch=branch) | Q(calendar__branch__isnull=True))
    else:
        qs = qs.filter(calendar__branch__isnull=True)
    return set(qs.values_list("date", flat=True))


def calculate_leave_days(branch, start_date, end_date, is_half_day=False):
    """
    Number of chargeable leave days in the range.
    Public holidays are excluded; weekend days are excluded only when configured
    via settings.LEAVE_WEEKEND_DAYS (Python weekday numbers, Monday=0).
    """
    from datetime import timedelta
    from django.conf import settings

    weekend_days = set(getattr(settings, "LEAVE_WEEKEND_DAYS", []))
    holidays = get_holiday_dates(branch, start_date, end_date)

    if is_half_day:
        if start_date in holidays or start_date.weekday() in weekend_days:
            return Decimal("0.00")
        return Decimal("0.50")

    count = 0
    day = start_date
    while day <= end_date:
        if day not in holidays and day.weekday() not in weekend_days:
            count += 1
        day += timedelta(days=1)
    return Decimal(count)


class LeaveRequestError(ValidationError):
    """Validation failure that may carry structured data (e.g. insufficient balance)."""

    def __init__(self, message, code=None, extra=None):
        super().__init__(message, code=code)
        self.extra = extra or {}


def validate_leave_request(employee, leave_type, start_date, end_date, is_half_day=False,
                           acknowledge_insufficient_balance=False, attachment=None,
                           exclude_application_id=None, require_attachment=True):
    """
    Validates a leave request and returns (number_of_days, end_date, warnings).
    Raises LeaveRequestError on blocking problems.
    """
    if end_date < start_date:
        raise LeaveRequestError("End date cannot be before start date.", code="invalid_dates")

    if is_half_day:
        end_date = start_date

    number_of_days = calculate_leave_days(employee.primary_branch, start_date, end_date, is_half_day)
    if number_of_days <= 0:
        raise LeaveRequestError(
            "The selected dates fall entirely on public holidays / weekly offs. No leave is required.",
            code="no_working_days",
        )

    overlapping = LeaveApplication.objects.filter(
        employee=employee,
        start_date__lte=end_date,
        end_date__gte=start_date,
        status__in=ACTIVE_LEAVE_STATUSES,
    )
    if exclude_application_id:
        overlapping = overlapping.exclude(id=exclude_application_id)
    clash = overlapping.first()
    if clash:
        raise LeaveRequestError(
            f"These dates overlap with leave application {clash.application_number} "
            f"({clash.start_date} to {clash.end_date}, {clash.get_status_display()}).",
            code="overlap",
        )

    if require_attachment and leave_type.requires_attachment and not attachment:
        raise LeaveRequestError(
            f"A supporting attachment (e.g. medical certificate) is mandatory for {leave_type.name}.",
            code="attachment_required",
        )

    warnings = []
    if leave_type.is_paid:
        balance = LeaveLedgerEntry.get_current_balance(employee, leave_type)
        pending = sum(
            (a.number_of_days for a in LeaveApplication.objects.filter(
                employee=employee, leave_type=leave_type, status__in=PENDING_LEAVE_STATUSES
            ).exclude(id=exclude_application_id or 0)),
            Decimal("0.00"),
        )
        effective = balance - pending
        if effective < number_of_days:
            message = (
                f"Insufficient {leave_type.name} balance. Available: {effective:g} day(s) "
                f"(balance {balance:g}, pending {pending:g}); requested: {number_of_days:g} day(s)."
            )
            if not acknowledge_insufficient_balance:
                raise LeaveRequestError(
                    message,
                    code="insufficient_balance",
                    extra={
                        "insufficient_balance": True,
                        "available_balance": float(effective),
                        "requested_days": float(number_of_days),
                    },
                )
            warnings.append(message)

    return number_of_days, end_date, warnings


def deduct_leave_from_ledger(application, actor, note_prefix="Approved Leave Application"):
    """Creates the ledger deduction for an approved application (paid leave types only)."""
    if not application.leave_type.is_paid:
        return None
    current_bal = LeaveLedgerEntry.get_current_balance(application.employee, application.leave_type)
    note = f"{note_prefix} {application.application_number} ({application.start_date} to {application.end_date})"
    if application.is_reduced:
        note += f" - partially approved {fmt_days(application.number_of_days)} of {fmt_days(application.requested_number_of_days)} day(s)"
    return LeaveLedgerEntry.objects.create(
        employee=application.employee,
        leave_type=application.leave_type,
        entry_type=LeaveLedgerEntryType.LEAVE_DEDUCTION,
        transaction_days=-application.number_of_days,
        balance_after=current_bal - application.number_of_days,
        reference_note=note[:255],
        created_by=actor,
    )


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

def client_ip(request):
    if request is None:
        return None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    ip = forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR")
    return ip or None


def log_leave_audit(application, actor, action, from_status="", stage="", remarks="",
                    changes=None, source="SYSTEM", ip_address=None):
    """Appends one immutable entry to the application's audit trail."""
    return LeaveAuditLog.objects.create(
        application=application,
        actor=actor,
        action=action,
        stage=stage or "",
        from_status=from_status or "",
        to_status=application.status,
        remarks=(remarks or "").strip(),
        changes=changes or {},
        source=source,
        ip_address=ip_address,
    )


# ---------------------------------------------------------------------------
# Approval routing and visibility
# ---------------------------------------------------------------------------

def initial_leave_route(employee):
    """Initial status / stage for a new or resubmitted application, based on the applicant's role."""
    stage = initial_approval_stage(employee)
    note = ""
    if stage != "LEAD_MANAGER":
        note = f"Routed directly to {STAGE_LABELS[stage]} (applicant is {employee.get_role_display()})."
    return {"stage": stage, "status": STAGE_PENDING_STATUS[stage], "note": note}


def visible_leave_applications(user, include_own=True):
    """
    Leave applications a user may track, with their current status and history.
      Employee        : own applications only
      Lead / Manager  : own + direct reports + anything they have acted on
      HR / Director / : every application in their accessible branches (all regions for
      Admin Manager     global-scope users) + anything they have acted on
    """
    from django.db.models import Q
    from accounts.models import RoleChoices

    own = Q(employee=user)
    acted = Q(approval_history__approver=user)
    if user.is_superuser:
        scope = Q()
    elif user.role == RoleChoices.EMPLOYEE:
        scope = own
    elif user.role == RoleChoices.LEAD_MANAGER:
        scope = own | Q(employee__profile__reporting_lead=user) | acted
    else:
        scope = own | Q(branch__in=user.get_accessible_branches()) | acted

    qs = LeaveApplication.objects.filter(scope)
    if not include_own:
        qs = qs.exclude(employee=user)
    return qs.distinct()


def leave_review_options(user, application):
    """
    What the signed-in user can decide on this application right now, or None.
    Single source of truth for the web detail page, the mobile detail screen and the approval queues.
    """
    if application.employee_id == user.id and not user.is_superuser:
        return None
    status = application.status
    can_partial = not application.is_half_day and application.start_date != application.end_date

    if status == LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION:
        lead_id = getattr(getattr(application.employee, "profile", None), "reporting_lead_id", None)
        if not (user.id == lead_id or user.is_superuser):
            return None
        stage, label, path, positive, positive_label = (
            "LEAD_MANAGER", "Lead recommendation", "lead-recommend", "RECOMMENDED", "Recommend")
    elif status == LeaveApplicationStatus.PENDING_HR_VERIFICATION:
        if not (user.is_hr_coordinator and user.can_access_branch(application.branch)):
            return None
        stage, label, path, positive, positive_label = (
            "HR_COORDINATOR", "HR verification", "hr-verify", "VERIFIED", "Verify & forward")
    elif status == LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL:
        if not (user.is_director and user.can_access_branch(application.branch)):
            return None
        stage, label, path, positive, positive_label = (
            "DIRECTOR", "Director final approval", "director-approval", "APPROVED", "Approve")
    else:
        return None

    return {
        "stage": stage,
        "stage_label": label,
        "endpoint": f"/leaves/applications/{application.id}/{path}/",
        "can_partial": can_partial,
        # Approvers may change the leave type / whole date range while forwarding or approving.
        "can_amend": True,
        # Earlier stages this approver may send the leave back to (action RETURNED + return_to).
        "return_targets": [
            {"value": t, "label": STAGE_LABELS[t]} for t in application.return_targets(stage)
        ],
        "actions": [
            {"value": positive, "label": positive_label, "requires_remarks": False, "tone": "positive"},
            {"value": "REJECTED", "label": "Reject", "requires_remarks": True, "tone": "negative"},
        ],
    }


REVIEW_EVENTS = {
    # stage: (positive event, rejected event)
    "LEAD_MANAGER": ("LEAVE_LEAD_RECOMMENDED", "LEAVE_LEAD_REJECTED"),
    "HR_COORDINATOR": ("LEAVE_HR_VERIFIED", "LEAVE_HR_REJECTED"),
    "DIRECTOR": ("LEAVE_DIRECTOR_APPROVED", "LEAVE_DIRECTOR_REJECTED"),
}


def notify_leave_review(application, stage, recorded, remarks="", actor=None):
    """Sends the notifications for one review decision (web and mobile API share this)."""
    from notifications.services import notify_leave_event
    amendment = getattr(application, "last_amendment", None)
    if amendment:
        notify_leave_event(application, "LEAVE_AMENDED", remarks=remarks, changes=amendment, actor=actor)
    if recorded == "RETURNED":
        notify_leave_event(application, "LEAVE_RETURNED", remarks=remarks, actor=actor)
        return
    positive, rejected = REVIEW_EVENTS[stage]
    notify_leave_event(application, rejected if recorded == "REJECTED" else positive,
                       remarks=remarks, partial=recorded.startswith("PARTIALLY_"))


def can_view_leave_application(user, application):
    if user.is_superuser or application.employee_id == user.id:
        return True
    return visible_leave_applications(user).filter(pk=application.pk).exists()


LEAVE_TRACK_FILTERS = {
    "awaiting_lead": {"status": LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION},
    "awaiting_hr": {"status": LeaveApplicationStatus.PENDING_HR_VERIFICATION},
    "awaiting_director": {"status": LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL},
    "approved": {"status": LeaveApplicationStatus.APPROVED},
    "rejected": {"status__in": [
        LeaveApplicationStatus.REJECTED_BY_LEAD,
        LeaveApplicationStatus.REJECTED_BY_HR,
        LeaveApplicationStatus.REJECTED_BY_DIRECTOR,
    ]},
    "withdrawn": {"status": LeaveApplicationStatus.WITHDRAWN},
}


def filter_leave_track(qs, stage, user):
    """Applies a tracker filter: a stage key from LEAVE_TRACK_FILTERS, 'pending' or 'acted_by_me'."""
    if not stage or stage == "all":
        return qs
    if stage == "acted_by_me":
        return qs.filter(approval_history__approver=user).exclude(employee=user).distinct()
    if stage == "pending":
        return qs.filter(status__in=list(STAGE_PENDING_STATUS.values()))
    return qs.filter(**LEAVE_TRACK_FILTERS.get(stage, {}))


def leave_track_counts(qs, user):
    counts = {key: qs.filter(**flt).count() for key, flt in LEAVE_TRACK_FILTERS.items()}
    counts["all"] = qs.count()
    counts["pending"] = counts["awaiting_lead"] + counts["awaiting_hr"] + counts["awaiting_director"]
    counts["acted_by_me"] = qs.filter(approval_history__approver=user).exclude(employee=user).distinct().count()
    return counts


# ---------------------------------------------------------------------------
# Resubmission of rejected applications
# ---------------------------------------------------------------------------

@transaction.atomic
def resubmit_leave_application(application, user, leave_type, start_date, end_date, reason,
                               is_half_day=False, half_day_period="NONE", attachment=None,
                               remove_attachment=False, acknowledge_insufficient_balance=False,
                               resubmit_note="", source="SYSTEM", ip_address=None):
    """
    Lets the employee correct a rejected application and send it back through the
    full approval chain (Lead -> HR -> Director). The application keeps its number
    and its entire approval and audit history.
    Returns (application, warnings).
    """
    application = LeaveApplication.objects.select_for_update().get(pk=application.pk)
    if application.employee_id != user.id:
        raise LeaveRequestError("Only the employee who applied can resubmit this leave application.")
    if not application.is_rejected:
        raise LeaveRequestError("Only rejected leave applications can be resubmitted.")
    if not reason or not reason.strip():
        raise LeaveRequestError("A reason is required.")

    final_attachment = attachment or (None if remove_attachment else application.attachment)
    number_of_days, end_date, warnings = validate_leave_request(
        user, leave_type, start_date, end_date, is_half_day,
        acknowledge_insufficient_balance=acknowledge_insufficient_balance,
        attachment=final_attachment,
        exclude_application_id=application.id,
    )

    half_day_period = half_day_period if is_half_day else "NONE"
    before = {
        "leave_type": application.leave_type.code,
        "start_date": application.start_date.isoformat(),
        "end_date": application.end_date.isoformat(),
        "is_half_day": application.is_half_day,
        "half_day_period": application.half_day_period,
        "number_of_days": fmt_days(application.number_of_days),
        "reason": application.reason,
        "attachment": application.attachment.name if application.attachment else "",
    }

    application.leave_type = leave_type
    application.start_date = start_date
    application.end_date = end_date
    application.is_half_day = is_half_day
    application.half_day_period = half_day_period
    application.number_of_days = number_of_days
    application.requested_start_date = start_date
    application.requested_end_date = end_date
    application.requested_number_of_days = number_of_days
    application.reason = reason.strip()
    if attachment:
        application.attachment = attachment
    elif remove_attachment:
        application.attachment = None
    if user.primary_branch_id:
        application.branch = user.primary_branch
    from_status = application.status
    route = initial_leave_route(user)
    application.status = route["status"]
    application.starts_at_stage = route["stage"]
    application.resubmission_count += 1
    application.save()

    after = {
        "leave_type": leave_type.code,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "is_half_day": is_half_day,
        "half_day_period": half_day_period,
        "number_of_days": fmt_days(number_of_days),
        "reason": application.reason,
        "attachment": application.attachment.name if application.attachment else "",
    }
    changes = {k: [before[k], after[k]] for k in before if before[k] != after[k]}

    note = (resubmit_note or "").strip() or f"Resubmitted after rejection (attempt {application.resubmission_count + 1})"
    if route["note"]:
        note = f"{note} {route['note']}"
    LeaveApproval.objects.create(
        application=application, stage="EMPLOYEE", approver=user, action="RESUBMITTED", remarks=note,
    )
    log_leave_audit(
        application, user, LeaveAuditAction.RESUBMITTED, from_status=from_status, stage="EMPLOYEE",
        remarks=note, changes=changes, source=source, ip_address=ip_address,
    )
    return application, warnings


@transaction.atomic
def record_leave_on_behalf(hr_user, employee, leave_type, start_date, end_date, reason,
                           is_half_day=False, half_day_period="NONE", attachment=None):
    """
    HR Coordinator records leave for an employee as a single entry.
    The leave is stored as APPROVED and the balance is updated immediately.
    """
    if not reason or not reason.strip():
        raise LeaveRequestError("A reason / remark is required when recording leave on behalf of an employee.")
    if not employee.primary_branch_id:
        raise LeaveRequestError("Employee has no primary branch assigned.")

    number_of_days, end_date, warnings = validate_leave_request(
        employee, leave_type, start_date, end_date, is_half_day,
        acknowledge_insufficient_balance=True, attachment=attachment, require_attachment=False,
    )

    application = LeaveApplication.objects.create(
        employee=employee,
        branch=employee.primary_branch,
        leave_type=leave_type,
        start_date=start_date,
        end_date=end_date,
        is_half_day=is_half_day,
        half_day_period=half_day_period if is_half_day else "NONE",
        number_of_days=number_of_days,
        reason=reason.strip(),
        attachment=attachment,
        status=LeaveApplicationStatus.APPROVED,
    )
    LeaveApproval.objects.create(
        application=application,
        stage="HR_COORDINATOR",
        approver=hr_user,
        action="RECORDED",
        remarks=f"Recorded on behalf of employee by HR Coordinator {hr_user.employee_id}",
    )
    deduct_leave_from_ledger(application, hr_user, note_prefix="Leave recorded by HR")
    log_leave_audit(
        application, hr_user, LeaveAuditAction.RECORDED_BY_HR, stage="HR_COORDINATOR",
        remarks=reason, source="WEB",
    )
    return application, warnings


@transaction.atomic
def run_yearly_leave_reset(year, actor=None):
    """
    Year-start processing (idempotent per employee/policy/year):
      1. Lapse balance above the policy's max carry-forward.
      2. Grant the yearly entitlement for non-monthly policies (e.g. Casual Leave).
    """
    from accounts.models import User

    results = {"year": year, "lapsed": 0, "granted": 0, "skipped": 0}
    employees = User.objects.filter(is_active=True, profile__isnull=False)
    policies = LeavePolicy.objects.filter(is_active=True).select_related("leave_type")

    for policy in policies:
        lt = policy.leave_type
        for emp in employees:
            # 1. Carry forward cap
            cf_key = f"CF-{year}"
            if not LeaveAccrualRun.objects.filter(employee=emp, policy=policy, period_key=cf_key).exists():
                balance = LeaveLedgerEntry.get_current_balance(emp, lt)
                excess = balance - policy.max_carry_forward
                lapsed = Decimal("0.00")
                if excess > 0:
                    lapsed = excess
                    LeaveLedgerEntry.objects.create(
                        employee=emp, leave_type=lt,
                        entry_type=LeaveLedgerEntryType.LAPSED,
                        transaction_days=-excess,
                        balance_after=balance - excess,
                        reference_note=f"Year-end lapse for {year - 1}: balance above carry-forward limit of {policy.max_carry_forward:g} day(s)",
                        created_by=actor,
                    )
                    results["lapsed"] += 1
                LeaveAccrualRun.objects.create(employee=emp, policy=policy, period_key=cf_key, days_credited=-lapsed)

            # 2. Yearly grant
            if policy.accrual_frequency in ("YEARLY", "NONE") and policy.yearly_entitlement > 0:
                grant_key = f"YEAR-{year}"
                if LeaveAccrualRun.objects.filter(employee=emp, policy=policy, period_key=grant_key).exists():
                    results["skipped"] += 1
                    continue
                profile = getattr(emp, "profile", None)
                if profile and profile.date_of_joining and profile.date_of_joining.year > year:
                    results["skipped"] += 1
                    continue
                balance = LeaveLedgerEntry.get_current_balance(emp, lt)
                LeaveLedgerEntry.objects.create(
                    employee=emp, leave_type=lt,
                    entry_type=LeaveLedgerEntryType.ACCRUAL_CREDIT,
                    transaction_days=policy.yearly_entitlement,
                    balance_after=balance + policy.yearly_entitlement,
                    reference_note=f"Yearly entitlement {year} under policy '{policy.name}'",
                    created_by=actor,
                )
                LeaveAccrualRun.objects.create(employee=emp, policy=policy, period_key=grant_key, days_credited=policy.yearly_entitlement)
                results["granted"] += 1

    return results
