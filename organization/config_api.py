"""
Read-only staff & configuration endpoints for the mobile app's sidebar.

These mirror the portal's Employees, Offices & Branches, Expense Categories and
Leave Settings pages, using the same role rules as the web sidebar.
"""
from decimal import Decimal, InvalidOperation

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import RoleChoices, User
from accounts.services import (
    EmployeeError, can_manage_users, can_set_scope, create_employee, lead_candidates,
    password_reset_block_reason, set_employee_password, toggle_employee_active, update_employee,
)
from expenses.models import ExpenseCategory
from leaves.models import LeavePolicy, LeaveType
from organization.models import Branch, Department

MANAGER_ROLES = [RoleChoices.DIRECTOR, RoleChoices.ADMIN_MANAGER, RoleChoices.HR_COORDINATOR]


def can_view_staff(user):
    """Employees / Branches pages: Director, Admin Manager, HR Coordinator (and the superuser)."""
    return user.is_superuser or user.role in MANAGER_ROLES


def can_view_expense_config(user):
    return user.is_superuser or user.role in [RoleChoices.DIRECTOR, RoleChoices.ADMIN_MANAGER]


def can_view_leave_config(user):
    return user.is_superuser or user.role in [RoleChoices.DIRECTOR, RoleChoices.HR_COORDINATOR]


def can_manage_branches(user):
    """Create / edit / deactivate offices — same rule as the web portal."""
    return user.is_superuser or user.role == RoleChoices.DIRECTOR or (
        user.is_global_scope and user.role in [RoleChoices.ADMIN_MANAGER, RoleChoices.HR_COORDINATOR]
    )


def _forbidden(msg="You do not have access to this page."):
    return Response({"detail": msg}, status=status.HTTP_403_FORBIDDEN)


def _bad(msg):
    return Response({"detail": msg}, status=status.HTTP_400_BAD_REQUEST)


def _bool(value, default=False):
    if value is None:
        return default
    return str(value).lower() in ("1", "true", "yes", "on")


def _text(data, key, max_len=None):
    value = data.get(key)
    value = "" if value is None else str(value).strip()
    return value[:max_len] if max_len else value


def _default_org(user):
    from portal.admin_views import get_default_organization  # local import avoids an app import cycle
    return get_default_organization(user)


# ---------------------------------------------------------------- Employees

class EmployeeSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    role_display = serializers.CharField(source="get_role_display", read_only=True)
    branch = serializers.SerializerMethodField()
    designation = serializers.SerializerMethodField()
    department = serializers.SerializerMethodField()
    reporting_lead = serializers.SerializerMethodField()
    date_of_joining = serializers.SerializerMethodField()
    department_id = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "employee_id", "first_name", "last_name", "full_name", "email", "phone",
            "role", "role_display", "is_active", "is_global_scope", "branch",
            "designation", "department", "department_id", "reporting_lead", "date_of_joining",
            "permissions",
        ]

    def get_department_id(self, obj):
        p = self._profile(obj)
        return p.department_id if p else None

    def get_permissions(self, obj):
        actor = self.context.get("actor")
        if actor is None:
            return None
        manage = can_manage_users(actor)
        return {
            "can_edit": manage,
            "can_toggle_active": manage and obj.pk != actor.pk and (actor.is_superuser or not obj.is_superuser),
            "can_reset_password": password_reset_block_reason(actor, obj) is None,
        }

    @staticmethod
    def _profile(obj):
        return getattr(obj, "profile", None)

    def get_branch(self, obj):
        b = obj.primary_branch
        return {"id": b.id, "code": b.code, "name": b.name, "city": b.city} if b else None

    def get_designation(self, obj):
        p = self._profile(obj)
        return p.designation if p else ""

    def get_department(self, obj):
        p = self._profile(obj)
        return p.department.name if p and p.department else None

    def get_reporting_lead(self, obj):
        p = self._profile(obj)
        if not p or not p.reporting_lead:
            return None
        return {"id": p.reporting_lead.id, "name": p.reporting_lead.full_name, "employee_id": p.reporting_lead.employee_id}

    def get_date_of_joining(self, obj):
        p = self._profile(obj)
        return p.date_of_joining.isoformat() if p and p.date_of_joining else None


def _employee_queryset(user):
    return User.objects.filter(primary_branch__in=user.get_accessible_branches()).select_related(
        "primary_branch", "profile__department", "profile__reporting_lead"
    )


