"""Employees & expenses datatables: pagination, bulk actions and delete rules."""
import shutil
import tempfile
from datetime import date
from decimal import Decimal

from django.contrib.messages import get_messages
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from accounts.models import RoleChoices, User
from expenses.models import (
    ExpenseAttachment, ExpenseCategory, ExpenseClaim, ExpenseClaimStatus, ReimbursementPayment,
)
from leaves.models import LeaveType
from organization.models import Branch, Organization

TMP_MEDIA = tempfile.mkdtemp(prefix="gw-test-media-")


@override_settings(MEDIA_ROOT=TMP_MEDIA)
class Base(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TMP_MEDIA, ignore_errors=True)

    def setUp(self):
        self.org = Organization.objects.create(name="Great Waters", code="GW")
        self.dxb = Branch.objects.create(organization=self.org, code="GW-DXB", name="Dubai")
        self.kch = Branch.objects.create(organization=self.org, code="GW-KCH", name="Kochi")
        self.category = ExpenseCategory.objects.create(organization=self.org, code="TRAVEL", name="Travel")
        self.employee = self.user("emp1", RoleChoices.EMPLOYEE, self.dxb)
        self.employee2 = self.user("emp2", RoleChoices.EMPLOYEE, self.dxb)
        self.hr = self.user("hr1", RoleChoices.HR_COORDINATOR, self.dxb)
        self.director = self.user("dir1", RoleChoices.DIRECTOR, None, is_global_scope=True)

    def user(self, name, role, branch, **extra):
        return User.objects.create_user(
            username=name, employee_id=f"GW-{name.upper()}", email=f"{name}@gw.test", password="Password@123",
            role=role, primary_branch=branch, first_name=name.title(), last_name="Test", **extra,
        )

    def claim(self, employee, status=None):
        c = ExpenseClaim.objects.create(
            employee=employee, branch=employee.primary_branch, category=self.category,
            expense_date=date.today(), amount=Decimal("100.00"), description="Site visit",
        )
        ExpenseAttachment.objects.create(claim=c, file=ContentFile(b"x", name="r.pdf"), original_filename="r.pdf")
        c.submit()
        if status:
            ExpenseClaim.objects.filter(pk=c.pk).update(status=status)
            c.refresh_from_db()
        return c

    def msgs(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]


class EmployeesDatatableTests(Base):
    def test_paginates_and_sorts(self):
        for i in range(15):
            self.user(f"bulk{i:02d}", RoleChoices.EMPLOYEE, self.dxb)
        self.client.force_login(self.director)
        r = self.client.get("/employees/?page_size=10&sort=id&dir=desc")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.context["employees"]), 10)
        self.assertEqual(r.context["paginator"].count, 18)
        self.assertNotContains(r, "Tenure")
        r = self.client.get("/employees/?page=2&page_size=10&sort=bogus&dir=x&role=NOPE")
        self.assertEqual(len(r.context["employees"]), 8)

    def test_bulk_delete_deletes_clean_and_deactivates_with_history(self):
        self.claim(self.employee)  # history -> PROTECT
        self.client.force_login(self.director)
        r = self.client.post("/employees/", {
            "action": "bulk", "bulk_action": "delete",
            "selected": [self.employee.pk, self.employee2.pk, self.director.pk],
        })
        self.assertEqual(r.status_code, 302)
        self.assertFalse(User.objects.filter(pk=self.employee2.pk).exists())
        self.employee.refresh_from_db()
        self.assertFalse(self.employee.is_active)
        self.assertTrue(User.objects.filter(pk=self.director.pk, is_active=True).exists())
        text = " ".join(self.msgs(r))
        self.assertIn("1 employee(s) deleted", text)
        self.assertIn("deactivated instead", text)
        self.assertIn("your own account", text)

    def test_hr_can_bulk_activate(self):
        self.employee.is_active = False
        self.employee.save()
        self.client.force_login(self.hr)
        self.client.post("/employees/", {"action": "bulk", "bulk_action": "activate",
                                         "selected": [self.employee.pk]})
        self.employee.refresh_from_db()
        self.assertTrue(self.employee.is_active)

    def test_plain_employee_cannot_bulk(self):
        self.client.force_login(self.employee)
        r = self.client.post("/employees/", {"action": "bulk", "bulk_action": "delete",
                                             "selected": [self.employee2.pk]})
        self.assertTrue(User.objects.filter(pk=self.employee2.pk).exists())
        self.assertTrue(any("not authorised" in m for m in self.msgs(r)))


