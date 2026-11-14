"""Director / HR can return a leave to an earlier stage; approvers can change leave type and dates."""
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from leaves import test_routing_tracking as base
from leaves.models import LeaveApplicationStatus as S, LeaveAuditAction, LeaveType
from notifications.models import NotificationEvent


class ReturnAndAmendTests(TestCase):
    setUp = base.LeaveRoutingAndTrackingTests.setUp
    _apply = base.LeaveRoutingAndTrackingTests._apply

    def _to_director(self):
        app = self._apply(self.emp)
        app.lead_review(self.lead, "RECOMMENDED")
        app.hr_review(self.hr, "VERIFIED")
        app.refresh_from_db()
        self.assertEqual(app.status, S.PENDING_DIRECTOR_APPROVAL)
        return app

    # ------------------------------------------------------------------ return
    def test_director_returns_to_hr_then_flow_continues(self):
        app = self._to_director()
        self.assertEqual(app.return_targets("DIRECTOR"), ["LEAD_MANAGER", "HR_COORDINATOR"])
        with self.assertRaises(ValidationError):
            app.director_review(self.director, "RETURNED", remarks="", return_to="HR_COORDINATOR")
        with self.assertRaises(ValidationError):
            app.director_review(self.director, "RETURNED", remarks="Check", return_to="EMPLOYEE")
        app.director_review(self.director, "RETURNED", remarks="Check the medical note", return_to="HR_COORDINATOR")
        app.refresh_from_db()
        self.assertEqual(app.status, S.PENDING_HR_VERIFICATION)
        step = app.approval_history.last()
        self.assertEqual((step.action, step.returned_to), ("RETURNED", "HR_COORDINATOR"))
        self.assertEqual(step.action_label, "Returned to HR Coordinator")
        log = app.audit_logs.order_by("-id").first()
        self.assertEqual(log.action, LeaveAuditAction.RETURNED)
        self.assertEqual(log.changes["returned_to"], ["Director", "HR Coordinator"])
        # HR can verify again and the Director can approve.
        app.hr_review(self.hr, "VERIFIED", remarks="Note attached")
        app.director_review(self.director, "APPROVED")
        app.refresh_from_db()
        self.assertEqual(app.status, S.APPROVED)

    def test_hr_returns_to_lead_and_skipped_lead_is_not_offered(self):
        app = self._apply(self.emp)
        app.lead_review(self.lead, "RECOMMENDED")
        self.assertEqual(app.return_targets("HR_COORDINATOR"), ["LEAD_MANAGER"])
        app.hr_review(self.hr, "RETURNED", remarks="Wrong dates?", return_to="LEAD_MANAGER")
        app.refresh_from_db()
        self.assertEqual(app.status, S.PENDING_LEAD_RECOMMENDATION)
        # A lead's own leave skips the Lead stage, so the Director can only return it to HR.
        lead_app = self._apply(self.lead)
        self.assertEqual(lead_app.return_targets("DIRECTOR"), ["HR_COORDINATOR"])

    def test_web_return_from_detail_page_and_progress_resets(self):
        app = self._to_director()
        self.client.force_login(self.director)
        page = self.client.get(f"/leaves/{app.pk}/")
        self.assertContains(page, "Return to…")
        self.client.post("/leaves/approvals/", {"app_id": app.pk, "action": "RETURNED", "return_to": "LEAD_MANAGER",
                                                "remarks": "Lead please re-check", "next": f"/leaves/{app.pk}/"})
        app.refresh_from_db()
        self.assertEqual(app.status, S.PENDING_LEAD_RECOMMENDATION)
        self.assertTrue(NotificationEvent.objects.filter(recipient=self.lead, event_type="LEAVE_RETURNED").exists())
        page = self.client.get(f"/leaves/{app.pk}/")
        steps = {s["title"]: s["state"] for s in page.context["progress"]["steps"]}
        self.assertEqual(steps["Lead recommendation"], "current")
        self.assertEqual(steps["HR verification"], "upcoming")
        self.assertContains(page, "Returned to Lead / Manager")

    def test_api_return(self):
        app = self._to_director()
        self.api.force_authenticate(self.director)
        detail = self.api.get(f"/api/v1/leaves/applications/{app.pk}/").data
        self.assertEqual([t["value"] for t in detail["review"]["return_targets"]], ["LEAD_MANAGER", "HR_COORDINATOR"])
        self.assertTrue(detail["review"]["can_amend"])
        r = self.api.post(f"/api/v1/leaves/applications/{app.pk}/director-approval/",
                          {"action": "RETURNED", "return_to": "HR_COORDINATOR", "remarks": "Recheck"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], S.PENDING_HR_VERIFICATION)
        self.assertIn("HR Coordinator", r.data["detail"])
        hist = self.api.get(f"/api/v1/leaves/applications/{app.pk}/").data["approval_history"]
        self.assertEqual(hist[-1]["returned_to"], "HR_COORDINATOR")

    # ------------------------------------------------------------------ amend
    def test_lead_changes_type_and_dates(self):
        sl = LeaveType.objects.create(organization=self.org, code="SL", name="Sick", is_paid=True)
        app = self._apply(self.emp, "2026-09-01", "2026-09-02")
        with self.assertRaises(ValidationError):  # remarks required
            app.lead_review(self.lead, "RECOMMENDED", amend_leave_type=sl)
        app.lead_review(self.lead, "RECOMMENDED", remarks="Was sick, not personal", amend_leave_type=sl,
                        amend_start_date=date(2026, 9, 3), amend_end_date=date(2026, 9, 7))
        app.refresh_from_db()
        self.assertEqual((app.leave_type, app.start_date, app.end_date, app.number_of_days),
                         (sl, date(2026, 9, 3), date(2026, 9, 7), Decimal("5")))
        self.assertEqual((app.requested_start_date, app.requested_end_date), (date(2026, 9, 1), date(2026, 9, 2)))
        self.assertEqual(app.status, S.PENDING_HR_VERIFICATION)
        step = app.approval_history.last()
        self.assertEqual(step.changes["leave_type"], ["Earned", "Sick"])
        self.assertEqual(step.changes["end_date"], ["2026-09-02", "2026-09-07"])
        amended = app.audit_logs.get(action=LeaveAuditAction.AMENDED)
        self.assertEqual(amended.changes["number_of_days"], ["2", "5"])

    def test_amend_blocks_overlap_and_director_deducts_amended_type(self):
        other = self._apply(self.emp, "2026-10-05", "2026-10-06")
        app = self._to_director()  # 2026-09-01..02
        with self.assertRaises(ValidationError):
            app.director_review(self.director, "APPROVED", remarks="x",
                                amend_start_date=date(2026, 10, 1), amend_end_date=date(2026, 10, 5))
        self.assertTrue(other.pk)
        r = self._web_amend(app, "2026-09-01", "2026-09-01")
        app.refresh_from_db()
        self.assertEqual((app.status, app.number_of_days), (S.APPROVED, Decimal("1")))
        page = self.client.get(f"/leaves/{app.pk}/")
        self.assertContains(page, "Changed by approver")
        self.assertContains(page, "Leave type / dates changed by approver")

    def _web_amend(self, app, start, end):
        self.client.force_login(self.director)
        return self.client.post("/leaves/approvals/", {
            "app_id": app.pk, "action": "APPROVED", "amend": "1", "amend_leave_type": self.el.pk,
            "amend_start_date": start, "amend_end_date": end, "remarks": "Only one day needed"})

    def test_api_amend(self):
        app = self._apply(self.emp, "2026-09-01", "2026-09-02")
        self.api.force_authenticate(self.lead)
        r = self.api.post(f"/api/v1/leaves/applications/{app.pk}/lead-recommend/", {
            "action": "RECOMMENDED", "remarks": "Shift by a day", "amend_start_date": "2026-09-02",
            "amend_end_date": "2026-09-03"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("Leave changed", r.data["detail"])
        hist = self.api.get(f"/api/v1/leaves/applications/{app.pk}/").data
        self.assertEqual(hist["approval_history"][-1]["changes"]["start_date"], ["2026-09-01", "2026-09-02"])
        self.assertEqual(hist["audit_trail"][-2]["action"], "AMENDED")
        self.assertTrue(NotificationEvent.objects.filter(recipient=self.emp, event_type="LEAVE_AMENDED").exists())
