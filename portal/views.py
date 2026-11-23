from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum, Count, Q, ProtectedError
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.utils.http import url_has_allowed_host_and_scheme
from datetime import datetime, date, timedelta
from decimal import Decimal

from accounts.models import User, RoleChoices, EmployeeProfile, UserBranchScope
from organization.models import Branch, Department
from organization.currencies import (
    all_currencies_json, currency_json, default_currency_for_country, normalize_code, parse_code_list,
)
from expenses.items import (
    ItemError, clean_items, expense_currency_context, raw_items_from_post, replace_items,
    retag_claims_to_branch_currency, summary_header,
)
from expenses.models import (
    ExpenseClaim, ExpenseCategory, ExpenseAttachment,
    ExpenseClaimStatus, PaymentMode, ReimbursementPayment, review_message
)
from leaves.models import (
    LeaveApplication, LeaveType, LeavePolicy, LeaveLedgerEntry,
    LeaveApplicationStatus, HolidayCalendar, Holiday
)
from leaves.models import LeaveAuditAction, fmt_days, STAGE_LABELS
from leaves.services import (
    get_employee_leave_balances, record_manual_leave_adjustment,
    validate_leave_request, LeaveRequestError,
    log_leave_audit, client_ip, resubmit_leave_application,
    initial_leave_route, visible_leave_applications, can_view_leave_application,
    filter_leave_track, leave_track_counts, leave_review_options, leave_correction_options, notify_leave_review,
    employee_leave_snapshot,
)
from portal.admin_views import validate_upload, can_manage_users, get_default_organization
from expenses.services import (
    approved_claim_delete_block_reason, bulk_delete_approved_claims, bulk_delete_claims,
    bulk_void_payments, claim_delete_block_reason, payment_void_block_reason,
)
from accounts.services import (
    EmployeeError, can_reset_passwords, can_manage_users, create_employee, set_employee_password,
    bulk_employee_action,
)
import secrets
from notifications.models import NotificationEvent
from notifications.services import notify_expense_event, notify_leave_event, notify_leave_correction
from portal.dashboard_services import (
    PENDING_EXPENSE_STATUSES,
    PENDING_LEAVE_STATUSES,
    build_all_staff_analytics,
    build_approver_reminders,
    build_leave_calendar,
    build_my_action_center,
    build_status_alerts,
    build_director_pipeline_summary,
    build_leave_utilization_series,
    build_monthly_expense_series,
    send_approver_reminder,
)


def _parse_post_date(value, label):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValidationError(f"Invalid {label}.")


def _leave_review_kwargs(request):
    """
    Remarks plus the optional extras posted from a web review form: a partial range, an amendment
    (leave type / whole date range) or the stage to return the leave to.
    """
    P = request.POST
    partial = P.get("partial") == "1"
    amend = P.get("amend") == "1"
    amend_type = None
    if amend and P.get("amend_leave_type"):
        amend_type = LeaveType.objects.filter(pk=P.get("amend_leave_type")).first() if P.get("amend_leave_type").isdigit() else None
        if amend_type is None:
            raise ValidationError("Unknown leave type.")
    return {
        "remarks": P.get("remarks", ""),
        "approved_start_date": _parse_post_date(P.get("approved_start_date"), "approved start date") if partial and not amend else None,
        "approved_end_date": _parse_post_date(P.get("approved_end_date"), "approved end date") if partial and not amend else None,
        "amend_leave_type": amend_type,
        "amend_start_date": _parse_post_date(P.get("amend_start_date"), "new start date") if amend else None,
        "amend_end_date": _parse_post_date(P.get("amend_end_date"), "new end date") if amend else None,
        "return_to": (P.get("return_to") or "").upper() or None,
        "source": "WEB",
        "ip_address": client_ip(request),
    }


def _leave_review_message(app, recorded, stage_label):
    if recorded == "REJECTED":
        return f"Application {app.application_number} {stage_label}: rejected."
    if recorded.startswith("PARTIALLY_"):
        return (f"Application {app.application_number} {stage_label}: partially approved - "
                f"{fmt_days(app.number_of_days)} of {fmt_days(app.requested_number_of_days)} day(s) "
                f"({app.start_date:%d %b} to {app.end_date:%d %b %Y}).")
    return f"Application {app.application_number} {stage_label}: {recorded.lower()}."


def login_view(request):
    if request.user.is_authenticated:
        return redirect("portal_dashboard")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")

        user = authenticate(request, username=username, password=password)
        if user is not None:
            if user.is_active:
                login(request, user)
                messages.success(request, f"Welcome back, {user.full_name}!")
                return redirect("portal_dashboard")
            else:
                messages.error(request, "This account has been deactivated. Please contact HR.")
        else:
            messages.error(request, "Invalid Employee ID, Email, or Password. Please try again.")

    return render(request, "portal/login.html")


@login_required
def logout_view(request):
    logout(request)
    messages.info(request, "You have been securely signed out.")
    return redirect("portal_login")


def _resolve_dashboard_scope(request, user):
    """Resolve accessible branches and branch-filtered expense/leave QuerySets."""
    accessible_branches = user.get_accessible_branches()
    requested_branch = request.GET.get("branch_filter")
    if requested_branch and (
        requested_branch == "all" or accessible_branches.filter(id=requested_branch).exists()
    ):
        selected_branch = requested_branch
        request.session["selected_branch_id"] = requested_branch
    else:
        selected_branch = request.session.get("selected_branch_id", "all")
        if selected_branch != "all" and not accessible_branches.filter(id=selected_branch).exists():
            selected_branch = "all"

    if user.role == RoleChoices.EMPLOYEE:
        branch_qs = accessible_branches
        claim_qs = ExpenseClaim.objects.filter(employee=user)
        leave_qs = LeaveApplication.objects.filter(employee=user)
    elif selected_branch and selected_branch != "all":
        branch_qs = accessible_branches.filter(id=selected_branch)
        claim_qs = ExpenseClaim.objects.filter(branch_id=selected_branch)
        leave_qs = LeaveApplication.objects.filter(branch_id=selected_branch)
    else:
        branch_qs = accessible_branches
        claim_qs = ExpenseClaim.objects.filter(branch__in=accessible_branches)
        leave_qs = LeaveApplication.objects.filter(branch__in=accessible_branches)

    return branch_qs, claim_qs, leave_qs


def _handle_director_dashboard_post(request, user, claim_qs, leave_qs):
    """Handle executive approval decisions and approver reminder dispatches on dashboard."""
    if not user.is_director:
        messages.error(request, "Action restricted to Director authority.")
        return redirect("portal_dashboard")

    action_scope = request.POST.get("action_scope", "")
    if action_scope == "expense_decision":
        claim = get_object_or_404(ExpenseClaim, id=request.POST.get("claim_id"))
        _review_claim(request, user, claim)
        return redirect("portal_dashboard")

    if action_scope == "leave_decision":
        app = get_object_or_404(LeaveApplication, id=request.POST.get("app_id"))
        if not user.can_access_branch(app.branch):
            messages.error(request, "You do not have permission for this branch.")
            return redirect("portal_dashboard")
        try:
            kwargs = _leave_review_kwargs(request)
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
            return redirect("portal_dashboard")
        level, msg = _apply_leave_review(user, app, request.POST.get("action", ""), **kwargs)
        getattr(messages, level)(request, msg)
        return redirect("portal_dashboard")

    if action_scope == "send_reminder":
        item_type = request.POST.get("item_type")
        item_id = request.POST.get("item_id")
        if item_type == "expense":
            item = get_object_or_404(ExpenseClaim, id=item_id)
            valid_pending = item.status in PENDING_EXPENSE_STATUSES
            ref = item.claim_number
        elif item_type == "leave":
            item = get_object_or_404(LeaveApplication, id=item_id)
            valid_pending = item.status in PENDING_LEAVE_STATUSES
            ref = item.application_number
        else:
            messages.error(request, "Invalid reminder target.")
            return redirect("portal_dashboard")

        if not user.can_access_branch(item.branch):
            messages.error(request, "You do not have permission for this branch.")
            return redirect("portal_dashboard")
        if not valid_pending:
            messages.info(request, f"{ref} is no longer awaiting approval.")
            return redirect("portal_dashboard")

        sent_count, approvers_label = send_approver_reminder(item, item_type, actor=user)
        if sent_count > 0:
            messages.success(request, f"Reminder sent for {ref} to {approvers_label}.")
        else:
            messages.warning(request, f"No active approver assigned for {ref} ({approvers_label}).")
        return redirect("portal_dashboard")

    if action_scope == "remind_all_pending":
        now = timezone.now()
        reminders = build_approver_reminders(claim_qs, leave_qs, now=now)
        sent_items = 0
        for row in reminders["items"]:
            if row["is_director_stage"] or not row["has_approver"]:
                continue
            if row["last_reminded_at"] and (now - row["last_reminded_at"]).total_seconds() < 3600:
                continue
            model = ExpenseClaim if row["item_type"] == "expense" else LeaveApplication
            item = model.objects.filter(id=row["item_id"]).select_related("branch", "employee__profile__reporting_lead").first()
            if item:
                cnt, _ = send_approver_reminder(item, row["item_type"], actor=user)
                if cnt > 0:
                    sent_items += 1
        if sent_items > 0:
            messages.success(request, f"Sent approval reminders for {sent_items} pending request(s).")
        else:
            messages.info(request, "All upstream pending requests were already reminded within the last hour.")
        return redirect("portal_dashboard")

    return None


@login_required
def dashboard_view(request):
    user = request.user
    branch_qs, claim_qs, leave_qs = _resolve_dashboard_scope(request, user)

    if request.method == "POST":
        post_response = _handle_director_dashboard_post(request, user, claim_qs, leave_qs)
        if post_response is not None:
            return post_response

    is_director_dashboard = user.is_director

    # Core KPI Metrics
    total_active_employees = User.objects.filter(is_active=True, primary_branch__in=branch_qs).count()
    pending_expense_claims = claim_qs.filter(
        status__in=[ExpenseClaimStatus.UNDER_ADMIN_REVIEW, ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW]
    ).count()
    approved_awaiting_payment = claim_qs.filter(status=ExpenseClaimStatus.APPROVED_BY_DIRECTOR).count()
    total_paid_expenses = claim_qs.filter(status=ExpenseClaimStatus.PAID).aggregate(
        total=Sum("amount")
    )["total"] or Decimal("0.00")
    pending_leaves_count = leave_qs.filter(status__in=PENDING_LEAVE_STATUSES).count()

    # Live Chart Series
    expense_chart_data = build_monthly_expense_series(claim_qs)
    leave_chart_data = build_leave_utilization_series(
        leave_qs if is_director_dashboard else LeaveApplication.objects.filter(employee=user)
    )

    context = {
        "active_nav": "dashboard",
        "is_director_dashboard": is_director_dashboard,
        "total_active_employees": total_active_employees,
        "pending_expense_claims": pending_expense_claims,
        "approved_awaiting_payment": approved_awaiting_payment,
        "total_paid_expenses": total_paid_expenses,
        "pending_leaves_count": pending_leaves_count,
        "expense_chart_data": expense_chart_data,
        "leave_chart_data": leave_chart_data,
        "leave_calendar": build_leave_calendar(user, request.session.get("selected_branch_id")),
        "action_center": build_my_action_center(user, request.session.get("selected_branch_id")),
        "status_alerts": build_status_alerts(user),
    }

    if is_director_dashboard:
        pipeline_summary = build_director_pipeline_summary(claim_qs, leave_qs)
        director_expense_queue = (
            claim_qs.filter(status=ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW, branch__in=user.get_expense_branches())
            .select_related("employee", "branch", "category")
            .prefetch_related("approval_history__approver", "attachments")
            .order_by("submitted_at", "created_at")
        )
        director_leave_queue = (
            leave_qs.filter(status=LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL)
            .select_related("employee", "branch", "leave_type")
            .prefetch_related("approval_history__approver")
            .order_by("submitted_at")
        )
        approver_reminders = build_approver_reminders(claim_qs, leave_qs)
        staff_analytics = build_all_staff_analytics(branch_qs, claim_qs, leave_qs)

        context.update(
            {
                "pipeline_summary": pipeline_summary,
                "director_expense_queue": director_expense_queue,
                "director_leave_queue": director_leave_queue,
                "approver_reminders": approver_reminders,
                "staff_analytics": staff_analytics,
            }
        )
    else:
        # Non-directors keep personal leave status and recent activity tables
        leave_balances = get_employee_leave_balances(user)
        recent_claims = claim_qs.select_related("category", "branch", "employee").order_by("-created_at")[:6]
        recent_leaves = leave_qs.select_related("leave_type", "branch", "employee").order_by("-submitted_at")[:6]
        team_leaves_pending = (
            LeaveApplication.objects.filter(
                employee__profile__reporting_lead=user,
                status=LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION,
            ).count()
            if user.is_lead_manager
            else 0
        )
        context.update(
            {
                "leave_balances": leave_balances,
                "recent_claims": recent_claims,
                "recent_leaves": recent_leaves,
                "team_leaves_pending": team_leaves_pending,
            }
        )

    return render(request, "portal/dashboard.html", context)


