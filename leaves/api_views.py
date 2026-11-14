from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from datetime import datetime, date
from decimal import Decimal

from leaves.models import (
    LeaveType, LeavePolicy, LeaveApplication,
    LeaveApplicationStatus, LeaveLedgerEntry
)
from leaves.serializers import (
    LeaveTypeSerializer, LeaveApplicationListSerializer,
    LeaveApplicationDetailSerializer, LeaveLedgerEntrySerializer
)
from leaves.services import (
    get_employee_leave_balances, record_manual_leave_adjustment,
    validate_leave_request, LeaveRequestError, calculate_leave_days,
    log_leave_audit, client_ip, resubmit_leave_application,
    initial_leave_route, visible_leave_applications, can_view_leave_application,
    filter_leave_track, leave_track_counts, notify_leave_review,
)
from leaves.models import LeaveAuditAction, fmt_days
from leaves.models import Holiday
from django.db.models import Q


def resolve_target_employee(request):
    """
    Returns the employee whose leave data may be viewed, honouring ?employee_id=<pk>.
    Leads see direct reports; HR / Admin / Director see employees in accessible branches.
    """
    user = request.user
    emp_id = request.query_params.get("employee_id")
    if not emp_id or user.role == RoleChoices.EMPLOYEE:
        return user
    target = get_object_or_404(User, id=emp_id)
    if target == user or user.is_superuser:
        return target
    if getattr(getattr(target, "profile", None), "reporting_lead_id", None) == user.id:
        return target
    if user.role != RoleChoices.LEAD_MANAGER and target.primary_branch and user.can_access_branch(target.primary_branch):
        return target
    return None
from accounts.models import RoleChoices, User
from notifications.services import notify_leave_event

API_SOURCE = "MOBILE"


def parse_optional_date(value, label):
    if value in (None, ""):
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        raise ValidationError(f"Invalid {label}. Use YYYY-MM-DD.")


def parse_amend_leave_type(value):
    if value in (None, ""):
        return None
    lt = LeaveType.objects.filter(pk=value).first() if str(value).isdigit() else None
    if lt is None:
        raise ValidationError("Unknown leave type.")
    return lt


def review_kwargs(request):
    """
    Remarks plus the optional extras shared by every review stage:
      approved_start_date / approved_end_date   partial approval inside the current range
      amend_leave_type / amend_start_date / amend_end_date   change the leave type and / or whole range
      return_to   earlier stage (LEAD_MANAGER / HR_COORDINATOR) when action is RETURNED
    """
    d = request.data
    return {
        "remarks": d.get("remarks", "") or "",
        "approved_start_date": parse_optional_date(d.get("approved_start_date"), "approved start date"),
        "approved_end_date": parse_optional_date(d.get("approved_end_date"), "approved end date"),
        "amend_leave_type": parse_amend_leave_type(d.get("amend_leave_type")),
        "amend_start_date": parse_optional_date(d.get("amend_start_date"), "new start date"),
        "amend_end_date": parse_optional_date(d.get("amend_end_date"), "new end date"),
        "return_to": (d.get("return_to") or "").upper() or None,
        "source": API_SOURCE,
        "ip_address": client_ip(request),
    }


def review_response(app, recorded):
    if recorded == "REJECTED":
        detail = "Leave rejected and returned to the employee."
    elif recorded == "RETURNED":
        last = app.approval_history.filter(action="RETURNED").order_by("-created_at").first()
        detail = f"Leave returned to {last.action_label.split('to ', 1)[-1] if last else 'an earlier stage'} for another review."
    elif recorded.startswith("PARTIALLY_"):
        detail = (f"Partially {recorded.split('_', 1)[1].lower()}: {fmt_days(app.number_of_days)} of "
                  f"{fmt_days(app.requested_number_of_days)} day(s) ({app.start_date:%d %b} to {app.end_date:%d %b %Y}).")
    else:
        detail = f"Leave {recorded.lower()} successfully."
    if getattr(app, "last_amendment", None) and recorded != "RETURNED":
        detail += f" Leave changed to {app.leave_type.name}, {app.start_date:%d %b} to {app.end_date:%d %b %Y} ({fmt_days(app.number_of_days)} day(s))."
    return Response({"detail": detail, "status": app.status, "action": recorded})


