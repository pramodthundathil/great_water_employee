from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from accounts.models import RoleChoices, User
from expenses.models import ExpenseClaim, ExpenseClaimStatus
from leaves.models import (
    LeaveApplication,
    LeaveApplicationStatus,
    LeaveLedgerEntry,
    LeaveType,
    REJECTED_LEAVE_STATUSES,
    fmt_days,
)
from notifications.models import NotificationEvent
from notifications.services import send_system_notification

PENDING_EXPENSE_STATUSES = [
    ExpenseClaimStatus.UNDER_ADMIN_REVIEW,
    ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW,
    ExpenseClaimStatus.APPROVED_BY_DIRECTOR,
]

ACTIVE_CLAIM_EXCLUDED_STATUSES = [
    ExpenseClaimStatus.DRAFT,
    ExpenseClaimStatus.WITHDRAWN,
]

PENDING_LEAVE_STATUSES = [
    LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION,
    LeaveApplicationStatus.PENDING_HR_VERIFICATION,
    LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL,
]


def get_scoped_approvers(branch, role):
    """Return active users holding `role` who are authorized for `branch`."""
    qs = User.objects.filter(is_active=True, role=role)
    if branch is not None:
        qs = qs.filter(
            Q(is_global_scope=True)
            | Q(primary_branch=branch)
            | Q(branch_scopes__branch=branch, branch_scopes__is_active=True)
        ).distinct()
    approvers = list(qs.order_by("employee_id"))
    if not approvers and role == RoleChoices.DIRECTOR:
        approvers = list(User.objects.filter(is_active=True, is_superuser=True).order_by("employee_id"))
    return approvers


def get_item_approver_info(item, item_type):
    """Resolve current approval stage metadata, responsible approvers, and URLs."""
    if item_type == "expense":
        portal_url = f"/expenses/{item.id}/"
        legacy_url = f"/expenses/claims/{item.id}/"
        from notifications.services import expense_approvers_for
        if item.status == ExpenseClaimStatus.UNDER_ADMIN_REVIEW:
            approvers = expense_approvers_for(item.branch, RoleChoices.ADMIN_MANAGER, exclude=item.employee)
            return {
                "stage_label": "Admin Manager Review",
                "stage_code": "ADMIN_REVIEW",
                "is_director_stage": False,
                "approvers": approvers,
                "approvers_label": _format_approver_names(approvers, "Branch Admin Manager"),
                "link_urls": [portal_url, legacy_url, "/expenses/approvals/"],
                "portal_url": portal_url,
                "approval_url": "/expenses/approvals/",
            }
        if item.status == ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW:
            approvers = expense_approvers_for(item.branch, RoleChoices.DIRECTOR, exclude=item.employee)
            return {
                "stage_label": "Director Final Approval",
                "stage_code": "DIRECTOR_APPROVAL",
                "is_director_stage": True,
                "approvers": approvers,
                "approvers_label": _format_approver_names(approvers, "Director"),
                "link_urls": [portal_url, legacy_url, "/expenses/approvals/"],
                "portal_url": portal_url,
                "approval_url": "/expenses/approvals/",
            }
        approvers = expense_approvers_for(item.branch, RoleChoices.ADMIN_MANAGER, exclude=item.employee)
        return {
            "stage_label": "Payment Disbursement",
            "stage_code": "DISBURSEMENT",
            "is_director_stage": False,
            "approvers": approvers,
            "approvers_label": _format_approver_names(approvers, "Admin / Finance"),
            "link_urls": [portal_url, legacy_url, "/expenses/reimbursements/"],
            "portal_url": portal_url,
            "approval_url": "/expenses/reimbursements/",
        }

    portal_url = f"/leaves/{item.id}/"
    legacy_url = f"/leaves/applications/{item.id}/"
    if item.status == LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION:
        profile = getattr(item.employee, "profile", None)
        lead = profile.reporting_lead if profile and profile.reporting_lead and profile.reporting_lead.is_active else None
        approvers = [lead] if lead else []
        return {
            "stage_label": "Lead Recommendation",
            "stage_code": "LEAD_RECOMMENDATION",
            "is_director_stage": False,
            "approvers": approvers,
            "approvers_label": _format_approver_names(approvers, "Unassigned Lead"),
            "link_urls": [portal_url, legacy_url, "/leaves/approvals/"],
            "portal_url": portal_url,
            "approval_url": "/leaves/approvals/",
        }
    if item.status == LeaveApplicationStatus.PENDING_HR_VERIFICATION:
        approvers = get_scoped_approvers(item.branch, RoleChoices.HR_COORDINATOR)
        return {
            "stage_label": "HR Verification",
            "stage_code": "HR_VERIFICATION",
            "is_director_stage": False,
            "approvers": approvers,
            "approvers_label": _format_approver_names(approvers, "HR Coordinator"),
            "link_urls": [portal_url, legacy_url, "/leaves/approvals/"],
            "portal_url": portal_url,
            "approval_url": "/leaves/approvals/",
        }
    approvers = get_scoped_approvers(item.branch, RoleChoices.DIRECTOR)
    return {
        "stage_label": "Director Final Approval",
        "stage_code": "DIRECTOR_APPROVAL",
        "is_director_stage": True,
        "approvers": approvers,
        "approvers_label": _format_approver_names(approvers, "Director"),
        "link_urls": [portal_url, legacy_url, "/leaves/approvals/"],
        "portal_url": portal_url,
        "approval_url": "/leaves/approvals/",
    }


