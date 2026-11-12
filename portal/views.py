from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum, Count, Q
from django.utils import timezone
from django.core.exceptions import ValidationError
from datetime import datetime, date, timedelta
from decimal import Decimal

from accounts.models import User, RoleChoices, EmployeeProfile
from organization.models import Branch, Department
from expenses.models import (
    ExpenseClaim, ExpenseCategory, ExpenseAttachment,
    ExpenseClaimStatus, ReimbursementPayment
)
from leaves.models import (
    LeaveApplication, LeaveType, LeavePolicy, LeaveLedgerEntry,
    LeaveApplicationStatus, HolidayCalendar
)
from leaves.services import (
    get_employee_leave_balances, record_manual_leave_adjustment,
    validate_leave_request, LeaveRequestError,
)
from portal.admin_views import validate_upload, can_manage_users
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
        if not user.can_access_branch(claim.branch):
            messages.error(request, "You do not have permission for this branch.")
            return redirect("portal_dashboard")
        decision = request.POST.get("action", "").upper()
        remarks = request.POST.get("remarks", "")
        try:
            claim.director_review(approver=user, action=decision, remarks=remarks)
            event_type = "EXPENSE_DIRECTOR_APPROVED" if decision == "APPROVED" else "EXPENSE_DIRECTOR_REJECTED"
            notify_expense_event(claim, event_type, remarks=remarks)
            messages.success(request, f"Expense {claim.claim_number} marked {decision}.")
        except ValidationError as e:
            messages.error(request, str(e))
        return redirect("portal_dashboard")

    if action_scope == "leave_decision":
        app = get_object_or_404(LeaveApplication, id=request.POST.get("app_id"))
        if not user.can_access_branch(app.branch):
            messages.error(request, "You do not have permission for this branch.")
            return redirect("portal_dashboard")
        decision = request.POST.get("action", "").upper()
        remarks = request.POST.get("remarks", "")
        try:
            app.director_review(approver=user, action=decision, remarks=remarks)
            event_type = "LEAVE_DIRECTOR_APPROVED" if decision == "APPROVED" else "LEAVE_DIRECTOR_REJECTED"
            notify_leave_event(app, event_type, remarks=remarks)
            messages.success(request, f"Leave {app.application_number} marked {decision}.")
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
            claim_qs.filter(status=ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW)
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
                messages.success(request, f"Claim {claim.claim_number} created and submitted for review!")
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
                messages.success(request, "Claim submitted for review.")
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
    }
    return render(request, "portal/expense_detail.html", context)


@login_required
def expense_approvals_view(request):
    user = request.user
    if user.role == RoleChoices.EMPLOYEE:
        messages.error(request, "Access restricted to approval authorities.")
        return redirect("portal_dashboard")

    accessible_branches = user.get_accessible_branches()
    selected_branch = request.session.get("selected_branch_id", "all")

    base_qs = ExpenseClaim.objects.filter(branch__in=accessible_branches)
    if selected_branch and selected_branch != "all":
        base_qs = base_qs.filter(branch_id=selected_branch)

    # Handle Decision Action POST
    if request.method == "POST":
        claim_id = request.POST.get("claim_id")
        action = request.POST.get("action", "").upper()
        remarks = request.POST.get("remarks", "")
        claim = get_object_or_404(ExpenseClaim, id=claim_id)

        if not user.can_access_branch(claim.branch):
            messages.error(request, "You do not have permission for this branch.")
            return redirect("portal_expense_approvals")

        if user.is_admin_manager and claim.status == ExpenseClaimStatus.UNDER_ADMIN_REVIEW:
            try:
                claim.admin_review(approver=user, action=action, remarks=remarks)
                event_type = "EXPENSE_ADMIN_APPROVED" if action == "APPROVED" else "EXPENSE_ADMIN_REJECTED"
                notify_expense_event(claim, event_type, remarks=remarks)
                messages.success(request, f"Claim {claim.claim_number} Stage-1 review: {action}!")
            except ValidationError as e:
                messages.error(request, str(e))

        elif user.is_director and claim.status == ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW:
            try:
                claim.director_review(approver=user, action=action, remarks=remarks)
                event_type = "EXPENSE_DIRECTOR_APPROVED" if action == "APPROVED" else "EXPENSE_DIRECTOR_REJECTED"
                notify_expense_event(claim, event_type, remarks=remarks)
                messages.success(request, f"Claim {claim.claim_number} Final Decision: {action}!")
            except ValidationError as e:
                messages.error(request, str(e))

        return redirect("portal_expense_approvals")

    admin_queue = base_qs.filter(status=ExpenseClaimStatus.UNDER_ADMIN_REVIEW).order_by("-submitted_at")
    director_queue = base_qs.filter(status=ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW).order_by("-submitted_at")

    context = {
        "active_nav": "expense_approvals",
        "admin_queue": admin_queue,
        "director_queue": director_queue,
    }
    return render(request, "portal/expense_approvals.html", context)


