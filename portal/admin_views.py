"""
Portal views for administration features required by the specification:
employee edit / deactivate, expense edit-before-review, expense category
management, leave settings (types, entitlement & accrual rules, holidays),
HR recording leave on behalf of employees, and password change.
"""
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse

from accounts.models import User, RoleChoices, EmployeeProfile
from organization.models import Organization, Department
from expenses.models import ExpenseClaim, ExpenseCategory, ExpenseAttachment, ExpenseClaimStatus
from leaves.models import LeaveType, LeavePolicy, LeaveAccrualRun, HolidayCalendar, Holiday
from leaves.services import (
    record_leave_on_behalf, LeaveRequestError, run_batch_monthly_accrual,
)
from notifications.services import notify_expense_event

EDITABLE_EXPENSE_STATUSES = [
    ExpenseClaimStatus.DRAFT,
    ExpenseClaimStatus.UNDER_ADMIN_REVIEW,
    ExpenseClaimStatus.REJECTED_BY_ADMIN,
    ExpenseClaimStatus.REJECTED_BY_DIRECTOR,
]

ALLOWED_UPLOAD_TYPES = ("image/jpeg", "image/png", "image/heic", "image/heif", "image/webp", "application/pdf")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def can_manage_users(user):
    return user.is_superuser or user.role in [
        RoleChoices.DIRECTOR, RoleChoices.ADMIN_MANAGER, RoleChoices.HR_COORDINATOR
    ]


def get_default_organization(user=None):
    """Resolve the active organization, creating a default record if none exists yet."""
    branch = getattr(user, "primary_branch", None) if user is not None else None
    if branch is not None and branch.organization_id:
        return branch.organization
    org = Organization.objects.first()
    if org is None:
        org, _ = Organization.objects.get_or_create(
            code="GW",
            defaults={"name": "Great Waters"},
        )
    return org


def validate_upload(f):
    """Returns an error string, or None if the uploaded proof file is acceptable."""
    import mimetypes

    if f.size > MAX_UPLOAD_BYTES:
        return f"{f.name} is larger than 10 MB."
    # Mobile clients sometimes send a generic type; trust the extension in that case.
    if not f.content_type or f.content_type.lower() in ("application/octet-stream", "binary/octet-stream"):
        guessed = mimetypes.guess_type(f.name or "")[0] or ("image/heic" if (f.name or "").lower().endswith((".heic", ".heif")) else None)
        if guessed:
            f.content_type = guessed
    if f.content_type and f.content_type.lower() not in ALLOWED_UPLOAD_TYPES:
        return f"{f.name}: only images (JPG/PNG/HEIC/WEBP) and PDF files are accepted."
    return None


def _parse_date(value):
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _parse_decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------