def _format_approver_names(approvers, fallback):
    if not approvers:
        return fallback
    names = [u.full_name for u in approvers[:2]]
    if len(approvers) > 2:
        names.append(f"+{len(approvers) - 2}")
    return ", ".join(names)


def _format_wait_duration(hours):
    if hours < 1:
        return f"{max(1, int(hours * 60))}m"
    if hours < 24:
        return f"{int(hours)}h"
    days = int(hours // 24)
    rem_hours = int(hours % 24)
    return f"{days}d {rem_hours}h" if rem_hours else f"{days}d"


def _waiting_since(item, item_type):
    history = list(item.approval_history.all())
    if history:
        return history[-1].created_at
    if item_type == "expense":
        return item.submitted_at or item.created_at
    return item.submitted_at


def build_approver_reminders(claim_qs, leave_qs, now=None):
    """Build pending approval items with SLA aging and NotificationEvent reminder/read status."""
    now = now or timezone.now()

    pending_claims = list(
        claim_qs.filter(status__in=PENDING_EXPENSE_STATUSES)
        .select_related("employee__profile__reporting_lead", "branch", "category")
        .prefetch_related("approval_history__approver")
    )
    pending_leaves = list(
        leave_qs.filter(status__in=PENDING_LEAVE_STATUSES)
        .select_related("employee__profile__reporting_lead", "branch", "leave_type")
        .prefetch_related("approval_history__approver")
    )

    if not pending_claims and not pending_leaves:
        return {
            "items": [],
            "overdue_count": 0,
            "unread_alert_count": 0,
            "reminded_count": 0,
            "upstream_reminded_eligible_count": 0,
        }

    # Preload recent relevant notifications to avoid per-row NotificationEvent queries
    ref_numbers = [c.claim_number for c in pending_claims] + [l.application_number for l in pending_leaves]
    all_links = set()
    claim_infos = {}
    for c in pending_claims:
        info = get_item_approver_info(c, "expense")
        claim_infos[c.id] = info
        all_links.update(info["link_urls"])
    leave_infos = {}
    for l in pending_leaves:
        info = get_item_approver_info(l, "leave")
        leave_infos[l.id] = info
        all_links.update(info["link_urls"])

    notif_q = Q(link_url__in=all_links)
    for ref in ref_numbers:
        notif_q |= Q(title__contains=ref) | Q(message__contains=ref)
    recent_notifs = list(
        NotificationEvent.objects.filter(notif_q)
        .select_related("recipient")
        .order_by("-created_at")[:500]
    )

    rows = []
    overdue_count = 0
    unread_alert_count = 0
    reminded_count = 0
    upstream_eligible = 0

    for item, item_type, info in (
        [(c, "expense", claim_infos[c.id]) for c in pending_claims]
        + [(l, "leave", leave_infos[l.id]) for l in pending_leaves]
    ):
        ref_number = item.claim_number if item_type == "expense" else item.application_number
        waiting_dt = _waiting_since(item, item_type)
        wait_hours = max(0.0, (now - waiting_dt).total_seconds() / 3600.0)

        if wait_hours >= 48:
            sla_status = "overdue"
            sla_label = f"Overdue ({_format_wait_duration(wait_hours)})"
            overdue_count += 1
        elif wait_hours >= 24:
            sla_status = "due"
            sla_label = f"Due ({_format_wait_duration(wait_hours)})"
        else:
            sla_status = "ontrack"
            sla_label = f"On Track ({_format_wait_duration(wait_hours)})"

        approver_ids = {u.id for u in info["approvers"]}
        item_specific_urls = set(info["link_urls"][:2])
        matching_notifs = [
            n
            for n in recent_notifs
            if (
                n.link_url in item_specific_urls
                or ref_number in n.title
                or ref_number in n.message
            )
            and (not approver_ids or n.recipient_id in approver_ids)
        ]

        reminder_events = [n for n in matching_notifs if n.event_type == "APPROVAL_REMINDER"]
        reminder_hits = len(reminder_events)
        last_notified = matching_notifs[0] if matching_notifs else None
        last_reminded = reminder_events[0] if reminder_events else None

        if reminder_hits > 0:
            reminded_count += 1

        if not matching_notifs:
            read_state = "not_sent"
            read_label = "No Alert Sent"
        elif any(n.is_read for n in matching_notifs if n.created_at >= waiting_dt - timedelta(minutes=5)):
            read_state = "read"
            read_label = "Seen by Approver"
        elif last_notified.is_read:
            read_state = "read"
            read_label = "Seen by Approver"
        else:
            read_state = "unread"
            read_label = "Unread Alert"
            unread_alert_count += 1

        if not info["is_director_stage"] and info["approvers"]:
            upstream_eligible += 1

        summary_text = (
            f"{item.category.name} • ${item.amount:,.2f}"
            if item_type == "expense"
            else f"{item.leave_type.name} • {item.number_of_days:.1f} days"
        )

        rows.append(
            {
                "item_type": item_type,
                "item_id": item.id,
                "ref_number": ref_number,
                "employee": item.employee,
                "branch": item.branch,
                "summary_text": summary_text,
                "stage_label": info["stage_label"],
                "stage_code": info["stage_code"],
                "is_director_stage": info["is_director_stage"],
                "approvers_label": info["approvers_label"],
                "has_approver": bool(info["approvers"]),
                "portal_url": info["portal_url"],
                "approval_url": info["approval_url"],
                "waiting_since": waiting_dt,
                "wait_hours": wait_hours,
                "wait_display": _format_wait_duration(wait_hours),
                "sla_status": sla_status,
                "sla_label": sla_label,
                "reminder_count": reminder_hits,
                "last_reminded_at": last_reminded.created_at if last_reminded else None,
                "last_notified_at": last_notified.created_at if last_notified else None,
                "read_state": read_state,
                "read_label": read_label,
            }
        )

    rows.sort(key=lambda r: (-r["wait_hours"], r["ref_number"]))
    return {
        "items": rows,
        "overdue_count": overdue_count,
        "unread_alert_count": unread_alert_count,
        "reminded_count": reminded_count,
        "upstream_reminded_eligible_count": upstream_eligible,
    }


def send_approver_reminder(item, item_type, actor):
    """Send an in-app/email/push reminder notification to current stage approver(s)."""
    info = get_item_approver_info(item, item_type)
    approvers = info["approvers"]
    if not approvers:
        return 0, info["approvers_label"]

    if item_type == "expense":
        ref = item.claim_number
        detail = f"${item.amount:,.2f} ({item.category.name}) for {item.employee.full_name}"
    else:
        ref = item.application_number
        detail = f"{item.number_of_days:.1f} days of {item.leave_type.name} for {item.employee.full_name}"

    title = f"Approval Reminder: {ref} ({info['stage_label']})"
    message = (
        f"Reminder from {actor.full_name}: {detail} at {item.branch.name} "
        f"is awaiting {info['stage_label']}."
    )

    for recipient in approvers:
        send_system_notification(
            recipient=recipient,
            title=title,
            message=message,
            event_type="APPROVAL_REMINDER",
            link_url=info["approval_url"],
        )

    return len(approvers), info["approvers_label"]


def build_monthly_expense_series(claim_qs, months=6, as_of_date=None):
    """Aggregate real monthly claimed vs reimbursed totals for the last N months."""
    as_of_date = as_of_date or timezone.localdate()
    first_of_month = as_of_date.replace(day=1)
    month_starts = [first_of_month - relativedelta(months=i) for i in range(months - 1, -1, -1)]
    window_start = month_starts[0]

    monthly_rows = (
        claim_qs.filter(expense_date__gte=window_start)
        .exclude(status__in=ACTIVE_CLAIM_EXCLUDED_STATUSES)
        .annotate(month=TruncMonth("expense_date"))
        .values("month", "status")
        .annotate(total=Sum("amount"))
    )

    claimed_by_month = defaultdict(Decimal)
    reimbursed_by_month = defaultdict(Decimal)
    for row in monthly_rows:
        m = row["month"]
        if not m:
            continue
        key = (m.year, m.month)
        amt = row["total"] or Decimal("0.00")
        claimed_by_month[key] += amt
        if row["status"] == ExpenseClaimStatus.PAID:
            reimbursed_by_month[key] += amt

    labels = [dt.strftime("%b %Y") for dt in month_starts]
    claimed = [float(claimed_by_month[(dt.year, dt.month)]) for dt in month_starts]
    reimbursed = [float(reimbursed_by_month[(dt.year, dt.month)]) for dt in month_starts]

    return {
        "labels": labels,
        "claimed": claimed,
        "reimbursed": reimbursed,
    }


def build_leave_utilization_series(leave_qs):
    """Aggregate approved leave days grouped by LeaveType across the scoped queryset."""
    leave_types = list(LeaveType.objects.filter(is_active=True).order_by("name"))
    approved_totals = {
        row["leave_type_id"]: float(row["total_days"] or 0)
        for row in leave_qs.filter(status=LeaveApplicationStatus.APPROVED)
        .values("leave_type_id")
        .annotate(total_days=Sum("number_of_days"))
    }

    labels = [lt.name for lt in leave_types]
    values = [approved_totals.get(lt.id, 0.0) for lt in leave_types]
    return {
        "labels": labels,
        "values": values,
        "has_data": any(v > 0 for v in values),
    }


def build_director_pipeline_summary(claim_qs, leave_qs):
    """Summarize pending counts and totals across all expense & leave approval stages."""
    exp_stats = {
        row["status"]: {"count": row["cnt"], "amount": row["amt"] or Decimal("0.00")}
        for row in claim_qs.filter(status__in=PENDING_EXPENSE_STATUSES)
        .values("status")
        .annotate(cnt=Count("id"), amt=Sum("amount"))
    }
    leave_stats = {
        row["status"]: {"count": row["cnt"], "days": row["days"] or Decimal("0.00")}
        for row in leave_qs.filter(status__in=PENDING_LEAVE_STATUSES)
        .values("status")
        .annotate(cnt=Count("id"), days=Sum("number_of_days"))
    }

    expense_stages = [
        {
            "stage": "Stage 1: Admin Review",
            "owner": "Admin Manager",
            "count": exp_stats.get(ExpenseClaimStatus.UNDER_ADMIN_REVIEW, {}).get("count", 0),
            "metric": exp_stats.get(ExpenseClaimStatus.UNDER_ADMIN_REVIEW, {}).get("amount", Decimal("0.00")),
            "is_director": False,
            "url_name": "portal_expense_approvals",
        },
        {
            "stage": "Stage 2: Director Final Approval",
            "owner": "Director (You)",
            "count": exp_stats.get(ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW, {}).get("count", 0),
            "metric": exp_stats.get(ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW, {}).get("amount", Decimal("0.00")),
            "is_director": True,
            "url_name": "portal_expense_approvals",
        },
        {
            "stage": "Stage 3: Ready for Payment",
            "owner": "Admin / Finance",
            "count": exp_stats.get(ExpenseClaimStatus.APPROVED_BY_DIRECTOR, {}).get("count", 0),
            "metric": exp_stats.get(ExpenseClaimStatus.APPROVED_BY_DIRECTOR, {}).get("amount", Decimal("0.00")),
            "is_director": False,
            "url_name": "portal_reimbursements",
        },
    ]

    leave_stages = [
        {
            "stage": "Stage 1: Lead Recommendation",
            "owner": "Reporting Lead",
            "count": leave_stats.get(LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION, {}).get("count", 0),
            "metric": leave_stats.get(LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION, {}).get("days", Decimal("0.00")),
            "is_director": False,
            "url_name": "portal_leave_approvals",
        },
        {
            "stage": "Stage 2: HR Verification",
            "owner": "HR Coordinator",
            "count": leave_stats.get(LeaveApplicationStatus.PENDING_HR_VERIFICATION, {}).get("count", 0),
            "metric": leave_stats.get(LeaveApplicationStatus.PENDING_HR_VERIFICATION, {}).get("days", Decimal("0.00")),
            "is_director": False,
            "url_name": "portal_leave_approvals",
        },
        {
            "stage": "Stage 3: Director Final Approval",
            "owner": "Director (You)",
            "count": leave_stats.get(LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL, {}).get("count", 0),
            "metric": leave_stats.get(LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL, {}).get("days", Decimal("0.00")),
            "is_director": True,
            "url_name": "portal_leave_approvals",
        },
    ]

    return {
        "expense_stages": expense_stages,
        "leave_stages": leave_stages,
        "director_expense_pending": expense_stages[1]["count"],
        "director_leave_pending": leave_stages[2]["count"],
        "director_action_total": expense_stages[1]["count"] + leave_stages[2]["count"],
        "upstream_pending_total": (
            expense_stages[0]["count"] + leave_stages[0]["count"] + leave_stages[1]["count"]
        ),
        "approved_awaiting_payment_amount": expense_stages[2]["metric"],
    }


def build_all_staff_analytics(branch_qs, claim_qs, leave_qs):
    """Compute per-staff and per-branch expense & leave analytics in bulk queries."""
    staff_users = list(
        User.objects.filter(is_active=True, primary_branch__in=branch_qs)
        .select_related("primary_branch", "profile__department", "profile__reporting_lead")
        .order_by("employee_id")
    )
    staff_ids = [u.id for u in staff_users]

    # 1. Expense aggregation per staff
    exp_by_emp = defaultdict(
        lambda: {
            "count": 0,
            "claimed": Decimal("0.00"),
            "paid": Decimal("0.00"),
            "pending": 0,
        }
    )
    for row in (
        claim_qs.filter(employee_id__in=staff_ids)
        .exclude(status__in=ACTIVE_CLAIM_EXCLUDED_STATUSES)
        .values("employee_id", "status")
        .annotate(cnt=Count("id"), amt=Sum("amount"))
    ):
        bucket = exp_by_emp[row["employee_id"]]
        amt = row["amt"] or Decimal("0.00")
        bucket["count"] += row["cnt"]
        bucket["claimed"] += amt
        if row["status"] == ExpenseClaimStatus.PAID:
            bucket["paid"] += amt
        if row["status"] in [ExpenseClaimStatus.UNDER_ADMIN_REVIEW, ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW]:
            bucket["pending"] += row["cnt"]

    # 2. Leave application aggregation per staff
    leave_by_emp = defaultdict(
        lambda: {
            "approved_days": Decimal("0.00"),
            "approved_count": 0,
            "pending_days": Decimal("0.00"),
            "pending_count": 0,
        }
    )
    for row in (
        leave_qs.filter(employee_id__in=staff_ids)
        .values("employee_id", "status")
        .annotate(cnt=Count("id"), days=Sum("number_of_days"))
    ):
        bucket = leave_by_emp[row["employee_id"]]
        days = row["days"] or Decimal("0.00")
        if row["status"] == LeaveApplicationStatus.APPROVED:
            bucket["approved_days"] += days
            bucket["approved_count"] += row["cnt"]
        elif row["status"] in PENDING_LEAVE_STATUSES:
            bucket["pending_days"] += days
            bucket["pending_count"] += row["cnt"]

    # 3. Official available leave balances per (employee, leave_type)
    active_leave_types = list(LeaveType.objects.filter(is_active=True).order_by("code"))
    active_lt_ids = {lt.id for lt in active_leave_types}
    latest_balances = {}
    for emp_id, lt_id, bal in (
        LeaveLedgerEntry.objects.filter(employee_id__in=staff_ids, leave_type_id__in=active_lt_ids)
        .order_by("-created_at", "-id")
        .values_list("employee_id", "leave_type_id", "balance_after")
    ):
        key = (emp_id, lt_id)
        if key not in latest_balances:
            latest_balances[key] = bal

    staff_rows = []
    branch_summary = defaultdict(
        lambda: {
            "branch": None,
            "staff_count": 0,
            "claimed_total": Decimal("0.00"),
            "paid_total": Decimal("0.00"),
            "approved_leave_days": Decimal("0.00"),
            "pending_items": 0,
        }
    )

    for emp in staff_users:
        exp = exp_by_emp[emp.id]
        lv = leave_by_emp[emp.id]
        balances_breakdown = []
        available_total = Decimal("0.00")
        for lt in active_leave_types:
            bal = latest_balances.get((emp.id, lt.id), Decimal("0.00"))
            available_total += bal
            balances_breakdown.append(f"{lt.code}: {bal:.1f}")

        pending_total = exp["pending"] + lv["pending_count"]
        profile = getattr(emp, "profile", None)
        dept_name = profile.department.name if profile and profile.department else "—"
        designation = profile.designation if profile and profile.designation else emp.get_role_display()

        staff_rows.append(
            {
                "user": emp,
                "department": dept_name,
                "designation": designation,
                "expense_count": exp["count"],
                "expense_claimed": exp["claimed"],
                "expense_paid": exp["paid"],
                "expense_pending": exp["pending"],
                "leave_approved_days": lv["approved_days"],
                "leave_approved_count": lv["approved_count"],
                "leave_pending_days": lv["pending_days"],
                "leave_pending_count": lv["pending_count"],
                "pending_total": pending_total,
                "available_leave_total": available_total,
                "balances_summary": " • ".join(balances_breakdown) if balances_breakdown else "No policy",
            }
        )

        if emp.primary_branch:
            b_bucket = branch_summary[emp.primary_branch_id]
            b_bucket["branch"] = emp.primary_branch
            b_bucket["staff_count"] += 1
            b_bucket["claimed_total"] += exp["claimed"]
            b_bucket["paid_total"] += exp["paid"]
            b_bucket["approved_leave_days"] += lv["approved_days"]
            b_bucket["pending_items"] += pending_total

    branch_rows = sorted(
        branch_summary.values(),
        key=lambda b: b["branch"].code if b["branch"] else "",
    )

    return {
        "staff_rows": staff_rows,
        "branch_rows": branch_rows,
        "total_claimed_all": sum((r["expense_claimed"] for r in staff_rows), Decimal("0.00")),
        "total_paid_all": sum((r["expense_paid"] for r in staff_rows), Decimal("0.00")),
        "total_leave_days_all": sum((r["leave_approved_days"] for r in staff_rows), Decimal("0.00")),
    }



# Leaves that will actually keep someone away: approved, or still moving through approval.
ON_LEAVE_STATUSES = [
    LeaveApplicationStatus.APPROVED,
    LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION,
    LeaveApplicationStatus.PENDING_HR_VERIFICATION,
    LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL,
]
UPCOMING_LEAVE_WINDOW_DAYS = 15


def leave_calendar_scope(user):
    """
    Leave applications whose employees this user may see on the dashboard leave calendar.

    - Director: every employee in the organisation.
    - HR Coordinator: every employee, unless they are tied to branch(es) (primary branch or
      branch scopes, without organisation-wide scope) — then only those branches.
    - Lead / Admin Manager: employees of their own branch(es) (primary + active branch scopes).
    - Employee: only their own applications.
    """
    qs = LeaveApplication.objects.all()
    if user.is_director:
        return qs
    if user.is_hr_coordinator:
        if user.is_global_scope:
            return qs
        branch_ids = set(user.branch_scopes.filter(is_active=True).values_list("branch_id", flat=True))
        if user.primary_branch_id:
            branch_ids.add(user.primary_branch_id)
        return qs.filter(branch_id__in=branch_ids) if branch_ids else qs
    if user.role in (RoleChoices.LEAD_MANAGER, RoleChoices.ADMIN_MANAGER):
        return qs.filter(branch__in=user.get_accessible_branches())
    return qs.filter(employee=user)


def build_leave_calendar(user, selected_branch_id=None, today=None):
    """
    Today's leaves and those starting within the next UPCOMING_LEAVE_WINDOW_DAYS days, limited to
    what the user's role may see. A dashboard branch filter narrows the result further (it can
    never widen it).
    """
    today = today or timezone.localdate()
    window_end = today + timedelta(days=UPCOMING_LEAVE_WINDOW_DAYS)

    qs = (
        leave_calendar_scope(user)
        .filter(status__in=ON_LEAVE_STATUSES, employee__is_active=True)
        .select_related("employee", "branch", "leave_type")
    )
    if selected_branch_id and selected_branch_id != "all" and user.role != RoleChoices.EMPLOYEE:
        qs = qs.filter(branch_id=selected_branch_id)

    on_leave_today = list(
        qs.filter(start_date__lte=today, end_date__gte=today).order_by("end_date", "employee__first_name")
    )
    upcoming = list(
        qs.filter(start_date__gt=today, start_date__lte=window_end).order_by("start_date", "employee__first_name")
    )
    for app in on_leave_today:
        app.days_remaining = (app.end_date - today).days
    for app in upcoming:
        app.starts_in_days = (app.start_date - today).days

    return {
        "today": today,
        "window_end": window_end,
        "window_days": UPCOMING_LEAVE_WINDOW_DAYS,
        "on_leave_today": on_leave_today,
        "upcoming": upcoming,
        "is_personal": user.role == RoleChoices.EMPLOYEE,
    }


# ---------------------------------------------------------------------------
# "My Action Center": the single leave / expense items that concern this user
# ---------------------------------------------------------------------------
ACTION_CENTER_LIMIT = 12

def _leave_item(app, tone, tag, when=None, note=""):
    days = app.number_of_days
    span = app.start_date.strftime("%b %d")
    if app.end_date != app.start_date:
        span += " – " + app.end_date.strftime("%b %d")
    return {
        "kind": "leave",
        "pk": app.pk,
        "ref": app.application_number,
        "url_name": "portal_leave_detail",
        "employee": app.employee,
        "title": app.leave_type.name,
        "meta": f"{span} · {fmt_days(days)} day{'s' if days != 1 else ''}",
        "status_label": app.display_status,
        "tone": tone,
        "tag": tag,
        "note": note,
        "when": when or app.updated_at,
    }


def _expense_item(claim, tone, tag, when=None, note=""):
    return {
        "kind": "expense",
        "pk": claim.pk,
        "ref": claim.claim_number,
        "url_name": "portal_expense_detail",
        "employee": claim.employee,
        "title": claim.category.name if claim.category_id else "Expense",
        "meta": f"${claim.amount:,.2f}",
        "status_label": claim.get_status_display(),
        "tone": tone,
        "tag": tag,
        "note": note,
        "when": when or claim.updated_at,
    }


def _last_history(obj):
    history = list(obj.approval_history.all())
    return history[-1] if history else None


def build_my_action_center(user, selected_branch_id=None, today=None):
    """
    Leave / expense items currently pending at a stage this user approves (own items excluded,
    as on the approval pages), each with its employee. Items another approver sent back to this
    user's stage are flagged as returned and listed first.
    """
    now = timezone.now()
    branch_filter = selected_branch_id if selected_branch_id and selected_branch_id != "all" else None

    # ---- Leave items waiting on this user ----
    leave_sources = []
    lead_qs = LeaveApplication.objects.filter(
        employee__profile__reporting_lead=user, status=LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION
    )
    leave_sources.append(lead_qs)
    if user.is_hr_coordinator or user.is_director:
        scoped = LeaveApplication.objects.filter(branch__in=user.get_accessible_branches())
        if branch_filter:
            scoped = scoped.filter(branch_id=branch_filter)
        if user.is_hr_coordinator:
            leave_sources.append(scoped.filter(status=LeaveApplicationStatus.PENDING_HR_VERIFICATION))
        if user.is_director:
            leave_sources.append(scoped.filter(status=LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL))
    leave_pending_q = leave_sources[0]
    for extra in leave_sources[1:]:
        leave_pending_q = leave_pending_q | extra
    pending_leaves = (
        leave_pending_q.exclude(employee=user).distinct()
        .select_related("employee", "branch", "leave_type")
        .prefetch_related("approval_history__approver")
        .order_by("submitted_at")
    )

    # ---- Expense items waiting on this user ----
    expense_statuses = []
    if user.is_admin_manager:
        expense_statuses.append(ExpenseClaimStatus.UNDER_ADMIN_REVIEW)
    if user.is_director:
        expense_statuses.append(ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW)
    pending_claims = ExpenseClaim.objects.none()
    if expense_statuses:
        pending_claims = ExpenseClaim.objects.filter(
            branch__in=user.get_expense_branches(), status__in=expense_statuses
        ).exclude(employee=user)
        if branch_filter:
            pending_claims = pending_claims.filter(branch_id=branch_filter)
        pending_claims = (
            pending_claims.select_related("employee", "branch", "category")
            .order_by("submitted_at", "created_at")
        )

    needs_action, returned_to_me = [], []
    for app in pending_leaves:
        last = _last_history(app)
        if last and last.action == "RETURNED":
            note = f"Returned by {last.approver.full_name}" + (f": {last.remarks}" if last.remarks else "")
            item = _leave_item(app, "returned", "Returned to you", when=last.created_at, note=note)
            returned_to_me.append(item)
            needs_action.append(item)
        else:
            needs_action.append(_leave_item(app, "action", "Pending with you", when=app.submitted_at))
    for claim in pending_claims:
        needs_action.append(_expense_item(claim, "action", "Pending with you", when=claim.submitted_at or claim.created_at))

    # Returned items first, then oldest waiting first.
    needs_action.sort(key=lambda i: (i["tone"] != "returned", i["when"] or now))

    is_approver = bool(expense_statuses) or user.is_hr_coordinator or user.is_lead_manager or lead_qs.exists()
    return {
        "items": needs_action[:ACTION_CENTER_LIMIT],
        "count": len(needs_action),
        "returned_count": len(returned_to_me),
        "is_approver": is_approver,
    }


# ---------------------------------------------------------------------------
# Unread status alerts (rejected / final approval) for the user's own items
# ---------------------------------------------------------------------------
REJECTED_EXPENSE_STATUSES = [
    ExpenseClaimStatus.REJECTED_BY_ADMIN,
    ExpenseClaimStatus.REJECTED_BY_DIRECTOR,
]
# event_type -> (tone, statuses the item must still be in for the alert to stay relevant)
STATUS_ALERT_EVENTS = {
    "LEAVE_LEAD_REJECTED": ("rejected", REJECTED_LEAVE_STATUSES),
    "LEAVE_HR_REJECTED": ("rejected", REJECTED_LEAVE_STATUSES),
    "LEAVE_DIRECTOR_REJECTED": ("rejected", REJECTED_LEAVE_STATUSES),
    "LEAVE_DIRECTOR_APPROVED": ("approved", [LeaveApplicationStatus.APPROVED]),
    "EXPENSE_ADMIN_REJECTED": ("rejected", REJECTED_EXPENSE_STATUSES),
    "EXPENSE_DIRECTOR_REJECTED": ("rejected", REJECTED_EXPENSE_STATUSES),
    "EXPENSE_DIRECTOR_APPROVED": ("approved", [ExpenseClaimStatus.APPROVED_BY_DIRECTOR, ExpenseClaimStatus.PAID]),
    "EXPENSE_PAID": ("approved", [ExpenseClaimStatus.PAID]),
}


def _id_from_link(link_url, prefix):
    """'/leaves/12/' -> 12 for prefix '/leaves/'; None when the link is not an item link."""
    if not link_url or not link_url.startswith(prefix):
        return None
    part = link_url[len(prefix):].strip("/")
    return int(part) if part.isdigit() else None


def _remarks_from_message(message):
    if "Remarks:" not in message:
        return ""
    remarks = message.split("Remarks:", 1)[1].strip()
    return remarks.split(". You can edit and resubmit")[0].rstrip(".").strip()


def build_status_alerts(user):
    """
    Unread notifications telling this user one of their own leaves / claims was rejected or
    reached final approval (leave approved, claim approved or paid). Shown as dashboard alerts
    (web + mobile) until read. One alert per item (latest event); an alert is dropped once the
    item has moved on (e.g. a rejected leave was resubmitted).
    """
    events = list(
        NotificationEvent.objects.filter(
            recipient=user, is_read=False, event_type__in=STATUS_ALERT_EVENTS.keys()
        ).order_by("-created_at")[:30]
    )
    leave_ids = {_id_from_link(e.link_url, "/leaves/") for e in events if e.event_type.startswith("LEAVE_")}
    claim_ids = {_id_from_link(e.link_url, "/expenses/") for e in events if e.event_type.startswith("EXPENSE_")}
    leaves = {
        a.pk: a for a in LeaveApplication.objects.filter(pk__in=leave_ids - {None}, employee=user)
        .select_related("leave_type")
    }
    claims = {
        c.pk: c for c in ExpenseClaim.objects.filter(pk__in=claim_ids - {None}, employee=user)
        .select_related("category")
    }

    alerts, seen = [], set()
    for e in events:
        tone, valid_statuses = STATUS_ALERT_EVENTS[e.event_type]
        if e.event_type.startswith("LEAVE_"):
            obj = leaves.get(_id_from_link(e.link_url, "/leaves/"))
            kind = "leave"
        else:
            obj = claims.get(_id_from_link(e.link_url, "/expenses/"))
            kind = "expense"
        if not obj or (kind, obj.pk) in seen:
            continue
        seen.add((kind, obj.pk))  # newest event per item decides; older ones are superseded
        if obj.status not in valid_statuses:
            continue
        if kind == "leave":
            ref = obj.application_number
            summary = f"{obj.leave_type.name} · {obj.start_date:%b %d}" + (
                f" – {obj.end_date:%b %d}" if obj.end_date != obj.start_date else "")
            summary += f" · {fmt_days(obj.number_of_days)} day{'s' if obj.number_of_days != 1 else ''}"
            status = obj.display_status
        else:
            ref = obj.claim_number
            summary = f"{obj.category.name if obj.category_id else 'Expense'} · ${obj.amount:,.2f}"
            status = obj.get_status_display()
        alerts.append({
            "notification_id": e.pk,
            "kind": kind,
            "object_id": obj.pk,
            "tone": tone,
            "ref": ref,
            "title": e.title,
            "summary": summary,
            "status": status,
            "remarks": _remarks_from_message(e.message),
            "link_url": e.link_url,
            "created_at": e.created_at,
        })
    # Rejections first (they need action), then newest first.
    alerts.sort(key=lambda x: (x["tone"] != "rejected", -x["created_at"].timestamp()))
    return alerts