class ExpensesDatatableTests(Base):
    def test_list_excludes_drafts_and_paginates(self):
        for _ in range(12):
            self.claim(self.employee)
        ExpenseClaim.objects.create(
            employee=self.employee, branch=self.dxb, category=self.category,
            expense_date=date.today(), amount=Decimal("5"), description="draft",
        )
        self.client.force_login(self.employee)
        r = self.client.get("/expenses/?page_size=10&sort=amount&dir=asc")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["paginator"].count, 12)
        self.assertEqual(len(r.context["claims"]), 10)
        self.assertFalse(r.context["show_employee"])
        self.assertContains(r, "data-href=")
        r = self.client.get("/expenses/?page=99&sort=zz&status=DRAFT&category=x")
        self.assertEqual(r.status_code, 200)

    def test_create_requires_receipt_and_submits_immediately(self):
        self.client.force_login(self.employee)
        data = {"category": self.category.pk, "amount": "12.50", "expense_date": "2026-01-05", "description": "Taxi"}
        self.client.post("/expenses/", data)
        self.assertFalse(ExpenseClaim.objects.filter(description="Taxi").exists())
        data["receipts"] = SimpleUploadedFile("r.pdf", b"%PDF-1.4 test", content_type="application/pdf")
        self.client.post("/expenses/", data)
        claim = ExpenseClaim.objects.get(description="Taxi")
        self.assertEqual(claim.status, ExpenseClaimStatus.UNDER_ADMIN_REVIEW)

    def test_owner_bulk_delete_rules(self):
        own_pending = self.claim(self.employee)
        own_paid = self.claim(self.employee, ExpenseClaimStatus.PAID)
        own_in_director = self.claim(self.employee, ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW)
        someone_else = self.claim(self.employee2, ExpenseClaimStatus.REJECTED_BY_ADMIN)
        self.client.force_login(self.employee)
        r = self.client.post("/expenses/?q=", {"action": "bulk_delete", "selected": [
            own_pending.pk, own_paid.pk, own_in_director.pk, someone_else.pk]})
        self.assertEqual(r.status_code, 302)
        remaining = set(ExpenseClaim.objects.values_list("pk", flat=True))
        self.assertNotIn(own_pending.pk, remaining)
        self.assertTrue({own_paid.pk, own_in_director.pk, someone_else.pk} <= remaining)
        self.assertTrue(any("could not be deleted" in m for m in self.msgs(r)))

    def test_director_deletes_others_but_never_approved(self):
        rejected = self.claim(self.employee2, ExpenseClaimStatus.REJECTED_BY_DIRECTOR)
        approved = self.claim(self.employee2, ExpenseClaimStatus.APPROVED_BY_DIRECTOR)
        self.client.force_login(self.director)
        self.client.post("/expenses/", {"action": "bulk_delete", "selected": [rejected.pk, approved.pk]})
        self.assertFalse(ExpenseClaim.objects.filter(pk=rejected.pk).exists())
        self.assertTrue(ExpenseClaim.objects.filter(pk=approved.pk).exists())


