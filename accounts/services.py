"""
Employee management rules shared by the web portal and the mobile API,
so both channels enforce exactly the same permissions and validation.
"""
import secrets
from datetime import datetime

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import EmployeeProfile, RoleChoices, User
from organization.models import Department

MANAGER_ROLES = [RoleChoices.DIRECTOR, RoleChoices.ADMIN_MANAGER, RoleChoices.HR_COORDINATOR]
LEAD_ROLES = [RoleChoices.LEAD_MANAGER, RoleChoices.ADMIN_MANAGER, RoleChoices.HR_COORDINATOR, RoleChoices.DIRECTOR]


class EmployeeError(Exception):
    """A user-facing validation / permission error."""


# ------------------------------------------------------------------ permissions

def can_manage_users(user):
    """Add / edit / deactivate employees: Director, Admin Manager, HR Coordinator."""
    return user.is_superuser or user.role in MANAGER_ROLES


def can_reset_passwords(user):
    """Setting another employee's password is reserved for the Director and the HR Coordinator."""
    return user.is_superuser or user.role in [RoleChoices.DIRECTOR, RoleChoices.HR_COORDINATOR]


def can_set_scope(user):
    return user.is_superuser or user.role == RoleChoices.DIRECTOR


def password_reset_block_reason(actor, target):
    """Return why `actor` may not set `target`'s password, or None when allowed."""
    if not can_reset_passwords(actor):
        return "Only the Director or the HR Coordinator can change an employee's password."
    if actor.pk == target.pk:
        return "Use 'Change password' on your own profile to change your password."
    if target.is_superuser and not actor.is_superuser:
        return "The system administrator's password can only be changed by the administrator."
    if target.role == RoleChoices.DIRECTOR and not (actor.is_superuser or actor.role == RoleChoices.DIRECTOR):
        return "Only the Director can change another Director's password."
    if target.primary_branch and not actor.can_access_branch(target.primary_branch):
        return "This employee belongs to a branch outside your access scope."
    return None


def can_access_employee(actor, target):
    return not target.primary_branch or actor.can_access_branch(target.primary_branch)


# ------------------------------------------------------------------ helpers

def _parse_date(value):
    if not value:
        return None
    if hasattr(value, "year"):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _str(data, key, default=""):
    value = data.get(key, default)
    return "" if value is None else str(value).strip()


def _validate_password(password, user=None):
    if len(password) < 8:
        raise EmployeeError("Password must be at least 8 characters.")
    try:
        validate_password(password, user=user)
    except ValidationError as e:
        raise EmployeeError(" ".join(e.messages))


def lead_candidates(actor, exclude=None):
    qs = User.objects.filter(
        is_active=True, primary_branch__in=actor.get_accessible_branches(), role__in=LEAD_ROLES,
    )
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    return qs.order_by("first_name", "last_name")


# ------------------------------------------------------------------ actions

@transaction.atomic
def create_employee(actor, data):
    """
    Create an employee. Returns (user, generated_password_or_None).
    `data` is a dict-like of form / JSON fields.
    """
    if not can_manage_users(actor):
        raise EmployeeError("You do not have administrative authorization to register new staff.")

    emp_id = _str(data, "employee_id").upper()
    email = _str(data, "email").lower()
    if not emp_id or not email:
        raise EmployeeError("Employee ID and Email address are required fields.")
    if User.objects.filter(Q(employee_id__iexact=emp_id) | Q(email__iexact=email)).exists():
        raise EmployeeError(f"An employee profile with ID '{emp_id}' or Email '{email}' already exists.")

    role = _str(data, "role") or RoleChoices.EMPLOYEE
    if role not in RoleChoices.values:
        role = RoleChoices.EMPLOYEE

    password = _str(data, "password")
    generated = None
    if not password:
        password = generated = "GW-" + secrets.token_urlsafe(9)
    else:
        _validate_password(password)

    accessible = actor.get_accessible_branches()
    branch_id = data.get("primary_branch") or data.get("branch")
    branch = accessible.filter(id=branch_id).first() if branch_id else actor.primary_branch
    if branch_id and branch is None:
        raise EmployeeError("You cannot assign employees to that branch.")

    user = User.objects.create_user(
        username=emp_id, employee_id=emp_id, email=email, password=password,
        first_name=_str(data, "first_name"), last_name=_str(data, "last_name"),
        phone=_str(data, "phone"), role=role, primary_branch=branch,
    )
    if can_set_scope(actor) and str(data.get("is_global_scope", "")).lower() in ("1", "true"):
        user.is_global_scope = True
        user.save(update_fields=["is_global_scope"])

    dept_id = data.get("department")
    lead_id = data.get("reporting_lead")
    EmployeeProfile.objects.create(
        user=user,
        department=Department.objects.filter(id=dept_id).first() if dept_id else None,
        designation=_str(data, "designation") or "Staff Specialist",
        reporting_lead=User.objects.filter(id=lead_id).first() if lead_id else None,
        date_of_joining=_parse_date(data.get("date_of_joining")) or timezone.now().date(),
    )
    return user, generated