@login_required
def reimbursements_view(request):
    user = request.user
    if not (user.is_admin_manager or user.is_director or user.is_superuser):
        messages.error(request, "Access restricted.")
        return redirect("portal_dashboard")

    accessible_branches = user.get_accessible_branches()
    selected_branch = request.session.get("selected_branch_id", "all")

    base_qs = ExpenseClaim.objects.filter(branch__in=accessible_branches)
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
        if not user.can_access_branch(claim.branch):
            messages.error(request, "You do not have permission for this branch.")
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
            status=LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION
        )

        notify_leave_event(app, "LEAVE_SUBMITTED")
        messages.success(request, f"Leave application {app.application_number} submitted for Lead recommendation!")
        return redirect("portal_leaves")

    # GET
    if user.role == RoleChoices.EMPLOYEE:
        applications = LeaveApplication.objects.filter(employee=user)
    elif user.role == RoleChoices.LEAD_MANAGER:
        applications = LeaveApplication.objects.filter(
            Q(employee=user) | Q(employee__profile__reporting_lead=user)
        )
    else:
        if selected_branch and selected_branch != "all":
            applications = LeaveApplication.objects.filter(branch_id=selected_branch)
        else:
            applications = LeaveApplication.objects.filter(branch__in=accessible_branches)

    applications = applications.select_related("leave_type", "branch", "employee").order_by("-submitted_at")
    leave_types = LeaveType.objects.filter(is_active=True)
    balances = get_employee_leave_balances(user)

    context = {
        "active_nav": "leaves",
        "applications": applications,
        "leave_types": leave_types,
        "balances": balances,
    }
    return render(request, "portal/leaves_list.html", context)