class ExpenseApprovalsDatatableTests(Base):
    def setUp(self):
        super().setUp()
        self.admin = self.user("adm1", RoleChoices.ADMIN_MANAGER, self.dxb)

    def test_admin_queue_paginates_filters_and_hides_director_tab(self):
        for _ in range(12):
            self.claim(self.employee)
        self.claim(self.employee2)
        self.client.force_login(self.admin)
        r = self.client.get("/expenses/approvals/?page_size=10")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["stage"], "admin")
        self.assertEqual([t["key"] for t in r.context["stage_tabs"]], ["admin"])
        self.assertEqual(r.context["paginator"].count, 13)
        self.assertEqual(len(r.context["claims"]), 10)
        r = self.client.get("/expenses/approvals/?q=emp2")
        self.assertEqual(r.context["paginator"].count, 1)
        r = self.client.get("/expenses/approvals/?min=500")
        self.assertEqual(r.context["paginator"].count, 0)
        r = self.client.get("/expenses/approvals/?stage=director&sort=x&dir=y&min=abc&age=zzz&page=50")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["stage"], "admin")  # admin cannot open the director stage

    def test_bulk_approve_then_director_bulk_reject_requires_reason(self):
        claims = [self.claim(self.employee) for _ in range(3)]
        ids = [c.pk for c in claims]
        self.client.force_login(self.admin)
        r = self.client.post("/expenses/approvals/", {
            "bulk": "1", "decision": "APPROVED", "selected": ids, "next": "/expenses/approvals/?page_size=25"})
        self.assertEqual(r["Location"], "/expenses/approvals/?page_size=25")
        self.assertEqual(ExpenseClaim.objects.filter(pk__in=ids, status=ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW).count(), 3)

        self.client.force_login(self.director)
        r = self.client.get("/expenses/approvals/")
        self.assertEqual(r.context["stage"], "director")
        self.client.post("/expenses/approvals/", {"bulk": "1", "decision": "REJECTED", "selected": ids})
        self.assertEqual(ExpenseClaim.objects.filter(pk__in=ids, status=ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW).count(), 3)
        r = self.client.post("/expenses/approvals/", {"bulk": "1", "decision": "REJECTED", "remarks": "Missing invoices",
                                                       "selected": ids[:2]})
        self.assertEqual(ExpenseClaim.objects.filter(pk__in=ids[:2], status=ExpenseClaimStatus.REJECTED_BY_DIRECTOR).count(), 2)
        self.assertTrue(any("2 claim(s) rejected" in m for m in self.msgs(r)))

    def test_bulk_skips_claims_not_awaiting_this_reviewer(self):
        mine = self.claim(self.admin)  # admin's own claim -> director stage, and it's their own
        other = self.claim(self.employee)
        self.client.force_login(self.admin)
        r = self.client.post("/expenses/approvals/", {"bulk": "1", "decision": "APPROVED", "selected": [mine.pk, other.pk]})
        mine.refresh_from_db(); other.refresh_from_db()
        self.assertEqual(mine.status, ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW)
        self.assertEqual(other.status, ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW)
        self.assertTrue(any("not processed" in m for m in self.msgs(r)))


from leaves.models import LeaveApplication, LeaveApplicationStatus  # noqa: E402
from leaves import test_routing_tracking as _leave_tests  # noqa: E402  (module import: avoids re-running its tests here)


class LeaveApprovalsDatatableTests(TestCase):
    setUp = _leave_tests.LeaveRoutingAndTrackingTests.setUp
    _apply = _leave_tests.LeaveRoutingAndTrackingTests._apply

    def _apps(self, user, n):
        from decimal import Decimal as D
        from leaves.models import LeaveLedgerEntry, LeaveLedgerEntryType
        LeaveLedgerEntry.objects.create(employee=user, leave_type=self.el, entry_type=LeaveLedgerEntryType.OPENING_BALANCE,
                                        transaction_days=D("50"), balance_after=D("60"), reference_note="Top-up")
        return [self._apply(user, f"2026-12-{2 * i + 1:02d}", f"2026-12-{2 * i + 2:02d}") for i in range(n)]

    def test_lead_queue_paginates_filters_and_tracker_compat(self):
        self._apps(self.emp, 12)
        self.client.force_login(self.lead)
        r = self.client.get("/leaves/approvals/?page_size=10")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["view"], "lead")
        self.assertEqual(r.context["paginator"].count, 12)
        self.assertEqual(len(r.context["rows"]), 10)
        r = self.client.get("/leaves/approvals/?q=nomatch")
        self.assertEqual(r.context["paginator"].count, 0)
        r = self.client.get("/leaves/approvals/?view=director&sort=x&dir=y&starts=zz&page=99")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["view"], "lead")  # a lead cannot open the director queue
        r = self.client.get("/leaves/approvals/?track=all")
        self.assertEqual(r.context["view"], "tracker")
        self.assertContains(r, "Approval Tracker")

    def test_bulk_flow_lead_hr_director(self):
        apps = self._apps(self.emp, 3)
        ids = [a.pk for a in apps]
        self.client.force_login(self.lead)
        r = self.client.post("/leaves/approvals/", {"bulk": "1", "decision": "POSITIVE", "selected": ids,
                                                    "next": "/leaves/approvals/?view=lead&page_size=25"})
        self.assertEqual(r["Location"], "/leaves/approvals/?view=lead&page_size=25")
        self.assertEqual(LeaveApplication.objects.filter(pk__in=ids, status=LeaveApplicationStatus.PENDING_HR_VERIFICATION).count(), 3)

        self.client.force_login(self.hr)
        self.client.post("/leaves/approvals/", {"bulk": "1", "decision": "POSITIVE", "selected": ids})
        self.assertEqual(LeaveApplication.objects.filter(pk__in=ids, status=LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL).count(), 3)

        self.client.force_login(self.director)
        self.client.post("/leaves/approvals/", {"bulk": "1", "decision": "REJECTED", "selected": ids[:1]})
        self.assertEqual(LeaveApplication.objects.get(pk=ids[0]).status, LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL)
        self.client.post("/leaves/approvals/", {"bulk": "1", "decision": "REJECTED", "remarks": "Peak season", "selected": ids[:1]})
        self.assertEqual(LeaveApplication.objects.get(pk=ids[0]).status, LeaveApplicationStatus.REJECTED_BY_DIRECTOR)
        r = self.client.post("/leaves/approvals/", {"bulk": "1", "decision": "POSITIVE", "selected": ids[1:]})
        self.assertEqual(LeaveApplication.objects.filter(pk__in=ids[1:], status=LeaveApplicationStatus.APPROVED).count(), 2)
        self.assertTrue(any("2 application(s) approved" in str(m) for m in get_messages(r.wsgi_request)))

    def test_bulk_skips_applications_not_awaiting_user(self):
        mine = self._apply(self.hr)  # HR's own leave
        other_branch = self._apply(self.emp_dxb)  # Dubai branch: outside this HR's scope
        before = {a.pk: a.status for a in (mine, other_branch)}
        self.client.force_login(self.hr)
        r = self.client.post("/leaves/approvals/", {"bulk": "1", "decision": "POSITIVE", "selected": [mine.pk, other_branch.pk]})
        mine.refresh_from_db(); other_branch.refresh_from_db()
        self.assertEqual({a.pk: a.status for a in (mine, other_branch)}, before)
        self.assertTrue(any("not processed" in str(m) for m in get_messages(r.wsgi_request)))


