import io
from datetime import datetime, date
from decimal import Decimal
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

from expenses.models import ExpenseClaim, ReimbursementPayment
from leaves.models import LeaveApplication, LeaveLedgerEntry
from organization.models import Branch


def generate_expense_excel(claims, branch_title="All Branches"):
    wb = Workbook()
    ws = wb.active
    ws.title = "Expense Report"

    # Header style
    brand_fill = PatternFill(start_color="156B9E", end_color="156B9E", fill_type="solid")
    brand_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    title_font = Font(name="Calibri", size=14, bold=True, color="156B9E")

    ws.merge_cells("A1:H1")
    ws["A1"] = f"Great Waters — Expense Report ({branch_title})"
    ws["A1"].font = title_font
    ws["A1"].alignment = Alignment(horizontal="left", vertical="center")

    ws["A2"] = f"Generated at: {timezone.now().strftime('%Y-%m-%d %H:%M UTC')}"
    ws["A2"].font = Font(italic=True, size=9, color="555555")

    headers = ["Claim Number", "Date", "Employee ID", "Employee Name", "Branch", "Category", "Amount", "Status"]
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=4, column=col_idx, value=h)
        cell.fill = brand_fill
        cell.font = brand_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    total_amount = Decimal("0.00")
    for row_idx, c in enumerate(claims, start=5):
        ws.cell(row=row_idx, column=1, value=c.claim_number)
        ws.cell(row=row_idx, column=2, value=c.expense_date.strftime("%Y-%m-%d"))
        ws.cell(row=row_idx, column=3, value=c.employee.employee_id)
        ws.cell(row=row_idx, column=4, value=c.employee.full_name)
        ws.cell(row=row_idx, column=5, value=c.branch.code)
        ws.cell(row=row_idx, column=6, value=c.category.name)
        
        amt_cell = ws.cell(row=row_idx, column=7, value=float(c.amount))
        amt_cell.number_format = "$#,##0.00"
        amt_cell.alignment = Alignment(horizontal="right")
        total_amount += c.amount

        ws.cell(row=row_idx, column=8, value=c.get_status_display())

    # Total row
    tot_row = len(claims) + 5
    ws.merge_cells(f"A{tot_row}:F{tot_row}")
    ws.cell(row=tot_row, column=1, value="TOTAL")
    ws.cell(row=tot_row, column=1).font = Font(bold=True)
    ws.cell(row=tot_row, column=1).alignment = Alignment(horizontal="right")

    tot_cell = ws.cell(row=tot_row, column=7, value=float(total_amount))
    tot_cell.number_format = "$#,##0.00"
    tot_cell.font = Font(bold=True)
    tot_cell.alignment = Alignment(horizontal="right")

    # Column dimensions
    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        col_letter = col[0].column_letter
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output.getvalue()


def generate_expense_pdf(claims, branch_title="All Branches"):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter), leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    elements = []

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleStyle", parent=styles["Heading1"], fontSize=18, textColor=colors.HexColor("#156B9E"), spaceAfter=4)
    sub_style = ParagraphStyle("SubStyle", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#555555"), spaceAfter=12)

    elements.append(Paragraph(f"<b>Great Waters</b> — Expense Reimbursement Report", title_style))
    elements.append(Paragraph(f"Scope: {branch_title} | Generated: {timezone.now().strftime('%Y-%m-%d %H:%M UTC')}", sub_style))

    data = [["Claim #", "Date", "Emp ID", "Employee Name", "Branch", "Category", "Amount", "Status"]]
    total_amount = Decimal("0.00")

    for c in claims:
        data.append([
            c.claim_number,
            c.expense_date.strftime("%Y-%m-%d"),
            c.employee.employee_id,
            c.employee.full_name[:18],
            c.branch.code,
            c.category.name[:16],
            f"${c.amount:,.2f}",
            c.get_status_display()[:15]
        ])
        total_amount += c.amount

    data.append(["TOTAL", "", "", "", "", "", f"${total_amount:,.2f}", ""])

    t = Table(data, colWidths=[90, 65, 60, 110, 60, 100, 75, 110])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#156B9E")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("ALIGN", (6, 1), (6, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 1.5, colors.HexColor("#0D4D73")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F1F5F9")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ]))

    elements.append(t)
    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


def generate_leave_excel(applications, branch_title="All Branches"):
    wb = Workbook()
    ws = wb.active
    ws.title = "Leave Report"

    brand_fill = PatternFill(start_color="156B9E", end_color="156B9E", fill_type="solid")
    brand_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")

    ws.merge_cells("A1:H1")
    ws["A1"] = f"Great Waters — Leave Report ({branch_title})"
    ws["A1"].font = Font(name="Calibri", size=14, bold=True, color="156B9E")

    headers = ["App Number", "Emp ID", "Employee Name", "Branch", "Leave Type", "Start Date", "End Date", "Days", "Status"]
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=4, column=col_idx, value=h)
        cell.fill = brand_fill
        cell.font = brand_font

    total_days = Decimal("0.00")
    for row_idx, app in enumerate(applications, start=5):
        ws.cell(row=row_idx, column=1, value=app.application_number)
        ws.cell(row=row_idx, column=2, value=app.employee.employee_id)
        ws.cell(row=row_idx, column=3, value=app.employee.full_name)
        ws.cell(row=row_idx, column=4, value=app.branch.code)
        ws.cell(row=row_idx, column=5, value=app.leave_type.name)
        ws.cell(row=row_idx, column=6, value=app.start_date.strftime("%Y-%m-%d"))
        ws.cell(row=row_idx, column=7, value=app.end_date.strftime("%Y-%m-%d"))
        ws.cell(row=row_idx, column=8, value=float(app.number_of_days))
        ws.cell(row=row_idx, column=9, value=app.get_status_display())
        total_days += app.number_of_days

    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = max(max_len + 3, 12)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output.getvalue()