class EmployeeListView(APIView):
    """
    GET /api/v1/organization/employees/?q=&branch=&role=&status=active|inactive
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not can_view_staff(request.user):
            return _forbidden()
        qs = _employee_queryset(request.user)
        p = request.query_params
        q = (p.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(employee_id__icontains=q)
                | Q(email__icontains=q) | Q(profile__designation__icontains=q)
                | Q(profile__department__name__icontains=q) | Q(primary_branch__name__icontains=q)
            )
        if p.get("branch") and p.get("branch") != "all":
            qs = qs.filter(primary_branch_id=p.get("branch"))
        if p.get("role") in RoleChoices.values:
            qs = qs.filter(role=p.get("role"))
        if p.get("status") == "active":
            qs = qs.filter(is_active=True)
        elif p.get("status") == "inactive":
            qs = qs.filter(is_active=False)
        return Response(EmployeeSerializer(qs.order_by("employee_id"), many=True, context={"actor": request.user}).data)

    def post(self, request):
        try:
            emp, generated = create_employee(request.user, request.data)
        except EmployeeError as e:
            return _bad(str(e))
        emp = _employee_queryset(request.user).filter(pk=emp.pk).first() or emp
        data = EmployeeSerializer(emp, context={"actor": request.user}).data
        data["generated_password"] = generated
        return Response(data, status=status.HTTP_201_CREATED)


class EmployeeDetailView(APIView):
    """GET /api/v1/organization/employees/<pk>/"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not can_view_staff(request.user):
            return _forbidden()
        emp = get_object_or_404(_employee_queryset(request.user), pk=pk)
        return Response(EmployeeSerializer(emp, context={"actor": request.user}).data)

    def patch(self, request, pk):
        emp = get_object_or_404(_employee_queryset(request.user), pk=pk)
        try:
            update_employee(request.user, emp, request.data)
        except EmployeeError as e:
            return _bad(str(e))
        emp = _employee_queryset(request.user).filter(pk=pk).first() or emp
        return Response(EmployeeSerializer(emp, context={"actor": request.user}).data)


class EmployeeToggleActiveView(APIView):
    """POST /api/v1/organization/employees/<pk>/toggle-active/"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        emp = get_object_or_404(_employee_queryset(request.user), pk=pk)
        try:
            toggle_employee_active(request.user, emp)
        except EmployeeError as e:
            return _bad(str(e))
        return Response(EmployeeSerializer(emp, context={"actor": request.user}).data)


class EmployeeSetPasswordView(APIView):
    """POST /api/v1/organization/employees/<pk>/set-password/  {new_password} — Director / HR Coordinator only."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        emp = get_object_or_404(_employee_queryset(request.user), pk=pk)
        reason = password_reset_block_reason(request.user, emp)
        if reason:
            return _forbidden(reason)
        try:
            set_employee_password(request.user, emp, request.data.get("new_password", ""))
        except EmployeeError as e:
            return _bad(str(e))
        return Response({"detail": f"New password set for {emp.full_name} ({emp.employee_id})."})


class EmployeeFormOptionsView(APIView):
    """GET /api/v1/organization/employees/form-options/?exclude=<pk> — choices for the add / edit form."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if not can_manage_users(user):
            return _forbidden()
        exclude = User.objects.filter(pk=request.query_params.get("exclude")).first() if request.query_params.get("exclude") else None
        return Response({
            "branches": [{"id": b.id, "code": b.code, "name": b.name} for b in user.get_accessible_branches().order_by("name")],
            "departments": [{"id": d.id, "code": d.code, "name": d.name} for d in Department.objects.filter(is_active=True).order_by("name")],
            "roles": [{"value": v, "label": l} for v, l in RoleChoices.choices],
            "leads": [
                {"id": u.id, "name": u.full_name, "employee_id": u.employee_id, "role": u.get_role_display()}
                for u in lead_candidates(user, exclude=exclude)
            ],
            "can_set_scope": can_set_scope(user),
        })


# ---------------------------------------------------------------- Branches

class BranchOverviewView(APIView):
    """GET /api/v1/organization/branches/overview/ — branches with staff counts."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if not can_view_staff(user):
            return _forbidden()
        sees_all = user.is_superuser or user.role == RoleChoices.DIRECTOR or (
            user.is_global_scope and user.role in [RoleChoices.ADMIN_MANAGER, RoleChoices.HR_COORDINATOR]
        )
        base = Branch.objects.all() if sees_all else user.get_accessible_branches()
        branches = base.annotate(
            employee_count=Count("primary_employees", filter=Q(primary_employees__is_active=True), distinct=True),
        ).order_by("-is_active", "code")
        return Response([_branch_json(b, b.employee_count) for b in branches])

    def post(self, request):
        if not can_manage_branches(request.user):
            return _forbidden("You do not have permission to manage branches.")
        fields, error = _branch_fields(request.data)
        if error:
            return _bad(error)
        if Branch.objects.filter(code=fields["code"]).exists():
            return _bad(f"A branch with code '{fields['code']}' already exists.")
        fields.setdefault("is_active", True)
        branch = Branch.objects.create(organization=_default_org(request.user), **fields)
        return Response(_branch_json(branch, 0), status=status.HTTP_201_CREATED)


