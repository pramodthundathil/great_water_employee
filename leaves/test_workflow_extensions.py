"""Tests for leave resubmission, partial approval at every stage, and the audit trail."""
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User, RoleChoices, EmployeeProfile
from leaves.models import (
    LeaveType, LeaveApplication, LeaveApplicationStatus, LeaveAuditLog,
    LeaveLedgerEntry, LeaveLedgerEntryType,
)
from leaves.services import resubmit_leave_application, LeaveRequestError
from organization.models import Organization, Branch


class LeaveWorkflowExtensionTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="GW", code="GW")
        self.branch = Branch.objects.create(organization=self.org, code="GW-KCH", name="Kochi")

        def mk(eid, role, **kw):
            return User.objects.create_user(username=eid, employee_id=eid, email=f"{eid}@gw.test",
                                            password="Password@123", role=role, primary_branch=self.branch, **kw)

        self.lead = mk("L1", RoleChoices.LEAD_MANAGER)
        self.hr = mk("H1", RoleChoices.HR_COORDINATOR)
        self.director = mk("D1", RoleChoices.DIRECTOR, is_global_scope=True)
        self.emp = mk("E1", RoleChoices.EMPLOYEE)
        self.other = mk("E2", RoleChoices.EMPLOYEE)
        EmployeeProfile.objects.create(user=self.emp, reporting_lead=self.lead, date_of_joining=date(2024, 1, 1))
        self.el = LeaveType.objects.create(organization=self.org, code="EL", name="Earned", is_paid=True)
        LeaveLedgerEntry.objects.create(employee=self.emp, leave_type=self.el, entry_type=LeaveLedgerEntryType.OPENING_BALANCE,
                                        transaction_days=Decimal("10"), balance_after=Decimal("10"), reference_note="Opening")
        self.api = APIClient()

    def _apply(self, start="2026-09-01", end="2026-09-05"):
        self.api.force_authenticate(self.emp)
        r = self.api.post("/api/v1/leaves/applications/", {
            "leave_type": self.el.id, "start_date": start, "end_date": end, "reason": "Family trip",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return LeaveApplication.objects.get(pk=r.data["id"])

    def _act(self, user, app, path, body):
        self.api.force_authenticate(user)
        return self.api.post(f"/api/v1/leaves/applications/{app.id}/{path}/", body, format="json")

    # ------------------------------------------------------------------ partial approval

    def test_partial_approval_at_every_stage_deducts_only_approved_days(self):
        app = self._apply()  # 5 days
        r = self._act(self.lead, app, "lead-recommend", {
            "action": "RECOMMENDED", "remarks": "Project deadline on 5th",
            "approved_start_date": "2026-09-01", "approved_end_date": "2026-09-04"})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["action"], "PARTIALLY_RECOMMENDED")

        r = self._act(self.hr, app, "hr-verify", {
            "action": "VERIFIED", "remarks": "Only 3 days by policy",
            "approved_start_date": "2026-09-02", "approved_end_date": "2026-09-04"})
        self.assertEqual(r.data["action"], "PARTIALLY_VERIFIED")

        r = self._act(self.director, app, "director-approval", {
            "action": "PARTIALLY_APPROVED", "remarks": "Two days only",
            "approved_start_date": "2026-09-03", "approved_end_date": "2026-09-04"})
        self.assertEqual(r.status_code, 200, r.data)

        app.refresh_from_db()
        self.assertEqual(app.status, LeaveApplicationStatus.APPROVED)
        self.assertEqual(app.number_of_days, Decimal("2"))
        self.assertEqual(app.requested_number_of_days, Decimal("5"))
        self.assertEqual(app.requested_start_date, date(2026, 9, 1))
        self.assertTrue(app.is_partially_approved)
        self.assertEqual(app.display_status, "Partially Approved")
        self.assertEqual(LeaveLedgerEntry.get_current_balance(self.emp, self.el), Decimal("8"))
        self.assertEqual(
            list(app.approval_history.values_list("action", flat=True)),
            ["PARTIALLY_RECOMMENDED", "PARTIALLY_VERIFIED", "PARTIALLY_APPROVED"],
        )
        self.assertEqual(app.approval_history.last().approved_days, Decimal("2"))

        self.api.force_authenticate(self.emp)
        data = self.api.get(f"/api/v1/leaves/applications/{app.id}/").data
        self.assertEqual(data["status_display"], "Partially Approved")
        self.assertTrue(data["is_partially_approved"])

    def test_full_range_with_dates_is_a_normal_approval(self):
        app = self._apply()
        r = self._act(self.lead, app, "lead-recommend", {
            "action": "RECOMMENDED", "approved_start_date": "2026-09-01", "approved_end_date": "2026-09-05"})
        self.assertEqual(r.data["action"], "RECOMMENDED")

    def test_partial_requires_remarks_and_dates_inside_range(self):
        app = self._apply()
        with self.assertRaises(ValidationError):
            app.lead_review(self.lead, "RECOMMENDED", "", date(2026, 9, 1), date(2026, 9, 2))
        with self.assertRaises(ValidationError):
            app.lead_review(self.lead, "RECOMMENDED", "x", date(2026, 8, 30), date(2026, 9, 2))
        with self.assertRaises(ValidationError):
            app.lead_review(self.lead, "PARTIALLY_RECOMMENDED", "x")
        app.refresh_from_db()
        self.assertEqual(app.status, LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION)

    def test_half_day_cannot_be_partially_approved(self):
        self.api.force_authenticate(self.emp)
        r = self.api.post("/api/v1/leaves/applications/", {
            "leave_type": self.el.id, "start_date": "2026-09-01", "end_date": "2026-09-01",
            "is_half_day": True, "half_day_period": "FIRST_HALF", "reason": "x"}, format="json")
        app = LeaveApplication.objects.get(pk=r.data["id"])
        with self.assertRaises(ValidationError):
            app.lead_review(self.lead, "RECOMMENDED", "x", date(2026, 9, 1), date(2026, 9, 1))

    def test_range_days_endpoint(self):
        app = self._apply()
        self.api.force_authenticate(self.lead)
        r = self.api.get(f"/api/v1/leaves/applications/{app.id}/range-days/?start_date=2026-09-02&end_date=2026-09-03")
        self.assertEqual(r.data["number_of_days"], 2.0)
        r = self.api.get(f"/api/v1/leaves/applications/{app.id}/range-days/?start_date=2026-08-01&end_date=2026-09-03")
        self.assertFalse(r.data["ok"])
        self.api.force_authenticate(self.other)
        self.assertEqual(self.api.get(f"/api/v1/leaves/applications/{app.id}/range-days/").status_code, 403)

    # ------------------------------------------------------------------ resubmission

    def test_rejected_leave_can_be_edited_and_resubmitted(self):
        app = self._apply()
        self._act(self.lead, app, "lead-recommend", {"action": "REJECTED", "remarks": "Too long"})
        self.api.force_authenticate(self.emp)
        detail = self.api.get(f"/api/v1/leaves/applications/{app.id}/").data
        self.assertTrue(detail["can_resubmit"])

        r = self._act(self.emp, app, "resubmit", {
            "start_date": "2026-09-01", "end_date": "2026-09-02", "reason": "Shortened trip",
            "resubmit_note": "Reduced to 2 days"})
        self.assertEqual(r.status_code, 200, r.data)
        app.refresh_from_db()
        self.assertEqual(app.status, LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION)
        self.assertEqual(app.resubmission_count, 1)
        self.assertEqual(app.number_of_days, Decimal("2"))
        self.assertEqual(app.requested_number_of_days, Decimal("2"))
        self.assertEqual(list(app.approval_history.values_list("action", flat=True)), ["REJECTED", "RESUBMITTED"])

        # It runs through the full chain again
        self.assertEqual(self._act(self.lead, app, "lead-recommend", {"action": "RECOMMENDED"}).status_code, 200)

    def test_resubmit_rules(self):
        app = self._apply()
        # pending -> not allowed
        r = self._act(self.emp, app, "resubmit", {"reason": "x"})
        self.assertEqual(r.status_code, 400)
        app.lead_review(self.lead, "REJECTED", "No")
        # someone else -> 404 (not their application)
        self.assertEqual(self._act(self.other, app, "resubmit", {"reason": "x"}).status_code, 404)
        with self.assertRaises(LeaveRequestError):
            resubmit_leave_application(app, self.other, self.el, date(2026, 9, 1), date(2026, 9, 1), "x")

    def test_resubmit_still_checks_overlap_with_other_leave(self):
        first = self._apply("2026-09-01", "2026-09-02")
        second = self._apply("2026-09-10", "2026-09-11")
        second.lead_review(self.lead, "REJECTED", "No")
        r = self._act(self.emp, second, "resubmit", {"start_date": "2026-09-02", "end_date": "2026-09-03", "reason": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data["code"], "overlap")
        self.assertTrue(first.pk)

    # ------------------------------------------------------------------ audit trail

    def test_audit_trail_records_every_event(self):
        app = self._apply()
        self._act(self.lead, app, "lead-recommend", {"action": "REJECTED", "remarks": "Too long"})
        self._act(self.emp, app, "resubmit", {"start_date": "2026-09-01", "end_date": "2026-09-03", "reason": "Shorter"})
        self._act(self.lead, app, "lead-recommend", {
            "action": "RECOMMENDED", "remarks": "Two days", "approved_start_date": "2026-09-01",
            "approved_end_date": "2026-09-02"})
        self._act(self.hr, app, "hr-verify", {"action": "VERIFIED"})
        self._act(self.director, app, "director-approval", {"action": "APPROVED"})

        actions = list(app.audit_logs.values_list("action", flat=True))
        self.assertEqual(actions, ["CREATED", "REJECTED", "RESUBMITTED", "PARTIALLY_RECOMMENDED", "VERIFIED", "APPROVED"])
        resub = app.audit_logs.get(action="RESUBMITTED")
        self.assertEqual(resub.from_status, "REJECTED_BY_LEAD")
        self.assertEqual(resub.to_status, "PENDING_LEAD_RECOMMENDATION")
        self.assertEqual(resub.changes["end_date"], ["2026-09-05", "2026-09-03"])
        self.assertEqual(resub.changes["reason"], ["Family trip", "Shorter"])
        partial = app.audit_logs.get(action="PARTIALLY_RECOMMENDED")
        self.assertEqual(partial.changes["number_of_days"], ["3", "2"])
        self.assertEqual(partial.source, "MOBILE")

        self.api.force_authenticate(self.emp)
        trail = self.api.get(f"/api/v1/leaves/applications/{app.id}/audit-trail/").data
        self.assertEqual(len(trail), 6)
        self.assertEqual(trail[0]["actor_name"], self.emp.full_name)
        self.api.force_authenticate(self.other)
        self.assertEqual(self.api.get(f"/api/v1/leaves/applications/{app.id}/audit-trail/").status_code, 403)

    def test_withdraw_is_audited_and_audit_entries_are_immutable(self):
        app = self._apply()
        self._act(self.emp, app, "withdraw", {})
        log = app.audit_logs.last()
        self.assertEqual(log.action, "WITHDRAWN")
        log.remarks = "tamper"
        with self.assertRaises(ValidationError):
            log.save()

    # ------------------------------------------------------------------ web portal

    def test_web_partial_approval_and_resubmit(self):
        app = self._apply()
        for approver in (self.lead, self.hr, self.director):
            self.client.force_login(approver)
            self.assertContains(self.client.get("/leaves/approvals/"), "Approve fewer days (partial approval)")
        self.client.force_login(self.lead)
        self.client.post("/leaves/approvals/", {
            "app_id": app.id, "action": "RECOMMENDED", "partial": "1", "remarks": "Need you on 5th",
            "approved_start_date": "2026-09-01", "approved_end_date": "2026-09-04"})
        app.refresh_from_db()
        self.assertEqual(app.number_of_days, Decimal("4"))
        self.assertEqual(app.audit_logs.last().source, "WEB")

        self.client.force_login(self.hr)
        self.client.post("/leaves/approvals/", {"app_id": app.id, "action": "REJECTED", "remarks": "Policy"})
        app.refresh_from_db()
        self.assertEqual(app.status, LeaveApplicationStatus.REJECTED_BY_HR)

        self.client.force_login(self.emp)
        page = self.client.get(f"/leaves/{app.id}/")
        self.assertContains(page, "Edit &amp; Resubmit")
        self.assertContains(page, "Audit Trail")
        self.client.post(f"/leaves/{app.id}/", {
            "action": "resubmit", "leave_type": self.el.id, "start_date": "2026-09-01",
            "end_date": "2026-09-02", "reason": "Fixed", "resubmit_note": "Two days"})
        app.refresh_from_db()
        self.assertEqual(app.status, LeaveApplicationStatus.PENDING_LEAD_RECOMMENDATION)
        self.assertEqual(app.resubmission_count, 1)
        self.assertEqual(LeaveAuditLog.objects.filter(application=app, action="RESUBMITTED").count(), 1)