@login_required
def leave_detail_view(request, pk):
    app = get_object_or_404(LeaveApplication, pk=pk)
    user = request.user

    is_owner = app.employee == user
    is_lead = hasattr(app.employee, "profile") and app.employee.profile.reporting_lead == user
    has_branch_access = user.can_access_branch(app.branch) and user.role != RoleChoices.EMPLOYEE

    if not (is_owner or is_lead or has_branch_access or user.is_superuser):
        messages.error(request, "Permission denied.")
        return redirect("portal_leaves")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "withdraw":
            try:
                app.withdraw(user)
                messages.info(request, "Leave application has been withdrawn.")
            except ValidationError as e:
                messages.error(request, str(e))
        return redirect("portal_leave_detail", pk=app.pk)

    context = {
        "active_nav": "leaves",
        "app": app,
        "approval_history": app.approval_history.all().order_by("created_at"),
    }
    return render(request, "portal/leave_detail.html", context)


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
        remarks = request.POST.get("remarks", "")
        app = get_object_or_404(LeaveApplication, id=app_id)

        reporting_lead = getattr(getattr(app.employee, "profile", None), "reporting_lead", None)
        if app.employee == user:
            messages.error(request, "You cannot act on your own leave application.")
            return redirect("portal_leave_approvals")
        if app.status == LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION:
            if not (reporting_lead == user or user.is_superuser):
                messages.error(request, "Only the employee's reporting Lead / Manager can give the recommendation.")
                return redirect("portal_leave_approvals")
        elif not user.can_access_branch(app.branch):
            messages.error(request, "You do not have permission for this branch.")
            return redirect("portal_leave_approvals")

        try:
            if app.status == LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION and (reporting_lead == user or user.is_superuser):
                app.lead_review(approver=user, action=action, remarks=remarks)
                event_type = "LEAVE_LEAD_RECOMMENDED" if action == "RECOMMENDED" else "LEAVE_LEAD_REJECTED"
                notify_leave_event(app, event_type, remarks=remarks)
                messages.success(request, f"Application {app.application_number} Lead Review: {action}!")

            elif user.is_hr_coordinator and app.status == LeaveApplicationStatus.PENDING_HR_VERIFICATION:
                app.hr_review(approver=user, action=action, remarks=remarks)
                event_type = "LEAVE_HR_VERIFIED" if action == "VERIFIED" else "LEAVE_HR_REJECTED"
                notify_leave_event(app, event_type, remarks=remarks)
                messages.success(request, f"Application {app.application_number} HR Verification: {action}!")

            elif user.is_director and app.status == LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL:
                app.director_review(approver=user, action=action, remarks=remarks)
                event_type = "LEAVE_DIRECTOR_APPROVED" if action == "APPROVED" else "LEAVE_DIRECTOR_REJECTED"
                notify_leave_event(app, event_type, remarks=remarks)
                messages.success(request, f"Application {app.application_number} Final Decision: {action}! Official balance updated.")

            else:
                messages.error(request, "This application is not awaiting your action.")

        except ValidationError as e:
            messages.error(request, " ".join(e.messages))

        return redirect("portal_leave_approvals")

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

    context = {
        "active_nav": "leave_approvals",
        "lead_queue": lead_queue,
        "hr_queue": hr_queue,
        "director_queue": director_queue,
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


@login_required
def branches_view(request):
    user = request.user
    branches = user.get_accessible_branches().annotate(employee_count=Count("primary_employees"))
    context = {
        "active_nav": "branches",
        "branches": branches,
    }
    return render(request, "portal/branches.html", context)


@login_required
def employees_view(request):
    user = request.user
    accessible_branches = user.get_accessible_branches()
    selected_branch = request.session.get("selected_branch_id", "all")

    can_manage = user.is_superuser or user.role in [
        RoleChoices.DIRECTOR, RoleChoices.ADMIN_MANAGER, RoleChoices.HR_COORDINATOR
    ]

    # Handle Add Employee modal submission
    if request.method == "POST":
        if not can_manage:
            messages.error(request, "You do not have administrative authorization to register new staff.")
            return redirect("portal_employees")

        emp_id = request.POST.get("employee_id", "").strip().upper()
        first_name = request.POST.get("first_name", "").strip()
        last_name = request.POST.get("last_name", "").strip()
        email = request.POST.get("email", "").strip().lower()
        phone = request.POST.get("phone", "").strip()
        role = request.POST.get("role", RoleChoices.EMPLOYEE)
        branch_id = request.POST.get("primary_branch")
        dept_id = request.POST.get("department")
        designation = request.POST.get("designation", "").strip()
        reporting_lead_id = request.POST.get("reporting_lead")
        gender = request.POST.get("gender", "Unspecified").strip()
        password = request.POST.get("password", "").strip()
        generated_password = not password
        if generated_password:
            password = "GW-" + secrets.token_urlsafe(9)
        elif len(password) < 8:
            messages.error(request, "Temporary password must be at least 8 characters.")
            return redirect("portal_employees")
        if role not in RoleChoices.values:
            role = RoleChoices.EMPLOYEE
        try:
            date_of_joining = datetime.strptime(request.POST.get("date_of_joining", ""), "%Y-%m-%d").date()
        except ValueError:
            date_of_joining = timezone.now().date()

        if not emp_id or not email:
            messages.error(request, "Employee ID and Email address are required fields.")
            return redirect("portal_employees")

        if User.objects.filter(Q(employee_id=emp_id) | Q(email=email)).exists():
            messages.error(request, f"An employee profile with ID '{emp_id}' or Email '{email}' already exists.")
            return redirect("portal_employees")

        target_branch = accessible_branches.filter(id=branch_id).first() if branch_id else user.primary_branch

        new_user = User.objects.create_user(
            username=emp_id,
            employee_id=emp_id,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            role=role,
            primary_branch=target_branch
        )

        dept = Department.objects.filter(id=dept_id).first() if dept_id else None
        rep_lead = User.objects.filter(id=reporting_lead_id).first() if reporting_lead_id else None

        EmployeeProfile.objects.create(
            user=new_user,
            department=dept,
            designation=designation or "Staff Specialist",
            reporting_lead=rep_lead,
            date_of_joining=date_of_joining
        )

        messages.success(request, f"Employee {new_user.full_name} ({emp_id}) added successfully to the roster.")
        if generated_password:
            messages.info(request, f"Temporary password for {emp_id}: {password}  — share it securely; the employee can change it from their profile.")
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
