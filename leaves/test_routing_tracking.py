"""Role-based approval routing and the location-scoped approval tracker."""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User, RoleChoices, EmployeeProfile
from leaves.models import LeaveType, LeaveApplication, LeaveApplicationStatus, LeaveLedgerEntry, LeaveLedgerEntryType
from notifications.models import NotificationEvent
from organization.models import Organization, Branch


class LeaveRoutingAndTrackingTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="GW", code="GW")
        self.kch = Branch.objects.create(organization=self.org, code="GW-KCH", name="Kochi")
        self.dxb = Branch.objects.create(organization=self.org, code="GW-DXB", name="Dubai")

        def mk(eid, role, branch=None, **kw):
            u = User.objects.create_user(username=eid, employee_id=eid, email=f"{eid}@gw.test", password="Password@123",
                                         role=role, primary_branch=branch or self.kch, **kw)
            return u

        self.lead = mk("L1", RoleChoices.LEAD_MANAGER)
        self.admin_mgr = mk("A1", RoleChoices.ADMIN_MANAGER)
        self.hr = mk("H1", RoleChoices.HR_COORDINATOR)
        self.hr2 = mk("H2", RoleChoices.HR_COORDINATOR)
        self.hr_dxb = mk("H3", RoleChoices.HR_COORDINATOR, self.dxb)
        self.director = mk("D1", RoleChoices.DIRECTOR, is_global_scope=True)
        self.director_kch = mk("D2", RoleChoices.DIRECTOR)
        self.emp = mk("E1", RoleChoices.EMPLOYEE)
        self.emp_dxb = mk("E2", RoleChoices.EMPLOYEE, self.dxb)
        EmployeeProfile.objects.create(user=self.emp, reporting_lead=self.lead, date_of_joining=date(2024, 1, 1))
        EmployeeProfile.objects.create(user=self.emp_dxb, date_of_joining=date(2024, 1, 1))
        self.el = LeaveType.objects.create(organization=self.org, code="EL", name="Earned", is_paid=True)
        for u in (self.lead, self.admin_mgr, self.hr, self.emp, self.emp_dxb):
            LeaveLedgerEntry.objects.create(employee=u, leave_type=self.el, entry_type=LeaveLedgerEntryType.OPENING_BALANCE,
                                            transaction_days=Decimal("10"), balance_after=Decimal("10"), reference_note="Opening")
        self.api = APIClient()

    def _apply(self, user, start="2026-09-01", end="2026-09-02"):
        self.api.force_authenticate(user)
        r = self.api.post("/api/v1/leaves/applications/", {
            "leave_type": self.el.id, "start_date": start, "end_date": end, "reason": "Personal"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return LeaveApplication.objects.get(pk=r.data["id"])

    def _track(self, user, stage="all"):
        self.api.force_authenticate(user)
        r = self.api.get(f"/api/v1/leaves/track/?stage={stage}")
        self.assertEqual(r.status_code, 200, r.data)
        return {item["id"]: item for item in r.data["results"]}, r.data["counts"]

    # ------------------------------------------------------------------ routing

    def test_employee_leave_starts_with_lead(self):
        app = self._apply(self.emp)
        self.assertEqual(app.status, LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION)
        self.assertEqual(app.starts_at_stage, "LEAD_MANAGER")

    def test_lead_and_admin_manager_leave_goes_directly_to_hr(self):
        for applicant in (self.lead, self.admin_mgr):
            app = self._apply(applicant)
            self.assertEqual(app.status, LeaveApplicationStatus.PENDING_HR_VERIFICATION)
            self.assertEqual(app.starts_at_stage, "HR_COORDINATOR")
            self.assertEqual(app.skipped_stages, ["LEAD_MANAGER"])
            self.assertIn("Routed directly to HR Coordinator", app.audit_logs.first().remarks)
        self.assertTrue(NotificationEvent.objects.filter(recipient=self.hr, event_type="LEAVE_SUBMITTED").exists())
        self.assertFalse(NotificationEvent.objects.filter(recipient=self.hr_dxb, event_type="LEAVE_SUBMITTED").exists())

        # HR queue on mobile shows it; HR verifies, Director approves.
        self.api.force_authenticate(self.hr)
        pending = self.api.get("/api/v1/approvals/pending/").data
        self.assertEqual(pending["count"], 2)
        app = LeaveApplication.objects.get(employee=self.lead)
        app.hr_review(self.hr, "VERIFIED")
        app.director_review(self.director, "APPROVED")
        self.assertEqual(LeaveLedgerEntry.get_current_balance(self.lead, self.el), Decimal("8"))

    def test_hr_leave_goes_directly_to_director_and_resubmits_there(self):
        app = self._apply(self.hr)
        self.assertEqual(app.status, LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL)
        self.assertEqual(app.skipped_stages, ["LEAD_MANAGER", "HR_COORDINATOR"])
        self.assertTrue(NotificationEvent.objects.filter(recipient=self.director, event_type="LEAVE_SUBMITTED").exists())
        # Other HR coordinators must not see it in their verification queue
        self.api.force_authenticate(self.hr2)
        self.assertEqual(self.api.get("/api/v1/approvals/pending/").data["count"], 0)

        app.director_review(self.director, "REJECTED", "Busy month")
        self.api.force_authenticate(self.hr)
        r = self.api.post(f"/api/v1/leaves/applications/{app.id}/resubmit/",
                          {"start_date": "2026-09-08", "end_date": "2026-09-08", "reason": "New date"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        app.refresh_from_db()
        self.assertEqual(app.status, LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL)

    def test_web_apply_uses_same_routing(self):
        self.client.force_login(self.lead)
        self.client.post("/leaves/", {"leave_type": self.el.id, "start_date": "2026-09-01",
                                      "end_date": "2026-09-01", "reason": "x"})
        app = LeaveApplication.objects.get(employee=self.lead)
        self.assertEqual(app.status, LeaveApplicationStatus.PENDING_HR_VERIFICATION)
        page = self.client.get(f"/leaves/{app.id}/")
        self.assertContains(page, "Not required")

    # ------------------------------------------------------------------ tracking

    def test_lead_keeps_tracking_leave_after_recommending(self):
        app = self._apply(self.emp)
        self.api.force_authenticate(self.lead)
        self.api.post(f"/api/v1/leaves/applications/{app.id}/lead-recommend/", {"action": "RECOMMENDED"}, format="json")

        items, counts = self._track(self.lead)
        self.assertEqual(items[app.id]["current_stage"], "Awaiting HR Coordinator")
        self.assertEqual(items[app.id]["my_last_action"]["action"], "RECOMMENDED")
        self.assertEqual(counts["awaiting_hr"], 1)
        self.assertIn(app.id, self._track(self.lead, "acted_by_me")[0])

        app.refresh_from_db()
        app.hr_review(self.hr, "VERIFIED")
        items, _ = self._track(self.lead, "awaiting_director")
        self.assertEqual(items[app.id]["current_stage"], "Awaiting Director")
        self.assertEqual(items[app.id]["last_action"]["by"], self.hr.full_name)

        app.director_review(self.director, "APPROVED")
        items, _ = self._track(self.lead, "approved")
        self.assertEqual(items[app.id]["current_stage"], "Approved")

        # Detail with full history + audit trail is visible to the lead
        detail = self.api.get(f"/api/v1/leaves/applications/{app.id}/").data
        self.assertEqual(len(detail["approval_history"]), 3)
        self.assertEqual(len(detail["audit_trail"]), 4)

    def test_lead_who_acted_can_still_view_after_reassignment(self):
        app = self._apply(self.emp)
        app.lead_review(self.lead, "RECOMMENDED")
        other_lead = User.objects.create_user(username="L2", employee_id="L2", email="l2@gw.test", password="x",
                                              role=RoleChoices.LEAD_MANAGER, primary_branch=self.kch)
        self.emp.profile.reporting_lead = other_lead
        self.emp.profile.save()
        self.api.force_authenticate(self.lead)
        self.assertEqual(self.api.get(f"/api/v1/leaves/applications/{app.id}/").status_code, 200)
        self.assertIn(app.id, self._track(self.lead)[0])

    def test_tracker_is_location_scoped(self):
        kch_app = self._apply(self.emp)
        dxb_app = self._apply(self.emp_dxb)

        hr_items, _ = self._track(self.hr)
        self.assertIn(kch_app.id, hr_items)
        self.assertNotIn(dxb_app.id, hr_items)

        dxb_items, _ = self._track(self.hr_dxb)
        self.assertEqual(set(dxb_items), {dxb_app.id})

        global_items, _ = self._track(self.director)
        self.assertTrue({kch_app.id, dxb_app.id} <= set(global_items))

        kch_director_items, _ = self._track(self.director_kch)
        self.assertNotIn(dxb_app.id, kch_director_items)

        self.api.force_authenticate(self.hr)
        self.assertEqual(self.api.get(f"/api/v1/leaves/applications/{dxb_app.id}/").status_code, 403)

        # Lead sees only their own team
        self.assertEqual(set(self._track(self.lead)[0]), {kch_app.id})

    def test_tracker_excludes_own_leave_and_blocks_employees(self):
        own = self._apply(self.hr)
        self.assertNotIn(own.id, self._track(self.hr)[0])
        self.api.force_authenticate(self.emp)
        self.assertEqual(self.api.get("/api/v1/leaves/track/").status_code, 403)

    def test_web_tracker_renders_for_each_approver(self):
        app = self._apply(self.emp)
        app.lead_review(self.lead, "RECOMMENDED")
        for user in (self.lead, self.hr, self.director):
            self.client.force_login(user)
            page = self.client.get("/leaves/approvals/?track=all")
            self.assertContains(page, "Approval Tracker")
            self.assertContains(page, app.application_number)
        self.client.force_login(self.hr_dxb)
        self.assertNotContains(self.client.get("/leaves/approvals/?track=all"), app.application_number)
