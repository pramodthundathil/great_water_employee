"""Director can revoke / re-date an approved leave (also past ones); HR can revert verification until the Director decides."""
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from leaves import test_routing_tracking as base
from leaves.models import LeaveApplicationStatus as S, LeaveAuditAction, LeaveLedgerEntry, LeaveLedgerEntryType
from leaves.services import get_employee_leave_balances, leave_correction_options
from notifications.models import NotificationEvent


class LeaveCorrectionTests(TestCase):
    setUp = base.LeaveRoutingAndTrackingTests.setUp
    _apply = base.LeaveRoutingAndTrackingTests._apply

    def _verified(self):
        app = self._apply(self.emp)  # 2026-09-01 .. 02, 2 days, in the past
        app.lead_review(self.lead, "RECOMMENDED")
        app.hr_review(self.hr, "VERIFIED")
        app.refresh_from_db()
        return app

    def _approved(self):
        app = self._verified()
        app.director_review(self.director, "APPROVED")
        app.refresh_from_db()
        self.assertEqual(app.status, S.APPROVED)
        return app

    def _balance(self):
        return LeaveLedgerEntry.get_current_balance(self.emp, self.el)

    # ------------------------------------------------------------ director revoke
    def test_director_revokes_past_approved_leave_and_balance_is_restored(self):
        app = self._approved()
        self.assertEqual(self._balance(), Decimal("8"))
        self.assertTrue(leave_correction_options(self.director, app)["is_past"])
        with self.assertRaises(ValidationError):
            app.revoke_approval(self.director, remarks="")
        app.revoke_approval(self.director, remarks="Employee was at work")
        app.refresh_from_db()
        self.assertEqual(app.status, S.PENDING_DIRECTOR_APPROVAL)
        self.assertEqual(self._balance(), Decimal("10"))
        self.assertTrue(LeaveLedgerEntry.objects.filter(employee=self.emp, entry_type=LeaveLedgerEntryType.LEAVE_REVERSAL).exists())
        self.assertTrue(app.audit_logs.filter(action=LeaveAuditAction.APPROVAL_REVOKED).exists())
        # Reversal credit does not count as "earned".
        el = next(b for b in get_employee_leave_balances(self.emp) if b["leave_type"] == self.el)
        self.assertEqual(el["earned_days"], Decimal("10"))
        # Director can now reject it outright.
        app.director_review(self.director, "REJECTED", remarks="Not taken")
        app.refresh_from_db()
        self.assertEqual(app.status, S.REJECTED_BY_DIRECTOR)
        self.assertEqual(self._balance(), Decimal("10"))

    def test_only_director_can_revoke(self):
        app = self._approved()
        for user in (self.hr, self.lead):
            with self.assertRaises(ValidationError):
                app.revoke_approval(user, remarks="x")
            self.assertIsNone(leave_correction_options(user, app))

    def test_director_edits_dates_of_approved_leave_and_ledger_follows(self):
        app = self._approved()
        app.edit_dates(self.director, date(2026, 9, 1), date(2026, 9, 1), remarks="Only one day taken")
        app.refresh_from_db()
        self.assertEqual(app.status, S.APPROVED)
        self.assertEqual(app.number_of_days, Decimal("1"))
        self.assertEqual(self._balance(), Decimal("9"))
        self.assertEqual(app.approval_history.last().action, "DATES_EDITED")

    # ------------------------------------------------------------ HR revert / edit
    def test_hr_reverts_verification_before_director_decides(self):
        app = self._verified()
        opts = leave_correction_options(self.hr, app)
        self.assertTrue(opts["can_revert"] and opts["can_edit"])
        app.revert_verification(self.hr, remarks="Wrong dates")
        app.refresh_from_db()
        self.assertEqual(app.status, S.PENDING_HR_VERIFICATION)
        self.assertTrue(app.audit_logs.filter(action=LeaveAuditAction.VERIFICATION_REVERTED).exists())

    def test_hr_edits_dates_while_awaiting_director(self):
        app = self._verified()
        app.edit_dates(self.hr, date(2026, 9, 2), date(2026, 9, 2), remarks="Corrected")
        app.refresh_from_db()
        self.assertEqual(app.status, S.PENDING_DIRECTOR_APPROVAL)
        self.assertEqual(app.number_of_days, Decimal("1"))
        self.assertEqual(self._balance(), Decimal("10"))  # nothing deducted yet

    def test_hr_cannot_revert_or_edit_after_director_approval(self):
        app = self._approved()
        self.assertIsNone(leave_correction_options(self.hr, app))
        with self.assertRaises(ValidationError):
            app.revert_verification(self.hr, remarks="x")
        with self.assertRaises(ValidationError):
            app.edit_dates(self.hr, date(2026, 9, 1), date(2026, 9, 1), remarks="x")

    # ------------------------------------------------------------ web
    def test_web_detail_actions(self):
        app = self._approved()
        self.client.force_login(self.hr)
        r = self.client.get(f"/leaves/{app.pk}/")
        self.assertNotContains(r, "Revert verification")
        self.client.post(f"/leaves/{app.pk}/", {"action": "revert_verification", "remarks": "x"})
        app.refresh_from_db()
        self.assertEqual(app.status, S.APPROVED)

        self.client.force_login(self.director)
        r = self.client.get(f"/leaves/{app.pk}/")
        self.assertContains(r, "Revoke approval")
        self.client.post(f"/leaves/{app.pk}/", {"action": "revoke_approval", "remarks": "Cancelled"})
        app.refresh_from_db()
        self.assertEqual(app.status, S.PENDING_DIRECTOR_APPROVAL)
        for who in (self.emp, self.lead, self.hr):  # employee + everyone in the approval line
            self.assertTrue(NotificationEvent.objects.filter(recipient=who, event_type="LEAVE_APPROVAL_REVOKED").exists())
        self.assertContains(self.client.get(f"/leaves/{app.pk}/"), "Approval revoked by Director")  # audit trail

        self.client.force_login(self.hr)
        self.assertContains(self.client.get(f"/leaves/{app.pk}/"), "Revert verification")
        self.client.post(f"/leaves/{app.pk}/", {"action": "revert_verification", "remarks": "Recheck"})
        app.refresh_from_db()
        self.assertEqual(app.status, S.PENDING_HR_VERIFICATION)
        for who in (self.emp, self.lead, self.director, self.director_kch):
            self.assertTrue(NotificationEvent.objects.filter(recipient=who, event_type="LEAVE_VERIFICATION_REVERTED").exists())
        self.assertContains(self.client.get(f"/leaves/{app.pk}/"), "HR verification reverted")