class LeaveDetailSnapshotTests(TestCase):
    setUp = _leave_tests.LeaveRoutingAndTrackingTests.setUp
    _apply = _leave_tests.LeaveRoutingAndTrackingTests._apply

    def test_reviewer_sees_balance_and_last_three_leaves(self):
        for i, day in enumerate([3, 10, 17, 24]):
            LeaveApplication.objects.create(
                employee=self.emp, branch=self.kch, leave_type=self.el, start_date=date(2026, 3, day),
                end_date=date(2026, 3, day), number_of_days=Decimal("1"), reason=f"old {i}",
                status=LeaveApplicationStatus.APPROVED)
        app = self._apply(self.emp)  # 2 days, pending lead
        self.client.force_login(self.lead)
        r = self.client.get(f"/leaves/{app.pk}/")
        snap = r.context["snapshot"]
        self.assertEqual(snap["current"]["available_balance"], Decimal("10"))
        self.assertEqual(snap["current"]["after_this"], Decimal("8"))
        self.assertEqual(snap["current"]["other_pending"], Decimal("0"))
        self.assertEqual([x.start_date.day for x in snap["recent"]], [24, 17, 10])
        self.assertContains(r, "Last 3 leaves taken")

        self.client.force_login(self.emp)
        r = self.client.get(f"/leaves/{app.pk}/")
        self.assertIsNone(r.context["snapshot"])
        self.assertNotContains(r, "Last 3 leaves taken")