@login_required
def employee_edit_view(request, pk):
    user = request.user
    if not can_manage_users(user):
        messages.error(request, "You are not authorised to manage employee records.")
        return redirect("portal_dashboard")

    emp = get_object_or_404(User, pk=pk)
    accessible_branches = user.get_accessible_branches()
    if emp.primary_branch and not user.can_access_branch(emp.primary_branch):
        messages.error(request, "This employee belongs to a branch outside your access scope.")
        return redirect("portal_employees")

    profile, _ = EmployeeProfile.objects.get_or_create(user=emp)

    if request.method == "POST":
        action = request.POST.get("action", "save")

        if action == "toggle_active":
            if emp == user:
                messages.error(request, "You cannot deactivate your own account.")
            else:
                emp.is_active = not emp.is_active
                emp.save(update_fields=["is_active"])
                messages.success(request, f"{emp.full_name} has been {'re-activated' if emp.is_active else 'deactivated'}.")
            return redirect("portal_employee_edit", pk=emp.pk)

        if action == "reset_password":
            new_password = request.POST.get("new_password", "").strip()
            if len(new_password) < 8:
                messages.error(request, "Temporary password must be at least 8 characters.")
            else:
                emp.set_password(new_password)
                emp.save()
                messages.success(request, f"Temporary password set for {emp.employee_id}. Share it securely with the employee.")
            return redirect("portal_employee_edit", pk=emp.pk)

        email = request.POST.get("email", "").strip().lower()
        if not email:
            messages.error(request, "E-mail is required.")
            return redirect("portal_employee_edit", pk=emp.pk)
        if User.objects.filter(email__iexact=email).exclude(pk=emp.pk).exists():
            messages.error(request, f"Another employee already uses {email}.")
            return redirect("portal_employee_edit", pk=emp.pk)

        role = request.POST.get("role", emp.role)
        if role not in RoleChoices.values:
            role = emp.role

        branch_id = request.POST.get("primary_branch")
        branch = accessible_branches.filter(id=branch_id).first() if branch_id else emp.primary_branch

        doj = _parse_date(request.POST.get("date_of_joining"))
        lead_id = request.POST.get("reporting_lead")
        lead = User.objects.filter(id=lead_id).exclude(pk=emp.pk).first() if lead_id else None
        dept_id = request.POST.get("department")

        emp.first_name = request.POST.get("first_name", "").strip()
        emp.last_name = request.POST.get("last_name", "").strip()
        emp.email = email
        emp.phone = request.POST.get("phone", "").strip()
        emp.role = role
        emp.primary_branch = branch
        if user.is_superuser or user.is_director:
            emp.is_global_scope = request.POST.get("is_global_scope") == "1"
        emp.save()

        profile.department = Department.objects.filter(id=dept_id).first() if dept_id else None
        profile.designation = request.POST.get("designation", "").strip()
        profile.reporting_lead = lead
        if doj:
            profile.date_of_joining = doj
        profile.save()

        messages.success(request, f"Employee record for {emp.full_name} ({emp.employee_id}) updated.")
        return redirect("portal_employee_edit", pk=emp.pk)

    leads = User.objects.filter(
        is_active=True,
        primary_branch__in=accessible_branches,
        role__in=[RoleChoices.LEAD_MANAGER, RoleChoices.ADMIN_MANAGER, RoleChoices.HR_COORDINATOR, RoleChoices.DIRECTOR],
    ).exclude(pk=emp.pk).order_by("first_name")

    context = {
        "active_nav": "employees",
        "emp": emp,
        "profile": profile,
        "all_branches": accessible_branches,
        "all_departments": Department.objects.filter(is_active=True),
        "all_roles": RoleChoices.choices,
        "all_leads": leads,
        "can_set_scope": user.is_superuser or user.is_director,
    }
    return render(request, "portal/employee_edit.html", context)


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------

@login_required
def expense_edit_view(request, pk):
    claim = get_object_or_404(ExpenseClaim, pk=pk)
    user = request.user

    if claim.employee != user:
        messages.error(request, "Only the employee who created this claim can edit it.")
        return redirect("portal_expense_detail", pk=claim.pk)
    if claim.status not in EDITABLE_EXPENSE_STATUSES:
        messages.error(request, "This claim can no longer be edited because review has already started.")
        return redirect("portal_expense_detail", pk=claim.pk)

    if request.method == "POST":
        action = request.POST.get("action", "save")

        if action == "delete_attachment":
            att = get_object_or_404(ExpenseAttachment, pk=request.POST.get("attachment_id"), claim=claim)
            att.file.delete(save=False)
            att.delete()
            messages.info(request, "Attachment removed.")
            return redirect("portal_expense_edit", pk=claim.pk)

        category = ExpenseCategory.objects.filter(id=request.POST.get("category"), is_active=True).first()
        amount = _parse_decimal(request.POST.get("amount"))
        expense_date = _parse_date(request.POST.get("expense_date"))
        if not category or amount is None or amount <= 0 or not expense_date:
            messages.error(request, "Category, a positive amount and the expense date are required.")
            return redirect("portal_expense_edit", pk=claim.pk)
        if expense_date > date.today():
            messages.error(request, "Expense date cannot be in the future.")
            return redirect("portal_expense_edit", pk=claim.pk)

        files = request.FILES.getlist("receipts")
        for f in files:
            err = validate_upload(f)
            if err:
                messages.error(request, err)
                return redirect("portal_expense_edit", pk=claim.pk)

        claim.category = category
        claim.amount = amount
        claim.expense_date = expense_date
        claim.description = request.POST.get("description", "").strip()
        claim.project_reference = request.POST.get("project_reference", "").strip()
        claim.save()

        for f in files:
            ExpenseAttachment.objects.create(
                claim=claim, file=f, original_filename=f.name,
                file_type=f.content_type or "", file_size=f.size,
            )

        if action == "save_submit" and claim.status != ExpenseClaimStatus.UNDER_ADMIN_REVIEW:
            try:
                claim.submit()
                notify_expense_event(claim, "EXPENSE_SUBMITTED")
                messages.success(request, f"Claim {claim.claim_number} updated and submitted for review.")
            except ValidationError as e:
                messages.error(request, " ".join(e.messages))
                return redirect("portal_expense_edit", pk=claim.pk)
        else:
            messages.success(request, f"Claim {claim.claim_number} updated.")
        return redirect("portal_expense_detail", pk=claim.pk)

    context = {
        "active_nav": "expenses",
        "claim": claim,
        "categories": ExpenseCategory.objects.filter(is_active=True),
        "attachments": claim.attachments.all(),
        "today": date.today(),
    }
    return render(request, "portal/expense_edit.html", context)