def _branch_json(b, employee_count):
    return {
        "id": b.id, "code": b.code, "name": b.name, "address": b.address, "city": b.city,
        "country": b.country, "phone": b.phone, "is_active": b.is_active, "employee_count": employee_count,
    }


def _branch_fields(data, partial=False):
    fields = {}
    for key in ("code", "name", "city", "country", "phone", "address"):
        if key in data or not partial:
            fields[key] = _text(data, key)
    if "code" in fields:
        fields["code"] = fields["code"].upper()[:20]
    if "name" in fields:
        fields["name"] = fields["name"][:255]
    if "is_active" in data:
        fields["is_active"] = _bool(data.get("is_active"))
    if ("code" in fields and not fields["code"]) or ("name" in fields and not fields["name"]):
        return None, "Branch code and name are required."
    return fields, None


class BranchUpdateView(APIView):
    """PATCH /api/v1/organization/branches/<pk>/"""
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        if not can_manage_branches(request.user):
            return _forbidden("You do not have permission to manage branches.")
        branch = get_object_or_404(Branch, pk=pk)
        fields, error = _branch_fields(request.data, partial=True)
        if error:
            return _bad(error)
        if "code" in fields and Branch.objects.filter(code=fields["code"]).exclude(pk=branch.pk).exists():
            return _bad(f"Another branch already uses code '{fields['code']}'.")
        for key, value in fields.items():
            setattr(branch, key, value)
        branch.save()
        count = branch.primary_employees.filter(is_active=True).count()
        return Response(_branch_json(branch, count))


# ---------------------------------------------------------------- Configuration

class ExpenseCategoryConfigView(APIView):
    """GET /api/v1/settings/expense-categories/ — all categories, including inactive ones."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not can_view_expense_config(request.user):
            return _forbidden()
        return Response([_category_json(c) for c in ExpenseCategory.objects.order_by("-is_active", "name")])

    def post(self, request):
        if not can_view_expense_config(request.user):
            return _forbidden("Expense categories are managed by the Admin Manager.")
        name = _text(request.data, "name", 100)
        code = _text(request.data, "code", 50).upper()
        if not name or not code:
            return _bad("Category name and code are required.")
        if ExpenseCategory.objects.filter(code=code).exists():
            return _bad(f"Category code {code} already exists.")
        cat = ExpenseCategory.objects.create(
            organization=_default_org(request.user), name=name, code=code,
            description=_text(request.data, "description"),
        )
        return Response(_category_json(cat), status=status.HTTP_201_CREATED)


def _category_json(c):
    return {"id": c.id, "name": c.name, "code": c.code, "description": c.description, "is_active": c.is_active}


class ExpenseCategoryUpdateView(APIView):
    """PATCH /api/v1/settings/expense-categories/<pk>/  {name?, description?, is_active?}"""
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        if not can_view_expense_config(request.user):
            return _forbidden("Expense categories are managed by the Admin Manager.")
        cat = get_object_or_404(ExpenseCategory, pk=pk)
        if "name" in request.data:
            name = _text(request.data, "name", 100)
            if not name:
                return _bad("Category name is required.")
            cat.name = name
        if "description" in request.data:
            cat.description = _text(request.data, "description")
        if "is_active" in request.data:
            cat.is_active = _bool(request.data.get("is_active"))
        cat.save()
        return Response(_category_json(cat))


class LeaveSettingsView(APIView):
    """GET /api/v1/settings/leave-types/ — leave types with their entitlement policies."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not can_view_leave_config(request.user):
            return _forbidden()
        types = LeaveType.objects.prefetch_related("policies").order_by("-is_active", "name")
        return Response([_leave_type_json(lt) for lt in types])

    def post(self, request):
        if not can_view_leave_config(request.user):
            return _forbidden("Leave settings are managed by the HR Coordinator.")
        name = _text(request.data, "name", 50)
        code = _text(request.data, "code", 20).upper()
        if not name or not code:
            return _bad("Leave type name and code are required.")
        if LeaveType.objects.filter(code=code).exists():
            return _bad(f"Leave type code {code} already exists.")
        lt = LeaveType.objects.create(
            organization=_default_org(request.user), name=name, code=code,
            is_paid=_bool(request.data.get("is_paid"), True),
            is_accrual_based=_bool(request.data.get("is_accrual_based")),
            requires_attachment=_bool(request.data.get("requires_attachment")),
        )
        return Response(_leave_type_json(lt), status=status.HTTP_201_CREATED)