def can_view_application(user, application):
    return can_view_leave_application(user, application)


class LeaveTypeListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        types = LeaveType.objects.filter(is_active=True)
        return Response(LeaveTypeSerializer(types, many=True).data)


class EmployeeLeaveBalanceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        target_user = resolve_target_employee(request)
        if target_user is None:
            return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)

        summary = get_employee_leave_balances(target_user)
        data = []
        for item in summary:
            data.append({
                "leave_type_id": item["leave_type"].id,
                "leave_type_code": item["leave_type"].code,
                "leave_type_name": item["leave_type"].name,
                "is_accrual_based": item["leave_type"].is_accrual_based,
                "earned_days": float(item["earned_days"]),
                "taken_days": float(item["taken_days"]),
                "pending_days": float(item["pending_days"]),
                "available_balance": float(item["available_balance"]),
                "effective_remaining": float(item["effective_remaining"]),
            })
        return Response({
            "employee_id": target_user.employee_id,
            "employee_name": target_user.full_name,
            "balances": data
        })


class EmployeeLeaveLedgerView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        target_user = resolve_target_employee(request)
        if target_user is None:
            return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)

        entries = LeaveLedgerEntry.objects.filter(employee=target_user).select_related("leave_type", "created_by").order_by("-created_at")
        serializer = LeaveLedgerEntrySerializer(entries[:100], many=True)
        return Response(serializer.data)


