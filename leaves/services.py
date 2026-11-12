from decimal import Decimal
from datetime import date
from dateutil.relativedelta import relativedelta
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ValidationError

from leaves.models import (
    LeaveType, LeavePolicy, LeaveAccrualRun, LeaveLedgerEntry,
    LeaveLedgerEntryType, LeaveApplication, LeaveApplicationStatus
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
    return LeaveLedgerEntry.objects.create(
        employee=application.employee,
        leave_type=application.leave_type,
        entry_type=LeaveLedgerEntryType.LEAVE_DEDUCTION,
        transaction_days=-application.number_of_days,
        balance_after=current_bal - application.number_of_days,
        reference_note=f"{note_prefix} {application.application_number} ({application.start_date} to {application.end_date})",
        created_by=actor,
    )


@transaction.atomic
def record_leave_on_behalf(hr_user, employee, leave_type, start_date, end_date, reason,
                           is_half_day=False, half_day_period="NONE", attachment=None):
    """
    HR Coordinator records leave for an employee as a single entry.
    The leave is stored as APPROVED and the balance is updated immediately.
    """
    from leaves.models import LeaveApproval

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
