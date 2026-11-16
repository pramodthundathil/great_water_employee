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


class DashboardLeaveCalendarTests(TestCase):
    """Today's / next-15-days leave panels are scoped by role and branch."""

    def setUp(self):
        from django.utils import timezone

        self.today = timezone.localdate()
        org = Organization.objects.create(name="Great Waters", code="GW")
        self.dxb = Branch.objects.create(organization=org, code="DXB", name="Dubai", city="Dubai")
        self.auh = Branch.objects.create(organization=org, code="AUH", name="Abu Dhabi", city="Abu Dhabi")
        self.lt = LeaveType.objects.create(organization=org, name="Annual Leave", code="AL")
        self.n = 0

        self.director = self._user(RoleChoices.DIRECTOR, self.dxb, is_global_scope=True)
        self.hr_global = self._user(RoleChoices.HR_COORDINATOR, None, is_global_scope=True)
        self.hr_dxb = self._user(RoleChoices.HR_COORDINATOR, self.dxb)
        self.manager_dxb = self._user(RoleChoices.LEAD_MANAGER, self.dxb)
        self.emp_dxb = self._user(RoleChoices.EMPLOYEE, self.dxb)
        self.emp_dxb2 = self._user(RoleChoices.EMPLOYEE, self.dxb)
        self.emp_auh = self._user(RoleChoices.EMPLOYEE, self.auh)

        self.dxb_today = self._leave(self.emp_dxb, 0, 1)
        self.dxb2_upcoming = self._leave(self.emp_dxb2, 3, 4)
        self.auh_today = self._leave(self.emp_auh, -1, 0)
        self.auh_upcoming = self._leave(self.emp_auh, 15, 16, status=LeaveApplicationStatus.PENDING_HR_VERIFICATION)
        # Never shown: outside the window, rejected, or already finished.
        self._leave(self.emp_dxb, 16, 17)
        self._leave(self.emp_dxb, 2, 2, status=LeaveApplicationStatus.REJECTED_BY_LEAD)
        self._leave(self.emp_dxb, -5, -2)

    def _user(self, role, branch, **extra):
        self.n += 1
        code = f"GW-{self.n:03d}"
        return User.objects.create_user(
            username=code, employee_id=code, email=f"u{self.n}@gw.test", password="Password@123",
            first_name=f"User{self.n}", role=role, primary_branch=branch, **extra,
        )

    def _leave(self, emp, start, end, status=LeaveApplicationStatus.APPROVED):
        return LeaveApplication.objects.create(
            employee=emp, branch=emp.primary_branch, leave_type=self.lt,
            start_date=self.today + timedelta(days=start), end_date=self.today + timedelta(days=end),
            number_of_days=Decimal(end - start + 1), reason="x", status=status,
        )

    def _calendar(self, user):
        self.client.force_login(user)
        resp = self.client.get(reverse("portal_dashboard"))
        self.assertEqual(resp.status_code, 200)
        cal = resp.context["leave_calendar"]
        return {a.pk for a in cal["on_leave_today"]}, {a.pk for a in cal["upcoming"]}

    def test_director_sees_all_branches(self):
        today, upcoming = self._calendar(self.director)
        self.assertEqual(today, {self.dxb_today.pk, self.auh_today.pk})
        self.assertEqual(upcoming, {self.dxb2_upcoming.pk, self.auh_upcoming.pk})

    def test_global_hr_sees_all_branches(self):
        today, upcoming = self._calendar(self.hr_global)
        self.assertEqual(today, {self.dxb_today.pk, self.auh_today.pk})
        self.assertEqual(upcoming, {self.dxb2_upcoming.pk, self.auh_upcoming.pk})

    def test_branch_hr_sees_only_their_branch(self):
        today, upcoming = self._calendar(self.hr_dxb)
        self.assertEqual(today, {self.dxb_today.pk})
        self.assertEqual(upcoming, {self.dxb2_upcoming.pk})

    def test_manager_sees_only_their_branch(self):
        today, upcoming = self._calendar(self.manager_dxb)
        self.assertEqual(today, {self.dxb_today.pk})
        self.assertEqual(upcoming, {self.dxb2_upcoming.pk})

    def test_employee_sees_only_own_leaves(self):
        today, upcoming = self._calendar(self.emp_dxb)
        self.assertEqual(today, {self.dxb_today.pk})
        self.assertEqual(upcoming, set())
        today, upcoming = self._calendar(self.emp_dxb2)
        self.assertEqual(today, set())
        self.assertEqual(upcoming, {self.dxb2_upcoming.pk})

    def test_branch_filter_narrows_director_view(self):
        self.client.force_login(self.director)
        resp = self.client.get(reverse("portal_dashboard"), {"branch_filter": self.auh.pk})
        cal = resp.context["leave_calendar"]
        self.assertEqual([a.pk for a in cal["on_leave_today"]], [self.auh_today.pk])
        self.assertEqual([a.pk for a in cal["upcoming"]], [self.auh_upcoming.pk])