@login_required
def expenses_list_view(request):
    user = request.user
    accessible_branches = user.get_accessible_branches()
    selected_branch = request.session.get("selected_branch_id", "all")

    if request.method == "POST":
        if request.POST.get("action") == "bulk_delete":
            back = request.get_full_path()
            result = bulk_delete_claims(user, request.POST.getlist("selected"))
            if result["deleted"]:
                messages.success(request, f"{len(result['deleted'])} claim(s) deleted: {', '.join(result['deleted'][:5])}"
                                 + ("…" if len(result["deleted"]) > 5 else ""))
            if result["skipped"]:
                details = ", ".join(f"{n} ({why})" for n, why in result["skipped"][:5])
                messages.error(request, f"{len(result['skipped'])} claim(s) could not be deleted: {details}"
                               + ("…" if len(result["skipped"]) > 5 else ""))
            if not result["deleted"] and not result["skipped"]:
                messages.error(request, "Select at least one claim.")
            return redirect(back)

        # Create + submit a new expense claim (claims are always submitted; no drafts).
        # A claim holds one or more item rows, each in any currency with a manual exchange rate
        # to the branch's reimbursement currency.
        description = request.POST.get("description", "").strip()
        project_ref = request.POST.get("project_reference", "").strip()
        branch = user.primary_branch or accessible_branches.first()
        if not branch:
            messages.error(request, "Your account has no branch assigned. Contact the Admin Manager.")
            return redirect("portal_expenses")
        try:
            items = clean_items(raw_items_from_post(request.POST), branch)
        except ItemError as e:
            messages.error(request, str(e))
            return redirect("portal_expenses")
        if not description:
            messages.error(request, "Describe the business purpose of the claim.")
            return redirect("portal_expenses")
        files = request.FILES.getlist("receipts")
        if not files:
            messages.error(request, "At least one supporting receipt / proof is required.")
            return redirect("portal_expenses")
        for f in files:
            err = validate_upload(f)
            if err:
                messages.error(request, err)
                return redirect("portal_expenses")

        try:
            with transaction.atomic():
                header = summary_header(items)
                claim = ExpenseClaim.objects.create(
                    employee=user,
                    branch=branch,
                    currency_code=branch.currency_code,
                    category=header["category"],
                    amount=header["amount"],
                    expense_date=header["expense_date"],
                    description=description,
                    project_reference=project_ref,
                    status=ExpenseClaimStatus.DRAFT,
                )
                replace_items(claim, items)
                for f in files:
                    ExpenseAttachment.objects.create(
                        claim=claim, file=f, original_filename=f.name,
                        file_type=f.content_type, file_size=f.size,
                    )
                claim.submit()
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
            return redirect("portal_expenses")

        notify_expense_event(claim, "EXPENSE_SUBMITTED")
        messages.success(request, f"Claim {claim.claim_number} submitted "
                                  + ("directly to the Director for approval." if claim.status == ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW else "for review."))
        return redirect("portal_expenses")

    # ---- GET: server-side datatable ----
    is_employee = user.role == RoleChoices.EMPLOYEE
    if is_employee:
        qs = ExpenseClaim.objects.filter(employee=user)
    elif selected_branch and selected_branch != "all":
        qs = ExpenseClaim.objects.filter(branch_id=selected_branch)
    else:
        qs = ExpenseClaim.objects.filter(branch__in=accessible_branches)
    qs = qs.exclude(status=ExpenseClaimStatus.DRAFT)

    status_groups = {
        "review": [ExpenseClaimStatus.UNDER_ADMIN_REVIEW, ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW],
        "approved": [ExpenseClaimStatus.APPROVED_BY_DIRECTOR],
        "paid": [ExpenseClaimStatus.PAID],
        "rejected": [ExpenseClaimStatus.REJECTED_BY_ADMIN, ExpenseClaimStatus.REJECTED_BY_DIRECTOR],
        "withdrawn": [ExpenseClaimStatus.WITHDRAWN],
    }
    status_filter = request.GET.get("status", "")
    if status_filter in status_groups:
        qs = qs.filter(status__in=status_groups[status_filter])
    else:
        status_filter = ""

    category_filter = request.GET.get("category", "")
    if category_filter.isdigit():
        qs = qs.filter(category_id=int(category_filter))
    else:
        category_filter = ""

    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(claim_number__icontains=q)
            | Q(employee__employee_id__icontains=q)
            | Q(employee__first_name__icontains=q)
            | Q(employee__last_name__icontains=q)
            | Q(description__icontains=q)
            | Q(project_reference__icontains=q)
            | Q(category__name__icontains=q)
        )

    sort_map = {
        "number": ["claim_number"],
        "date": ["expense_date"],
        "employee": ["employee__first_name", "employee__last_name"],
        "category": ["category__name"],
        "amount": ["amount"],
        "status": ["status"],
        "submitted": ["submitted_at"],
    }
    sort = request.GET.get("sort", "submitted")
    direction = request.GET.get("dir", "desc")
    if sort not in sort_map:
        sort = "submitted"
    if direction not in ("asc", "desc"):
        direction = "desc"
    prefix = "-" if direction == "desc" else ""
    qs = (
        qs.select_related("category", "branch", "employee")
        .annotate(attachment_count=Count("attachments"))
        .order_by(*[prefix + f for f in sort_map[sort]], "-id")
    )

    page_size_options = [10, 25, 50, 100]
    try:
        page_size = int(request.GET.get("page_size", 10))
    except (TypeError, ValueError):
        page_size = 10
    if page_size not in page_size_options:
        page_size = 10

    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(request.GET.get("page"))
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1)
    claims = list(page_obj.object_list)
    for c in claims:
        c.deletable = claim_delete_block_reason(user, c) is None

    base_params = request.GET.copy()
    base_params.pop("page", None)
    sort_params = base_params.copy()
    for key in ("sort", "dir"):
        sort_params.pop(key, None)

    context = {
        "active_nav": "expenses",
        "claims": claims,
        "show_employee": not is_employee,
        "page_obj": page_obj,
        "paginator": paginator,
        "page_range": page_range,
        "page_size": page_size,
        "page_size_options": page_size_options,
        "sort": sort,
        "dir": direction,
        "query_string": base_params.urlencode(),
        "sort_query_string": sort_params.urlencode(),
        "q": q,
        "status_filter": status_filter,
        "category_filter": category_filter,
        "is_filtered": bool(q or status_filter or category_filter),
        "categories": ExpenseCategory.objects.filter(is_active=True),
        "today": date.today(),
        **expense_currency_context(user.primary_branch or accessible_branches.first()),
    }
    return render(request, "portal/expenses_list.html", context)


def _claim_review_stage(user, claim):
    """'admin' / 'director' when `user` can review this claim right now, else None."""
    if claim.employee_id == user.id or not user.can_manage_expenses_for(claim.branch):
        return None
    if claim.status == ExpenseClaimStatus.UNDER_ADMIN_REVIEW and user.is_admin_manager:
        return "admin"
    if claim.status == ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW and user.is_director:
        return "director"
    return None


@login_required
def expense_detail_view(request, pk):
    claim = get_object_or_404(ExpenseClaim, pk=pk)
    user = request.user

    # Permission check
    if claim.employee != user and not (user.can_access_branch(claim.branch) and user.role != RoleChoices.EMPLOYEE):
        messages.error(request, "You are not authorized to view this expense claim.")
        return redirect("portal_expenses")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "submit":
            try:
                claim.submit()
                notify_expense_event(claim, "EXPENSE_SUBMITTED")
                messages.success(request, "Claim submitted directly to the Director for approval."
                                 if claim.status == ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW else "Claim submitted for review.")
            except ValidationError as e:
                messages.error(request, str(e))
        elif action == "withdraw":
            try:
                claim.withdraw(user)
                messages.info(request, "Claim has been withdrawn.")
            except ValidationError as e:
                messages.error(request, str(e))
        return redirect("portal_expense_detail", pk=claim.pk)

    context = {
        "active_nav": "expenses",
        "claim": claim,
        "attachments": claim.attachments.all(),
        "items": claim.items.select_related("category"),
        "approval_history": claim.approval_history.all().order_by("created_at"),
        "can_edit": claim.employee == user and claim.status in [
            ExpenseClaimStatus.DRAFT, ExpenseClaimStatus.UNDER_ADMIN_REVIEW,
            ExpenseClaimStatus.REJECTED_BY_ADMIN, ExpenseClaimStatus.REJECTED_BY_DIRECTOR,
        ],
        "can_withdraw": claim.employee == user and claim.can_withdraw,
        "review_stage": _claim_review_stage(user, claim),
    }
    return render(request, "portal/expense_detail.html", context)


def _apply_claim_review(user, claim, action, remarks="", approved_amount=None):
    """
    Apply an Admin Manager / Director decision (full, partial amount or rejection) to one claim.
    Branch-aware: the reviewer must be an expense approver for the claim's branch.
    Returns (level, message) where level is "success" | "info" | "error".
    """
    if claim.employee_id == user.id:
        return "error", f"{claim.claim_number}: you cannot review your own claim."
    if not user.can_manage_expenses_for(claim.branch):
        return "error", f"You are not an expense approver for {claim.branch.name}."
    action = (action or "").upper()
    if claim.status == ExpenseClaimStatus.UNDER_ADMIN_REVIEW and user.is_admin_manager:
        review, stage = claim.admin_review, "ADMIN"
    elif claim.status == ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW and user.is_director:
        review, stage = claim.director_review, "DIRECTOR"
    else:
        return "info", f"Claim {claim.claim_number} is not awaiting your review ({claim.get_status_display()})."
    try:
        recorded = review(approver=user, action=action, remarks=remarks, approved_amount=approved_amount)
    except ValidationError as e:
        return "error", f"{claim.claim_number}: " + " ".join(e.messages)
    notify_expense_event(claim, f"EXPENSE_{stage}_{'REJECTED' if recorded == 'REJECTED' else 'APPROVED'}", remarks=remarks)
    return "success", review_message(claim, recorded)


def _review_claim(request, user, claim):
    """Single-claim review posted from a web review form; reports the outcome as a flash message."""
    approved_amount = request.POST.get("approved_amount") if request.POST.get("partial") == "1" else None
    level, msg = _apply_claim_review(user, claim, request.POST.get("action", ""),
                                     request.POST.get("remarks", ""), approved_amount)
    getattr(messages, level)(request, msg)


def _safe_next(request, fallback):
    nxt = request.POST.get("next") or ""
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(nxt)
    return redirect(fallback)


