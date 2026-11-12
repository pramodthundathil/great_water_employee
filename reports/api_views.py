from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404
from django.utils import timezone
from datetime import datetime

from expenses.models import ExpenseClaim
from leaves.models import LeaveApplication
from accounts.models import RoleChoices
from accounts.models import User
from reports.services import (
    generate_expense_excel, generate_expense_pdf,
    generate_leave_excel, generate_leave_pdf,
    build_table_excel, build_table_pdf,
    pending_approvals_rows, payment_rows, leave_balance_rows, expense_summary_rows,
)


def apply_common_filters(qs, params, kind):
    """Optional filters: employee, department, category / leave type, status."""
    if params.get("employee"):
        qs = qs.filter(employee__employee_id__iexact=params["employee"].strip())
    if params.get("department_id"):
        qs = qs.filter(employee__profile__department_id=params["department_id"])
    if kind == "expense" and params.get("category_id"):
        qs = qs.filter(category_id=params["category_id"])
    if kind == "leave" and params.get("leave_type_id"):
        qs = qs.filter(leave_type_id=params["leave_type_id"])
    if params.get("status"):
        qs = qs.filter(status=params["status"])
    return qs


def _file_response(content, fmt, slug):
    ts = timezone.now().strftime("%Y%m%d_%H%M")
    if fmt == "pdf":
        resp = HttpResponse(content, content_type="application/pdf")
        resp["Content-Disposition"] = f'attachment; filename="great_waters_{slug}_{ts}.pdf"'
    else:
        resp = HttpResponse(content, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = f'attachment; filename="great_waters_{slug}_{ts}.xlsx"'
    return resp


class ReportExportView(APIView):
    """
    GET /api/v1/reports/<report>/export/?format=pdf|excel&branch_id=&start_date=&end_date=
    report: pending-approvals | payments | leave-balances | expense-summary
    """
    permission_classes = [IsAuthenticated]

    REPORTS = {
        "pending-approvals": ("Pending Approvals Report", (), ()),
        "payments": ("Reimbursement / Payment Report", (9,), (9,)),
        "leave-balances": ("Leave Balance Report", (), (5, 6, 7, 8)),
        "expense-summary": ("Monthly Expense Summary by Employee & Category", (4,), (4,)),
    }

    def get(self, request, report):
        if report not in self.REPORTS:
            return HttpResponse("Unknown report", status=404)
        user = request.user
        fmt = request.query_params.get("format", "excel").lower()
        branches = user.get_accessible_branches()
        branch_id = request.query_params.get("branch_id")
        if branch_id:
            branches = branches.filter(id=branch_id)

        if user.role == RoleChoices.EMPLOYEE:
            claim_qs = ExpenseClaim.objects.filter(employee=user)
            leave_qs = LeaveApplication.objects.filter(employee=user)
            employees = User.objects.filter(pk=user.pk)
        elif user.role == RoleChoices.LEAD_MANAGER and not user.is_superuser:
            claim_qs = ExpenseClaim.objects.filter(employee=user)
            leave_qs = LeaveApplication.objects.filter(employee__profile__reporting_lead=user)
            employees = User.objects.filter(profile__reporting_lead=user, is_active=True)
        else:
            claim_qs = ExpenseClaim.objects.filter(branch__in=branches)
            leave_qs = LeaveApplication.objects.filter(branch__in=branches)
            employees = User.objects.filter(primary_branch__in=branches, is_active=True)

        params = request.query_params
        if params.get("department_id"):
            employees = employees.filter(profile__department_id=params["department_id"])
        if params.get("employee"):
            employees = employees.filter(employee_id__iexact=params["employee"].strip())
        claim_qs = apply_common_filters(claim_qs, {k: v for k, v in params.items() if k != "status"}, "expense")
        leave_qs = apply_common_filters(leave_qs, {k: v for k, v in params.items() if k != "status"}, "leave")
        start, end = params.get("start_date"), params.get("end_date")

        if report == "pending-approvals":
            headers, rows = pending_approvals_rows(claim_qs, leave_qs)
        elif report == "payments":
            headers, rows = payment_rows(claim_qs, start, end)
        elif report == "leave-balances":
            headers, rows = leave_balance_rows(employees.order_by("employee_id"))
        else:
            if start:
                claim_qs = claim_qs.filter(expense_date__gte=start)
            if end:
                claim_qs = claim_qs.filter(expense_date__lte=end)
            headers, rows = expense_summary_rows(claim_qs)

        title, money_cols, total_cols = self.REPORTS[report]
        if fmt == "pdf":
            content = build_table_pdf(title, headers, rows, money_cols, total_cols)
        else:
            content = build_table_excel(title, headers, rows, money_cols, total_cols)
        return _file_response(content, fmt, report.replace("-", "_"))


class ExpenseReportExportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        export_format = request.query_params.get("format", "excel").lower()
        branch_id = request.query_params.get("branch_id")
        accessible_branches = user.get_accessible_branches()

        if user.role == RoleChoices.EMPLOYEE:
            qs = ExpenseClaim.objects.filter(employee=user)
            branch_title = "My Claims"
        else:
            qs = ExpenseClaim.objects.filter(branch__in=accessible_branches)
            branch_title = "Authorized Branches"

        if branch_id and user.can_access_branch(get_object_or_404(accessible_branches, id=branch_id)):
            qs = qs.filter(branch_id=branch_id)
            branch_obj = accessible_branches.filter(id=branch_id).first()
            if branch_obj:
                branch_title = branch_obj.name

        qs = apply_common_filters(qs, request.query_params, kind="expense")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        if start_date:
            qs = qs.filter(expense_date__gte=start_date)
        if end_date:
            qs = qs.filter(expense_date__lte=end_date)

        claims = list(qs.select_related("category", "branch", "employee").order_by("-expense_date"))

        timestamp_str = timezone.now().strftime("%Y%m%d_%H%M")
        if export_format == "pdf":
            pdf_bytes = generate_expense_pdf(claims, branch_title=branch_title)
            response = HttpResponse(pdf_bytes, content_type="application/pdf")
            response["Content-Disposition"] = f'attachment; filename="great_waters_expenses_{timestamp_str}.pdf"'
            return response
        else:
            xlsx_bytes = generate_expense_excel(claims, branch_title=branch_title)
            response = HttpResponse(xlsx_bytes, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            response["Content-Disposition"] = f'attachment; filename="great_waters_expenses_{timestamp_str}.xlsx"'
            return response


class LeaveReportExportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        export_format = request.query_params.get("format", "excel").lower()
        branch_id = request.query_params.get("branch_id")
        accessible_branches = user.get_accessible_branches()

        if user.role == RoleChoices.EMPLOYEE:
            qs = LeaveApplication.objects.filter(employee=user)
            branch_title = "My Leaves"
        elif user.role == RoleChoices.LEAD_MANAGER:
            qs = LeaveApplication.objects.filter(employee__profile__reporting_lead=user)
            branch_title = "Team Leaves"
        else:
            qs = LeaveApplication.objects.filter(branch__in=accessible_branches)
            branch_title = "Authorized Branches"

        if branch_id and user.can_access_branch(get_object_or_404(accessible_branches, id=branch_id)):
            qs = qs.filter(branch_id=branch_id)
            branch_obj = accessible_branches.filter(id=branch_id).first()
            if branch_obj:
                branch_title = branch_obj.name

        qs = apply_common_filters(qs, request.query_params, kind="leave")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        if start_date:
            qs = qs.filter(start_date__gte=start_date)
        if end_date:
            qs = qs.filter(end_date__lte=end_date)

        applications = list(qs.select_related("leave_type", "branch", "employee").order_by("-start_date"))

        timestamp_str = timezone.now().strftime("%Y%m%d_%H%M")
        if export_format == "pdf":
            pdf_bytes = generate_leave_pdf(applications, branch_title=branch_title)
            response = HttpResponse(pdf_bytes, content_type="application/pdf")
            response["Content-Disposition"] = f'attachment; filename="great_waters_leaves_{timestamp_str}.pdf"'
            return response
        else:
            xlsx_bytes = generate_leave_excel(applications, branch_title=branch_title)
            response = HttpResponse(xlsx_bytes, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            response["Content-Disposition"] = f'attachment; filename="great_waters_leaves_{timestamp_str}.xlsx"'
            return response