class DashboardHighlightTests(TestCase):
    """Leave KPI tiles, My Action Center and unread status alerts (web + mobile summary API)."""

    setUp = DirectorDashboardTests.setUp  # same org / users / claims / leaves fixture

    def test_leave_kpi_tiles_open_modals(self):
        self.client.force_login(self.emp)
        html = self.client.get(reverse("portal_dashboard")).content.decode()
        self.assertEqual(html.count("kpi-card-clickable"), 2)
        self.assertIn('id="leaveTodayModal"', html)
        self.assertIn('id="leaveUpcomingModal"', html)

    def test_action_center_lists_pending_items_with_employee(self):
        from leaves.models import LeaveApproval

        self.client.force_login(self.lead)
        ac = self.client.get(reverse("portal_dashboard")).context["action_center"]
        self.assertTrue(ac["is_approver"])
        self.assertEqual([(i["ref"], i["employee"]) for i in ac["items"]],
                         [(self.leave_lead.application_number, self.emp)])
        self.assertEqual(ac["items"][0]["tone"], "action")

        # Sent back to the lead's stage -> flagged as returned.
        LeaveApproval.objects.create(application=self.leave_lead, stage="HR_COORDINATOR", approver=self.hr,
                                     action="RETURNED", returned_to="LEAD_MANAGER", remarks="Check dates")
        ac = self.client.get(reverse("portal_dashboard")).context["action_center"]
        self.assertEqual(ac["returned_count"], 1)
        self.assertEqual(ac["items"][0]["tone"], "returned")

        # Employees have nothing pending with them: no action center card.
        self.client.force_login(self.emp)
        html = self.client.get(reverse("portal_dashboard")).content.decode()
        self.assertNotIn("myActionCenter", html)

    def _notify(self, obj, event_type, kind):
        prefix = "/leaves/" if kind == "leave" else "/expenses/"
        return NotificationEvent.objects.create(
            recipient=self.emp, title=event_type, message=f"Update. Remarks: Because {event_type}",
            event_type=event_type, link_url=f"{prefix}{obj.pk}/",
        )

    def test_status_alerts_rejected_and_approved(self):
        self.leave_lead.status = LeaveApplicationStatus.REJECTED_BY_LEAD
        self.leave_lead.save()
        rejected = self._notify(self.leave_lead, "LEAVE_LEAD_REJECTED", "leave")
        self.leave_director.status = LeaveApplicationStatus.APPROVED
        self.leave_director.save()
        self._notify(self.leave_director, "LEAVE_DIRECTOR_APPROVED", "leave")
        self.exp_director.status = ExpenseClaimStatus.PAID
        self.exp_director.save()
        self._notify(self.exp_director, "EXPENSE_DIRECTOR_APPROVED", "expense")
        self._notify(self.exp_director, "EXPENSE_PAID", "expense")  # newer: one alert per item

        self.client.force_login(self.emp)
        resp = self.client.get(reverse("portal_dashboard"))
        alerts = resp.context["status_alerts"]
        self.assertEqual(len(alerts), 3)
        self.assertEqual((alerts[0]["ref"], alerts[0]["tone"]), (self.leave_lead.application_number, "rejected"))
        self.assertEqual({a["tone"] for a in alerts[1:]}, {"approved"})
        self.assertContains(resp, "gw-status-item--rejected")

        # Mobile summary API carries the same alerts.
        from rest_framework.test import APIClient
        api = APIClient()
        api.force_authenticate(self.emp)
        data = api.get(reverse("api_dashboard_summary")).json()
        self.assertEqual([a["notification_id"] for a in data["status_alerts"]],
                         [a["notification_id"] for a in alerts])

        # "View" marks it read and opens the item; unsafe next falls back to notifications.
        resp = self.client.post(reverse("portal_notifications"), {
            "action": "mark_read", "notification_id": rejected.pk, "next": f"/leaves/{self.leave_lead.pk}/"})
        self.assertRedirects(resp, f"/leaves/{self.leave_lead.pk}/", fetch_redirect_response=False)
        rejected.refresh_from_db()
        self.assertTrue(rejected.is_read)
        resp = self.client.post(reverse("portal_notifications"), {"action": "mark_read", "next": "//evil.example/"})
        self.assertRedirects(resp, reverse("portal_notifications"), fetch_redirect_response=False)
        self.assertEqual(len(self.client.get(reverse("portal_dashboard")).context["status_alerts"]), 2)

    def test_status_alert_dropped_once_item_moves_on(self):
        self.leave_lead.status = LeaveApplicationStatus.REJECTED_BY_LEAD
        self.leave_lead.save()
        self._notify(self.leave_lead, "LEAVE_LEAD_REJECTED", "leave")
        self.leave_lead.status = LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION  # resubmitted
        self.leave_lead.save()
        self.client.force_login(self.emp)
        self.assertEqual(self.client.get(reverse("portal_dashboard")).context["status_alerts"], [])