@login_required
def expense_approvals_view(request):
    user = request.user
    if not (user.is_admin_manager or user.is_director):
        messages.error(request, "Access restricted to expense approval authorities.")
        return redirect("portal_dashboard")

    expense_branches = user.get_expense_branches()
    selected_branch = request.session.get("selected_branch_id", "all")
    requested = request.GET.get("branch")
    if requested and (requested == "all" or expense_branches.filter(id=requested).exists()):
        selected_branch = requested
        request.session["selected_branch_id"] = requested
    if selected_branch != "all" and not expense_branches.filter(id=selected_branch).exists():
        selected_branch = "all"

    base_qs = ExpenseClaim.objects.filter(branch__in=expense_branches).exclude(employee=user)
    if selected_branch and selected_branch != "all":
        base_qs = base_qs.filter(branch_id=selected_branch)

    if request.method == "POST":
        if request.POST.get("bulk") == "1":
            decision = request.POST.get("decision", "").upper()
            remarks = request.POST.get("remarks", "").strip()
            ids = [int(i) for i in request.POST.getlist("selected") if i.isdigit()]
            if decision not in ("APPROVED", "REJECTED") or not ids:
                messages.error(request, "Select at least one claim and a decision.")
            elif decision == "REJECTED" and not remarks:
                messages.error(request, "A reason is required to reject claims. The employees will see it.")
            else:
                done, problems = [], []
                for claim in ExpenseClaim.objects.filter(pk__in=ids).select_related("branch", "employee"):
                    level, msg = _apply_claim_review(user, claim, decision, remarks)
                    (done if level == "success" else problems).append(claim.claim_number if level == "success" else msg)
                verb = "approved" if decision == "APPROVED" else "rejected"
                if done:
                    messages.success(request, f"{len(done)} claim(s) {verb}: {', '.join(done[:6])}" + ("…" if len(done) > 6 else ""))
                if problems:
                    messages.error(request, f"{len(problems)} claim(s) not processed: " + " · ".join(problems[:4])
                                   + (" …" if len(problems) > 4 else ""))
            return _safe_next(request, "portal_expense_approvals")

        claim = get_object_or_404(ExpenseClaim, id=request.POST.get("claim_id"))
        _review_claim(request, user, claim)
        return _safe_next(request, "portal_expense_approvals")

    # ---- Stage tabs: only the stages this user can act on ----
    stages = []
    if user.is_admin_manager:
        stages.append(("admin", "Admin review", ExpenseClaimStatus.UNDER_ADMIN_REVIEW))
    if user.is_director:
        stages.append(("director", "Director approval", ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW))
    stage_status = {key: status for key, _, status in stages}

    stage_stats = {
        row["status"]: row
        for row in base_qs.filter(status__in=stage_status.values())
        .values("status").annotate(n=Count("id"), total=Sum("amount"))
    }
    stage_tabs = []
    for key, label, status in stages:
        st = stage_stats.get(status, {})
        stage_tabs.append({"key": key, "label": label, "count": st.get("n", 0), "total": st.get("total") or 0})

    stage = request.GET.get("stage", "")
    if stage not in stage_status:
        # Default to the first stage with work waiting, else the first stage.
        stage = next((t["key"] for t in stage_tabs if t["count"]), stages[0][0])
    qs = base_qs.filter(status=stage_status[stage])

    # ---- Filters ----
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(claim_number__icontains=q)
            | Q(employee__first_name__icontains=q)
            | Q(employee__last_name__icontains=q)
            | Q(employee__employee_id__icontains=q)
            | Q(description__icontains=q)
            | Q(project_reference__icontains=q)
        )
    category_filter = request.GET.get("category", "")
    if category_filter.isdigit():
        qs = qs.filter(category_id=int(category_filter))
    else:
        category_filter = ""

    now = timezone.now()
    age_filter = request.GET.get("age", "")
    age_ranges = {
        "today": (now - timedelta(days=1), None),
        "week": (now - timedelta(days=7), None),
        "overdue": (None, now - timedelta(days=7)),
    }
    if age_filter in age_ranges:
        after, before = age_ranges[age_filter]
        if after:
            qs = qs.filter(submitted_at__gte=after)
        if before:
            qs = qs.filter(submitted_at__lt=before)
    else:
        age_filter = ""

    def _amount(key):
        try:
            return Decimal(request.GET.get(key, "").strip()) if request.GET.get(key, "").strip() else None
        except Exception:
            return None
    min_amount, max_amount = _amount("min"), _amount("max")
    if min_amount is not None:
        qs = qs.filter(amount__gte=min_amount)
    if max_amount is not None:
        qs = qs.filter(amount__lte=max_amount)

    filtered_stats = qs.aggregate(n=Count("id"), total=Sum("amount"))
    overdue_count = qs.filter(submitted_at__lt=now - timedelta(days=7)).count()

    # ---- Sorting + pagination ----
    sort_map = {
        "number": ["claim_number"],
        "employee": ["employee__first_name", "employee__last_name"],
        "category": ["category__name"],
        "amount": ["amount"],
        "submitted": ["submitted_at"],
    }
    sort = request.GET.get("sort", "submitted")
    direction = request.GET.get("dir", "asc")  # oldest first: work the queue in order
    if sort not in sort_map:
        sort = "submitted"
    if direction not in ("asc", "desc"):
        direction = "asc"
    prefix = "-" if direction == "desc" else ""
    qs = (qs.select_related("employee", "branch", "category")
          .prefetch_related("attachments", "approval_history__approver")
          .order_by(*[prefix + f for f in sort_map[sort]], "id"))

    page_size_options = [10, 25, 50, 100]
    try:
        page_size = int(request.GET.get("page_size", 10))
    except (TypeError, ValueError):
        page_size = 10
    if page_size not in page_size_options:
        page_size = 10
    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(request.GET.get("page"))
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1)

    claims = list(page_obj.object_list)
    for c in claims:
        c.waiting_days = (now - c.submitted_at).days if c.submitted_at else 0
        c.admin_approval = None
        if stage == "director" and not c.skips_admin_stage:
            history = [a for a in c.approval_history.all() if a.stage == "ADMIN_MANAGER"]
            c.admin_approval = history[-1] if history else None

    base_params = request.GET.copy()
    base_params.pop("page", None)
    sort_params = base_params.copy()
    for key in ("sort", "dir"):
        sort_params.pop(key, None)
    tab_params = base_params.copy()
    for key in ("stage", "sort", "dir"):
        tab_params.pop(key, None)

    context = {
        "active_nav": "expense_approvals",
        "claims": claims,
        "stage": stage,
        "stage_tabs": stage_tabs,
        "tab_query_string": tab_params.urlencode(),
        "filtered_count": filtered_stats["n"] or 0,
        "filtered_total": filtered_stats["total"] or 0,
        "overdue_count": overdue_count,
        "page_obj": page_obj,
        "paginator": paginator,
        "page_range": page_range,
        "page_size": page_size,
        "page_size_options": page_size_options,
        "sort": sort,
        "dir": direction,
        "query_string": base_params.urlencode(),
        "sort_query_string": sort_params.urlencode(),
        "q": q,
        "category_filter": category_filter,
        "age_filter": age_filter,
        "min_amount": request.GET.get("min", "") if min_amount is not None else "",
        "max_amount": request.GET.get("max", "") if max_amount is not None else "",
        "is_filtered": bool(q or category_filter or age_filter or min_amount is not None or max_amount is not None),
        "categories": ExpenseCategory.objects.filter(is_active=True),
        "expense_branches": expense_branches,
        "selected_branch": str(selected_branch),
    }
    return render(request, "portal/expense_approvals.html", context)


def _report_bulk_result(request, done, skipped, verb):
    """Flash the outcome of a bulk action: `done` claim numbers and `skipped` (number, reason) pairs."""
    if done:
        messages.success(request, f"{len(done)} claim(s) {verb}: {', '.join(done[:5])}" + ("…" if len(done) > 5 else ""))
    if skipped:
        details = ", ".join(f"{n} ({why})" for n, why in skipped[:5])
        messages.error(request, f"{len(skipped)} claim(s) could not be {verb}: {details}" + ("…" if len(skipped) > 5 else ""))
    if not done and not skipped:
        messages.error(request, "Select at least one claim.")


def _record_single_payment(request, user):
    claim_id = request.POST.get("claim_id")
    payment_date = request.POST.get("payment_date") or date.today()
    payment_mode = request.POST.get("payment_mode", PaymentMode.BANK_TRANSFER)
    payment_ref = request.POST.get("payment_reference", "").strip()
    remarks = request.POST.get("remarks", "")

    if not payment_ref:
        messages.error(request, "Payment transaction reference number is mandatory.")
        return
    if payment_mode not in PaymentMode.values:
        messages.error(request, "Choose a valid payment mode.")
        return
    if not (user.is_admin_manager or user.is_superuser):
        messages.error(request, "Only the Admin Manager can mark claims as paid.")
        return
    claim = get_object_or_404(ExpenseClaim, id=claim_id)
    if not user.can_manage_expenses_for(claim.branch):
        messages.error(request, f"You are not an expense approver for {claim.branch.name}.")
        return
    if claim.employee_id == user.id and not user.is_superuser:
        messages.error(request, "Another Admin Manager must record payment of your own claim.")
        return
    try:
        claim.record_payment(
            recorded_by=user,
            payment_date=payment_date,
            payment_mode=payment_mode,
            payment_reference=payment_ref,
            remarks=remarks,
        )
        notify_expense_event(claim, "EXPENSE_PAID")
        messages.success(request, f"Reimbursement payment recorded for claim {claim.claim_number}!")
    except ValidationError as e:
        messages.error(request, " ".join(e.messages))