@login_required
def expense_categories_view(request):
    user = request.user
    if not (user.is_admin_manager or user.is_director or user.is_superuser):
        messages.error(request, "Expense categories are managed by the Admin Manager.")
        return redirect("portal_dashboard")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "create":
            name = request.POST.get("name", "").strip()
            code = request.POST.get("code", "").strip().upper()
            if not name or not code:
                messages.error(request, "Category name and code are required.")
            elif ExpenseCategory.objects.filter(code=code).exists():
                messages.error(request, f"Category code {code} already exists.")
            else:
                org = get_default_organization(user)
                ExpenseCategory.objects.create(
                    organization=org, name=name[:100], code=code[:50],
                    description=request.POST.get("description", "").strip(),
                )
                messages.success(request, f"Category '{name}' added.")
        elif action == "update":
            cat = get_object_or_404(ExpenseCategory, pk=request.POST.get("category_id"))
            cat.name = request.POST.get("name", cat.name).strip() or cat.name
            cat.description = request.POST.get("description", "").strip()
            cat.save()
            messages.success(request, f"Category '{cat.name}' updated.")
        elif action == "toggle":
            cat = get_object_or_404(ExpenseCategory, pk=request.POST.get("category_id"))
            cat.is_active = not cat.is_active
            cat.save(update_fields=["is_active"])
            messages.success(request, f"Category '{cat.name}' {'enabled' if cat.is_active else 'disabled'}.")
        return redirect("portal_expense_categories")

    context = {
        "active_nav": "expense_categories",
        "categories": ExpenseCategory.objects.all().order_by("-is_active", "name"),
    }
    return render(request, "portal/expense_categories.html", context)


# ---------------------------------------------------------------------------
# Leave settings (HR Coordinator)
# ---------------------------------------------------------------------------

def _redirect_leave_settings(tab="types", year=None, open_type=None):
    url = reverse("portal_leave_settings")
    params = {}
    if tab in ("types", "holidays"):
        params["tab"] = tab
    if year:
        params["year"] = str(year)
    if open_type:
        params["open_type"] = str(open_type)
    if params:
        return redirect(f"{url}?{urlencode(params)}")
    return redirect(url)


