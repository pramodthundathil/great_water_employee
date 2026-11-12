"""
Mobile-oriented aggregate endpoints:
  GET /api/v1/dashboard/summary/   — everything the home screen needs in one call
  GET /api/v1/approvals/pending/   — items awaiting the signed-in user's action
"""
from datetime import date
from decimal import Decimal

from django.db.models import Sum
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import RoleChoices
from expenses.models import ExpenseClaim, ExpenseClaimStatus
from expenses.serializers import ExpenseClaimListSerializer
from leaves.models import LeaveApplication, LeaveApplicationStatus
from leaves.serializers import LeaveApplicationListSerializer
from leaves.services import get_employee_leave_balances
from notifications.models import NotificationEvent

EXPENSE_ACTIONS = [
    {"value": "APPROVED", "label": "Approve", "requires_remarks": False, "tone": "positive"},
    {"value": "REJECTED", "label": "Reject", "requires_remarks": True, "tone": "negative"},
]


def _expense_item(c, stage, endpoint):
    return {
        "kind": "expense",
        "id": c.id,
        "reference": c.claim_number,
        "employee_name": c.employee.full_name,
        "employee_id": c.employee.employee_id,
        "branch_code": c.branch.code,
        "title": f"{c.category.name}",
        "subtitle": c.description[:120],
        "amount": float(c.amount),
        "date": c.expense_date.isoformat(),
        "status": c.status,
        "status_display": c.get_status_display(),
        "stage": stage,
        "submitted_at": c.submitted_at,
        "attachments_count": c.attachments.count(),
        "endpoint": endpoint.format(id=c.id),
        "actions": EXPENSE_ACTIONS,
    }


def _leave_item(a, stage, endpoint, positive_value, positive_label):
    return {
        "kind": "leave",
        "id": a.id,
        "reference": a.application_number,
        "employee_name": a.employee.full_name,
        "employee_id": a.employee.employee_id,
        "branch_code": a.branch.code,
        "title": a.leave_type.name,
        "subtitle": a.reason[:120],
        "days": float(a.number_of_days),
        "start_date": a.start_date.isoformat(),
        "end_date": a.end_date.isoformat(),
        "is_half_day": a.is_half_day,
        "status": a.status,
        "status_display": a.get_status_display(),
        "stage": stage,
        "submitted_at": a.submitted_at,
        "endpoint": endpoint.format(id=a.id),
        "actions": [
            {"value": positive_value, "label": positive_label, "requires_remarks": False, "tone": "positive"},
            {"value": "REJECTED", "label": "Reject", "requires_remarks": True, "tone": "negative"},
        ],
    }


def pending_items_for(user):
    items = []
    branches = user.get_accessible_branches()

    if user.is_lead_manager or LeaveApplication.objects.filter(employee__profile__reporting_lead=user).exists():
        qs = LeaveApplication.objects.filter(
            status=LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION,
        ).select_related("employee", "branch", "leave_type")
        if not user.is_superuser:
            qs = qs.filter(employee__profile__reporting_lead=user)
        items += [_leave_item(a, "Lead recommendation", "/leaves/applications/{id}/lead-recommend/", "RECOMMENDED", "Recommend")
                  for a in qs.exclude(employee=user)]

    if user.is_admin_manager:
        qs = ExpenseClaim.objects.filter(status=ExpenseClaimStatus.UNDER_ADMIN_REVIEW, branch__in=branches) \
            .exclude(employee=user).select_related("employee", "branch", "category")
        items += [_expense_item(c, "Admin Manager review", "/expenses/claims/{id}/admin-review/") for c in qs]

    if user.is_hr_coordinator:
        qs = LeaveApplication.objects.filter(status=LeaveApplicationStatus.PENDING_HR_VERIFICATION, branch__in=branches) \
            .exclude(employee=user).select_related("employee", "branch", "leave_type")
        items += [_leave_item(a, "HR verification", "/leaves/applications/{id}/hr-verify/", "VERIFIED", "Verify")
                  for a in qs]

    if user.is_director:
        qs = ExpenseClaim.objects.filter(status=ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW, branch__in=branches) \
            .select_related("employee", "branch", "category")
        items += [_expense_item(c, "Director final approval", "/expenses/claims/{id}/director-approval/") for c in qs]
        qs = LeaveApplication.objects.filter(status=LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL, branch__in=branches) \
            .select_related("employee", "branch", "leave_type")
        items += [_leave_item(a, "Director final approval", "/leaves/applications/{id}/director-approval/", "APPROVED", "Approve")
                  for a in qs]

    items.sort(key=lambda i: str(i["submitted_at"] or ""))
    return items


class PendingApprovalsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        items = pending_items_for(user)
        awaiting_payment = []
        if user.is_admin_manager:
            qs = ExpenseClaim.objects.filter(
                status=ExpenseClaimStatus.APPROVED_BY_DIRECTOR, branch__in=user.get_accessible_branches()
            ).select_related("employee", "branch", "category").order_by("updated_at")
            awaiting_payment = ExpenseClaimListSerializer(qs[:50], many=True).data
        return Response({"count": len(items), "items": items, "awaiting_payment": awaiting_payment})


class DashboardSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        mine = ExpenseClaim.objects.filter(employee=user)
        year_start = date(date.today().year, 1, 1)

        balances = []
        for b in get_employee_leave_balances(user):
            lt = b["leave_type"]
            balances.append({
                "leave_type_id": lt.id,
                "leave_type_code": lt.code,
                "leave_type_name": lt.name,
                "is_paid": lt.is_paid,
                "is_accrual_based": lt.is_accrual_based,
                "earned_days": float(b["earned_days"]),
                "taken_days": float(b["taken_days"]),
                "pending_days": float(b["pending_days"]),
                "available_balance": float(b["available_balance"]),
                "effective_remaining": float(b["effective_remaining"]),
            })

        pending_review = mine.filter(status__in=[
            ExpenseClaimStatus.UNDER_ADMIN_REVIEW, ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW
        ])
        awaiting_payment = mine.filter(status=ExpenseClaimStatus.APPROVED_BY_DIRECTOR)
        paid_this_year = mine.filter(status=ExpenseClaimStatus.PAID, payment_record__payment_date__gte=year_start)

        return Response({
            "user": {
                "id": user.id,
                "full_name": user.full_name,
                "first_name": user.first_name,
                "employee_id": user.employee_id,
                "role": user.role,
                "role_display": user.get_role_display(),
                "branch": user.primary_branch.name if user.primary_branch else None,
                "is_approver": user.role != RoleChoices.EMPLOYEE or user.is_superuser,
            },
            "leave_balances": balances,
            "expenses": {
                "drafts": mine.filter(status=ExpenseClaimStatus.DRAFT).count(),
                "pending_review_count": pending_review.count(),
                "pending_review_amount": float(pending_review.aggregate(t=Sum("amount"))["t"] or Decimal("0")),
                "awaiting_payment_count": awaiting_payment.count(),
                "awaiting_payment_amount": float(awaiting_payment.aggregate(t=Sum("amount"))["t"] or Decimal("0")),
                "paid_this_year_amount": float(paid_this_year.aggregate(t=Sum("amount"))["t"] or Decimal("0")),
                "rejected_count": mine.filter(status__in=[
                    ExpenseClaimStatus.REJECTED_BY_ADMIN, ExpenseClaimStatus.REJECTED_BY_DIRECTOR
                ]).count(),
            },
            "recent_claims": ExpenseClaimListSerializer(
                mine.select_related("category", "branch", "employee").order_by("-created_at")[:5], many=True
            ).data,
            "recent_leaves": LeaveApplicationListSerializer(
                LeaveApplication.objects.filter(employee=user).select_related("leave_type", "branch", "employee")
                .order_by("-submitted_at")[:5], many=True
            ).data,
            "approvals_pending_count": len(pending_items_for(user)) if user.role != RoleChoices.EMPLOYEE or user.is_superuser else 0,
            "unread_notifications": NotificationEvent.objects.filter(recipient=user, is_read=False).count(),
        })