@login_required
def reimbursements_view(request):
    user = request.user
    if not (user.is_admin_manager or user.is_director or user.is_superuser):
        messages.error(request, "Access restricted.")
        return redirect("portal_dashboard")

    if request.method == "POST":
        action = request.POST.get("action", "record_payment")
        if action == "bulk_delete":
            result = bulk_delete_approved_claims(user, request.POST.getlist("selected"))
            _report_bulk_result(request, result["deleted"], result["skipped"], "deleted")
        elif action == "bulk_void":
            result = bulk_void_payments(user, request.POST.getlist("selected"))
            _report_bulk_result(request, result["voided"], result["skipped"], "returned to the payment queue")
        else:
            _record_single_payment(request, user)
        return _safe_next(request, "portal_reimbursements")

    # ---- Branch scope (shared with the approvals page through the session) ----
    expense_branches = user.get_expense_branches()
    selected_branch = request.session.get("selected_branch_id", "all")
    requested = request.GET.get("branch")
    if requested and (requested == "all" or expense_branches.filter(id=requested).exists()):
        selected_branch = requested
        request.session["selected_branch_id"] = requested
    if selected_branch != "all" and not expense_branches.filter(id=selected_branch).exists():
        selected_branch = "all"

    base_qs = ExpenseClaim.objects.filter(branch__in=expense_branches)
    if selected_branch != "all":
        base_qs = base_qs.filter(branch_id=selected_branch)
    pending_base = base_qs.filter(status=ExpenseClaimStatus.APPROVED_BY_DIRECTOR)
    paid_base = base_qs.filter(status=ExpenseClaimStatus.PAID, payment_record__isnull=False)

    # ---- KPIs (whole scope, unaffected by table filters) ----
    now = timezone.now()
    today = timezone.localdate()
    month_start = today.replace(day=1)
    pending_stats = pending_base.aggregate(n=Count("id"), total=Sum("amount"))
    oldest = pending_base.order_by("updated_at").values_list("updated_at", flat=True).first()
    month_stats = paid_base.filter(payment_record__payment_date__gte=month_start).aggregate(n=Count("id"), total=Sum("amount"))
    paid_stats = paid_base.aggregate(n=Count("id"), total=Sum("amount"))
    kpis = {
        "pending_count": pending_stats["n"] or 0,
        "pending_total": pending_stats["total"] or 0,
        "oldest_days": (now - oldest).days if oldest else None,
        "overdue_count": pending_base.filter(updated_at__lt=now - timedelta(days=7)).count(),
        "month_count": month_stats["n"] or 0,
        "month_total": month_stats["total"] or 0,
        "paid_count": paid_stats["n"] or 0,
        "paid_total": paid_stats["total"] or 0,
    }

    tab = request.GET.get("tab", "pending")
    if tab not in ("pending", "paid"):
        tab = "pending"
    qs = pending_base if tab == "pending" else paid_base

    # ---- Filters ----
    q = request.GET.get("q", "").strip()
    if q:
        search = (Q(claim_number__icontains=q) | Q(employee__first_name__icontains=q)
                  | Q(employee__last_name__icontains=q) | Q(employee__employee_id__icontains=q)
                  | Q(project_reference__icontains=q) | Q(description__icontains=q))
        if tab == "paid":
            search |= Q(payment_record__payment_reference__icontains=q)
        qs = qs.filter(search)

    category_filter = request.GET.get("category", "")
    if category_filter.isdigit():
        qs = qs.filter(category_id=int(category_filter))
    else:
        category_filter = ""

    def _amount(key):
        raw = request.GET.get(key, "").strip()
        try:
            return Decimal(raw) if raw else None
        except Exception:
            return None
    min_amount, max_amount = _amount("min"), _amount("max")
    if min_amount is not None:
        qs = qs.filter(amount__gte=min_amount)
    if max_amount is not None:
        qs = qs.filter(amount__lte=max_amount)

    mode_filter, date_from, date_to = "", "", ""
    if tab == "paid":
        mode_filter = request.GET.get("mode", "")
        if mode_filter in PaymentMode.values:
            qs = qs.filter(payment_record__payment_mode=mode_filter)
        else:
            mode_filter = ""

        def _date(key):
            try:
                return date.fromisoformat(request.GET.get(key, "").strip())
            except ValueError:
                return None
        d_from, d_to = _date("from"), _date("to")
        if d_from:
            qs = qs.filter(payment_record__payment_date__gte=d_from)
            date_from = d_from.isoformat()
        if d_to:
            qs = qs.filter(payment_record__payment_date__lte=d_to)
            date_to = d_to.isoformat()

    filtered_stats = qs.aggregate(n=Count("id"), total=Sum("amount"))

    # ---- Sorting ----
    if tab == "pending":
        sort_map = {
            "number": ["claim_number"],
            "employee": ["employee__first_name", "employee__last_name"],
            "category": ["category__name"],
            "amount": ["amount"],
            "approved": ["updated_at"],
        }
        default_sort, default_dir = "approved", "asc"  # oldest approval first: pay in order
    else:
        sort_map = {
            "number": ["claim_number"],
            "employee": ["employee__first_name", "employee__last_name"],
            "amount": ["amount"],
            "paid_on": ["payment_record__payment_date", "payment_record__created_at"],
            "mode": ["payment_record__payment_mode"],
            "reference": ["payment_record__payment_reference"],
        }
        default_sort, default_dir = "paid_on", "desc"
    sort = request.GET.get("sort", default_sort)
    if sort not in sort_map:
        sort = default_sort
    direction = request.GET.get("dir", default_dir)
    if direction not in ("asc", "desc"):
        direction = default_dir
    prefix = "-" if direction == "desc" else ""
    qs = qs.select_related("employee", "branch", "category", "payment_record__recorded_by")
    qs = qs.order_by(*[prefix + f for f in sort_map[sort]], "id")

    # ---- Pagination ----
    page_size_options = [10, 25, 50, 100]
    try:
        page_size = int(request.GET.get("page_size", 10))
    except (TypeError, ValueError):
        page_size = 10
    if page_size not in page_size_options:
        page_size = 10
    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(request.GET.get("page"))
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1)

    claims = list(page_obj.object_list)
    for c in claims:
        if tab == "pending":
            c.waiting_days = (now - c.updated_at).days
            c.payable = (user.is_admin_manager or user.is_superuser) and (c.employee_id != user.id or user.is_superuser)
            c.delete_block = approved_claim_delete_block_reason(user, c)
        else:
            c.delete_block = payment_void_block_reason(user, c)

    base_params = request.GET.copy()
    base_params.pop("page", None)
    sort_params = base_params.copy()
    for key in ("sort", "dir"):
        sort_params.pop(key, None)
    is_filtered = bool(q or category_filter or min_amount is not None or max_amount is not None
                       or mode_filter or date_from or date_to)

    context = {
        "active_nav": "reimbursements",
        "tab": tab,
        "kpis": kpis,
        "claims": claims,
        "filtered_count": filtered_stats["n"] or 0,
        "filtered_total": filtered_stats["total"] or 0,
        "page_obj": page_obj,
        "paginator": paginator,
        "page_range": page_range,
        "page_size": page_size,
        "page_size_options": page_size_options,
        "sort": sort,
        "dir": direction,
        "query_string": base_params.urlencode(),
        "sort_query_string": sort_params.urlencode(),
        "q": q,
        "category_filter": category_filter,
        "mode_filter": mode_filter,
        "date_from": date_from,
        "date_to": date_to,
        "min_amount": request.GET.get("min", "") if min_amount is not None else "",
        "max_amount": request.GET.get("max", "") if max_amount is not None else "",
        "is_filtered": is_filtered,
        "categories": ExpenseCategory.objects.filter(is_active=True),
        "payment_modes": PaymentMode.choices,
        "expense_branches": expense_branches,
        "selected_branch": str(selected_branch),
        "can_pay": user.is_admin_manager or user.is_superuser,
        "can_delete_approved": user.is_director or user.is_superuser,
        "can_void_payments": user.is_admin_manager or user.is_superuser,
        # Row checkboxes appear only when this user has a bulk action on the current tab.
        "show_check": (user.is_director or user.is_superuser) if tab == "pending" else (user.is_admin_manager or user.is_superuser),
        "today": today,
    }
    return render(request, "portal/reimbursements.html", context)


@login_required
def leaves_list_view(request):
    user = request.user
    accessible_branches = user.get_accessible_branches()
    selected_branch = request.session.get("selected_branch_id", "all")

    if request.method == "POST":
        leave_type_id = request.POST.get("leave_type")
        start_date_str = request.POST.get("start_date")
        end_date_str = request.POST.get("end_date")
        is_half_day = request.POST.get("is_half_day") == "1"
        half_day_period = request.POST.get("half_day_period", "NONE")
        reason = request.POST.get("reason", "").strip()
        attachment = request.FILES.get("attachment")

        if not leave_type_id or not start_date_str or not end_date_str or not reason:
            messages.error(request, "All required leave fields must be completed.")
            return redirect("portal_leaves")

        leave_type = get_object_or_404(LeaveType, id=leave_type_id, is_active=True)

        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        except ValueError:
            messages.error(request, "Invalid date format.")
            return redirect("portal_leaves")

        try:
            number_of_days, end_date, warnings = validate_leave_request(
                user, leave_type, start_date, end_date, is_half_day,
                acknowledge_insufficient_balance=request.POST.get("acknowledge_insufficient_balance") == "1",
                attachment=attachment,
            )
        except LeaveRequestError as e:
            messages.error(request, " ".join(e.messages))
            if e.extra.get("insufficient_balance"):
                messages.warning(request, "To apply anyway, tick 'Apply even if my balance is insufficient' and submit again.")
            return redirect("portal_leaves")
        for w in warnings:
            messages.warning(request, w)

        route = initial_leave_route(user)
        app = LeaveApplication.objects.create(
            employee=user,
            branch=user.primary_branch or accessible_branches.first(),
            leave_type=leave_type,
            start_date=start_date,
            end_date=end_date,
            is_half_day=is_half_day,
            half_day_period=half_day_period if is_half_day else "NONE",
            number_of_days=number_of_days,
            reason=reason,
            attachment=attachment,
            status=route["status"],
            starts_at_stage=route["stage"],
        )
        log_leave_audit(app, user, LeaveAuditAction.CREATED, stage="EMPLOYEE", remarks=route["note"],
                        source="WEB", ip_address=client_ip(request))

        notify_leave_event(app, "LEAVE_SUBMITTED")
        messages.success(request, f"Leave application {app.application_number} submitted - {app.current_stage}.")
        return redirect("portal_leaves")

    # GET
    applications = visible_leave_applications(user)
    if user.role not in (RoleChoices.EMPLOYEE, RoleChoices.LEAD_MANAGER) and selected_branch and selected_branch != "all":
        applications = applications.filter(branch_id=selected_branch)

    applications = applications.select_related("leave_type", "branch", "employee")
    leave_types = LeaveType.objects.filter(is_active=True)

    # ---- Server-side datatable: search, filters, sorting, pagination ----
    q = request.GET.get("q", "").strip()
    if q:
        applications = applications.filter(
            Q(employee__first_name__icontains=q)
            | Q(employee__last_name__icontains=q)
            | Q(employee__employee_id__icontains=q)
            | Q(application_number__icontains=q)
            | Q(branch__name__icontains=q)
            | Q(branch__city__icontains=q)
            | Q(leave_type__name__icontains=q)
        )

    status_filter = request.GET.get("status", "")
    status_groups = {
        "pending": [
            LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION,
            LeaveApplicationStatus.PENDING_HR_VERIFICATION,
            LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL,
        ],
        "approved": [LeaveApplicationStatus.APPROVED],
        "rejected": [
            LeaveApplicationStatus.REJECTED_BY_LEAD,
            LeaveApplicationStatus.REJECTED_BY_HR,
            LeaveApplicationStatus.REJECTED_BY_DIRECTOR,
        ],
        "withdrawn": [LeaveApplicationStatus.WITHDRAWN],
    }
    if status_filter in status_groups:
        applications = applications.filter(status__in=status_groups[status_filter])
    else:
        status_filter = ""

    leave_type_filter = request.GET.get("leave_type", "")
    if leave_type_filter.isdigit():
        applications = applications.filter(leave_type_id=int(leave_type_filter))
    else:
        leave_type_filter = ""

    sort_map = {
        "employee": ["employee__first_name", "employee__last_name"],
        "location": ["branch__name"],
        "number": ["application_number"],
        "type": ["leave_type__name"],
        "dates": ["start_date"],
        "days": ["number_of_days"],
        "status": ["status"],
        "submitted": ["submitted_at"],
    }
    sort = request.GET.get("sort", "submitted")
    direction = request.GET.get("dir", "desc")
    if sort not in sort_map:
        sort = "submitted"
    if direction not in ("asc", "desc"):
        direction = "desc"
    prefix = "-" if direction == "desc" else ""
    applications = applications.order_by(*[prefix + f for f in sort_map[sort]], "-id")

    page_size_options = [10, 25, 50, 100]
    try:
        page_size = int(request.GET.get("page_size", 10))
    except (TypeError, ValueError):
        page_size = 10
    if page_size not in page_size_options:
        page_size = 10

    paginator = Paginator(applications, page_size)
    page_obj = paginator.get_page(request.GET.get("page"))
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1)

    # Query string without page/sort params, for building links
    base_params = request.GET.copy()
    for key in ("page",):
        base_params.pop(key, None)
    sort_params = base_params.copy()
    for key in ("sort", "dir"):
        sort_params.pop(key, None)

    context = {
        "active_nav": "leaves",
        "applications": page_obj.object_list,
        "page_obj": page_obj,
        "paginator": paginator,
        "page_range": page_range,
        "page_size": page_size,
        "page_size_options": page_size_options,
        "q": q,
        "status_filter": status_filter,
        "leave_type_filter": leave_type_filter,
        "sort": sort,
        "dir": direction,
        "query_string": base_params.urlencode(),
        "sort_query_string": sort_params.urlencode(),
        "leave_types": leave_types,
        "is_filtered": bool(q or status_filter or leave_type_filter),
    }
    return render(request, "portal/leaves_list.html", context)


@login_required
def leave_detail_view(request, pk):
    app = get_object_or_404(LeaveApplication, pk=pk)
    user = request.user

    if not can_view_leave_application(user, app):
        messages.error(request, "Permission denied.")
        return redirect("portal_leaves")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "withdraw":
            try:
                app.withdraw(user, source="WEB", ip_address=client_ip(request))
                messages.info(request, "Leave application has been withdrawn.")
            except ValidationError as e:
                messages.error(request, " ".join(e.messages))
        elif action == "resubmit":
            return _handle_leave_resubmit(request, app)
        elif action in ("revoke_approval", "revert_verification", "edit_dates"):
            _handle_leave_correction(request, app, action)
        return redirect("portal_leave_detail", pk=app.pk)

    approval_history = list(app.approval_history.select_related("approver").order_by("created_at"))
    review = leave_review_options(user, app)
    corrections = leave_correction_options(user, app)
    context = {
        "active_nav": "leaves",
        "snapshot": employee_leave_snapshot(app) if app.employee_id != user.id else None,
        "app": app,
        "approval_history": approval_history,
        "progress": _leave_progress(app, approval_history),
        "audit_trail": _audit_rows(app),
        "can_resubmit": app.can_resubmit(user),
        "review": review,
        "leave_types": LeaveType.objects.filter(is_active=True) if app.can_resubmit(user) else [],
        "amend_leave_types": LeaveType.objects.filter(is_active=True) if review or corrections else [],
        "corrections": corrections,
    }
    return render(request, "portal/leave_detail.html", context)