class ReimbursementsDatatableTests(Base):
    def setUp(self):
        super().setUp()
        self.admin = self.user("adm1", RoleChoices.ADMIN_MANAGER, self.dxb)
        self.admin2 = self.user("adm2", RoleChoices.ADMIN_MANAGER, self.dxb)

    def approved(self, employee=None):
        return self.claim(employee or self.employee, ExpenseClaimStatus.APPROVED_BY_DIRECTOR)

    def paid(self, ref="UTR-1", employee=None):
        c = self.approved(employee)
        c.record_payment(recorded_by=self.admin, payment_date=date.today(), payment_mode="BANK_TRANSFER",
                         payment_reference=ref)
        return c

    def test_pending_tab_paginates_searches_and_counts(self):
        for _ in range(12):
            self.approved()
        self.approved(self.employee2)
        self.paid()
        self.client.force_login(self.admin)
        r = self.client.get("/expenses/reimbursements/?page_size=10")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["tab"], "pending")
        self.assertEqual(r.context["paginator"].count, 13)
        self.assertEqual(len(r.context["claims"]), 10)
        self.assertEqual(r.context["kpis"]["pending_count"], 13)
        self.assertEqual(r.context["kpis"]["paid_count"], 1)
        r = self.client.get("/expenses/reimbursements/?page_size=10&page=2")
        self.assertEqual(len(r.context["claims"]), 3)
        r = self.client.get("/expenses/reimbursements/?q=emp2")
        self.assertEqual(r.context["paginator"].count, 1)

    def test_paid_tab_filters_by_reference_and_mode(self):
        self.paid("UTR-AAA")
        self.paid("CHQ-BBB")
        self.client.force_login(self.admin)
        r = self.client.get("/expenses/reimbursements/?tab=paid")
        self.assertEqual(r.context["paginator"].count, 2)
        r = self.client.get("/expenses/reimbursements/?tab=paid&q=CHQ")
        self.assertEqual(r.context["paginator"].count, 1)
        r = self.client.get("/expenses/reimbursements/?tab=paid&mode=CASH")
        self.assertEqual(r.context["paginator"].count, 0)
        r = self.client.get("/expenses/reimbursements/?tab=paid&sort=reference&dir=asc")
        self.assertEqual(r.context["claims"][0].payment_record.payment_reference, "CHQ-BBB")

    def test_director_bulk_deletes_approved_claims_but_admin_cannot(self):
        a, b = self.approved(), self.approved()
        self.client.force_login(self.admin)
        self.client.post("/expenses/reimbursements/", {"action": "bulk_delete", "selected": [a.pk]})
        self.assertTrue(ExpenseClaim.objects.filter(pk=a.pk).exists())
        self.client.force_login(self.director)
        self.client.post("/expenses/reimbursements/", {"action": "bulk_delete", "selected": [a.pk, b.pk]})
        self.assertFalse(ExpenseClaim.objects.filter(pk__in=[a.pk, b.pk]).exists())

    def test_paid_claims_are_never_deleted_from_pending_action(self):
        c = self.paid()
        self.client.force_login(self.director)
        self.client.post("/expenses/reimbursements/", {"action": "bulk_delete", "selected": [c.pk]})
        self.assertTrue(ExpenseClaim.objects.filter(pk=c.pk).exists())

    def test_bulk_void_returns_claims_to_queue_with_audit_entry(self):
        c1, c2 = self.paid("R1"), self.paid("R2")
        self.client.force_login(self.admin2)
        r = self.client.post("/expenses/reimbursements/", {"action": "bulk_void", "selected": [c1.pk, c2.pk]})
        self.assertEqual(r.status_code, 302)
        for c in (c1, c2):
            c.refresh_from_db()
            self.assertEqual(c.status, ExpenseClaimStatus.APPROVED_BY_DIRECTOR)
            self.assertFalse(ReimbursementPayment.objects.filter(claim=c).exists())
            self.assertTrue(c.approval_history.filter(action="PAYMENT_VOIDED").exists())
        # The claim can be paid again afterwards.
        c1.record_payment(recorded_by=self.admin2, payment_date=date.today(), payment_mode="CASH", payment_reference="R1b")

    def test_void_rules_director_and_own_claim(self):
        c = self.paid()
        own = self.paid(employee=self.admin2)
        self.client.force_login(self.director)
        self.client.post("/expenses/reimbursements/", {"action": "bulk_void", "selected": [c.pk]})
        self.assertTrue(ReimbursementPayment.objects.filter(claim=c).exists())
        self.client.force_login(self.admin2)
        self.client.post("/expenses/reimbursements/", {"action": "bulk_void", "selected": [own.pk]})
        self.assertTrue(ReimbursementPayment.objects.filter(claim=own).exists())

    def test_record_payment_still_works_and_keeps_filters(self):
        c = self.approved()
        self.client.force_login(self.admin)
        r = self.client.post("/expenses/reimbursements/", {
            "claim_id": c.pk, "payment_date": date.today().isoformat(), "payment_mode": "CHEQUE",
            "payment_reference": "CHQ-9", "next": "/expenses/reimbursements/?page_size=25",
        })
        self.assertRedirects(r, "/expenses/reimbursements/?page_size=25", fetch_redirect_response=False)
        c.refresh_from_db()
        self.assertEqual(c.status, ExpenseClaimStatus.PAID)

    def test_page_renders_both_tabs_for_each_role(self):
        self.approved()
        self.paid()
        for u in (self.admin, self.director):
            self.client.force_login(u)
            for tab in ("pending", "paid"):
                r = self.client.get(f"/expenses/reimbursements/?tab={tab}")
                self.assertEqual(r.status_code, 200)
