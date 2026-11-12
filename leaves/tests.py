from django.test import TestCase
from django.core.exceptions import ValidationError
from decimal import Decimal
from datetime import date, timedelta
from organization.models import Organization, Branch
from accounts.models import User, RoleChoices, EmployeeProfile
from leaves.models import (
    LeaveType, LeavePolicy, LeaveApplication,
    LeaveApplicationStatus, LeaveLedgerEntry, LeaveLedgerEntryType
)
from leaves.services import (
    get_employee_leave_balances, run_accrual_for_employee,
    record_manual_leave_adjustment
)


class LeaveWorkflowAndAccrualTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Great Waters", code="GW")
        self.branch = Branch.objects.create(organization=self.org, code="GW-KCH", name="Kochi Branch")

        self.lead_manager = User.objects.create_user(
            username="lead1", employee_id="GW-ENG-001", email="lead1@greatwaters.com",
            password="Password@123", role=RoleChoices.LEAD_MANAGER, primary_branch=self.branch
        )
        self.hr_coordinator = User.objects.create_user(
            username="hr1", employee_id="GW-HRC-001", email="hr1@greatwaters.com",
            password="Password@123", role=RoleChoices.HR_COORDINATOR, is_global_scope=True
        )
        self.director = User.objects.create_user(
            username="dir1", employee_id="GW-DIR-001", email="dir1@greatwaters.com",
            password="Password@123", role=RoleChoices.DIRECTOR, is_global_scope=True
        )

        self.employee = User.objects.create_user(
            username="emp1", employee_id="GW-EMP-001", email="emp1@greatwaters.com",
            password="Password@123", role=RoleChoices.EMPLOYEE, primary_branch=self.branch
        )
        self.profile = EmployeeProfile.objects.create(
            user=self.employee, reporting_lead=self.lead_manager, date_of_joining=date(2025, 1, 1)
        )

        self.leave_type_el = LeaveType.objects.create(
            organization=self.org, code="EL", name="Earned Leave", is_paid=True, is_accrual_based=True
        )
        self.policy_el = LeavePolicy.objects.create(
            leave_type=self.leave_type_el,
            name="Standard EL Policy",
            yearly_entitlement=Decimal("18.00"),
            accrual_frequency="MONTHLY",
            monthly_accrual_rate=Decimal("1.50"),
            effective_date=date(2025, 1, 1)
        )

        # Opening balance of 10 days
        LeaveLedgerEntry.objects.create(
            employee=self.employee,
            leave_type=self.leave_type_el,
            entry_type=LeaveLedgerEntryType.OPENING_BALANCE,
            transaction_days=Decimal("10.00"),
            balance_after=Decimal("10.00"),
            reference_note="Opening Credit",
            created_by=self.hr_coordinator
        )

    def test_four_stage_leave_workflow_and_deduction(self):
        # Initial official balance is 10.0
        self.assertEqual(LeaveLedgerEntry.get_current_balance(self.employee, self.leave_type_el), Decimal("10.00"))

        # Step 1: Employee applies for 3 days
        app = LeaveApplication.objects.create(
            employee=self.employee,
            branch=self.branch,
            leave_type=self.leave_type_el,
            start_date=date.today() + timedelta(days=5),
            end_date=date.today() + timedelta(days=7),
            number_of_days=Decimal("3.00"),
            reason="Family vacation",
            status=LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION
        )

        # Balance check: Pending leave MUST NOT be deducted yet!
        self.assertEqual(LeaveLedgerEntry.get_current_balance(self.employee, self.leave_type_el), Decimal("10.00"))

        # Step 2: Lead Manager recommends
        app.lead_review(approver=self.lead_manager, action="RECOMMENDED", remarks="Deadlines managed")
        self.assertEqual(app.status, LeaveApplicationStatus.PENDING_HR_VERIFICATION)
        self.assertEqual(LeaveLedgerEntry.get_current_balance(self.employee, self.leave_type_el), Decimal("10.00"))

        # Step 3: HR Coordinator verifies
        app.hr_review(approver=self.hr_coordinator, action="VERIFIED", remarks="Policy balance verified")
        self.assertEqual(app.status, LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL)
        self.assertEqual(LeaveLedgerEntry.get_current_balance(self.employee, self.leave_type_el), Decimal("10.00"))

        # Step 4: Director Final Approval -> Deducts from official balance!
        app.director_review(approver=self.director, action="APPROVED", remarks="Approved")
        self.assertEqual(app.status, LeaveApplicationStatus.APPROVED)

        # Now official balance is 10.00 - 3.00 = 7.00
        new_balance = LeaveLedgerEntry.get_current_balance(self.employee, self.leave_type_el)
        self.assertEqual(new_balance, Decimal("7.00"))

    def test_earned_leave_accrual_engine_and_idempotency(self):
        period_key = "2026-10"
        initial_bal = LeaveLedgerEntry.get_current_balance(self.employee, self.leave_type_el)

        # First run: should credit 1.5 days
        run, msg = run_accrual_for_employee(self.employee, self.policy_el, period_key)
        self.assertIsNotNone(run)
        self.assertEqual(run.days_credited, Decimal("1.50"))
        
        bal_after = LeaveLedgerEntry.get_current_balance(self.employee, self.leave_type_el)
        self.assertEqual(bal_after, initial_bal + Decimal("1.50"))

        # Second run with same period: MUST be skipped and not create duplicate credit
        run2, msg2 = run_accrual_for_employee(self.employee, self.policy_el, period_key)
        self.assertIsNone(run2)
        self.assertIn("Already processed", msg2)

        # Balance remains unchanged
        self.assertEqual(LeaveLedgerEntry.get_current_balance(self.employee, self.leave_type_el), bal_after)

    def test_hr_manual_adjustment_with_mandatory_remarks(self):
        # Adjustment without remarks must fail
        with self.assertRaises(ValidationError):
            record_manual_leave_adjustment(
                employee=self.employee,
                leave_type=self.leave_type_el,
                days=2.0,
                remarks="",
                hr_user=self.hr_coordinator
            )

        # Adjustment with remarks succeeds
        entry = record_manual_leave_adjustment(
            employee=self.employee,
            leave_type=self.leave_type_el,
            days=2.0,
            remarks="Compensatory off for dock inspection",
            hr_user=self.hr_coordinator
        )
        self.assertEqual(entry.transaction_days, Decimal("2.0"))
        self.assertEqual(entry.balance_after, Decimal("12.00"))