def _handle_leave_correction(request, app, action):
    """Director: revoke approval / change dates of an approved leave. HR: revert verification / change dates."""
    user = request.user
    options = leave_correction_options(user, app)
    allowed = options and (
        (action == "revoke_approval" and options["can_revoke"])
        or (action == "revert_verification" and options["can_revert"])
        or (action == "edit_dates" and options["can_edit"]))
    if not allowed:
        if app.status == LeaveApplicationStatus.APPROVED and user.is_hr_coordinator and not user.is_director:
            messages.error(request, "The Director has already approved this leave. HR can no longer revert or change it.")
        else:
            messages.error(request, "You do not have permission to make this change.")
        return
    remarks = request.POST.get("remarks", "").strip()
    kw = {"source": "WEB", "ip_address": client_ip(request)}
    try:
        if action == "revoke_approval":
            app.revoke_approval(user, remarks, **kw)
            notify_leave_correction(app, "REVOKED", user, remarks)
            messages.success(request, f"Approval of {app.application_number} revoked. {fmt_days(app.number_of_days)} day(s) "
                                      "credited back; the leave is pending your decision again.")
        elif action == "revert_verification":
            app.revert_verification(user, remarks, **kw)
            notify_leave_correction(app, "REVERTED", user, remarks)
            messages.success(request, f"HR verification of {app.application_number} reverted. It is back in the HR verification queue.")
        else:
            P = request.POST
            leave_type = None
            if P.get("amend_leave_type", "").isdigit():
                leave_type = LeaveType.objects.filter(pk=P["amend_leave_type"], is_active=True).first()
            changes = app.edit_dates(
                user,
                _parse_post_date(P.get("amend_start_date"), "new start date"),
                _parse_post_date(P.get("amend_end_date"), "new end date"),
                remarks, leave_type=leave_type, **kw)
            notify_leave_correction(app, "DATES", user, remarks, changes=changes)
            extra = " Leave balance re-adjusted." if app.status == LeaveApplicationStatus.APPROVED else ""
            messages.success(request, f"{app.application_number} updated: {app.start_date:%d %b %Y} to "
                                      f"{app.end_date:%d %b %Y}, {fmt_days(app.number_of_days)} day(s).{extra}")
    except ValidationError as e:
        messages.error(request, " ".join(e.messages))


LEAVE_PROGRESS_STAGES = [
    ("LEAD_MANAGER", "Lead recommendation", "Lead / Manager"),
    ("HR_COORDINATOR", "HR verification", "HR Coordinator"),
    ("DIRECTOR", "Director approval", "Director"),
]


def _leave_progress(app, history):
    """
    Per-stage view model for the approval tracker on the leave detail page.
    Only the current cycle counts: decisions made before the latest resubmission are ignored.
    """
    S = LeaveApplicationStatus
    stage_keys = [key for key, _, _ in LEAVE_PROGRESS_STAGES]
    resubmissions = [h for h in history if h.action == "RESUBMITTED"]
    last_resubmit = resubmissions[-1] if resubmissions else None
    cycle = history[history.index(last_resubmit) + 1:] if last_resubmit else history
    latest = {}
    for h in cycle:
        if h.stage not in stage_keys:
            continue
        if h.action == "RETURNED":
            # Sent back: decisions from the return target onwards no longer count.
            if h.returned_to in stage_keys:
                for k in stage_keys[stage_keys.index(h.returned_to):]:
                    latest.pop(k, None)
            continue
        if h.action in ("REVOKED", "VERIFICATION_REVERTED"):
            # Taken back: this stage's decision and everything after it no longer count.
            for k in stage_keys[stage_keys.index(h.stage):]:
                latest.pop(k, None)
            continue
        if h.action == "DATES_EDITED":
            continue
        latest[h.stage] = h

    pending_stage = {
        S.PENDING_LEAD_RECOMMENDATION: "LEAD_MANAGER",
        S.PENDING_HR_VERIFICATION: "HR_COORDINATOR",
        S.PENDING_DIRECTOR_APPROVAL: "DIRECTOR",
    }.get(app.status)
    rejected_stage = {
        S.REJECTED_BY_LEAD: "LEAD_MANAGER",
        S.REJECTED_BY_HR: "HR_COORDINATOR",
        S.REJECTED_BY_DIRECTOR: "DIRECTOR",
    }.get(app.status)
    halted_at = pending_stage or rejected_stage
    halted_idx = stage_keys.index(halted_at) if halted_at else len(stage_keys)
    skipped = set(app.skipped_stages)
    withdrawn = app.status == S.WITHDRAWN

    steps = [{
        "title": "Resubmitted" if last_resubmit else "Submitted",
        "role": "Employee",
        "state": "done",
        "status": "Resubmitted" if last_resubmit else "Submitted",
        "actor": app.employee.full_name,
        "date": last_resubmit.created_at if last_resubmit else app.submitted_at,
    }]
    for idx, (key, title, role) in enumerate(LEAVE_PROGRESS_STAGES):
        entry = latest.get(key)
        step = {"title": title, "role": role, "actor": None, "date": None, "partial": False}
        if key in skipped:
            step.update(state="skipped", status="Not required")
        elif key == rejected_stage:
            step.update(state="rejected", status="Rejected")
        elif key == pending_stage:
            step.update(state="current", status=f"Awaiting {role}")
        elif entry and "REJECT" not in entry.action:
            step.update(state="done", status=entry.action_label, partial="PARTIALLY" in entry.action)
        elif app.status == S.APPROVED or idx < halted_idx and not withdrawn:
            step.update(state="done", status="Completed")
        elif withdrawn:
            step.update(state="upcoming", status="Withdrawn")
        elif rejected_stage:
            step.update(state="upcoming", status="Not reached")
        else:
            step.update(state="upcoming", status="Waiting")
        if entry and step["state"] in ("done", "rejected"):
            step.update(actor=entry.approver.full_name, date=entry.created_at)
        steps.append(step)

    applicable = [s for s in steps if s["state"] != "skipped"]
    completed = sum(1 for s in applicable if s["state"] == "done")
    current_title = next((s["title"] for s in steps if s["state"] in ("current", "rejected")), "")
    if app.status == S.APPROVED:
        headline, tone = ("Partially approved" if app.is_partially_approved else "Approved"), "success"
    elif rejected_stage:
        headline, tone = f"Rejected at {current_title.lower()}", "danger"
    elif withdrawn:
        headline, tone = "Withdrawn by employee", "neutral"
    else:
        headline, tone = app.current_stage, "primary"
    return {
        "steps": steps,
        "completed": completed,
        "total": len(applicable),
        "percent": round(completed * 100 / len(applicable)) if applicable else 0,
        "headline": headline,
        "tone": tone,
    }


AUDIT_FIELD_LABELS = {
    "returned_to": "Returned", "leave_type": "Leave type", "start_date": "From", "end_date": "To", "is_half_day": "Half day",
    "half_day_period": "Half-day period", "number_of_days": "Days", "reason": "Reason", "attachment": "Attachment",
}


def _audit_rows(app):
    """Audit log entries with status labels and readable field changes for the template."""
    status_labels = dict(LeaveApplicationStatus.choices)
    rows = []
    for log in app.audit_logs.select_related("actor").order_by("created_at", "id"):
        log.from_status_label = status_labels.get(log.from_status, log.from_status)
        log.to_status_label = status_labels.get(log.to_status, log.to_status)
        log.change_rows = [
            (AUDIT_FIELD_LABELS.get(field, field.replace("_", " ").capitalize()), vals[0], vals[1])
            for field, vals in (log.changes or {}).items()
            if isinstance(vals, (list, tuple)) and len(vals) == 2
        ]
        rows.append(log)
    return rows


def _handle_leave_resubmit(request, app):
    user = request.user
    redirect_detail = redirect("portal_leave_detail", pk=app.pk)
    if not app.can_resubmit(user):
        messages.error(request, "Only your own rejected leave applications can be resubmitted.")
        return redirect_detail

    leave_type = get_object_or_404(LeaveType, id=request.POST.get("leave_type") or app.leave_type_id, is_active=True)
    try:
        start_date = _parse_post_date(request.POST.get("start_date"), "start date")
        end_date = _parse_post_date(request.POST.get("end_date"), "end date") or start_date
    except ValidationError as e:
        messages.error(request, " ".join(e.messages))
        return redirect_detail
    if not start_date:
        messages.error(request, "Start date is required.")
        return redirect_detail

    attachment = request.FILES.get("attachment")
    if attachment:
        upload_error = validate_upload(attachment)
        if upload_error:
            messages.error(request, upload_error)
            return redirect_detail

    is_half_day = request.POST.get("is_half_day") == "1"
    try:
        app, warnings = resubmit_leave_application(
            app, user, leave_type, start_date, end_date,
            reason=request.POST.get("reason", ""),
            is_half_day=is_half_day,
            half_day_period=request.POST.get("half_day_period", "FIRST_HALF"),
            attachment=attachment,
            remove_attachment=request.POST.get("remove_attachment") == "1",
            acknowledge_insufficient_balance=request.POST.get("acknowledge_insufficient_balance") == "1",
            resubmit_note=request.POST.get("resubmit_note", ""),
            source="WEB", ip_address=client_ip(request),
        )
    except LeaveRequestError as e:
        messages.error(request, " ".join(e.messages))
        if e.extra.get("insufficient_balance"):
            messages.warning(request, "To resubmit anyway, tick 'Apply even if my balance is insufficient'.")
        return redirect_detail

    for w in warnings:
        messages.warning(request, w)
    notify_leave_event(app, "LEAVE_RESUBMITTED")
    messages.success(request, f"Leave application {app.application_number} resubmitted for Lead recommendation.")
    return redirect_detail


LEAVE_STAGES = [
    # key, tab label, pending status, positive action, positive label, history stage of the previous step
    ("lead", "Lead recommendation", LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION, "RECOMMENDED", "Recommend", None),
    ("hr", "HR verification", LeaveApplicationStatus.PENDING_HR_VERIFICATION, "VERIFIED", "Verify & forward", "LEAD_MANAGER"),
    ("director", "Director approval", LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL, "APPROVED", "Approve", "HR_COORDINATOR"),
]


def _apply_leave_review(user, app, action, remarks="", source="WEB", ip_address=None, **extra):
    """
    Apply one Lead / HR / Director decision: forward / approve (optionally partial or amended),
    return to an earlier stage, or reject. Returns (level, message); level is success | error.
    """
    reporting_lead = getattr(getattr(app.employee, "profile", None), "reporting_lead", None)
    if app.employee_id == user.id:
        return "error", f"{app.application_number}: you cannot act on your own leave application."
    if app.status == LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION:
        if not (reporting_lead == user or user.is_superuser):
            return "error", f"{app.application_number}: only the employee's reporting Lead / Manager can recommend it."
    elif not user.can_access_branch(app.branch):
        return "error", f"{app.application_number}: you do not have permission for this branch."

    kwargs = dict(remarks=remarks, source=source, ip_address=ip_address, **extra)
    action = (action or "").upper()
    try:
        if app.status == LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION:
            stage, label, extra_msg = "LEAD_MANAGER", "Lead review", ""
            recorded = app.lead_review(approver=user, action=action, **kwargs)
        elif user.is_hr_coordinator and app.status == LeaveApplicationStatus.PENDING_HR_VERIFICATION:
            stage, label, extra_msg = "HR_COORDINATOR", "HR verification", ""
            recorded = app.hr_review(approver=user, action=action, **kwargs)
        elif user.is_director and app.status == LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL:
            stage, label, extra_msg = "DIRECTOR", "Final decision", " Official balance updated."
            recorded = app.director_review(approver=user, action=action, **kwargs)
        else:
            return "error", f"{app.application_number} is not awaiting your action."
    except ValidationError as e:
        return "error", f"{app.application_number}: " + " ".join(e.messages)
    notify_leave_review(app, stage, recorded, remarks=remarks, actor=user)

    if recorded == "RETURNED":
        last = app.approval_history.filter(action="RETURNED").order_by("-created_at").first()
        target = STAGE_LABELS.get(last.returned_to, "an earlier stage") if last else "an earlier stage"
        return "success", f"Application {app.application_number} returned to the {target} for another review."
    msg = _leave_review_message(app, recorded, label)
    if getattr(app, "last_amendment", None):
        msg += (f" Changed to {app.leave_type.name}, {app.start_date:%d %b} to {app.end_date:%d %b %Y} "
                f"({fmt_days(app.number_of_days)} day(s)).")
    if recorded == "REJECTED" or app.status != LeaveApplicationStatus.APPROVED:
        extra_msg = ""
    return "success", msg + extra_msg


