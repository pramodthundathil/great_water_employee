from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from accounts.models import EmployeeProfile, RoleChoices, User
from expenses.models import ExpenseCategory, ExpenseClaim, ExpenseClaimStatus
from leaves.models import (
    LeaveApplication,
    LeaveApplicationStatus,
    LeaveLedgerEntry,
    LeaveLedgerEntryType,
    LeaveType,
)
from notifications.models import NotificationEvent
from organization.models import Branch, Department, Organization


class DirectorDashboardTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Great Waters", code="GW")
        self.branch = Branch.objects.create(
            organization=self.org, code="GW-DXB", name="Dubai HQ", city="Dubai"
        )
        self.dept = Department.objects.create(
            organization=self.org, code="ENG", name="Engineering"
        )

        self.director = User.objects.create_user(
            username="GW-DIR-001",
            employee_id="GW-DIR-001",
            email="dir@greatwaters.com",
            password="Password@123",
            first_name="Elena",
            last_name="Vance",
            role=RoleChoices.DIRECTOR,
            is_global_scope=True,
            primary_branch=self.branch,
        )
        self.admin_mgr = User.objects.create_user(
            username="GW-ADM-001",
            employee_id="GW-ADM-001",
            email="adm@greatwaters.com",
            password="Password@123",
            first_name="Marco",
            last_name="Silva",
            role=RoleChoices.ADMIN_MANAGER,
            primary_branch=self.branch,
        )
        self.lead = User.objects.create_user(
            username="GW-LEAD-001",
            employee_id="GW-LEAD-001",
            email="lead@greatwaters.com",
            password="Password@123",
            first_name="Liam",
            last_name="Chen",
            role=RoleChoices.LEAD_MANAGER,
            primary_branch=self.branch,
        )
        self.hr = User.objects.create_user(
            username="GW-HRC-001",
            employee_id="GW-HRC-001",
            email="hr@greatwaters.com",
            password="Password@123",
            first_name="Nadia",
            last_name="Patel",
            role=RoleChoices.HR_COORDINATOR,
            primary_branch=self.branch,
        )
        self.emp = User.objects.create_user(
            username="GW-EMP-001",
            employee_id="GW-EMP-001",
            email="emp@greatwaters.com",
            password="Password@123",
            first_name="Sam",
            last_name="Rivera",
            role=RoleChoices.EMPLOYEE,
            primary_branch=self.branch,
        )
        EmployeeProfile.objects.create(
            user=self.emp,
            department=self.dept,
            designation="Marine Engineer",
            reporting_lead=self.lead,
        )

        self.category = ExpenseCategory.objects.create(
            organization=self.org, name="Travel", code="TRV"
        )
        self.leave_type = LeaveType.objects.create(
            organization=self.org, name="Earned Leave", code="EL", is_accrual_based=True
        )
        LeaveLedgerEntry.objects.create(
            employee=self.emp,
            leave_type=self.leave_type,
            entry_type=LeaveLedgerEntryType.OPENING_BALANCE,
            transaction_days=Decimal("12.00"),
            balance_after=Decimal("12.00"),
            reference_note="Opening balance",
        )

        # Expense waiting Admin review + Expense waiting Director final approval
        self.exp_admin = ExpenseClaim.objects.create(
            employee=self.emp,
            branch=self.branch,
            category=self.category,
            expense_date=date.today(),
            amount=Decimal("220.00"),
            description="Taxi",
            status=ExpenseClaimStatus.UNDER_ADMIN_REVIEW,
        )
        self.exp_director = ExpenseClaim.objects.create(
            employee=self.emp,
            branch=self.branch,
            category=self.category,
            expense_date=date.today(),
            amount=Decimal("450.00"),
            description="Flight",
            status=ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW,
        )

        # Leave waiting Lead + Leave waiting Director final approval
        self.leave_lead = LeaveApplication.objects.create(
            employee=self.emp,
            branch=self.branch,
            leave_type=self.leave_type,
            start_date=date.today() + timedelta(days=5),
            end_date=date.today() + timedelta(days=6),
            number_of_days=Decimal("2.00"),
            reason="Family",
            status=LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION,
        )
        self.leave_director = LeaveApplication.objects.create(
            employee=self.emp,
            branch=self.branch,
            leave_type=self.leave_type,
            start_date=date.today() + timedelta(days=10),
            end_date=date.today() + timedelta(days=12),
            number_of_days=Decimal("3.00"),
            reason="Vacation",
            status=LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL,
        )

    def test_director_dashboard_hides_self_leave_and_shows_pipeline_reminders_and_staff_analytics(self):
        self.client.force_login(self.director)
        resp = self.client.get(reverse("portal_dashboard"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()

        # No self leave status section for Director
        self.assertNotIn("My Leave Entitlements", html)

        # Approver process needed, director queues, reminders, and all-staff analytics present
        self.assertIn("Expense Approval Process Needed", html)
        self.assertIn("Leave Approval Process Needed", html)
        self.assertIn("Approver Reminders", html)
        self.assertIn("All-Staff Expense", html)
        self.assertIn(self.exp_director.claim_number, html)
        self.assertIn(self.leave_director.application_number, html)
        self.assertIn("Sam Rivera", html)
        self.assertIn("12.0 days", html)

    def test_employee_dashboard_keeps_self_leave_status(self):
        self.client.force_login(self.emp)
        resp = self.client.get(reverse("portal_dashboard"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("My Leave Entitlements", html)
        self.assertNotIn("Approver Reminders", html)

    def test_director_can_send_reminder_and_track_read_status(self):
        self.client.force_login(self.director)
        resp = self.client.post(
            reverse("portal_dashboard"),
            {
                "action_scope": "send_reminder",
                "item_type": "leave",
                "item_id": self.leave_lead.id,
            },
        )
        self.assertRedirects(resp, reverse("portal_dashboard"))
        notif = NotificationEvent.objects.filter(
            recipient=self.lead, event_type="APPROVAL_REMINDER"
        ).first()
        self.assertIsNotNone(notif)
        self.assertIn(self.leave_lead.application_number, notif.title)

        # Unread initially
        page = self.client.get(reverse("portal_dashboard")).content.decode()
        self.assertIn("Unread Alert", page)
        self.assertIn("1 reminder", page)

        # Mark as read -> dashboard reports Seen by Approver
        notif.is_read = True
        notif.save()
        page_read = self.client.get(reverse("portal_dashboard")).content.decode()
        self.assertIn("Seen by Approver", page_read)

    def test_director_can_approve_expense_and_leave_from_dashboard(self):
        self.client.force_login(self.director)
        resp_exp = self.client.post(
            reverse("portal_dashboard"),
            {
                "action_scope": "expense_decision",
                "claim_id": self.exp_director.id,
                "action": "APPROVED",
                "remarks": "Approved from executive dashboard",
            },
        )
        self.assertRedirects(resp_exp, reverse("portal_dashboard"))
        self.exp_director.refresh_from_db()
        self.assertEqual(self.exp_director.status, ExpenseClaimStatus.APPROVED_BY_DIRECTOR)

        resp_lv = self.client.post(
            reverse("portal_dashboard"),
            {
                "action_scope": "leave_decision",
                "app_id": self.leave_director.id,
                "action": "APPROVED",
                "remarks": "Approved from executive dashboard",
            },
        )
        self.assertRedirects(resp_lv, reverse("portal_dashboard"))
        self.leave_director.refresh_from_db()
        self.assertEqual(self.leave_director.status, LeaveApplicationStatus.APPROVED)
        self.assertEqual(
            LeaveLedgerEntry.get_current_balance(self.emp, self.leave_type),
            Decimal("9.00"),
        )

    def test_employee_cannot_execute_director_post_actions(self):
        self.client.force_login(self.emp)
        resp = self.client.post(
            reverse("portal_dashboard"),
            {
                "action_scope": "expense_decision",
                "claim_id": self.exp_director.id,
                "action": "APPROVED",
            },
        )
        self.assertRedirects(resp, reverse("portal_dashboard"))
        self.exp_director.refresh_from_db()
        self.assertEqual(self.exp_director.status, ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW)