def _safe_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@login_required
def leave_settings_view(request):
    user = request.user
    if not (user.is_hr_coordinator or user.is_director or user.is_superuser):
        messages.error(request, "Leave settings are managed by the HR Coordinator.")
        return redirect("portal_dashboard")

    org = get_default_organization(user)

    if request.method == "POST":
        action = request.POST.get("action")
        posted_tab = request.POST.get("tab")
        posted_year = _safe_int(request.POST.get("year"))

        if action == "create_leave_type":
            name = request.POST.get("name", "").strip()[:50]
            code = request.POST.get("code", "").strip().upper()[:20]
            if not name or not code:
                messages.error(request, "Leave type name and code are required.")
                return _redirect_leave_settings(tab="types", year=posted_year)
            elif LeaveType.objects.filter(code=code).exists():
                messages.error(request, f"Leave type code {code} already exists.")
                return _redirect_leave_settings(tab="types", year=posted_year)
            else:
                new_lt = LeaveType.objects.create(
                    organization=org, name=name, code=code,
                    is_paid=request.POST.get("is_paid") == "1",
                    is_accrual_based=request.POST.get("is_accrual_based") == "1",
                    requires_attachment=request.POST.get("requires_attachment") == "1",
                )
                messages.success(
                    request,
                    f"Leave type '{name}' ({code}) created."
                    + (" Add an entitlement policy below." if new_lt.is_paid else ""),
                )
                return _redirect_leave_settings(tab="types", year=posted_year, open_type=new_lt.id)

        elif action == "update_leave_type":
            lt = get_object_or_404(LeaveType, pk=request.POST.get("leave_type_id"))
            if not lt.organization_id:
                lt.organization = org
            lt.name = (request.POST.get("name", lt.name).strip() or lt.name)[:50]
            lt.is_paid = request.POST.get("is_paid") == "1"
            lt.is_accrual_based = request.POST.get("is_accrual_based") == "1"
            lt.requires_attachment = request.POST.get("requires_attachment") == "1"
            lt.is_active = request.POST.get("is_active") == "1"
            lt.save()
            messages.success(request, f"Leave type '{lt.name}' updated.")
            return _redirect_leave_settings(tab="types", year=posted_year, open_type=lt.id)

        elif action == "save_policy":
            policy_id = request.POST.get("policy_id")
            lt = get_object_or_404(LeaveType, pk=request.POST.get("leave_type_id"))
            policy = LeavePolicy.objects.filter(pk=policy_id).first() if policy_id else LeavePolicy(leave_type=lt)
            values = {
                "yearly_entitlement": _parse_decimal(request.POST.get("yearly_entitlement")),
                "monthly_accrual_rate": _parse_decimal(request.POST.get("monthly_accrual_rate") or "0"),
                "max_carry_forward": _parse_decimal(request.POST.get("max_carry_forward") or "0"),
            }
            if any(v is None or v < 0 for v in values.values()):
                messages.error(request, "Entitlement, accrual rate and carry-forward must be non-negative numbers.")
                return _redirect_leave_settings(tab="types", year=posted_year, open_type=lt.id)
            if (
                values["yearly_entitlement"] > Decimal("999.99")
                or values["monthly_accrual_rate"] > Decimal("99.99")
                or values["max_carry_forward"] > Decimal("999.99")
            ):
                messages.error(request, "Entitlement/carry-forward cannot exceed 999.99 days, and monthly accrual rate cannot exceed 99.99 days.")
                return _redirect_leave_settings(tab="types", year=posted_year, open_type=lt.id)
            frequency = request.POST.get("accrual_frequency", "MONTHLY")
            if frequency not in ("MONTHLY", "YEARLY", "NONE"):
                frequency = "MONTHLY"
            policy.leave_type = lt
            policy.name = request.POST.get("name", "").strip() or f"{lt.name} Policy"
            policy.accrual_frequency = frequency
            policy.yearly_entitlement = values["yearly_entitlement"]
            policy.monthly_accrual_rate = values["monthly_accrual_rate"]
            policy.max_carry_forward = values["max_carry_forward"]
            policy.probation_period_months = max(0, _safe_int(request.POST.get("probation_period_months"), 0))
            policy.is_active = request.POST.get("is_active", "1") == "1"
            policy.save()
            messages.success(request, f"Policy '{policy.name}' saved.")
            return _redirect_leave_settings(tab="types", year=posted_year, open_type=lt.id)

        elif action == "add_holiday":
            hdate = _parse_date(request.POST.get("date"))
            name = request.POST.get("name", "").strip()
            branch_id = request.POST.get("branch") or None
            if not hdate or not name:
                messages.error(request, "Holiday date and name are required.")
                return _redirect_leave_settings(tab="holidays", year=posted_year)
            branch = user.get_accessible_branches().filter(id=branch_id).first() if branch_id else None
            calendar, _ = HolidayCalendar.objects.get_or_create(
                organization=org, branch=branch, year=hdate.year,
                defaults={"name": f"{branch.name if branch else 'All Branches'} Holidays {hdate.year}"},
            )
            holiday, created = Holiday.objects.get_or_create(
                calendar=calendar, date=hdate,
                defaults={"name": name, "is_optional": request.POST.get("is_optional") == "1"},
            )
            if created:
                messages.success(request, f"Holiday '{name}' on {hdate:%d %b %Y} added.")
            else:
                messages.warning(
                    request,
                    f"A holiday already exists on {hdate:%d %b %Y} for {branch.name if branch else 'All Branches'} ('{holiday.name}'). Edit it instead.",
                )
            return _redirect_leave_settings(tab="holidays", year=hdate.year)

        elif action == "update_holiday":
            holiday = get_object_or_404(
                Holiday.objects.select_related("calendar"),
                pk=request.POST.get("holiday_id"),
                calendar__organization=org,
            )
            hdate = _parse_date(request.POST.get("date"))
            name = request.POST.get("name", "").strip()
            branch_id = request.POST.get("branch") or None
            if not hdate or not name:
                messages.error(request, "Holiday date and name are required.")
                return _redirect_leave_settings(tab="holidays", year=posted_year or holiday.date.year)
            branch = user.get_accessible_branches().filter(id=branch_id).first() if branch_id else None
            target_calendar, _ = HolidayCalendar.objects.get_or_create(
                organization=org, branch=branch, year=hdate.year,
                defaults={"name": f"{branch.name if branch else 'All Branches'} Holidays {hdate.year}"},
            )
            duplicate = Holiday.objects.filter(calendar=target_calendar, date=hdate).exclude(pk=holiday.pk).first()
            if duplicate:
                messages.error(
                    request,
                    f"Cannot move holiday: '{duplicate.name}' is already set on {hdate:%d %b %Y} for {branch.name if branch else 'All Branches'}.",
                )
                return _redirect_leave_settings(tab="holidays", year=holiday.date.year)
            holiday.calendar = target_calendar
            holiday.date = hdate
            holiday.name = name
            holiday.is_optional = request.POST.get("is_optional") == "1"
            holiday.save()
            messages.success(request, f"Holiday '{holiday.name}' updated.")
            return _redirect_leave_settings(tab="holidays", year=hdate.year)

        elif action == "delete_holiday":
            holiday = Holiday.objects.filter(
                pk=request.POST.get("holiday_id"),
                calendar__organization=org,
            ).first()
            target_year = holiday.date.year if holiday else posted_year
            if holiday:
                holiday_name = holiday.name
                holiday.delete()
                messages.info(request, f"Holiday '{holiday_name}' removed.")
            return _redirect_leave_settings(tab="holidays", year=target_year)

        elif action == "run_accrual":
            res = run_batch_monthly_accrual()
            messages.success(
                request,
                f"Earned-leave accrual for {res['period_key']}: {res['credited']} credited, "
                f"{res['skipped']} skipped (already credited, on probation, or not yet joined).",
            )
            return _redirect_leave_settings(tab=posted_tab or "types", year=posted_year)

        return _redirect_leave_settings(tab=posted_tab or "types", year=posted_year)

    today = date.today()
    year = _safe_int(request.GET.get("year"), today.year)
    active_tab = "holidays" if request.GET.get("tab") == "holidays" else "types"
    open_type_id = _safe_int(request.GET.get("open_type"))

    holidays = list(
        Holiday.objects.filter(calendar__year=year, calendar__organization=org)
        .filter(Q(calendar__branch__isnull=True) | Q(calendar__branch__in=user.get_accessible_branches()))
        .select_related("calendar__branch")
        .order_by("date")
    )
    leave_types = list(LeaveType.objects.all().prefetch_related("policies").order_by("-is_active", "name"))
    branches = list(user.get_accessible_branches())

    current_period_key = today.strftime("%Y-%m")
    current_period_credited_count = LeaveAccrualRun.objects.filter(period_key=current_period_key).count()
    active_leave_types_count = sum(1 for lt in leave_types if lt.is_active)
    all_policies = [p for lt in leave_types for p in lt.policies.all()]
    active_policies_count = sum(1 for p in all_policies if p.is_active)
    monthly_accrual_policies_count = sum(
        1 for p in all_policies if p.is_active and p.accrual_frequency == "MONTHLY"
    )
    mandatory_holidays_count = sum(1 for h in holidays if not h.is_optional)
    optional_holidays_count = sum(1 for h in holidays if h.is_optional)

    weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    configured_weekends = getattr(settings, "LEAVE_WEEKEND_DAYS", [])
    weekend_days_label = ", ".join(
        weekday_names[d] for d in configured_weekends if 0 <= d < 7
    )

    context = {
        "active_nav": "leave_settings",
        "active_tab": active_tab,
        "open_type_id": open_type_id,
        "leave_types": leave_types,
        "holidays": holidays,
        "today": today,
        "year": year,
        "years": [year - 1, year, year + 1],
        "branches": branches,
        "current_period_key": current_period_key,
        "current_period_label": today.strftime("%B %Y"),
        "current_period_credited_count": current_period_credited_count,
        "active_leave_types_count": active_leave_types_count,
        "active_policies_count": active_policies_count,
        "monthly_accrual_policies_count": monthly_accrual_policies_count,
        "mandatory_holidays_count": mandatory_holidays_count,
        "optional_holidays_count": optional_holidays_count,
        "weekend_days_label": weekend_days_label,
    }
    return render(request, "portal/leave_settings.html", context)