class LeaveApplicationListCreateView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        user = request.user
        accessible_branches = user.get_accessible_branches()

        scope = request.query_params.get("scope")
        if user.role == RoleChoices.EMPLOYEE or scope == "mine":
            qs = LeaveApplication.objects.filter(employee=user)
        else:
            # team: everyone else's leave this user may track (location-scoped)
            qs = visible_leave_applications(user, include_own=scope != "team")
        qs = filter_leave_track(qs, request.query_params.get("stage"), user)

        branch_id = request.query_params.get("branch_id")
        if branch_id and user.can_access_branch(get_object_or_404(accessible_branches, id=branch_id)):
            qs = qs.filter(branch_id=branch_id)

        status_param = request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)

        leave_type_id = request.query_params.get("leave_type_id")
        if leave_type_id:
            qs = qs.filter(leave_type_id=leave_type_id)

        qs = qs.select_related("leave_type", "branch", "employee").prefetch_related("approval_history__approver")
        serializer = LeaveApplicationListSerializer(qs[:100], many=True, context={"request": request})
        return Response(serializer.data)

    @transaction.atomic
    def post(self, request):
        user = request.user
        if user.is_director:
            return Response({"detail": "Directors approve leave applications; they cannot raise their own."},
                            status=status.HTTP_403_FORBIDDEN)
        branch = user.primary_branch
        if not branch:
            return Response({"detail": "User has no primary branch assigned."}, status=status.HTTP_400_BAD_REQUEST)

        leave_type_id = request.data.get("leave_type")
        start_date_str = request.data.get("start_date")
        end_date_str = request.data.get("end_date")
        is_half_day = str(request.data.get("is_half_day", "false")).lower() in ("true", "1")
        half_day_period = request.data.get("half_day_period", "NONE")
        reason = request.data.get("reason", "")
        attachment = request.FILES.get("attachment")

        if not leave_type_id or not start_date_str or not end_date_str or not reason:
            return Response({"detail": "Leave type, start date, end date, and reason are required."}, status=status.HTTP_400_BAD_REQUEST)

        leave_type = get_object_or_404(LeaveType, id=leave_type_id, is_active=True)

        try:
            start_date = datetime.strptime(str(start_date_str)[:10], "%Y-%m-%d").date()
            end_date = datetime.strptime(str(end_date_str)[:10], "%Y-%m-%d").date()
        except ValueError:
            return Response({"detail": "Invalid date format. Use YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)

        ack = str(request.data.get("acknowledge_insufficient_balance", "false")).lower() in ("true", "1")
        try:
            number_of_days, end_date, warnings = validate_leave_request(
                user, leave_type, start_date, end_date, is_half_day,
                acknowledge_insufficient_balance=ack, attachment=attachment,
            )
        except LeaveRequestError as e:
            payload = {"detail": " ".join(e.messages), "code": e.code}
            payload.update(e.extra)
            return Response(payload, status=status.HTTP_400_BAD_REQUEST)

        route = initial_leave_route(user)
        application = LeaveApplication.objects.create(
            employee=user,
            branch=branch,
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
        log_leave_audit(application, user, LeaveAuditAction.CREATED, stage="EMPLOYEE", remarks=route["note"],
                        source=API_SOURCE, ip_address=client_ip(request))

        notify_leave_event(application, "LEAVE_SUBMITTED")
        detail_serializer = LeaveApplicationDetailSerializer(application, context={"request": request})
        data = dict(detail_serializer.data)
        data["warnings"] = warnings
        return Response(data, status=status.HTTP_201_CREATED)


class LeavePreviewView(APIView):
    """
    POST /api/v1/leaves/preview/ {leave_type, start_date, end_date, is_half_day}
    Returns chargeable days and any overlap / balance problem before submitting.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        leave_type = get_object_or_404(LeaveType, id=request.data.get("leave_type"), is_active=True)
        try:
            start_date = datetime.strptime(str(request.data.get("start_date"))[:10], "%Y-%m-%d").date()
            end_date = datetime.strptime(str(request.data.get("end_date") or request.data.get("start_date"))[:10], "%Y-%m-%d").date()
        except ValueError:
            return Response({"detail": "Invalid date format. Use YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
        is_half_day = str(request.data.get("is_half_day", "false")).lower() in ("true", "1")
        if end_date < start_date and not is_half_day:
            return Response({"ok": False, "detail": "End date cannot be before start date.", "number_of_days": 0})
        days = calculate_leave_days(user.primary_branch, start_date, start_date if is_half_day else end_date, is_half_day)
        try:
            validate_leave_request(user, leave_type, start_date, end_date, is_half_day, require_attachment=False)
            return Response({"ok": True, "number_of_days": float(days), "detail": ""})
        except LeaveRequestError as e:
            payload = {"ok": False, "number_of_days": float(days), "detail": " ".join(e.messages), "code": e.code}
            payload.update(e.extra)
            return Response(payload)


class HolidayListView(APIView):
    """GET /api/v1/leaves/holidays/?year=2026 — public holidays for the user's branch."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        year = int(request.query_params.get("year") or date.today().year)
        branch = request.user.primary_branch
        qs = Holiday.objects.filter(calendar__year=year, calendar__is_active=True)
        qs = qs.filter(Q(calendar__branch=branch) | Q(calendar__branch__isnull=True)) if branch else qs.filter(calendar__branch__isnull=True)
        return Response([
            {"id": h.id, "date": h.date.isoformat(), "name": h.name, "is_optional": h.is_optional}
            for h in qs.order_by("date")
        ])


class LeaveApplicationDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        user = request.user
        application = get_object_or_404(LeaveApplication, pk=pk)

        if not can_view_application(user, application):
            return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)

        serializer = LeaveApplicationDetailSerializer(application, context={"request": request})
        return Response(serializer.data)


class LeaveApplicationWithdrawView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        app = get_object_or_404(LeaveApplication, pk=pk, employee=request.user)
        try:
            app.withdraw(request.user, source=API_SOURCE, ip_address=client_ip(request))
            return Response({"detail": "Leave application withdrawn.", "status": app.status})
        except ValidationError as e:
            return Response({"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)


class LeaveApplicationLeadRecommendView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        user = request.user
        app = get_object_or_404(LeaveApplication, pk=pk)

        # Must be the employee's designated reporting lead or superuser
        reporting_lead = getattr(app.employee.profile, "reporting_lead", None) if hasattr(app.employee, "profile") else None
        if user != reporting_lead and not user.is_superuser:
            return Response({"detail": "Only the designated Lead/Manager can recommend this leave."}, status=status.HTTP_403_FORBIDDEN)

        action = request.data.get("action", "").upper()
        try:
            kwargs = review_kwargs(request)
            recorded = app.lead_review(approver=user, action=action, **kwargs)
            notify_leave_review(app, "LEAD_MANAGER", recorded, remarks=kwargs["remarks"], actor=user)
            return review_response(app, recorded)
        except ValidationError as e:
            return Response({"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)


class LeaveApplicationHrVerifyView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        user = request.user
        if not (user.is_hr_coordinator or user.is_superuser):
            return Response({"detail": "Only HR Coordinators can perform Stage 2 verification."}, status=status.HTTP_403_FORBIDDEN)

        app = get_object_or_404(LeaveApplication, pk=pk)
        if not user.can_access_branch(app.branch):
            return Response({"detail": "You do not have access to this branch."}, status=status.HTTP_403_FORBIDDEN)

        action = request.data.get("action", "").upper()
        try:
            kwargs = review_kwargs(request)
            recorded = app.hr_review(approver=user, action=action, **kwargs)
            notify_leave_review(app, "HR_COORDINATOR", recorded, remarks=kwargs["remarks"], actor=user)
            return review_response(app, recorded)
        except ValidationError as e:
            return Response({"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)


class LeaveApplicationDirectorApprovalView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        user = request.user
        if not (user.is_director or user.is_superuser):
            return Response({"detail": "Only Directors can give final leave approval."}, status=status.HTTP_403_FORBIDDEN)

        app = get_object_or_404(LeaveApplication, pk=pk)
        if not user.can_access_branch(app.branch):
            return Response({"detail": "You do not have access to this branch."}, status=status.HTTP_403_FORBIDDEN)

        action = request.data.get("action", "").upper()
        try:
            kwargs = review_kwargs(request)
            recorded = app.director_review(approver=user, action=action, **kwargs)
            notify_leave_review(app, "DIRECTOR", recorded, remarks=kwargs["remarks"], actor=user)
            return review_response(app, recorded)
        except ValidationError as e:
            return Response({"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)


class LeaveApplicationResubmitView(APIView):
    """
    POST /api/v1/leaves/applications/<pk>/resubmit/
    Edit a rejected application and send it back to the Lead / Manager.
    Accepts the same fields as creating a leave, plus optional resubmit_note and remove_attachment.
    Omitted fields keep their current value.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, pk):
        app = get_object_or_404(LeaveApplication, pk=pk, employee=request.user)
        data = request.data
        leave_type = app.leave_type
        if data.get("leave_type"):
            leave_type = get_object_or_404(LeaveType, id=data.get("leave_type"), is_active=True)
        try:
            start_date = parse_optional_date(data.get("start_date"), "start date") or app.requested_start_date or app.start_date
            end_date = parse_optional_date(data.get("end_date"), "end date") or app.requested_end_date or app.end_date
        except ValidationError as e:
            return Response({"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)
        if "is_half_day" in data:
            is_half_day = str(data.get("is_half_day")).lower() in ("true", "1")
        else:
            is_half_day = app.is_half_day

        def truthy(key):
            return str(data.get(key, "false")).lower() in ("true", "1")

        try:
            app, warnings = resubmit_leave_application(
                app, request.user, leave_type, start_date, end_date,
                reason=data.get("reason", app.reason),
                is_half_day=is_half_day,
                half_day_period=data.get("half_day_period", app.half_day_period),
                attachment=request.FILES.get("attachment"),
                remove_attachment=truthy("remove_attachment"),
                acknowledge_insufficient_balance=truthy("acknowledge_insufficient_balance"),
                resubmit_note=data.get("resubmit_note", ""),
                source=API_SOURCE, ip_address=client_ip(request),
            )
        except LeaveRequestError as e:
            payload = {"detail": " ".join(e.messages), "code": e.code}
            payload.update(e.extra)
            return Response(payload, status=status.HTTP_400_BAD_REQUEST)

        notify_leave_event(app, "LEAVE_RESUBMITTED")
        result = dict(LeaveApplicationDetailSerializer(app, context={"request": request}).data)
        result["warnings"] = warnings
        return Response(result)


class LeaveTrackView(APIView):
    """
    GET /api/v1/leaves/track/?stage=<filter>&branch_id=<id>
    Approval tracker: other staff's leave this user may see, with the current stage of each.
    Filters: all, pending, awaiting_lead, awaiting_hr, awaiting_director, approved, rejected,
    withdrawn, acted_by_me. Returns {counts, results}.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if user.role == RoleChoices.EMPLOYEE and not user.is_superuser:
            return Response({"detail": "Only approvers can track team leave."}, status=status.HTTP_403_FORBIDDEN)
        qs = visible_leave_applications(user, include_own=False)
        branch_id = request.query_params.get("branch_id")
        if branch_id:
            qs = qs.filter(branch_id=branch_id)
        counts = leave_track_counts(qs, user)
        qs = filter_leave_track(qs, request.query_params.get("stage"), user)
        qs = qs.select_related("leave_type", "branch", "employee").prefetch_related("approval_history__approver") \
            .order_by("-submitted_at")
        data = LeaveApplicationListSerializer(qs[:200], many=True, context={"request": request}).data
        return Response({"counts": counts, "results": data})


class LeaveApplicationAuditTrailView(APIView):
    """GET /api/v1/leaves/applications/<pk>/audit-trail/ — full audit history of one application."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from leaves.serializers import LeaveAuditLogSerializer
        app = get_object_or_404(LeaveApplication, pk=pk)
        if not can_view_application(request.user, app):
            return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
        logs = app.audit_logs.select_related("actor")
        return Response(LeaveAuditLogSerializer(logs, many=True).data)


class LeaveApplicationRangeDaysView(APIView):
    """
    GET /api/v1/leaves/applications/<pk>/range-days/?start_date=&end_date=
    Chargeable days for a sub-range of the application, used by approvers choosing a partial approval.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        app = get_object_or_404(LeaveApplication, pk=pk)
        if not can_view_application(request.user, app):
            return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
        try:
            start = parse_optional_date(request.query_params.get("start_date"), "start date") or app.start_date
            end = parse_optional_date(request.query_params.get("end_date"), "end date") or app.end_date
        except ValidationError as e:
            return Response({"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)
        if end < start:
            return Response({"ok": False, "number_of_days": 0, "detail": "End date cannot be before start date."})
        if start < app.start_date or end > app.end_date:
            return Response({"ok": False, "number_of_days": 0,
                             "detail": f"Dates must be within {app.start_date:%d %b} to {app.end_date:%d %b %Y}."})
        days = calculate_leave_days(app.employee.primary_branch or app.branch, start, end)
        return Response({"ok": days > 0, "number_of_days": float(days),
                         "current_days": float(app.number_of_days),
                         "detail": "" if days > 0 else "No working days in this range."})


class LeaveManualAdjustmentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        if not (user.is_hr_coordinator or user.is_superuser):
            return Response({"detail": "Only HR Coordinators can make manual balance adjustments."}, status=status.HTTP_403_FORBIDDEN)

        emp_id = request.data.get("employee_id")
        leave_type_id = request.data.get("leave_type_id")
        days = request.data.get("days")
        remarks = request.data.get("remarks", "")

        if not emp_id or not leave_type_id or days is None:
            return Response({"detail": "Employee, leave type, and days are required."}, status=status.HTTP_400_BAD_REQUEST)

        emp = get_object_or_404(User, id=emp_id)
        if not user.can_access_branch(emp.primary_branch):
            return Response({"detail": "You do not have access to this employee's branch."}, status=status.HTTP_403_FORBIDDEN)

        leave_type = get_object_or_404(LeaveType, id=leave_type_id)

        try:
            entry = record_manual_leave_adjustment(
                employee=emp,
                leave_type=leave_type,
                days=days,
                remarks=remarks,
                hr_user=user
            )
            return Response({
                "detail": "Leave balance adjusted successfully.",
                "new_balance": float(entry.balance_after),
                "transaction_days": float(entry.transaction_days)
            })
        except ValidationError as e:
            return Response({"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)