@transaction.atomic
def update_employee(actor, emp, data):
    """Update an employee's record from form / JSON data. Only keys present in `data` are changed."""
    if not can_manage_users(actor):
        raise EmployeeError("You are not authorised to manage employee records.")
    if not can_access_employee(actor, emp):
        raise EmployeeError("This employee belongs to a branch outside your access scope.")

    profile, _ = EmployeeProfile.objects.get_or_create(user=emp)

    if "email" in data:
        email = _str(data, "email").lower()
        if not email:
            raise EmployeeError("E-mail is required.")
        if User.objects.filter(email__iexact=email).exclude(pk=emp.pk).exists():
            raise EmployeeError(f"Another employee already uses {email}.")
        emp.email = email
    for field in ("first_name", "last_name", "phone"):
        if field in data:
            setattr(emp, field, _str(data, field))
    if "role" in data and _str(data, "role") in RoleChoices.values:
        emp.role = _str(data, "role")
    if "primary_branch" in data:
        branch_id = data.get("primary_branch")
        if branch_id:
            branch = actor.get_accessible_branches().filter(id=branch_id).first()
            if branch is None:
                raise EmployeeError("You cannot assign employees to that branch.")
            emp.primary_branch = branch
    if "is_global_scope" in data and can_set_scope(actor):
        emp.is_global_scope = str(data.get("is_global_scope")).lower() in ("1", "true")
    emp.save()

    if "department" in data:
        dept_id = data.get("department")
        profile.department = Department.objects.filter(id=dept_id).first() if dept_id else None
    if "designation" in data:
        profile.designation = _str(data, "designation")
    if "reporting_lead" in data:
        lead_id = data.get("reporting_lead")
        profile.reporting_lead = User.objects.filter(id=lead_id).exclude(pk=emp.pk).first() if lead_id else None
    if "date_of_joining" in data:
        doj = _parse_date(data.get("date_of_joining"))
        if doj:
            profile.date_of_joining = doj
    profile.save()
    return emp


def toggle_employee_active(actor, emp):
    if not can_manage_users(actor):
        raise EmployeeError("You are not authorised to manage employee records.")
    if not can_access_employee(actor, emp):
        raise EmployeeError("This employee belongs to a branch outside your access scope.")
    if emp.pk == actor.pk:
        raise EmployeeError("You cannot deactivate your own account.")
    if emp.is_superuser and not actor.is_superuser:
        raise EmployeeError("The system administrator account cannot be deactivated.")
    emp.is_active = not emp.is_active
    emp.save(update_fields=["is_active"])
    return emp


def set_employee_password(actor, emp, new_password):
    reason = password_reset_block_reason(actor, emp)
    if reason:
        raise EmployeeError(reason)
    new_password = (new_password or "").strip()
    _validate_password(new_password, user=emp)
    emp.set_password(new_password)
    emp.save(update_fields=["password"])
    return emp