def _policy_json(p):
    return {
        "id": p.id, "name": p.name,
        "yearly_entitlement": str(p.yearly_entitlement),
        "accrual_frequency": p.accrual_frequency,
        "accrual_frequency_display": p.get_accrual_frequency_display(),
        "monthly_accrual_rate": str(p.monthly_accrual_rate),
        "max_carry_forward": str(p.max_carry_forward),
        "probation_period_months": p.probation_period_months,
        "effective_date": p.effective_date.isoformat(),
        "is_active": p.is_active,
    }


def _leave_type_json(lt):
    policies = sorted(lt.policies.all(), key=lambda p: (not p.is_active, p.effective_date))
    return {
        "id": lt.id, "name": lt.name, "code": lt.code, "is_paid": lt.is_paid,
        "is_accrual_based": lt.is_accrual_based, "requires_attachment": lt.requires_attachment,
        "is_active": lt.is_active, "policies": [_policy_json(p) for p in policies],
    }


class LeaveTypeUpdateView(APIView):
    """PATCH /api/v1/settings/leave-types/<pk>/"""
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        if not can_view_leave_config(request.user):
            return _forbidden("Leave settings are managed by the HR Coordinator.")
        lt = get_object_or_404(LeaveType, pk=pk)
        if not lt.organization_id:
            lt.organization = _default_org(request.user)
        if "name" in request.data:
            name = _text(request.data, "name", 50)
            if not name:
                return _bad("Leave type name is required.")
            lt.name = name
        for key in ("is_paid", "is_accrual_based", "requires_attachment", "is_active"):
            if key in request.data:
                setattr(lt, key, _bool(request.data.get(key)))
        lt.save()
        return Response(_leave_type_json(lt))


def _decimal(value):
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None


def _save_policy(policy, lt, data):
    """Validate and save a leave policy (same limits as the web portal). Returns an error string or None."""
    values = {
        "yearly_entitlement": _decimal(data.get("yearly_entitlement", policy.yearly_entitlement if policy.pk else None)),
        "monthly_accrual_rate": _decimal(data.get("monthly_accrual_rate", policy.monthly_accrual_rate if policy.pk else 0) or 0),
        "max_carry_forward": _decimal(data.get("max_carry_forward", policy.max_carry_forward if policy.pk else 0) or 0),
    }
    if any(v is None or v < 0 for v in values.values()):
        return "Entitlement, accrual rate and carry-forward must be non-negative numbers."
    if values["yearly_entitlement"] > Decimal("999.99") or values["monthly_accrual_rate"] > Decimal("99.99") \
            or values["max_carry_forward"] > Decimal("999.99"):
        return "Entitlement/carry-forward cannot exceed 999.99 days, and monthly accrual rate cannot exceed 99.99 days."
    frequency = data.get("accrual_frequency", policy.accrual_frequency if policy.pk else "MONTHLY")
    if frequency not in ("MONTHLY", "YEARLY", "NONE"):
        frequency = "MONTHLY"
    try:
        probation = max(0, int(data.get("probation_period_months", policy.probation_period_months if policy.pk else 0) or 0))
    except (TypeError, ValueError):
        return "Probation period must be a whole number of months."
    policy.leave_type = lt
    if "name" in data or not policy.pk:
        policy.name = _text(data, "name", 100) or f"{lt.name} Policy"
    policy.accrual_frequency = frequency
    policy.yearly_entitlement = values["yearly_entitlement"]
    policy.monthly_accrual_rate = values["monthly_accrual_rate"]
    policy.max_carry_forward = values["max_carry_forward"]
    policy.probation_period_months = probation
    if "is_active" in data or not policy.pk:
        policy.is_active = _bool(data.get("is_active"), True)
    policy.save()
    return None


class LeavePolicyCreateView(APIView):
    """POST /api/v1/settings/leave-types/<pk>/policies/"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not can_view_leave_config(request.user):
            return _forbidden("Leave settings are managed by the HR Coordinator.")
        lt = get_object_or_404(LeaveType, pk=pk)
        policy = LeavePolicy(leave_type=lt)
        error = _save_policy(policy, lt, request.data)
        if error:
            return _bad(error)
        return Response(_policy_json(policy), status=status.HTTP_201_CREATED)


class LeavePolicyUpdateView(APIView):
    """PATCH /api/v1/settings/leave-policies/<pk>/"""
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        if not can_view_leave_config(request.user):
            return _forbidden("Leave settings are managed by the HR Coordinator.")
        policy = get_object_or_404(LeavePolicy.objects.select_related("leave_type"), pk=pk)
        error = _save_policy(policy, policy.leave_type, request.data)
        if error:
            return _bad(error)
        return Response(_policy_json(policy))
