"""
Read-only staff & configuration endpoints for the mobile app's sidebar.

These mirror the portal's Employees, Offices & Branches, Expense Categories and
Leave Settings pages, using the same role rules as the web sidebar.
"""
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import RoleChoices, User
from expenses.models import ExpenseCategory
from leaves.models import LeavePolicy, LeaveType
from organization.models import Branch

MANAGER_ROLES = [RoleChoices.DIRECTOR, RoleChoices.ADMIN_MANAGER, RoleChoices.HR_COORDINATOR]


def can_view_staff(user):
    """Employees / Branches pages: Director, Admin Manager, HR Coordinator (and the superuser)."""
    return user.is_superuser or user.role in MANAGER_ROLES


def can_view_expense_config(user):
    return user.is_superuser or user.role in [RoleChoices.DIRECTOR, RoleChoices.ADMIN_MANAGER]


def can_view_leave_config(user):
    return user.is_superuser or user.role in [RoleChoices.DIRECTOR, RoleChoices.HR_COORDINATOR]


def _forbidden():
    return Response({"detail": "You do not have access to this page."}, status=status.HTTP_403_FORBIDDEN)


# ---------------------------------------------------------------- Employees

class EmployeeSerializer(serializers.ModelSerializer):
    full_name = serializers.CharField(read_only=True)
    role_display = serializers.CharField(source="get_role_display", read_only=True)
    branch = serializers.SerializerMethodField()
    designation = serializers.SerializerMethodField()
    department = serializers.SerializerMethodField()
    reporting_lead = serializers.SerializerMethodField()
    date_of_joining = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "employee_id", "first_name", "last_name", "full_name", "email", "phone",
            "role", "role_display", "is_active", "is_global_scope", "branch",
            "designation", "department", "reporting_lead", "date_of_joining",
        ]

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
        return Response(EmployeeSerializer(qs.order_by("employee_id"), many=True).data)


class EmployeeDetailView(APIView):
    """GET /api/v1/organization/employees/<pk>/"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not can_view_staff(request.user):
            return _forbidden()
        emp = get_object_or_404(_employee_queryset(request.user), pk=pk)
        return Response(EmployeeSerializer(emp).data)


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
        return Response([
            {
                "id": b.id, "code": b.code, "name": b.name, "address": b.address, "city": b.city,
                "country": b.country, "phone": b.phone, "is_active": b.is_active,
                "employee_count": b.employee_count,
            }
            for b in branches
        ])


# ---------------------------------------------------------------- Configuration

class ExpenseCategoryConfigView(APIView):
    """GET /api/v1/settings/expense-categories/ — all categories, including inactive ones."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not can_view_expense_config(request.user):
            return _forbidden()
        return Response([
            {"id": c.id, "name": c.name, "code": c.code, "description": c.description, "is_active": c.is_active}
            for c in ExpenseCategory.objects.order_by("-is_active", "name")
        ])


class LeaveSettingsView(APIView):
    """GET /api/v1/settings/leave-types/ — leave types with their entitlement policies."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not can_view_leave_config(request.user):
            return _forbidden()
        types = LeaveType.objects.prefetch_related("policies").order_by("-is_active", "name")
        data = []
        for lt in types:
            policies = sorted(lt.policies.all(), key=lambda p: (not p.is_active, p.effective_date), reverse=False)
            data.append({
                "id": lt.id, "name": lt.name, "code": lt.code, "is_paid": lt.is_paid,
                "is_accrual_based": lt.is_accrual_based, "requires_attachment": lt.requires_attachment,
                "is_active": lt.is_active,
                "policies": [
                    {
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
                    for p in policies
                ],
            })
        return Response(data)
