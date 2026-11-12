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
)
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

        if user.role == RoleChoices.EMPLOYEE or request.query_params.get("scope") == "mine":
            qs = LeaveApplication.objects.filter(employee=user)
        elif user.role == RoleChoices.LEAD_MANAGER:
            # Direct reports of this Lead Manager, or in accessible branch
            qs = LeaveApplication.objects.filter(employee__profile__reporting_lead=user)
        else:
            # HR and Director see applications in authorized branches
            qs = LeaveApplication.objects.filter(branch__in=accessible_branches)

        branch_id = request.query_params.get("branch_id")
        if branch_id and user.can_access_branch(get_object_or_404(accessible_branches, id=branch_id)):
            qs = qs.filter(branch_id=branch_id)

        status_param = request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)

        leave_type_id = request.query_params.get("leave_type_id")
        if leave_type_id:
            qs = qs.filter(leave_type_id=leave_type_id)

        qs = qs.select_related("leave_type", "branch", "employee")
        serializer = LeaveApplicationListSerializer(qs[:50], many=True)
        return Response(serializer.data)

    @transaction.atomic
    def post(self, request):
        user = request.user
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
            status=LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION
        )

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

        # Security check
        is_owner = application.employee == user
        is_lead = hasattr(application.employee, "profile") and application.employee.profile.reporting_lead == user
        has_branch_access = user.can_access_branch(application.branch) and user.role in [RoleChoices.HR_COORDINATOR, RoleChoices.DIRECTOR, RoleChoices.ADMIN_MANAGER]

        if not (is_owner or is_lead or has_branch_access or user.is_superuser):
            return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)

        serializer = LeaveApplicationDetailSerializer(application, context={"request": request})
        return Response(serializer.data)


class LeaveApplicationWithdrawView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        app = get_object_or_404(LeaveApplication, pk=pk, employee=request.user)
        try:
            app.withdraw(request.user)
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
        remarks = request.data.get("remarks", "")

        try:
            app.lead_review(approver=user, action=action, remarks=remarks)
            event_type = "LEAVE_LEAD_RECOMMENDED" if action == "RECOMMENDED" else "LEAVE_LEAD_REJECTED"
            notify_leave_event(app, event_type, remarks=remarks)
            return Response({"detail": f"Leave {action.lower()} successfully.", "status": app.status})
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
        remarks = request.data.get("remarks", "")

        try:
            app.hr_review(approver=user, action=action, remarks=remarks)
            event_type = "LEAVE_HR_VERIFIED" if action == "VERIFIED" else "LEAVE_HR_REJECTED"
            notify_leave_event(app, event_type, remarks=remarks)
            return Response({"detail": f"Leave {action.lower()} successfully.", "status": app.status})
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
        remarks = request.data.get("remarks", "")

        try:
            app.director_review(approver=user, action=action, remarks=remarks)
            event_type = "LEAVE_DIRECTOR_APPROVED" if action == "APPROVED" else "LEAVE_DIRECTOR_REJECTED"
            notify_leave_event(app, event_type, remarks=remarks)
            return Response({"detail": f"Leave {action.lower()} successfully.", "status": app.status})
        except ValidationError as e:
            return Response({"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)


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
