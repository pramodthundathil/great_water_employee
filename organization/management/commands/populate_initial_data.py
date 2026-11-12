from django.core.management.base import BaseCommand
from django.utils import timezone
from django.core.files.base import ContentFile
from decimal import Decimal
from datetime import date, timedelta

from organization.models import Organization, Branch, Department
from accounts.models import User, RoleChoices, UserBranchScope, EmployeeProfile
from expenses.models import ExpenseCategory, ExpenseClaim, ExpenseAttachment, ExpenseClaimStatus
from leaves.models import (
    LeaveType, LeavePolicy, LeaveLedgerEntry, LeaveLedgerEntryType,
    LeaveApplication, LeaveApplicationStatus, HolidayCalendar, Holiday
)


class Command(BaseCommand):
    help = "Populate initial seed data for Great Waters (branches, roles, policies, demo users)"

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("Starting Great Waters data initialization..."))

        # 1. Organization Master
        org, _ = Organization.objects.get_or_create(
            code="GW",
            defaults={
                "name": "Great Waters Maritime & Engineering",
                "currency_code": "USD",
                "currency_symbol": "$",
                "contact_email": "operations@greatwaters.com",
                "contact_phone": "+971 4 000 0000",
            }
        )
        self.stdout.write(f"Organization: {org.name}")

        # 2. Branches
        branches_data = [
            {"code": "GW-DXB", "name": "Dubai Headquarters", "city": "Dubai", "country": "United Arab Emirates", "phone": "+971 4 123 4567"},
            {"code": "GW-KCH", "name": "Kochi Engineering Center", "city": "Kochi", "country": "India", "phone": "+91 484 234 5678"},
            {"code": "GW-SGP", "name": "Singapore Regional Office", "city": "Singapore", "country": "Singapore", "phone": "+65 6789 0123"},
        ]
        branch_objs = {}
        for b_data in branches_data:
            b, _ = Branch.objects.get_or_create(
                code=b_data["code"],
                defaults={
                    "organization": org,
                    "name": b_data["name"],
                    "city": b_data["city"],
                    "country": b_data["country"],
                    "phone": b_data["phone"],
                    "is_active": True,
                }
            )
            branch_objs[b.code] = b
            self.stdout.write(f"Branch created/ready: {b.name} ({b.code})")

        # 3. Departments
        depts_data = [
            {"code": "ENG", "name": "Marine & Offshore Engineering"},
            {"code": "OPS", "name": "Maritime Field Operations"},
            {"code": "NDT", "name": "Inspection & NDT Services"},
            {"code": "FIN", "name": "Finance & Administration"},
            {"code": "HR", "name": "Human Resources"},
        ]
        dept_objs = {}
        for d in depts_data:
            dept, _ = Department.objects.get_or_create(
                code=d["code"],
                defaults={"organization": org, "name": d["name"], "is_active": True}
            )
            dept_objs[d["code"]] = dept

        # 4. Expense Categories
        categories_data = [
            {"code": "TRAVEL", "name": "Travel & Transportation", "desc": "Flights, trains, local taxis, transit"},
            {"code": "HOTEL", "name": "Hotel & Accommodation", "desc": "On-site and field accommodation"},
            {"code": "FOOD", "name": "Food & Client Dining", "desc": "Per diem meals and business meals"},
            {"code": "FUEL", "name": "Fuel & Vehicle Allowance", "desc": "Field service vehicle expenses"},
            {"code": "EQUIP", "name": "Tools & Equipment", "desc": "Emergency tools, consumables, PPE"},
            {"code": "OFFICE", "name": "Office Supplies & Misc", "desc": "Printing, couriers, documentation"},
        ]
        cat_objs = {}
        for c in categories_data:
            cat, _ = ExpenseCategory.objects.get_or_create(
                code=c["code"],
                defaults={"organization": org, "name": c["name"], "description": c["desc"], "is_active": True}
            )
            cat_objs[c["code"]] = cat

        # 5. Leave Types
        leave_types_data = [
            {"code": "EL", "name": "Earned Leave", "is_paid": True, "is_accrual": True, "requires_att": False},
            {"code": "CL", "name": "Casual Leave", "is_paid": True, "is_accrual": False, "requires_att": False},
            {"code": "SL", "name": "Sick Leave", "is_paid": True, "is_accrual": False, "requires_att": True},
            {"code": "LWP", "name": "Leave Without Pay", "is_paid": False, "is_accrual": False, "requires_att": False},
        ]
        lt_objs = {}
        for lt in leave_types_data:
            obj, _ = LeaveType.objects.get_or_create(
                code=lt["code"],
                defaults={
                    "organization": org,
                    "name": lt["name"],
                    "is_paid": lt["is_paid"],
                    "is_accrual_based": lt["is_accrual"],
                    "requires_attachment": lt["requires_att"],
                    "is_active": True,
                }
            )
            lt_objs[lt["code"]] = obj

        # 6. Leave Policy for Earned Leave (18 days/year, 1.5 days/month)
        el_policy, _ = LeavePolicy.objects.get_or_create(
            leave_type=lt_objs["EL"],
            defaults={
                "name": "Standard Earned Leave Policy",
                "yearly_entitlement": Decimal("18.00"),
                "accrual_frequency": "MONTHLY",
                "monthly_accrual_rate": Decimal("1.50"),
                "max_carry_forward": Decimal("12.00"),
                "probation_period_months": 3,
                "effective_date": date(2026, 1, 1),
                "is_active": True,
            }
        )

        cl_policy, _ = LeavePolicy.objects.get_or_create(
            leave_type=lt_objs["CL"],
            defaults={
                "name": "Annual Casual Leave Policy",
                "yearly_entitlement": Decimal("12.00"),
                "accrual_frequency": "NONE",
                "monthly_accrual_rate": Decimal("0.00"),
                "max_carry_forward": Decimal("0.00"),
                "probation_period_months": 0,
                "effective_date": date(2026, 1, 1),
                "is_active": True,
            }
        )

        # 7. Holiday Calendars
        cal_dxb, _ = HolidayCalendar.objects.get_or_create(
            organization=org, branch=branch_objs["GW-DXB"], year=2026,
            defaults={"name": "UAE Public Holidays 2026"}
        )
        Holiday.objects.get_or_create(calendar=cal_dxb, date=date(2026, 1, 1), defaults={"name": "New Year's Day"})
        Holiday.objects.get_or_create(calendar=cal_dxb, date=date(2026, 12, 2), defaults={"name": "UAE National Day"})

        cal_kch, _ = HolidayCalendar.objects.get_or_create(
            organization=org, branch=branch_objs["GW-KCH"], year=2026,
            defaults={"name": "India Public Holidays 2026"}
        )
        Holiday.objects.get_or_create(calendar=cal_kch, date=date(2026, 1, 26), defaults={"name": "Republic Day"})
        Holiday.objects.get_or_create(calendar=cal_kch, date=date(2026, 8, 15), defaults={"name": "Independence Day"})

        # 8. Seed Users for All 5 Roles
        users_seed = [
            {
                "employee_id": "GW-DIR-001",
                "username": "director",
                "email": "director@greatwaters.com",
                "first_name": "Tariq",
                "last_name": "Al-Mansoor",
                "role": RoleChoices.DIRECTOR,
                "is_global_scope": True,
                "branch": branch_objs["GW-DXB"],
                "dept": dept_objs["OPS"],
                "designation": "Managing Director",
                "is_staff": True,
                "is_superuser": True,
            },
            {
                "employee_id": "GW-ADM-001",
                "username": "admin.manager",
                "email": "admin@greatwaters.com",
                "first_name": "Suresh",
                "last_name": "Nair",
                "role": RoleChoices.ADMIN_MANAGER,
                "is_global_scope": True,
                "branch": branch_objs["GW-DXB"],
                "dept": dept_objs["FIN"],
                "designation": "Admin & Finance Manager",
                "is_staff": True,
                "is_superuser": False,
            },
            {
                "employee_id": "GW-HRC-001",
                "username": "hr.coordinator",
                "email": "hr@greatwaters.com",
                "first_name": "Ananya",
                "last_name": "Menon",
                "role": RoleChoices.HR_COORDINATOR,
                "is_global_scope": True,
                "branch": branch_objs["GW-KCH"],
                "dept": dept_objs["HR"],
                "designation": "Lead HR Coordinator",
                "is_staff": True,
                "is_superuser": False,
            },
            {
                "employee_id": "GW-ENG-001",
                "username": "lead.kochi",
                "email": "lead.kochi@greatwaters.com",
                "first_name": "Rahul",
                "last_name": "Verma",
                "role": RoleChoices.LEAD_MANAGER,
                "is_global_scope": False,
                "branch": branch_objs["GW-KCH"],
                "dept": dept_objs["ENG"],
                "designation": "Engineering Team Lead",
                "is_staff": False,
                "is_superuser": False,
            },
            {
                "employee_id": "GW-EMP-001",
                "username": "emp.kochi",
                "email": "emp.kochi@greatwaters.com",
                "first_name": "Kiran",
                "last_name": "Joseph",
                "role": RoleChoices.EMPLOYEE,
                "is_global_scope": False,
                "branch": branch_objs["GW-KCH"],
                "dept": dept_objs["ENG"],
                "designation": "Marine Structural Engineer",
                "is_staff": False,
                "is_superuser": False,
            },
            {
                "employee_id": "GW-EMP-002",
                "username": "emp.dubai",
                "email": "emp.dubai@greatwaters.com",
                "first_name": "Fahad",
                "last_name": "Hassan",
                "role": RoleChoices.EMPLOYEE,
                "is_global_scope": False,
                "branch": branch_objs["GW-DXB"],
                "dept": dept_objs["OPS"],
                "designation": "Offshore Inspection Specialist",
                "is_staff": False,
                "is_superuser": False,
            },
        ]

        created_users = {}
        for u_data in users_seed:
            user, created = User.objects.get_or_create(
                employee_id=u_data["employee_id"],
                defaults={
                    "username": u_data["username"],
                    "email": u_data["email"],
                    "first_name": u_data["first_name"],
                    "last_name": u_data["last_name"],
                    "role": u_data["role"],
                    "is_global_scope": u_data["is_global_scope"],
                    "primary_branch": u_data["branch"],
                    "is_staff": u_data["is_staff"],
                    "is_superuser": u_data["is_superuser"],
                    "is_active": True,
                }
            )
            user.set_password("Password@123")
            user.save()
            created_users[user.employee_id] = user

            # Create/update profile
            profile, _ = EmployeeProfile.objects.get_or_create(
                user=user,
                defaults={
                    "department": u_data["dept"],
                    "designation": u_data["designation"],
                    "date_of_joining": date(2025, 1, 15),
                    "emergency_contact": "Emergency Desk +971 4 999 8888",
                    "bank_name": "First Abu Dhabi Bank" if "DXB" in u_data["branch"].code else "HDFC Bank",
                    "bank_account_no": "10029384756",
                    "bank_ifsc_or_swift": "FABAAEM" if "DXB" in u_data["branch"].code else "HDFC0001234",
                }
            )

        # Set reporting lead: emp.kochi reports to lead.kochi
        emp_kochi_prof = created_users["GW-EMP-001"].profile
        emp_kochi_prof.reporting_lead = created_users["GW-ENG-001"]
        emp_kochi_prof.save()

        # emp.dubai reports to Director
        emp_dubai_prof = created_users["GW-EMP-002"].profile
        emp_dubai_prof.reporting_lead = created_users["GW-DIR-001"]
        emp_dubai_prof.save()

        # Seed initial leave ledger balances for employees
        for emp_key in ["GW-EMP-001", "GW-EMP-002"]:
            emp_u = created_users[emp_key]
            # EL opening credit
            if not LeaveLedgerEntry.objects.filter(employee=emp_u, leave_type=lt_objs["EL"]).exists():
                LeaveLedgerEntry.objects.create(
                    employee=emp_u,
                    leave_type=lt_objs["EL"],
                    entry_type=LeaveLedgerEntryType.OPENING_BALANCE,
                    transaction_days=Decimal("15.00"),
                    balance_after=Decimal("15.00"),
                    reference_note="Opening Earned Leave Credit 2026",
                    created_by=created_users["GW-HRC-001"]
                )
            # CL opening credit
            if not LeaveLedgerEntry.objects.filter(employee=emp_u, leave_type=lt_objs["CL"]).exists():
                LeaveLedgerEntry.objects.create(
                    employee=emp_u,
                    leave_type=lt_objs["CL"],
                    entry_type=LeaveLedgerEntryType.OPENING_BALANCE,
                    transaction_days=Decimal("12.00"),
                    balance_after=Decimal("12.00"),
                    reference_note="Annual Casual Leave Entitlement 2026",
                    created_by=created_users["GW-HRC-001"]
                )
            # SL opening credit
            if not LeaveLedgerEntry.objects.filter(employee=emp_u, leave_type=lt_objs["SL"]).exists():
                LeaveLedgerEntry.objects.create(
                    employee=emp_u,
                    leave_type=lt_objs["SL"],
                    entry_type=LeaveLedgerEntryType.OPENING_BALANCE,
                    transaction_days=Decimal("10.00"),
                    balance_after=Decimal("10.00"),
                    reference_note="Annual Sick Leave Entitlement 2026",
                    created_by=created_users["GW-HRC-001"]
                )

        # Seed a sample expense claim with proof for GW-EMP-001
        emp1 = created_users["GW-EMP-001"]
        if not ExpenseClaim.objects.filter(employee=emp1).exists():
            claim = ExpenseClaim.objects.create(
                claim_number="EXP-202610-DEMO01",
                employee=emp1,
                branch=emp1.primary_branch,
                category=cat_objs["TRAVEL"],
                expense_date=date.today() - timedelta(days=2),
                amount=Decimal("145.50"),
                description="Client site inspection travel and ferry transit at Cochin Shipyard",
                project_reference="GW-NDT-MAR26",
                status=ExpenseClaimStatus.UNDER_ADMIN_REVIEW,
                submitted_at=timezone.now() - timedelta(days=1)
            )
            # Attach sample proof
            dummy_receipt = ContentFile(b"%PDF-1.4\n%Demo Great Waters Travel Receipt\n%%EOF", name="ferry_transit_receipt.pdf")
            ExpenseAttachment.objects.create(
                claim=claim,
                file=dummy_receipt,
                original_filename="ferry_transit_receipt.pdf",
                file_type="application/pdf",
                file_size=1024
            )
            self.stdout.write(f"Sample claim created: {claim.claim_number}")

        # Seed a sample leave application for GW-EMP-001 (awaiting lead recommendation)
        if not LeaveApplication.objects.filter(employee=emp1).exists():
            lv = LeaveApplication.objects.create(
                application_number="LV-202610-DEMO01",
                employee=emp1,
                branch=emp1.primary_branch,
                leave_type=lt_objs["EL"],
                start_date=date.today() + timedelta(days=10),
                end_date=date.today() + timedelta(days=12),
                is_half_day=False,
                number_of_days=Decimal("3.00"),
                reason="Annual family visit and personal leave",
                status=LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION
            )
            self.stdout.write(f"Sample leave created: {lv.application_number}")

        self.stdout.write(self.style.SUCCESS("Great Waters initial seed data created successfully!"))
        self.stdout.write(self.style.WARNING("Demo Users Created (Password for all: Password@123):"))
        self.stdout.write("  - Director: GW-DIR-001 (director@greatwaters.com) [Global Scope]")
        self.stdout.write("  - Admin Manager: GW-ADM-001 (admin@greatwaters.com) [Global Scope]")
        self.stdout.write("  - HR Coordinator: GW-HRC-001 (hr@greatwaters.com) [Global Scope]")
        self.stdout.write("  - Lead Manager: GW-ENG-001 (lead.kochi@greatwaters.com) [Kochi Branch]")
        self.stdout.write("  - Employee: GW-EMP-001 (emp.kochi@greatwaters.com) [Kochi Branch]")
        self.stdout.write("  - Employee: GW-EMP-002 (emp.dubai@greatwaters.com) [Dubai Branch]")