def _datatable_page(request, qs, sort_map, default_sort, default_dir="desc"):
    """Shared server-side datatable plumbing: sorting, page size, pagination and link query strings."""
    sort = request.GET.get("sort", default_sort)
    direction = request.GET.get("dir", default_dir)
    if sort not in sort_map:
        sort = default_sort
    if direction not in ("asc", "desc"):
        direction = default_dir
    prefix = "-" if direction == "desc" else ""
    qs = qs.order_by(*[prefix + f for f in sort_map[sort]], "id")

    page_size_options = [10, 25, 50, 100]
    try:
        page_size = int(request.GET.get("page_size", 10))
    except (TypeError, ValueError):
        page_size = 10
    if page_size not in page_size_options:
        page_size = 10
    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(request.GET.get("page"))

    base_params = request.GET.copy()
    base_params.pop("page", None)
    sort_params = base_params.copy()
    for key in ("sort", "dir"):
        sort_params.pop(key, None)
    return {
        "page_obj": page_obj,
        "paginator": paginator,
        "page_range": paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1),
        "page_size": page_size,
        "page_size_options": page_size_options,
        "sort": sort,
        "dir": direction,
        "query_string": base_params.urlencode(),
        "sort_query_string": sort_params.urlencode(),
    }


@login_required
def leave_approvals_view(request):
    user = request.user
    if user.role == RoleChoices.EMPLOYEE and not user.is_superuser:
        messages.error(request, "Access restricted to approval authorities.")
        return redirect("portal_dashboard")

    accessible_branches = user.get_accessible_branches()
    selected_branch = request.session.get("selected_branch_id", "all")

    if request.method == "POST":
        next_url = request.POST.get("next", "")
        back = redirect(next_url if next_url.startswith("/leaves/") and "//" not in next_url
                        else "portal_leave_approvals")

        if request.POST.get("bulk") == "1":
            decision = request.POST.get("decision", "")
            remarks = request.POST.get("remarks", "").strip()
            ids = [int(i) for i in request.POST.getlist("selected") if i.isdigit()]
            if decision not in ("POSITIVE", "REJECTED") or not ids:
                messages.error(request, "Select at least one application and a decision.")
                return back
            if decision == "REJECTED" and not remarks:
                messages.error(request, "A reason is required to reject applications. The employees will see it.")
                return back
            positive_by_status = {status: action for _, _, status, action, _, _ in LEAVE_STAGES}
            done, problems = [], []
            apps = LeaveApplication.objects.filter(pk__in=ids).select_related("employee__profile", "branch", "leave_type")
            for app in apps:
                action = "REJECTED" if decision == "REJECTED" else positive_by_status.get(app.status, "")
                level, msg = _apply_leave_review(user, app, action, remarks, ip_address=client_ip(request))
                if level == "success":
                    done.append(app.application_number)
                else:
                    problems.append(msg)
            verb = "rejected" if decision == "REJECTED" else "approved / forwarded"
            if done:
                messages.success(request, f"{len(done)} application(s) {verb}: {', '.join(done[:6])}" + ("…" if len(done) > 6 else ""))
            if problems:
                messages.error(request, f"{len(problems)} application(s) not processed: " + " · ".join(problems[:4])
                               + (" …" if len(problems) > 4 else ""))
            return back

        app = get_object_or_404(LeaveApplication, id=request.POST.get("app_id"))
        try:
            kwargs = _leave_review_kwargs(request)
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
            return back
        level, msg = _apply_leave_review(user, app, request.POST.get("action", ""), **kwargs)
        getattr(messages, level)(request, msg)
        return back

    # ---- Queues this user can act on ----
    branch_qs = LeaveApplication.objects.filter(branch__in=accessible_branches)
    if selected_branch and selected_branch != "all":
        branch_qs = branch_qs.filter(branch_id=selected_branch)
    lead_qs = LeaveApplication.objects.filter(
        employee__profile__reporting_lead=user, status=LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION)

    queue_sources = {}
    if user.is_lead_manager or user.is_superuser or lead_qs.exists():
        queue_sources["lead"] = lead_qs
    if user.is_hr_coordinator or user.is_superuser:
        queue_sources["hr"] = branch_qs.filter(status=LeaveApplicationStatus.PENDING_HR_VERIFICATION)
    if user.is_director or user.is_superuser:
        queue_sources["director"] = branch_qs.filter(status=LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL)
    queue_sources = {k: v.exclude(employee=user) for k, v in queue_sources.items()}

    tabs = []
    for key, label, _status, _action, positive_label, _prev in LEAVE_STAGES:
        if key in queue_sources:
            agg = queue_sources[key].aggregate(n=Count("id"), days=Sum("number_of_days"))
            tabs.append({"key": key, "label": label, "count": agg["n"] or 0, "days": agg["days"] or 0,
                         "positive_label": positive_label})

    # Approval tracker: everyone else's leave this user may follow, location-scoped.
    tracked_base = visible_leave_applications(user, include_own=False)
    if user.role != RoleChoices.LEAD_MANAGER and selected_branch and selected_branch != "all":
        tracked_base = tracked_base.filter(branch_id=selected_branch)

    view = request.GET.get("view", "")
    valid_views = [t["key"] for t in tabs] + ["tracker"]
    if view not in valid_views:
        if "track" in request.GET or not tabs:
            view = "tracker"
        else:
            view = next((t["key"] for t in tabs if t["count"]), tabs[0]["key"])

    track = request.GET.get("track", "pending")
    if view == "tracker":
        track_counts = leave_track_counts(tracked_base, user)
        qs = filter_leave_track(tracked_base, track, user)
        track_filters = [
            (key, label, track_counts.get(key, 0)) for key, label in [
                ("pending", "In progress"), ("awaiting_lead", "Awaiting Lead"), ("awaiting_hr", "Awaiting HR"),
                ("awaiting_director", "Awaiting Director"), ("approved", "Approved"), ("rejected", "Rejected"),
                ("acted_by_me", "Acted on by me"), ("all", "All"),
            ]
        ]
    else:
        qs = queue_sources[view]
        track_filters = []

    # ---- Filters ----
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(
            Q(application_number__icontains=q)
            | Q(employee__first_name__icontains=q)
            | Q(employee__last_name__icontains=q)
            | Q(employee__employee_id__icontains=q)
            | Q(reason__icontains=q)
        )
    leave_type_filter = request.GET.get("leave_type", "")
    if leave_type_filter.isdigit():
        qs = qs.filter(leave_type_id=int(leave_type_filter))
    else:
        leave_type_filter = ""

    today = date.today()
    starts_filter = request.GET.get("starts", "")
    if starts_filter == "started":
        qs = qs.filter(start_date__lte=today)
    elif starts_filter == "week":
        qs = qs.filter(start_date__gte=today, start_date__lte=today + timedelta(days=7))
    elif starts_filter == "month":
        qs = qs.filter(start_date__gte=today, start_date__lte=today + timedelta(days=30))
    elif starts_filter == "later":
        qs = qs.filter(start_date__gt=today + timedelta(days=30))
    else:
        starts_filter = ""

    qs = qs.distinct()
    is_queue = view != "tracker"
    urgent_count = qs.filter(start_date__lte=today + timedelta(days=3)).count() if is_queue else 0
    filtered_days = qs.aggregate(d=Sum("number_of_days"))["d"] or 0 if is_queue else 0

    sort_map = {
        "number": ["application_number"],
        "employee": ["employee__first_name", "employee__last_name"],
        "type": ["leave_type__name"],
        "dates": ["start_date"],
        "days": ["number_of_days"],
        "submitted": ["submitted_at"],
        "status": ["status"],
    }
    qs = qs.select_related("employee", "employee__profile", "branch", "leave_type") \
           .prefetch_related("approval_history__approver")
    dt = _datatable_page(request, qs, sort_map,
                         default_sort="dates" if is_queue else "submitted",
                         default_dir="asc" if is_queue else "desc")

    prev_stage = next((p for k, *_rest, p in LEAVE_STAGES if k == view), None)
    now = timezone.now()
    rows = list(dt["page_obj"].object_list)
    for a in rows:
        history = list(a.approval_history.all())
        a.last_step = history[-1] if history else None
        mine = [h for h in history if h.approver_id == user.id]
        a.my_step = mine[-1] if mine else None
        prev = [h for h in history if h.stage == prev_stage] if prev_stage else []
        a.prev_step = prev[-1] if prev else None
        a.waiting_days = (now - a.submitted_at).days if a.submitted_at else 0
        a.starts_in = (a.start_date - today).days
        a.started_days_ago = max(0, -a.starts_in)
        a.can_partial = not a.is_half_day and a.start_date != a.end_date
        stage_key = {"lead": "LEAD_MANAGER", "hr": "HR_COORDINATOR", "director": "DIRECTOR"}.get(view)
        a.return_targets_attr = ",".join(
            f"{t}:{STAGE_LABELS[t]}" for t in a.return_targets(stage_key)) if stage_key else ""

    tab_params = request.GET.copy()
    for key in ("view", "track", "page", "sort", "dir"):
        tab_params.pop(key, None)
    current_tab = next((t for t in tabs if t["key"] == view), None)

    context = {
        "active_nav": "leave_approvals",
        "view": view,
        "is_queue": is_queue,
        "tabs": tabs,
        "current_tab": current_tab,
        "tracker_count": tracked_base.count(),
        "tab_query_string": tab_params.urlencode(),
        "rows": rows,
        "track": track,
        "track_filters": track_filters,
        "q": q,
        "leave_type_filter": leave_type_filter,
        "starts_filter": starts_filter,
        "is_filtered": bool(q or leave_type_filter or starts_filter),
        "leave_types": LeaveType.objects.filter(is_active=True),
        "urgent_count": urgent_count,
        "filtered_days": filtered_days,
        **dt,
    }
    return render(request, "portal/leave_approvals.html", context)


@login_required
def leave_ledger_view(request):
    user = request.user
    target_emp = user

    emp_id = request.GET.get("emp_id")
    if emp_id and user.role != RoleChoices.EMPLOYEE:
        candidate = get_object_or_404(User, id=emp_id)
        is_direct_report = getattr(getattr(candidate, "profile", None), "reporting_lead_id", None) == user.id
        if candidate == user or is_direct_report or user.is_superuser or (
            user.role != RoleChoices.LEAD_MANAGER and candidate.primary_branch and user.can_access_branch(candidate.primary_branch)
        ):
            target_emp = candidate
        else:
            messages.error(request, "You do not have access to this employee's leave records.")

    if request.method == "POST" and (user.is_hr_coordinator or user.is_superuser):
        leave_type_id = request.POST.get("leave_type_id")
        days = request.POST.get("days")
        remarks = request.POST.get("remarks", "")
        lt = get_object_or_404(LeaveType, id=leave_type_id)

        try:
            entry = record_manual_leave_adjustment(
                employee=target_emp,
                leave_type=lt,
                days=days,
                remarks=remarks,
                hr_user=user
            )
            messages.success(request, f"Balance adjusted for {target_emp.employee_id}. New balance: {entry.balance_after} days.")
        except ValidationError as e:
            messages.error(request, str(e))

        return redirect(f"{request.path}?emp_id={target_emp.id}")

    balances = get_employee_leave_balances(target_emp)
    entries = LeaveLedgerEntry.objects.filter(employee=target_emp).select_related("leave_type", "created_by").order_by("-created_at")
    leave_types = LeaveType.objects.filter(is_active=True)

    if user.role == RoleChoices.EMPLOYEE:
        accessible_employees = []
    elif user.role == RoleChoices.LEAD_MANAGER and not user.is_superuser:
        accessible_employees = User.objects.filter(Q(pk=user.pk) | Q(profile__reporting_lead=user), is_active=True)
    else:
        accessible_employees = User.objects.filter(is_active=True, primary_branch__in=user.get_accessible_branches())

    context = {
        "active_nav": "leave_ledger",
        "target_emp": target_emp,
        "balances": balances,
        "entries": entries,
        "leave_types": leave_types,
        "accessible_employees": accessible_employees,
    }
    return render(request, "portal/leave_ledger.html", context)


def _can_manage_branches(user):
    """Return True when the user is authorized to create, edit, or deactivate branches."""
    return (
        user.is_superuser
        or user.role == RoleChoices.DIRECTOR
        or (
            user.is_global_scope
            and user.role in [RoleChoices.ADMIN_MANAGER, RoleChoices.HR_COORDINATOR]
        )
    )


