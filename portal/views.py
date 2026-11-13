from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum, Count, Q, ProtectedError
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.utils.http import url_has_allowed_host_and_scheme
from datetime import datetime, date, timedelta
from decimal import Decimal

from accounts.models import User, RoleChoices, EmployeeProfile, UserBranchScope
from organization.models import Branch, Department
from expenses.models import (
    ExpenseClaim, ExpenseCategory, ExpenseAttachment,
    ExpenseClaimStatus, ReimbursementPayment, review_message
)
from leaves.models import (
    LeaveApplication, LeaveType, LeavePolicy, LeaveLedgerEntry,
    LeaveApplicationStatus, HolidayCalendar, Holiday
)
from leaves.models import LeaveAuditAction, fmt_days
from leaves.services import (
    get_employee_leave_balances, record_manual_leave_adjustment,
    validate_leave_request, LeaveRequestError,
    log_leave_audit, client_ip, resubmit_leave_application,
    initial_leave_route, visible_leave_applications, can_view_leave_application,
    filter_leave_track, leave_track_counts, leave_review_options,
)
from portal.admin_views import validate_upload, can_manage_users, get_default_organization
from accounts.services import EmployeeError, can_reset_passwords, create_employee, set_employee_password
import secrets
from notifications.models import NotificationEvent
from notifications.services import notify_expense_event, notify_leave_event
from portal.dashboard_services import (
    PENDING_EXPENSE_STATUSES,
    PENDING_LEAVE_STATUSES,
    build_all_staff_analytics,
    build_approver_reminders,
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
    """Remarks and the optional partial-approval range posted from a web review form."""
    partial = request.POST.get("partial") == "1"
    return {
        "remarks": request.POST.get("remarks", ""),
        "approved_start_date": _parse_post_date(request.POST.get("approved_start_date"), "approved start date") if partial else None,
        "approved_end_date": _parse_post_date(request.POST.get("approved_end_date"), "approved end date") if partial else None,
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
        decision = request.POST.get("action", "").upper()
        try:
            kwargs = _leave_review_kwargs(request)
            recorded = app.director_review(approver=user, action=decision, **kwargs)
            event_type = "LEAVE_DIRECTOR_REJECTED" if recorded == "REJECTED" else "LEAVE_DIRECTOR_APPROVED"
            notify_leave_event(app, event_type, remarks=kwargs["remarks"], partial=recorded.startswith("PARTIALLY_"))
            messages.success(request, _leave_review_message(app, recorded, "Director decision"))
        except ValidationError as e:
            messages.error(request, str(e))
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
        # Create new expense claim
        category_id = request.POST.get("category")
        amount = request.POST.get("amount")
        expense_date = request.POST.get("expense_date")
        description = request.POST.get("description", "")
        project_ref = request.POST.get("project_reference", "")
        submit_now = request.POST.get("submit_now") == "1"

        if not category_id or not amount or not expense_date:
            messages.error(request, "Category, amount, and expense date are required.")
            return redirect("portal_expenses")

        category = get_object_or_404(ExpenseCategory, id=category_id, is_active=True)
        try:
            amount_dec = Decimal(str(amount))
        except Exception:
            amount_dec = Decimal("0")
        if amount_dec <= 0:
            messages.error(request, "Amount must be greater than zero.")
            return redirect("portal_expenses")
        if expense_date > date.today().isoformat():
            messages.error(request, "Expense date cannot be in the future.")
            return redirect("portal_expenses")
        files = request.FILES.getlist("receipts")
        for f in files:
            err = validate_upload(f)
            if err:
                messages.error(request, err)
                return redirect("portal_expenses")
        if not (user.primary_branch or accessible_branches.exists()):
            messages.error(request, "Your account has no branch assigned. Contact the Admin Manager.")
            return redirect("portal_expenses")

        claim = ExpenseClaim.objects.create(
            employee=user,
            branch=user.primary_branch or accessible_branches.first(),
            category=category,
            amount=amount_dec,
            expense_date=expense_date,
            description=description,
            project_reference=project_ref,
            status=ExpenseClaimStatus.DRAFT
        )

        for f in files:
            ExpenseAttachment.objects.create(
                claim=claim,
                file=f,
                original_filename=f.name,
                file_type=f.content_type,
                file_size=f.size
            )

        if submit_now:
            try:
                claim.submit()
                notify_expense_event(claim, "EXPENSE_SUBMITTED")
                messages.success(request, f"Claim {claim.claim_number} created and submitted "
                                          + ("directly to the Director for approval." if claim.status == ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW else "for review!"))
            except ValidationError as e:
                messages.warning(request, f"Claim created as draft: {e}")
        else:
            messages.success(request, f"Claim {claim.claim_number} saved as draft.")

        return redirect("portal_expenses")

    # GET
    if user.role == RoleChoices.EMPLOYEE:
        qs = ExpenseClaim.objects.filter(employee=user)
    else:
        if selected_branch and selected_branch != "all":
            qs = ExpenseClaim.objects.filter(branch_id=selected_branch)
        else:
            qs = ExpenseClaim.objects.filter(branch__in=accessible_branches)

    status_filter = request.GET.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)

    search_q = request.GET.get("q", "").strip()
    if search_q:
        qs = qs.filter(
            Q(claim_number__icontains=search_q)
            | Q(employee__employee_id__iexact=search_q)
            | Q(employee__first_name__icontains=search_q)
            | Q(employee__last_name__icontains=search_q)
            | Q(description__icontains=search_q)
        )

    claims = qs.select_related("category", "branch", "employee").prefetch_related("attachments").order_by("-created_at")
    categories = ExpenseCategory.objects.filter(is_active=True)

    context = {
        "active_nav": "expenses",
        "claims": claims,
        "categories": categories,
        "selected_status": status_filter,
        "search_q": search_q,
        "today": date.today(),
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
        "approval_history": claim.approval_history.all().order_by("created_at"),
        "can_edit": claim.employee == user and claim.status in [
            ExpenseClaimStatus.DRAFT, ExpenseClaimStatus.UNDER_ADMIN_REVIEW,
            ExpenseClaimStatus.REJECTED_BY_ADMIN, ExpenseClaimStatus.REJECTED_BY_DIRECTOR,
        ],
        "can_withdraw": claim.employee == user and claim.can_withdraw,
        "review_stage": _claim_review_stage(user, claim),
    }
    return render(request, "portal/expense_detail.html", context)


def _review_claim(request, user, claim):
    """
    Apply an Admin Manager / Director decision (full, partial amount or rejection) posted from a web
    review form. Branch-aware: the reviewer must be an expense approver for the claim's branch.
    """
    if not user.can_manage_expenses_for(claim.branch):
        messages.error(request, f"You are not an expense approver for {claim.branch.name}.")
        return
    action = request.POST.get("action", "").upper()
    remarks = request.POST.get("remarks", "")
    approved_amount = request.POST.get("approved_amount") if request.POST.get("partial") == "1" else None

    if claim.status == ExpenseClaimStatus.UNDER_ADMIN_REVIEW and user.is_admin_manager:
        review, stage = claim.admin_review, "ADMIN"
    elif claim.status == ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW and user.is_director:
        review, stage = claim.director_review, "DIRECTOR"
    else:
        messages.info(request, f"Claim {claim.claim_number} is not awaiting your review ({claim.get_status_display()}).")
        return
    try:
        recorded = review(approver=user, action=action, remarks=remarks, approved_amount=approved_amount)
    except ValidationError as e:
        messages.error(request, " ".join(e.messages))
        return
    notify_expense_event(claim, f"EXPENSE_{stage}_{'REJECTED' if recorded == 'REJECTED' else 'APPROVED'}", remarks=remarks)
    messages.success(request, review_message(claim, recorded))


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

    base_qs = ExpenseClaim.objects.filter(branch__in=expense_branches).exclude(employee=user)
    if selected_branch and selected_branch != "all":
        base_qs = base_qs.filter(branch_id=selected_branch)

    if request.method == "POST":
        claim = get_object_or_404(ExpenseClaim, id=request.POST.get("claim_id"))
        _review_claim(request, user, claim)
        return _safe_next(request, "portal_expense_approvals")

    related = ("employee", "branch", "category")
    admin_queue = (base_qs.filter(status=ExpenseClaimStatus.UNDER_ADMIN_REVIEW)
                   .select_related(*related).prefetch_related("attachments").order_by("-submitted_at"))
    director_queue = (base_qs.filter(status=ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW)
                      .select_related(*related).prefetch_related("attachments", "approval_history__approver")
                      .order_by("-submitted_at"))

    context = {
        "active_nav": "expense_approvals",
        "admin_queue": admin_queue,
        "director_queue": director_queue,
        "expense_branches": expense_branches,
        "selected_branch": str(selected_branch),
    }
    return render(request, "portal/expense_approvals.html", context)


@login_required
def reimbursements_view(request):
    user = request.user
    if not (user.is_admin_manager or user.is_director or user.is_superuser):
        messages.error(request, "Access restricted.")
        return redirect("portal_dashboard")

    expense_branches = user.get_expense_branches()
    selected_branch = request.session.get("selected_branch_id", "all")

    base_qs = ExpenseClaim.objects.filter(branch__in=expense_branches)
    if selected_branch and selected_branch != "all":
        base_qs = base_qs.filter(branch_id=selected_branch)

    if request.method == "POST":
        claim_id = request.POST.get("claim_id")
        payment_date = request.POST.get("payment_date") or date.today()
        payment_mode = request.POST.get("payment_mode", "BANK_TRANSFER")
        payment_ref = request.POST.get("payment_reference", "").strip()
        remarks = request.POST.get("remarks", "")

        if not payment_ref:
            messages.error(request, "Payment transaction reference number is mandatory.")
            return redirect("portal_reimbursements")

        if not (user.is_admin_manager or user.is_superuser):
            messages.error(request, "Only the Admin Manager can mark claims as paid.")
            return redirect("portal_reimbursements")
        claim = get_object_or_404(ExpenseClaim, id=claim_id)
        if not user.can_manage_expenses_for(claim.branch):
            messages.error(request, f"You are not an expense approver for {claim.branch.name}.")
            return redirect("portal_reimbursements")
        if claim.employee_id == user.id and not user.is_superuser:
            messages.error(request, "Another Admin Manager must record payment of your own claim.")
            return redirect("portal_reimbursements")
        try:
            claim.record_payment(
                recorded_by=user,
                payment_date=payment_date,
                payment_mode=payment_mode,
                payment_reference=payment_ref,
                remarks=remarks
            )
            notify_expense_event(claim, "EXPENSE_PAID")
            messages.success(request, f"Reimbursement payment recorded for claim {claim.claim_number}!")
        except ValidationError as e:
            messages.error(request, str(e))

        return redirect("portal_reimbursements")

    pending_payments = base_qs.filter(status=ExpenseClaimStatus.APPROVED_BY_DIRECTOR).order_by("-updated_at")
    recent_paid = base_qs.filter(status=ExpenseClaimStatus.PAID).select_related("payment_record").order_by("-updated_at")[:20]

    context = {
        "active_nav": "reimbursements",
        "pending_payments": pending_payments,
        "recent_paid": recent_paid,
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
        return redirect("portal_leave_detail", pk=app.pk)

    context = {
        "active_nav": "leaves",
        "app": app,
        "approval_history": app.approval_history.select_related("approver").order_by("created_at"),
        "audit_trail": _audit_rows(app),
        "can_resubmit": app.can_resubmit(user),
        "review": leave_review_options(user, app),
        "leave_types": LeaveType.objects.filter(is_active=True) if app.can_resubmit(user) else [],
    }
    return render(request, "portal/leave_detail.html", context)


AUDIT_FIELD_LABELS = {
    "leave_type": "Leave type", "start_date": "From", "end_date": "To", "is_half_day": "Half day",
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


@login_required
def leave_approvals_view(request):
    user = request.user
    if user.role == RoleChoices.EMPLOYEE:
        messages.error(request, "Access restricted to approval authorities.")
        return redirect("portal_dashboard")

    accessible_branches = user.get_accessible_branches()
    selected_branch = request.session.get("selected_branch_id", "all")

    if request.method == "POST":
        app_id = request.POST.get("app_id")
        action = request.POST.get("action", "").upper()
        app = get_object_or_404(LeaveApplication, id=app_id)
        next_url = request.POST.get("next", "")
        back = redirect(next_url if next_url.startswith("/leaves/") and "//" not in next_url
                        else "portal_leave_approvals")

        reporting_lead = getattr(getattr(app.employee, "profile", None), "reporting_lead", None)
        if app.employee == user:
            messages.error(request, "You cannot act on your own leave application.")
            return back
        if app.status == LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION:
            if not (reporting_lead == user or user.is_superuser):
                messages.error(request, "Only the employee's reporting Lead / Manager can give the recommendation.")
                return back
        elif not user.can_access_branch(app.branch):
            messages.error(request, "You do not have permission for this branch.")
            return back

        try:
            kwargs = _leave_review_kwargs(request)
            remarks = kwargs["remarks"]
            if app.status == LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION and (reporting_lead == user or user.is_superuser):
                recorded = app.lead_review(approver=user, action=action, **kwargs)
                event_type = "LEAVE_LEAD_REJECTED" if recorded == "REJECTED" else "LEAVE_LEAD_RECOMMENDED"
                notify_leave_event(app, event_type, remarks=remarks, partial=recorded.startswith("PARTIALLY_"))
                messages.success(request, _leave_review_message(app, recorded, "Lead review"))

            elif user.is_hr_coordinator and app.status == LeaveApplicationStatus.PENDING_HR_VERIFICATION:
                recorded = app.hr_review(approver=user, action=action, **kwargs)
                event_type = "LEAVE_HR_REJECTED" if recorded == "REJECTED" else "LEAVE_HR_VERIFIED"
                notify_leave_event(app, event_type, remarks=remarks, partial=recorded.startswith("PARTIALLY_"))
                messages.success(request, _leave_review_message(app, recorded, "HR verification"))

            elif user.is_director and app.status == LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL:
                recorded = app.director_review(approver=user, action=action, **kwargs)
                event_type = "LEAVE_DIRECTOR_REJECTED" if recorded == "REJECTED" else "LEAVE_DIRECTOR_APPROVED"
                notify_leave_event(app, event_type, remarks=remarks, partial=recorded.startswith("PARTIALLY_"))
                messages.success(request, _leave_review_message(app, recorded, "Final decision") + " Official balance updated.")

            else:
                messages.error(request, "This application is not awaiting your action.")

        except ValidationError as e:
            messages.error(request, " ".join(e.messages))

        return back

    base_qs = LeaveApplication.objects.filter(branch__in=accessible_branches)
    if selected_branch and selected_branch != "all":
        base_qs = base_qs.filter(branch_id=selected_branch)

    # Lead Manager queue (direct reports)
    lead_queue = LeaveApplication.objects.filter(
        employee__profile__reporting_lead=user,
        status=LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION
    ).order_by("-submitted_at")

    # HR Coordinator queue
    hr_queue = base_qs.filter(
        status=LeaveApplicationStatus.PENDING_HR_VERIFICATION
    ).order_by("-submitted_at")

    # Director queue
    director_queue = base_qs.filter(
        status=LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL
    ).order_by("-submitted_at")

    # Approval tracker: everyone else's leave this user may follow, location-scoped.
    track = request.GET.get("track", "pending")
    tracked = visible_leave_applications(user, include_own=False)
    if user.role != RoleChoices.LEAD_MANAGER and selected_branch and selected_branch != "all":
        tracked = tracked.filter(branch_id=selected_branch)
    track_counts = leave_track_counts(tracked, user)
    tracked = filter_leave_track(tracked, track, user) \
        .select_related("employee", "branch", "leave_type") \
        .prefetch_related("approval_history__approver") \
        .order_by("-submitted_at")[:200]
    tracked = list(tracked)
    for a in tracked:
        history = list(a.approval_history.all())
        a.last_step = history[-1] if history else None
        mine = [h for h in history if h.approver_id == user.id]
        a.my_step = mine[-1] if mine else None

    context = {
        "active_nav": "leave_approvals",
        "lead_queue": lead_queue,
        "hr_queue": hr_queue,
        "director_queue": director_queue,
        "track": track,
        "track_filters": [
            (key, label, track_counts.get(key, 0)) for key, label in [
                ("pending", "In progress"), ("awaiting_lead", "Awaiting Lead"), ("awaiting_hr", "Awaiting HR"),
                ("awaiting_director", "Awaiting Director"), ("approved", "Approved"), ("rejected", "Rejected"),
                ("acted_by_me", "Acted on by me"), ("all", "All"),
            ]
        ],
        "tracked": tracked,
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
        "is_active": post_data.get("is_active") == "1",
    }


def _handle_branch_action(request, user, redirect_response, target_branch=None):
    """Execute create, update, toggle_active, or delete actions for branches."""
    if not _can_manage_branches(user):
        messages.error(request, "You do not have permission to manage branches.")
        return redirect("portal_branches")

    action = request.POST.get("action")

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
        for key, value in fields.items():
            setattr(branch, key, value)
        branch.save()
        messages.success(request, f"Branch '{branch.name}' updated.")
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
    }
    return render(request, "portal/branch_detail.html", context)


@login_required
def employees_view(request):
    user = request.user
    accessible_branches = user.get_accessible_branches()
    selected_branch = request.session.get("selected_branch_id", "all")

    can_manage = user.is_superuser or user.role in [
        RoleChoices.DIRECTOR, RoleChoices.ADMIN_MANAGER, RoleChoices.HR_COORDINATOR
    ]

    if request.method == "POST":
        back = redirect(f"{request.path}?{request.GET.urlencode()}" if request.GET else "portal_employees")
        try:
            if request.POST.get("action") == "reset_password":
                target = get_object_or_404(User, pk=request.POST.get("employee_pk"))
                set_employee_password(user, target, request.POST.get("new_password", ""))
                messages.success(request, f"New password set for {target.full_name} ({target.employee_id}). Share it securely with the employee.")
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

    # Search and Filter GET parameters
    search_q = request.GET.get("q", "").strip()
    branch_filter = request.GET.get("branch", "").strip()
    dept_filter = request.GET.get("dept", "").strip()
    role_filter = request.GET.get("role", "").strip()
    status_filter = request.GET.get("status", "").strip()

    qs = User.objects.filter(primary_branch__in=accessible_branches).select_related(
        "profile__department", "profile__reporting_lead", "primary_branch"
    )

    if selected_branch and selected_branch != "all":
        qs = qs.filter(primary_branch_id=selected_branch)

    if branch_filter and branch_filter != "all":
        qs = qs.filter(primary_branch_id=branch_filter)

    if dept_filter:
        qs = qs.filter(profile__department_id=dept_filter)

    if role_filter:
        qs = qs.filter(role=role_filter)

    if status_filter == "active":
        qs = qs.filter(is_active=True)
    elif status_filter == "inactive":
        qs = qs.filter(is_active=False)

    if search_q:
        qs = qs.filter(
            Q(first_name__icontains=search_q) |
            Q(last_name__icontains=search_q) |
            Q(employee_id__icontains=search_q) |
            Q(email__icontains=search_q) |
            Q(profile__designation__icontains=search_q) |
            Q(profile__department__name__icontains=search_q) |
            Q(primary_branch__name__icontains=search_q) |
            Q(primary_branch__city__icontains=search_q)
        )

    # Compile dynamic filter pills for active filters
    active_filters = []
    if search_q:
        active_filters.append({"label": f'Search: "{search_q}"', "key": "q"})
    if branch_filter and branch_filter != "all":
        b = accessible_branches.filter(id=branch_filter).first()
        if b:
            active_filters.append({"label": f"Location: {b.city or b.name}", "key": "branch"})
    if dept_filter:
        d = Department.objects.filter(id=dept_filter).first()
        if d:
            active_filters.append({"label": f"Dept: {d.name}", "key": "dept"})
    if role_filter:
        r_display = dict(RoleChoices.choices).get(role_filter, role_filter)
        active_filters.append({"label": f"Role: {r_display}", "key": "role"})
    if status_filter:
        active_filters.append({"label": f"Status: {status_filter.capitalize()}", "key": "status"})

    all_departments = Department.objects.filter(is_active=True)
    all_leads = User.objects.filter(
        primary_branch__in=accessible_branches,
        role__in=[RoleChoices.LEAD_MANAGER, RoleChoices.ADMIN_MANAGER, RoleChoices.DIRECTOR]
    ).distinct()

    total_count = qs.count()

    context = {
        "active_nav": "employees",
        "employees": qs.order_by("employee_id"),
        "total_count": total_count,
        "search_q": search_q,
        "branch_filter": branch_filter,
        "dept_filter": dept_filter,
        "role_filter": role_filter,
        "status_filter": status_filter,
        "active_filters": active_filters,
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
def reports_view(request):
    user = request.user
    accessible_branches = user.get_accessible_branches()
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
        "is_reviewer": user.role != RoleChoices.EMPLOYEE,
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
            profile.bank_name = bank_name
            profile.bank_account_no = bank_acc
            profile.bank_ifsc_or_swift = bank_swift
            profile.save()

        messages.success(request, "Profile updated successfully.")
        return redirect("portal_profile")

    context = {
        "active_nav": "profile",
        "profile": profile,
    }
    return render(request, "portal/profile.html", context)