def generate_leave_pdf(applications, branch_title="All Branches"):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter), leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    elements = []

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleStyle", parent=styles["Heading1"], fontSize=18, textColor=colors.HexColor("#156B9E"), spaceAfter=4)
    sub_style = ParagraphStyle("SubStyle", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#555555"), spaceAfter=12)

    elements.append(Paragraph(f"<b>Great Waters</b> — Leave Summary Report", title_style))
    elements.append(Paragraph(f"Scope: {branch_title} | Generated: {timezone.now().strftime('%Y-%m-%d %H:%M UTC')}", sub_style))

    data = [["App #", "Emp ID", "Employee Name", "Branch", "Type", "Start Date", "End Date", "Days", "Status"]]
    total_days = Decimal("0.00")

    for a in applications:
        data.append([
            a.application_number,
            a.employee.employee_id,
            a.employee.full_name[:18],
            a.branch.code,
            a.leave_type.code,
            a.start_date.strftime("%Y-%m-%d"),
            a.end_date.strftime("%Y-%m-%d"),
            f"{a.number_of_days:g}",
            a.get_status_display()[:15]
        ])
        total_days += a.number_of_days

    data.append(["TOTAL", "", "", "", "", "", "", f"{total_days:g}", ""])

    t = Table(data, colWidths=[90, 60, 110, 50, 50, 75, 75, 50, 110])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#156B9E")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F1F5F9")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
    ]))

    elements.append(t)
    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Generic tabular exports used by the additional reports
# ---------------------------------------------------------------------------

def build_table_excel(title, headers, rows, money_cols=(), total_cols=()):
    wb = Workbook()
    ws = wb.active
    ws.title = "".join(ch for ch in title if ch not in '\\/*?:[]')[:31]
    brand_fill = PatternFill(start_color="156B9E", end_color="156B9E", fill_type="solid")
    brand_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")

    ws["A1"] = f"Great Waters — {title}"
    ws["A1"].font = Font(name="Calibri", size=14, bold=True, color="156B9E")
    ws["A2"] = f"Generated at: {timezone.now().strftime('%Y-%m-%d %H:%M UTC')}"
    ws["A2"].font = Font(italic=True, size=9, color="555555")

    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=4, column=col_idx, value=h)
        cell.fill = brand_fill
        cell.font = brand_font

    totals = {c: Decimal("0") for c in total_cols}
    for r_idx, row in enumerate(rows, start=5):
        for c_idx, value in enumerate(row):
            if isinstance(value, Decimal):
                value = float(value)
            cell = ws.cell(row=r_idx, column=c_idx + 1, value=value)
            if c_idx in money_cols:
                cell.number_format = "$#,##0.00"
            if c_idx in totals and value not in (None, ""):
                totals[c_idx] += Decimal(str(value))

    if totals:
        tot_row = len(rows) + 5
        ws.cell(row=tot_row, column=1, value="TOTAL").font = Font(bold=True)
        for c_idx, total in totals.items():
            cell = ws.cell(row=tot_row, column=c_idx + 1, value=float(total))
            cell.font = Font(bold=True)
            if c_idx in money_cols:
                cell.number_format = "$#,##0.00"

    for col in ws.iter_cols(min_row=4):
        max_len = max(len(str(cell.value or "")) for cell in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(max_len + 3, 10), 50)

    output = io.BytesIO()
    wb.save(output)
    return output.getvalue()