def _branch_dependencies(branch):
    """Return human-readable dependency labels preventing hard deletion of a branch."""
    deps = []
    emp_count = branch.primary_employees.count()
    claim_count = branch.expense_claims.count()
    leave_count = branch.leave_applications.count()
    cal_count = branch.holiday_calendars.count()
    if emp_count:
        deps.append(f"{emp_count} assigned employee(s)")
    if claim_count:
        deps.append(f"{claim_count} expense claim(s)")
    if leave_count:
        deps.append(f"{leave_count} leave request(s)")
    if cal_count:
        deps.append(f"{cal_count} holiday calendar(s)")
    return deps


def _extract_branch_fields(post_data):
    """Normalize branch form fields from a POST payload."""
    return {
        "code": post_data.get("code", "").strip().upper(),
        "name": post_data.get("name", "").strip(),
        "city": post_data.get("city", "").strip(),
        "country": post_data.get("country", "").strip(),
        "phone": post_data.get("phone", "").strip(),
        "address": post_data.get("address", "").strip(),
        "currency_code": normalize_code(post_data.get("currency_code")) or default_currency_for_country(
            post_data.get("country", ""), city=post_data.get("city", "")),
        "allowed_currencies": ",".join(parse_code_list(post_data.getlist("allowed_currencies"))),
        "is_active": post_data.get("is_active") == "1",
    }


def _handle_branch_action(request, user, redirect_response, target_branch=None):
    """Execute create, update, toggle_active, or delete actions for branches."""
    if not _can_manage_branches(user):
        messages.error(request, "You do not have permission to manage branches.")
        return redirect("portal_branches")

    action = request.POST.get("action")

    if action and action.startswith("dept_"):
        return _handle_department_action(request, user, action, redirect_response)

    if action == "create":
        fields = _extract_branch_fields(request.POST)
        if not fields["code"] or not fields["name"]:
            messages.error(request, "Branch code and name are required.")
            return redirect_response
        if Branch.objects.filter(code=fields["code"]).exists():
            messages.error(request, f"A branch with code '{fields['code']}' already exists.")
            return redirect_response
        org = get_default_organization(user)
        branch = Branch.objects.create(organization=org, **fields)
        messages.success(request, f"Branch '{branch.name}' ({branch.code}) created.")
        if request.POST.get("open_after_create") == "1":
            return redirect("portal_branch_detail", pk=branch.id)
        return redirect_response

    branch = target_branch or get_object_or_404(Branch, pk=request.POST.get("branch_id"))

    if action == "update":
        fields = _extract_branch_fields(request.POST)
        if not fields["code"] or not fields["name"]:
            messages.error(request, "Branch code and name are required.")
            return redirect_response
        if Branch.objects.filter(code=fields["code"]).exclude(pk=branch.pk).exists():
            messages.error(request, f"Another branch already uses code '{fields['code']}'.")
            return redirect_response
        old_currency = branch.currency_code
        for key, value in fields.items():
            setattr(branch, key, value)
        branch.save()
        messages.success(request, f"Branch '{branch.name}' updated.")
        if branch.currency_code != old_currency:
            moved, skipped = retag_claims_to_branch_currency(branch)
            note = f"Reimbursement currency changed {old_currency} → {branch.currency_code}."
            if moved:
                note += f" {len(moved)} open claim(s) now in {branch.currency_code}."
            if skipped:
                note += f" {len(skipped)} claim(s) with foreign-currency bills kept their currency: {', '.join(skipped[:5])}."
            note += " Approved and paid claims are unchanged."
            messages.info(request, note)
        return redirect_response

    if action == "toggle_active":
        branch.is_active = not branch.is_active
        branch.save(update_fields=["is_active", "updated_at"])
        state_label = "active" if branch.is_active else "inactive"
        messages.success(request, f"Branch '{branch.name}' is now {state_label}.")
        return redirect_response

    if action == "delete":
        deps = _branch_dependencies(branch)
        if deps:
            messages.error(
                request,
                f"Cannot delete '{branch.name}' because it has {', '.join(deps)}. "
                "Deactivate it instead so historical records stay intact.",
            )
            return redirect_response
        branch_label = f"{branch.name} ({branch.code})"
        try:
            branch.delete()
            messages.info(request, f"Branch '{branch_label}' permanently deleted.")
            return redirect("portal_branches")
        except ProtectedError:
            messages.error(
                request,
                f"Cannot delete '{branch.name}' because it is referenced by existing records. Deactivate it instead.",
            )
            return redirect_response

    return redirect_response


def _handle_department_action(request, user, action, redirect_response):
    """Create, update, toggle, or delete organization departments (managed from the branches page)."""
    if action == "dept_create":
        code = (request.POST.get("code") or "").strip().upper()
        name = (request.POST.get("name") or "").strip()
        if not code or not name:
            messages.error(request, "Department code and name are required.")
            return redirect_response
        if Department.objects.filter(code__iexact=code).exists():
            messages.error(request, f"A department with code '{code}' already exists.")
            return redirect_response
        dept = Department.objects.create(
            organization=get_default_organization(user),
            code=code,
            name=name,
            is_active=request.POST.get("is_active") == "1",
        )
        messages.success(request, f"Department '{dept.name}' ({dept.code}) created.")
        return redirect_response

    dept = get_object_or_404(Department, pk=request.POST.get("department_id"))

    if action == "dept_update":
        code = (request.POST.get("code") or "").strip().upper()
        name = (request.POST.get("name") or "").strip()
        if not code or not name:
            messages.error(request, "Department code and name are required.")
            return redirect_response
        if Department.objects.filter(code__iexact=code).exclude(pk=dept.pk).exists():
            messages.error(request, f"Another department already uses code '{code}'.")
            return redirect_response
        dept.code = code
        dept.name = name
        dept.is_active = request.POST.get("is_active") == "1"
        dept.save()
        messages.success(request, f"Department '{dept.name}' updated.")
        return redirect_response

    if action == "dept_toggle_active":
        dept.is_active = not dept.is_active
        dept.save(update_fields=["is_active"])
        state_label = "active" if dept.is_active else "inactive"
        messages.success(request, f"Department '{dept.name}' is now {state_label}.")
        return redirect_response

    if action == "dept_delete":
        if dept.employees.exists():
            messages.error(
                request,
                f"Cannot delete '{dept.name}' because employees are assigned to it. Deactivate it instead.",
            )
            return redirect_response
        label = f"{dept.name} ({dept.code})"
        dept.delete()
        messages.info(request, f"Department '{label}' permanently deleted.")
        return redirect_response

    return redirect_response


@login_required
def branches_view(request):
    user = request.user
    can_manage = _can_manage_branches(user)

    if request.method == "POST":
        return _handle_branch_action(request, user, redirect("portal_branches"))

    base_qs = Branch.objects.all() if can_manage else user.get_accessible_branches()
    branches = list(
        base_qs.annotate(
            employee_count=Count("primary_employees", filter=Q(primary_employees__is_active=True), distinct=True),
            total_employee_count=Count("primary_employees", distinct=True),
            claims_count=Count("expense_claims", distinct=True),
            leaves_count=Count("leave_applications", distinct=True),
            calendars_count=Count("holiday_calendars", distinct=True),
        ).order_by("-is_active", "code")
    )
    for b in branches:
        b.can_delete = (
            b.total_employee_count == 0
            and b.claims_count == 0
            and b.leaves_count == 0
            and b.calendars_count == 0
        )

    active_branches_count = sum(1 for b in branches if b.is_active)
    inactive_branches_count = len(branches) - active_branches_count
    total_staff_count = sum(b.employee_count for b in branches)

    context = {
        "active_nav": "branches",
        "branches": branches,
        "can_manage_branches": can_manage,
        "active_branches_count": active_branches_count,
        "inactive_branches_count": inactive_branches_count,
        "total_staff_count": total_staff_count,
        "all_currencies": all_currencies_json(),
        "departments": Department.objects.annotate(
            employee_count=Count("employees", filter=Q(employees__user__is_active=True), distinct=True),
            total_employee_count=Count("employees", distinct=True),
        ).order_by("-is_active", "name"),
    }
    return render(request, "portal/branches.html", context)


@login_required
def branch_detail_view(request, pk):
    user = request.user
    branch = get_object_or_404(Branch, pk=pk)
    can_manage = _can_manage_branches(user)

    if not can_manage and not user.can_access_branch(branch):
        messages.error(request, "You do not have permission to view that branch.")
        return redirect("portal_branches")

    if request.method == "POST":
        return _handle_branch_action(
            request,
            user,
            redirect("portal_branch_detail", pk=branch.pk),
            target_branch=branch,
        )

    employees = list(
        User.objects.filter(primary_branch=branch)
        .select_related("profile__department", "profile__reporting_lead")
        .order_by("-is_active", "first_name", "employee_id")
    )
    active_employees_count = sum(1 for e in employees if e.is_active)
    scoped_users = list(
        UserBranchScope.objects.filter(branch=branch, is_active=True)
        .select_related("user")
        .order_by("user__first_name", "user__employee_id")
    )

    claims_qs = ExpenseClaim.objects.filter(branch=branch).select_related("employee", "category")
    claims_count = claims_qs.count()
    pending_claims_count = claims_qs.filter(status__in=PENDING_EXPENSE_STATUSES).count()
    recent_claims = list(claims_qs.order_by("-created_at")[:8])

    leaves_qs = LeaveApplication.objects.filter(branch=branch).select_related("employee", "leave_type")
    leaves_count = leaves_qs.count()
    pending_leaves_count = leaves_qs.filter(status__in=PENDING_LEAVE_STATUSES).count()
    recent_leaves = list(leaves_qs.order_by("-submitted_at")[:8])

    current_year = date.today().year
    holidays = list(
        Holiday.objects.filter(calendar__branch=branch, calendar__year=current_year)
        .select_related("calendar")
        .order_by("date")
    )
    deps = _branch_dependencies(branch)

    context = {
        "active_nav": "branches",
        "branch": branch,
        "can_manage_branches": can_manage,
        "can_manage_employees": can_manage_users(user),
        "employees": employees,
        "active_employees_count": active_employees_count,
        "scoped_users": scoped_users,
        "claims_count": claims_count,
        "pending_claims_count": pending_claims_count,
        "recent_claims": recent_claims,
        "leaves_count": leaves_count,
        "pending_leaves_count": pending_leaves_count,
        "recent_leaves": recent_leaves,
        "holidays": holidays,
        "current_year": current_year,
        "can_delete": len(deps) == 0,
        "delete_block_reasons": ", ".join(deps),
        "all_currencies": all_currencies_json(),
        "branch_currency": currency_json(branch.currency_code),
    }
    return render(request, "portal/branch_detail.html", context)