@login_required
def leave_record_on_behalf_view(request):
    user = request.user
    if not (user.is_hr_coordinator or user.is_superuser):
        messages.error(request, "Only the HR Coordinator can record leave on behalf of employees.")
        return redirect("portal_dashboard")

    employees = User.objects.filter(
        is_active=True, primary_branch__in=user.get_accessible_branches()
    ).order_by("first_name", "last_name")

    if request.method == "POST":
        emp = get_object_or_404(employees, pk=request.POST.get("employee"))
        lt = get_object_or_404(LeaveType, pk=request.POST.get("leave_type"), is_active=True)
        start = _parse_date(request.POST.get("start_date"))
        end = _parse_date(request.POST.get("end_date")) or start
        is_half = request.POST.get("is_half_day") == "1"
        if not start:
            messages.error(request, "Start date is required.")
            return redirect("portal_leave_record")
        try:
            app, warnings = record_leave_on_behalf(
                hr_user=user, employee=emp, leave_type=lt, start_date=start, end_date=end,
                reason=request.POST.get("reason", ""), is_half_day=is_half,
                half_day_period=request.POST.get("half_day_period", "FIRST_HALF"),
                attachment=request.FILES.get("attachment"),
            )
            for w in warnings:
                messages.warning(request, w)
            messages.success(
                request,
                f"Recorded {app.number_of_days:g} day(s) of {lt.name} for {emp.full_name} ({app.application_number}). Balance updated.",
            )
            return redirect(f"/leaves/ledger/?emp_id={emp.id}")
        except LeaveRequestError as e:
            messages.error(request, " ".join(e.messages))
        return redirect("portal_leave_record")

    context = {
        "active_nav": "leave_record",
        "employees": employees,
        "leave_types": LeaveType.objects.filter(is_active=True),
        "today": date.today(),
    }
    return render(request, "portal/leave_record.html", context)


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

@login_required
def password_change_view(request):
    if request.method == "POST":
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            messages.success(request, "Your password has been changed.")
        else:
            for errors in form.errors.values():
                for e in errors:
                    messages.error(request, e)
    return redirect("portal_profile")