def build_table_pdf(title, headers, rows, money_cols=(), total_cols=(), subtitle=""):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter), leftMargin=30, rightMargin=30, topMargin=30, bottomMargin=30)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("T", parent=styles["Heading1"], fontSize=16, textColor=colors.HexColor("#156B9E"), spaceAfter=4)
    sub_style = ParagraphStyle("S", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#555555"), spaceAfter=10)
    cell_style = ParagraphStyle("C", parent=styles["Normal"], fontSize=7.5, leading=9)

    elements = [
        Paragraph(f"<b>Great Waters</b> — {title}", title_style),
        Paragraph(f"{subtitle + ' | ' if subtitle else ''}Generated: {timezone.now().strftime('%Y-%m-%d %H:%M UTC')}", sub_style),
    ]

    def fmt(value, idx):
        if value is None:
            return ""
        if idx in money_cols and value != "":
            return f"${Decimal(str(value)):,.2f}"
        if isinstance(value, Decimal):
            return f"{value:g}"
        return Paragraph(str(value), cell_style) if isinstance(value, str) and len(value) > 22 else str(value)

    data = [headers] + [[fmt(v, i) for i, v in enumerate(row)] for row in rows]
    if total_cols:
        total_row = ["TOTAL"] + [""] * (len(headers) - 1)
        for c in total_cols:
            tot = sum((Decimal(str(r[c])) for r in rows if r[c] not in (None, "")), Decimal("0"))
            total_row[c] = f"${tot:,.2f}" if c in money_cols else f"{tot:g}"
        data.append(total_row)

    width = landscape(letter)[0] - 60
    t = Table(data, colWidths=[width / len(headers)] * len(headers), repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#156B9E")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
    ]
    if total_cols:
        style += [("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F1F5F9")), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold")]
    t.setStyle(TableStyle(style))
    elements.append(t)
    doc.build(elements)
    return buffer.getvalue()


def pending_approvals_rows(claim_qs, leave_qs):
    """Stage-wise and approver-wise list of everything awaiting action."""
    from portal.dashboard_services import get_item_approver_info, PENDING_EXPENSE_STATUSES, PENDING_LEAVE_STATUSES

    now = timezone.now()
    rows = []
    for c in claim_qs.filter(status__in=PENDING_EXPENSE_STATUSES).select_related("employee", "branch", "category"):
        info = get_item_approver_info(c, "expense")
        since = c.updated_at or c.submitted_at or c.created_at
        rows.append(["Expense", c.claim_number, c.employee.full_name, c.branch.code,
                     f"{c.category.name} ${c.amount:,.2f}", info["stage_label"], info["approvers_label"],
                     since.strftime("%Y-%m-%d"), (now - since).days])
    for a in leave_qs.filter(status__in=PENDING_LEAVE_STATUSES).select_related("employee__profile__reporting_lead", "branch", "leave_type"):
        info = get_item_approver_info(a, "leave")
        since = a.updated_at or a.submitted_at
        rows.append(["Leave", a.application_number, a.employee.full_name, a.branch.code,
                     f"{a.leave_type.code} {a.number_of_days:g}d ({a.start_date:%d %b})", info["stage_label"],
                     info["approvers_label"], since.strftime("%Y-%m-%d"), (now - since).days])
    rows.sort(key=lambda r: (r[5], r[6], -r[8]))
    headers = ["Type", "Reference", "Employee", "Branch", "Details", "Stage", "Approver(s)", "Waiting since", "Days waiting"]
    return headers, rows


def payment_rows(claim_qs, start_date=None, end_date=None):
    payments = ReimbursementPayment.objects.filter(claim__in=claim_qs).select_related(
        "claim__employee", "claim__branch", "claim__category", "recorded_by"
    ).order_by("-payment_date")
    if start_date:
        payments = payments.filter(payment_date__gte=start_date)
    if end_date:
        payments = payments.filter(payment_date__lte=end_date)
    rows = [[
        p.payment_date.strftime("%Y-%m-%d"), p.claim.claim_number, p.claim.employee.employee_id,
        p.claim.employee.full_name, p.claim.branch.code, p.claim.category.name,
        p.get_payment_mode_display(), p.payment_reference, p.recorded_by.full_name, p.claim.amount,
    ] for p in payments]
    headers = ["Paid on", "Claim", "Emp ID", "Employee", "Branch", "Category", "Mode", "Reference", "Recorded by", "Amount"]
    return headers, rows


def leave_balance_rows(employees):
    from leaves.services import get_employee_leave_balances

    rows = []
    for emp in employees.select_related("primary_branch", "profile__department"):
        for b in get_employee_leave_balances(emp):
            if not b["leave_type"].is_paid:
                continue
            dept = getattr(getattr(emp, "profile", None), "department", None)
            rows.append([
                emp.employee_id, emp.full_name, emp.primary_branch.code if emp.primary_branch else "",
                dept.name if dept else "", b["leave_type"].name,
                b["earned_days"], b["taken_days"], b["pending_days"], b["available_balance"], b["effective_remaining"],
            ])
    headers = ["Emp ID", "Employee", "Branch", "Department", "Leave type", "Earned / credited", "Taken", "Pending", "Balance", "Remaining after pending"]
    return headers, rows


def expense_summary_rows(claim_qs):
    """Monthly expense summary per employee with category-wise totals."""
    from collections import defaultdict
    from expenses.models import ExpenseClaimStatus

    totals = defaultdict(Decimal)
    for c in claim_qs.exclude(status__in=[ExpenseClaimStatus.DRAFT, ExpenseClaimStatus.WITHDRAWN]).select_related("employee", "category"):
        key = (c.expense_date.strftime("%Y-%m"), c.employee.employee_id, c.employee.full_name, c.category.name)
        totals[key] += c.amount
    rows = [[k[0], k[1], k[2], k[3], v] for k, v in sorted(totals.items(), key=lambda kv: (kv[0][0], kv[0][2], kv[0][3]), reverse=False)]
    headers = ["Month", "Emp ID", "Employee", "Category", "Total claimed"]
    return headers, rows