@login_required
def employees_view(request):
    user = request.user
    accessible_branches = user.get_accessible_branches()
    selected_branch = request.session.get("selected_branch_id", "all")

    can_manage = can_manage_users(user)

    if request.method == "POST":
        back = redirect(f"{request.path}?{request.GET.urlencode()}" if request.GET else "portal_employees")
        action = request.POST.get("action")
        try:
            if action == "reset_password":
                target = get_object_or_404(User, pk=request.POST.get("employee_pk"))
                set_employee_password(user, target, request.POST.get("new_password", ""))
                messages.success(request, f"New password set for {target.full_name} ({target.employee_id}). Share it securely with the employee.")
                return back
            if action == "bulk":
                bulk_action = request.POST.get("bulk_action", "")
                result = bulk_employee_action(user, request.POST.getlist("selected"), bulk_action)
                verb = {"delete": "deleted", "activate": "activated", "deactivate": "deactivated"}[bulk_action]
                if result["done"]:
                    messages.success(request, f"{len(result['done'])} employee(s) {verb}.")
                if result["archived"]:
                    messages.warning(
                        request,
                        f"{len(result['archived'])} employee(s) have leave or expense history and were deactivated "
                        f"instead of deleted to preserve audit records: {', '.join(result['archived'][:5])}"
                        + ("…" if len(result["archived"]) > 5 else ""),
                    )
                if result["skipped"]:
                    details = ", ".join(f"{e.full_name} ({why})" for e, why in result["skipped"][:5])
                    messages.error(request, f"{len(result['skipped'])} employee(s) skipped: {details}")
                return back
            # Add Employee modal submission
            new_user, generated_password = create_employee(user, request.POST)
        except EmployeeError as e:
            messages.error(request, str(e))
            return back
        messages.success(request, f"Employee {new_user.full_name} ({new_user.employee_id}) added successfully to the roster.")
        if generated_password:
            messages.info(request, f"Temporary password for {new_user.employee_id}: {generated_password}  — share it securely; the employee can change it from their profile.")
        return redirect("portal_employees")

    # ---- Server-side datatable: search, filters, sorting, pagination ----
    q = request.GET.get("q", "").strip()
    branch_filter = request.GET.get("branch", "").strip()
    dept_filter = request.GET.get("dept", "").strip()
    role_filter = request.GET.get("role", "").strip()
    status_filter = request.GET.get("status", "").strip()

    qs = User.objects.filter(primary_branch__in=accessible_branches).select_related(
        "profile__department", "profile__reporting_lead", "primary_branch"
    )

    if selected_branch and selected_branch != "all":
        qs = qs.filter(primary_branch_id=selected_branch)

    if branch_filter.isdigit():
        qs = qs.filter(primary_branch_id=int(branch_filter))
    else:
        branch_filter = ""

    if dept_filter.isdigit():
        qs = qs.filter(profile__department_id=int(dept_filter))
    else:
        dept_filter = ""

    if role_filter in RoleChoices.values:
        qs = qs.filter(role=role_filter)
    else:
        role_filter = ""

    if status_filter == "active":
        qs = qs.filter(is_active=True)
    elif status_filter == "inactive":
        qs = qs.filter(is_active=False)
    else:
        status_filter = ""

    if q:
        qs = qs.filter(
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q) |
            Q(employee_id__icontains=q) |
            Q(email__icontains=q) |
            Q(profile__designation__icontains=q) |
            Q(profile__department__name__icontains=q) |
            Q(primary_branch__name__icontains=q) |
            Q(primary_branch__city__icontains=q)
        )

    sort_map = {
        "employee": ["first_name", "last_name"],
        "department": ["profile__department__name"],
        "role": ["profile__designation", "role"],
        "lead": ["profile__reporting_lead__first_name"],
        "location": ["primary_branch__name"],
        "status": ["is_active"],
        "id": ["employee_id"],
    }
    sort = request.GET.get("sort", "employee")
    direction = request.GET.get("dir", "asc")
    if sort not in sort_map:
        sort = "employee"
    if direction not in ("asc", "desc"):
        direction = "asc"
    prefix = "-" if direction == "desc" else ""
    qs = qs.order_by(*[prefix + f for f in sort_map[sort]], "employee_id")

    page_size_options = [10, 25, 50, 100]
    try:
        page_size = int(request.GET.get("page_size", 10))
    except (TypeError, ValueError):
        page_size = 10
    if page_size not in page_size_options:
        page_size = 10

    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(request.GET.get("page"))
    page_range = paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1)

    base_params = request.GET.copy()
    base_params.pop("page", None)
    sort_params = base_params.copy()
    for key in ("sort", "dir"):
        sort_params.pop(key, None)

    all_departments = Department.objects.filter(is_active=True)
    all_leads = User.objects.filter(
        primary_branch__in=accessible_branches,
        role__in=[RoleChoices.LEAD_MANAGER, RoleChoices.ADMIN_MANAGER, RoleChoices.DIRECTOR]
    ).distinct()

    context = {
        "active_nav": "employees",
        "employees": page_obj.object_list,
        "page_obj": page_obj,
        "paginator": paginator,
        "page_range": page_range,
        "page_size": page_size,
        "page_size_options": page_size_options,
        "sort": sort,
        "dir": direction,
        "query_string": base_params.urlencode(),
        "sort_query_string": sort_params.urlencode(),
        "total_count": paginator.count,
        "q": q,
        "branch_filter": branch_filter,
        "dept_filter": dept_filter,
        "role_filter": role_filter,
        "status_filter": status_filter,
        "is_filtered": bool(q or branch_filter or dept_filter or role_filter or status_filter),
        "all_branches": accessible_branches,
        "all_departments": all_departments,
        "all_roles": RoleChoices.choices,
        "all_leads": all_leads,
        "can_manage_employees": can_manage,
        "can_reset_passwords": can_reset_passwords(user),
        "today": date.today(),
    }
    return render(request, "portal/employees.html", context)


@login_required
def employee_overview_view(request, pk):
    """Slide-in panel fragment: an employee's overall leave / expense status with KPIs."""
    user = request.user
    emp = get_object_or_404(
        User.objects.select_related("profile__department", "profile__reporting_lead", "primary_branch"),
        pk=pk,
    )
    if not (user.is_superuser or emp == user or (emp.primary_branch and user.can_access_branch(emp.primary_branch))):
        return render(request, "portal/_employee_overview.html", {"forbidden": True}, status=403)

    today = date.today()
    year_start = date(today.year, 1, 1)

    # ---- Leaves ----
    balances = get_employee_leave_balances(emp)
    leaves = LeaveApplication.objects.filter(employee=emp)
    leaves_year = leaves.filter(start_date__gte=year_start)
    taken_year = leaves_year.filter(status=LeaveApplicationStatus.APPROVED).aggregate(d=Sum("number_of_days"))["d"] or Decimal("0")
    pending_leave_qs = leaves.filter(status__in=PENDING_LEAVE_STATUSES)
    pending_leave_days = pending_leave_qs.aggregate(d=Sum("number_of_days"))["d"] or Decimal("0")
    paid_available = sum((b["available_balance"] for b in balances if b["leave_type"].is_paid), Decimal("0"))
    on_leave_now = leaves.filter(
        status=LeaveApplicationStatus.APPROVED, start_date__lte=today, end_date__gte=today
    ).select_related("leave_type").first()
    next_leave = leaves.filter(
        status=LeaveApplicationStatus.APPROVED, start_date__gt=today
    ).select_related("leave_type").order_by("start_date").first()
    max_scale = max([b["earned_days"] for b in balances] + [b["taken_days"] + b["available_balance"] for b in balances] + [Decimal("1")])
    for b in balances:
        b["taken_pct"] = float(b["taken_days"] / max_scale * 100) if max_scale else 0
        b["pending_pct"] = float(b["pending_days"] / max_scale * 100) if max_scale else 0
        b["available_pct"] = float(max(b["available_balance"] - b["pending_days"], Decimal("0")) / max_scale * 100) if max_scale else 0

    # ---- Expenses ----
    claims = ExpenseClaim.objects.filter(employee=emp).exclude(status=ExpenseClaimStatus.DRAFT)
    claims_year = claims.filter(expense_date__gte=year_start)
    claimed_year = sum((c.claimed_amount for c in claims_year.exclude(status=ExpenseClaimStatus.WITHDRAWN).only("amount", "requested_amount")), Decimal("0"))
    reimbursed_year = claims_year.filter(status=ExpenseClaimStatus.PAID).aggregate(a=Sum("amount"))["a"] or Decimal("0")
    pending_claims = claims.filter(status__in=PENDING_EXPENSE_STATUSES)
    pending_claims_amount = pending_claims.aggregate(a=Sum("amount"))["a"] or Decimal("0")
    expense_groups = [
        ("Under review", "badge-pending", [ExpenseClaimStatus.UNDER_ADMIN_REVIEW, ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW]),
        ("Awaiting payment", "badge-approved", [ExpenseClaimStatus.APPROVED_BY_DIRECTOR]),
        ("Paid", "badge-paid", [ExpenseClaimStatus.PAID]),
        ("Rejected", "badge-rejected", [ExpenseClaimStatus.REJECTED_BY_ADMIN, ExpenseClaimStatus.REJECTED_BY_DIRECTOR]),
        ("Withdrawn", "badge-draft", [ExpenseClaimStatus.WITHDRAWN]),
    ]
    expense_status_rows = []
    for label, css, statuses in expense_groups:
        agg = claims.filter(status__in=statuses).aggregate(n=Count("id"), a=Sum("amount"))
        if agg["n"]:
            expense_status_rows.append({"label": label, "css": css, "count": agg["n"], "amount": agg["a"] or Decimal("0")})

    profile = getattr(emp, "profile", None)
    tenure = None
    if profile and profile.date_of_joining:
        months = (today.year - profile.date_of_joining.year) * 12 + today.month - profile.date_of_joining.month
        if today.day < profile.date_of_joining.day:
            months -= 1
        months = max(months, 0)
        years, rem = divmod(months, 12)
        tenure = (f"{years}y " if years else "") + f"{rem}m"

    context = {
        "emp": emp,
        "profile": profile,
        "tenure": tenure,
        "today": today,
        "balances": balances,
        "paid_available": paid_available,
        "taken_year": taken_year,
        "pending_leave_count": pending_leave_qs.count(),
        "pending_leave_days": pending_leave_days,
        "on_leave_now": on_leave_now,
        "next_leave": next_leave,
        "recent_leaves": leaves.select_related("leave_type").order_by("-start_date")[:5],
        "claimed_year": claimed_year,
        "claims_year_count": claims_year.count(),
        "reimbursed_year": reimbursed_year,
        "pending_claims_count": pending_claims.count(),
        "pending_claims_amount": pending_claims_amount,
        "expense_status_rows": expense_status_rows,
        "recent_claims": claims.select_related("category").order_by("-expense_date", "-id")[:5],
        "can_manage_employees": can_manage_users(user),
    }
    return render(request, "portal/_employee_overview.html", context)



@login_required
def reports_view(request):
    from reports.access import allowed_reports, effective_role
    user = request.user
    allowed = allowed_reports(user)
    if not allowed:
        messages.error(request, "Reports & Analytics are available to managers, HR and the Director only.")
        return redirect("portal_dashboard")
    role = effective_role(user)
    accessible_branches = user.get_accessible_branches()
    scope_note = {
        RoleChoices.LEAD_MANAGER: "Leave reports for your direct reports.",
        RoleChoices.ADMIN_MANAGER: "Expense claim and reimbursement reports for the branches you manage.",
        RoleChoices.HR_COORDINATOR: "All reports for the branches you cover.",
        RoleChoices.DIRECTOR: "All reports, organisation-wide.",
    }.get(role, "")
    context = {
        "active_nav": "reports",
        "branches": accessible_branches,
        "departments": Department.objects.filter(is_active=True),
        "categories": ExpenseCategory.objects.filter(is_active=True),
        "leave_types": LeaveType.objects.filter(is_active=True),
        "expense_statuses": ExpenseClaimStatus.choices,
        "leave_statuses": LeaveApplicationStatus.choices,
        "today": date.today(),
        "year_start": date(date.today().year, 1, 1),
        "allowed_reports": allowed,
        "show_branch_filter": role != RoleChoices.LEAD_MANAGER and accessible_branches.count() > 1,
        "scope_note": scope_note,
    }
    return render(request, "portal/reports.html", context)


@login_required
def notifications_view(request):
    user = request.user
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "mark_all_read":
            NotificationEvent.objects.filter(recipient=user, is_read=False).update(is_read=True)
            messages.success(request, "All notifications marked as read.")
        elif action == "mark_read":
            n_id = request.POST.get("notification_id")
            NotificationEvent.objects.filter(id=n_id, recipient=user).update(is_read=True)
        # Dashboard alerts send the user on to the item (or back to the dashboard).
        next_url = request.POST.get("next", "")
        if next_url.startswith("/") and not next_url.startswith("//") and url_has_allowed_host_and_scheme(
            next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            return redirect(next_url)
        return redirect("portal_notifications")

    notifications = NotificationEvent.objects.filter(recipient=user).order_by("is_read", "-created_at")[:100]
    context = {
        "active_nav": "notifications",
        "notifications": notifications,
    }
    return render(request, "portal/notifications.html", context)


@login_required
def profile_view(request):
    user = request.user
    profile = getattr(user, "profile", None)

    if request.method == "POST":
        phone = request.POST.get("phone", "")
        emergency = request.POST.get("emergency_contact", "")
        address = request.POST.get("address", "")
        bank_name = request.POST.get("bank_name", "")
        bank_acc = request.POST.get("bank_account_no", "")
        bank_swift = request.POST.get("bank_ifsc_or_swift", "")

        user.phone = phone
        user.save()

        if profile:
            profile.emergency_contact = emergency
            profile.address = address
            if user.can_edit_bank_details:
                profile.bank_name = bank_name
                profile.bank_account_no = bank_acc
                profile.bank_ifsc_or_swift = bank_swift
            profile.save()

        messages.success(request, "Profile updated successfully.")
        return redirect("portal_profile")

    context = {
        "active_nav": "profile",
        "profile": profile,
        "can_edit_bank": user.can_edit_bank_details,
    }
    return render(request, "portal/profile.html", context)
