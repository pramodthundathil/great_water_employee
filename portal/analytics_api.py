"""
GET /api/v1/dashboard/analytics/?months=6&branch_id=<id>

Role-scoped analytics for the mobile admin dashboards. Each role only receives the
sections that match its responsibilities (see PERMISSION_MATRIX.md):

  LEAD_MANAGER    team leave analytics (direct reports)
  ADMIN_MANAGER   branch expense / reimbursement analytics
  HR_COORDINATOR  branch leave & workforce analytics
  DIRECTOR        everything, across all accessible branches (superusers are Directors)
"""
from collections import defaultdict
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.db.models import Count, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import RoleChoices, User
from expenses.models import ExpenseClaim, ExpenseClaimStatus
from leaves.models import LeaveApplication, LeaveApplicationStatus
from portal.dashboard_services import (
    ACTIVE_CLAIM_EXCLUDED_STATUSES,
    PENDING_LEAVE_STATUSES,
    build_all_staff_analytics,
    build_approver_reminders,
    build_director_pipeline_summary,
    build_leave_utilization_series,
    build_monthly_expense_series,
)

ROLE_SECTIONS = {
    RoleChoices.LEAD_MANAGER: {"leaves"},
    RoleChoices.ADMIN_MANAGER: {"expenses"},
    RoleChoices.HR_COORDINATOR: {"leaves", "workforce"},
    RoleChoices.DIRECTOR: {"expenses", "leaves", "workforce", "branches"},
}


def _f(v):
    return float(v or 0)


def _month_starts(months, today):
    first = today.replace(day=1)
    return [first - relativedelta(months=i) for i in range(months - 1, -1, -1)]


def _leave_trend(leave_qs, months, today):
    starts = _month_starts(months, today)
    totals = defaultdict(Decimal)
    for row in (
        leave_qs.filter(status=LeaveApplicationStatus.APPROVED, start_date__gte=starts[0])
        .annotate(m=TruncMonth("start_date")).values("m").annotate(d=Sum("number_of_days"))
    ):
        if row["m"]:
            totals[(row["m"].year, row["m"].month)] += row["d"] or 0
    return {
        "labels": [m.strftime("%b") for m in starts],
        "values": [_f(totals[(m.year, m.month)]) for m in starts],
    }


def _stage_rows(stages, metric_kind):
    return [
        {
            "stage": s["stage"],
            "owner": s["owner"].replace(" (You)", ""),
            "count": s["count"],
            "metric": _f(s["metric"]),
            "metric_kind": metric_kind,
        }
        for s in stages
    ]


class DashboardAnalyticsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        role = RoleChoices.DIRECTOR if user.is_superuser else user.role
        sections = ROLE_SECTIONS.get(role)
        if not sections:
            return Response({"detail": "Analytics are available to managers and directors only."}, status=403)

        try:
            months = max(3, min(12, int(request.query_params.get("months", 6))))
        except ValueError:
            months = 6
        today = timezone.localdate()
        month_start = today.replace(day=1)
        year_start = today.replace(month=1, day=1)

        branches = user.get_accessible_branches()
        branch_id = request.query_params.get("branch_id")
        all_branches = list(branches.order_by("code").values("id", "code", "name"))
        if branch_id:
            branches = branches.filter(id=branch_id)

        if role == RoleChoices.LEAD_MANAGER:
            leave_qs = LeaveApplication.objects.filter(employee__profile__reporting_lead=user)
            team = User.objects.filter(profile__reporting_lead=user, is_active=True)
            if branch_id:
                leave_qs = leave_qs.filter(branch__in=branches)
                team = team.filter(primary_branch__in=branches)
        else:
            leave_qs = LeaveApplication.objects.filter(branch__in=branches)
            team = User.objects.filter(primary_branch__in=branches, is_active=True)
        claim_qs = ExpenseClaim.objects.filter(branch__in=branches)

        data = {
            "role": role,
            "sections": sorted(sections),
            "months": months,
            "branches": all_branches if role != RoleChoices.LEAD_MANAGER else [],
            "branch_id": int(branch_id) if branch_id and branch_id.isdigit() else None,
            "generated_at": timezone.now(),
            "kpis": [],
        }
        kpis = data["kpis"]

        # ---------------------------------------------------------------- expenses
        if "expenses" in sections:
            active = claim_qs.exclude(status__in=ACTIVE_CLAIM_EXCLUDED_STATUSES)
            mtd = active.filter(expense_date__gte=month_start).aggregate(t=Sum("amount"))["t"]
            ytd_paid = claim_qs.filter(
                status=ExpenseClaimStatus.PAID, payment_record__payment_date__gte=year_start
            ).aggregate(t=Sum("amount"))["t"]
            in_review = claim_qs.filter(status__in=[
                ExpenseClaimStatus.UNDER_ADMIN_REVIEW, ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW,
            ]).aggregate(c=Count("id"), t=Sum("amount"))
            to_pay = claim_qs.filter(status=ExpenseClaimStatus.APPROVED_BY_DIRECTOR).aggregate(c=Count("id"), t=Sum("amount"))
            decided = claim_qs.filter(submitted_at__isnull=False).exclude(status=ExpenseClaimStatus.WITHDRAWN)
            decided_total = decided.exclude(status__in=[
                ExpenseClaimStatus.UNDER_ADMIN_REVIEW, ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW,
            ]).count()
            rejected = decided.filter(status__in=[
                ExpenseClaimStatus.REJECTED_BY_ADMIN, ExpenseClaimStatus.REJECTED_BY_DIRECTOR,
            ]).count()
            kpis += [
                {"key": "claimed_mtd", "label": "Claimed this month", "value": _f(mtd), "format": "money"},
                {"key": "paid_ytd", "label": "Reimbursed this year", "value": _f(ytd_paid), "format": "money"},
                {"key": "in_review", "label": "In review", "value": _f(in_review["t"]), "format": "money",
                 "hint": f"{in_review['c']} claim(s)"},
                {"key": "to_pay", "label": "Approved, to be paid", "value": _f(to_pay["t"]), "format": "money",
                 "hint": f"{to_pay['c']} claim(s)"},
                {"key": "rejection_rate", "label": "Rejection rate", "value": round(100 * rejected / decided_total, 1)
                 if decided_total else 0, "format": "percent", "hint": f"{rejected} of {decided_total} decided"},
            ]
            data["expense_trend"] = build_monthly_expense_series(claim_qs, months=months, as_of_date=today)
            data["expense_trend"]["labels"] = [l.split(" ")[0] for l in data["expense_trend"]["labels"]]
            window = active.filter(expense_date__gte=_month_starts(months, today)[0])
            data["expense_by_category"] = [
                {"name": r["category__name"], "amount": _f(r["t"]), "count": r["c"]}
                for r in window.values("category__name").annotate(t=Sum("amount"), c=Count("id")).order_by("-t")[:8]
            ]
            data["top_claimants"] = [
                {"name": f"{r['employee__first_name']} {r['employee__last_name']}".strip() or r["employee__employee_id"],
                 "employee_id": r["employee__employee_id"], "amount": _f(r["t"]), "count": r["c"]}
                for r in window.values("employee__employee_id", "employee__first_name", "employee__last_name")
                .annotate(t=Sum("amount"), c=Count("id")).order_by("-t")[:5]
            ]

        # ---------------------------------------------------------------- leaves
        if "leaves" in sections:
            on_leave = leave_qs.filter(
                status=LeaveApplicationStatus.APPROVED, start_date__lte=today, end_date__gte=today,
            ).select_related("employee", "leave_type").order_by("end_date")
            pending = leave_qs.filter(status__in=PENDING_LEAVE_STATUSES).aggregate(c=Count("id"), d=Sum("number_of_days"))
            approved_ytd = leave_qs.filter(
                status=LeaveApplicationStatus.APPROVED, start_date__gte=year_start,
            ).aggregate(d=Sum("number_of_days"))["d"]
            upcoming = leave_qs.filter(
                status=LeaveApplicationStatus.APPROVED, start_date__gt=today,
                start_date__lte=today + relativedelta(days=14),
            ).count()
            kpis += [
                {"key": "on_leave_today", "label": "On leave today", "value": on_leave.count(), "format": "number",
                 "hint": f"of {team.count()} staff"},
                {"key": "leave_pending", "label": "Leave requests pending", "value": pending["c"] or 0, "format": "number",
                 "hint": f"{_f(pending['d']):g} day(s)"},
                {"key": "leave_ytd", "label": "Leave days taken this year", "value": _f(approved_ytd), "format": "days"},
                {"key": "leave_upcoming", "label": "Starting in next 14 days", "value": upcoming, "format": "number"},
            ]
            util = build_leave_utilization_series(leave_qs.filter(start_date__gte=year_start))
            data["leave_by_type"] = [
                {"name": n, "days": v} for n, v in zip(util["labels"], util["values"]) if v
            ]
            data["leave_trend"] = _leave_trend(leave_qs, months, today)
            data["on_leave_today"] = [
                {"name": a.employee.full_name, "employee_id": a.employee.employee_id,
                 "leave_type": a.leave_type.name, "end_date": a.end_date.isoformat()}
                for a in on_leave[:20]
            ]

        # ---------------------------------------------------------------- pipeline & SLA
        pipeline = build_director_pipeline_summary(claim_qs, leave_qs)
        data["pipeline"] = {}
        if "expenses" in sections:
            data["pipeline"]["expense"] = _stage_rows(pipeline["expense_stages"], "money")
        if "leaves" in sections:
            data["pipeline"]["leave"] = _stage_rows(pipeline["leave_stages"], "days")
        if role in (RoleChoices.DIRECTOR, RoleChoices.ADMIN_MANAGER, RoleChoices.HR_COORDINATOR):
            sla = build_approver_reminders(
                claim_qs if "expenses" in sections else claim_qs.none(),
                leave_qs if "leaves" in sections else leave_qs.none(),
            )
            data["sla"] = {
                "pending": len(sla["items"]),
                "overdue": sla["overdue_count"],
                "oldest": [
                    {"reference": r["ref_number"], "kind": r["item_type"], "id": r["item_id"],
                     "employee_name": r["employee"].full_name, "stage": r["stage_label"],
                     "waiting": r["wait_display"], "sla": r["sla_status"], "summary": r["summary_text"]}
                    for r in sla["items"][:5]
                ],
            }

        # ---------------------------------------------------------------- branches / workforce
        if "branches" in sections:
            staff = build_all_staff_analytics(branches, claim_qs, leave_qs)
            data["branch_breakdown"] = [
                {"code": b["branch"].code, "name": b["branch"].name, "staff": b["staff_count"],
                 "claimed": _f(b["claimed_total"]), "paid": _f(b["paid_total"]),
                 "leave_days": _f(b["approved_leave_days"]), "pending": b["pending_items"]}
                for b in staff["branch_rows"] if b["branch"]
            ]
        if "workforce" in sections:
            data["headcount"] = team.count()
            data["headcount_by_role"] = [
                {"role": r["role"], "label": RoleChoices(r["role"]).label, "count": r["c"]}
                for r in team.values("role").annotate(c=Count("id")).order_by("-c")
            ]

        return Response(data)
