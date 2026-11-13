"""Partial-amount approval, Admin Manager direct-to-Director routing, and branch-aware expense approval."""
from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import RoleChoices, User, UserBranchScope
from expenses.models import ExpenseApproval, ExpenseAttachment, ExpenseCategory, ExpenseClaim, ExpenseClaimStatus
from notifications.models import NotificationEvent
from organization.models import Branch, Organization
from portal.api_views import pending_items_for


class Base(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Great Waters", code="GW")
        self.dxb = Branch.objects.create(organization=self.org, code="GW-DXB", name="Dubai")
        self.kch = Branch.objects.create(organization=self.org, code="GW-KCH", name="Kochi")
        self.category = ExpenseCategory.objects.create(organization=self.org, code="TRAVEL", name="Travel")
        self.employee = self.user("emp1", RoleChoices.EMPLOYEE, self.dxb)
        self.kch_employee = self.user("emp2", RoleChoices.EMPLOYEE, self.kch)
        self.admin = self.user("adm1", RoleChoices.ADMIN_MANAGER, self.dxb)
        self.admin2 = self.user("adm2", RoleChoices.ADMIN_MANAGER, self.dxb)
        self.kch_admin = self.user("adm3", RoleChoices.ADMIN_MANAGER, self.kch)
        self.director = self.user("dir1", RoleChoices.DIRECTOR, None, is_global_scope=True)
        self.kch_director = self.user("dir2", RoleChoices.DIRECTOR, self.kch)

    def user(self, name, role, branch, **extra):
        return User.objects.create_user(
            username=name, employee_id=f"GW-{name.upper()}", email=f"{name}@gw.test", password="Password@123",
            role=role, primary_branch=branch, **extra,
        )

    def claim(self, employee, amount="500.00", submit=True):
        c = ExpenseClaim.objects.create(
            employee=employee, branch=employee.primary_branch, category=self.category,
            expense_date=date.today(), amount=Decimal(amount), description="Site visit",
        )
        ExpenseAttachment.objects.create(claim=c, file=ContentFile(b"x", name="r.pdf"), original_filename="r.pdf")
        if submit:
            c.submit()
        return c


class PartialApprovalTests(Base):
    def test_admin_partial_then_director_full(self):
        c = self.claim(self.employee)
        recorded = c.admin_review(self.admin, "APPROVED", remarks="Hotel minibar not covered", approved_amount="420")
        self.assertEqual(recorded, "PARTIALLY_APPROVED")
        self.assertEqual(c.status, ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW)
        self.assertEqual(c.amount, Decimal("420.00"))
        self.assertEqual(c.claimed_amount, Decimal("500.00"))
        self.assertTrue(c.is_partially_approved)
        self.assertEqual(c.approval_history.get(stage="ADMIN_MANAGER").approved_amount, Decimal("420.00"))

        self.assertEqual(c.director_review(self.director, "APPROVED"), "APPROVED")
        self.assertEqual(c.status, ExpenseClaimStatus.APPROVED_BY_DIRECTOR)
        self.assertEqual(c.amount, Decimal("420.00"))

        c.record_payment(self.admin, date.today(), "BANK_TRANSFER", "TX-1")
        self.assertIn("420.00", c.approval_history.get(stage="PAYMENT").remarks)

    def test_director_can_reduce_further_but_not_raise(self):
        c = self.claim(self.employee)
        c.admin_review(self.admin, "APPROVED", remarks="trim", approved_amount="400")
        with self.assertRaises(ValidationError):
            c.director_review(self.director, "APPROVED", remarks="x", approved_amount="450")
        c.director_review(self.director, "PARTIALLY_APPROVED", remarks="policy cap", approved_amount="300")
        self.assertEqual(c.amount, Decimal("300.00"))
        self.assertEqual(c.status, ExpenseClaimStatus.APPROVED_BY_DIRECTOR)

    def test_partial_requires_remarks_and_positive_amount(self):
        c = self.claim(self.employee)
        with self.assertRaises(ValidationError):
            c.admin_review(self.admin, "APPROVED", approved_amount="100")
        with self.assertRaises(ValidationError):
            c.admin_review(self.admin, "APPROVED", remarks="r", approved_amount="0")
        with self.assertRaises(ValidationError):
            c.admin_review(self.admin, "PARTIALLY_APPROVED", remarks="r")
        c.refresh_from_db()
        self.assertEqual(c.status, ExpenseClaimStatus.UNDER_ADMIN_REVIEW)

    def test_same_amount_is_full_approval(self):
        c = self.claim(self.employee)
        self.assertEqual(c.admin_review(self.admin, "APPROVED", approved_amount="500.00"), "APPROVED")
        self.assertFalse(c.is_partially_approved)

    def test_resubmit_after_rejection_restores_claimed_amount(self):
        c = self.claim(self.employee)
        c.admin_review(self.admin, "APPROVED", remarks="trim", approved_amount="200")
        c.director_review(self.director, "REJECTED", remarks="missing invoice")
        c.submit()
        self.assertEqual(c.amount, Decimal("500.00"))
        self.assertEqual(c.status, ExpenseClaimStatus.UNDER_ADMIN_REVIEW)

    def test_api_partial_approval(self):
        c = self.claim(self.employee)
        api = APIClient()
        api.force_authenticate(self.admin)
        r = api.post(f"/api/v1/expenses/claims/{c.id}/admin-review/",
                     {"action": "APPROVED", "approved_amount": "350", "remarks": "cap"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("partially approved", r.data["detail"])
        r = api.get(f"/api/v1/expenses/claims/{c.id}/")
        self.assertEqual(r.data["amount"], "350.00")
        self.assertEqual(r.data["claimed_amount"], "500.00")
        self.assertTrue(r.data["is_partially_approved"])

    def test_web_partial_approval(self):
        c = self.claim(self.employee)
        self.client.force_login(self.admin)
        r = self.client.post("/expenses/approvals/", {
            "claim_id": c.id, "action": "APPROVED", "partial": "1", "approved_amount": "123.45", "remarks": "cap",
            "next": f"/expenses/{c.id}/",
        })
        self.assertRedirects(r, f"/expenses/{c.id}/", fetch_redirect_response=False)
        c.refresh_from_db()
        self.assertEqual(c.amount, Decimal("123.45"))
        self.assertEqual(c.status, ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW)

    def test_web_ignores_amount_without_partial_flag(self):
        c = self.claim(self.employee)
        self.client.force_login(self.admin)
        self.client.post("/expenses/approvals/", {"claim_id": c.id, "action": "APPROVED", "approved_amount": "10"})
        c.refresh_from_db()
        self.assertEqual(c.amount, Decimal("500.00"))


class AdminManagerRoutingTests(Base):
    def test_admin_manager_claim_goes_directly_to_director(self):
        c = self.claim(self.admin)
        self.assertEqual(c.status, ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW)
        self.assertTrue(c.skips_admin_stage)
        self.assertTrue(c.can_withdraw)
        # Not in any Admin Manager's queue; in the Director's.
        self.assertFalse([i for i in pending_items_for(self.admin2) if i["id"] == c.id and i["kind"] == "expense"])
        self.assertTrue([i for i in pending_items_for(self.director) if i["id"] == c.id and i["kind"] == "expense"])
        c.director_review(self.director, "APPROVED")
        self.assertEqual(c.status, ExpenseClaimStatus.APPROVED_BY_DIRECTOR)
        self.assertFalse(c.approval_history.filter(stage="ADMIN_MANAGER").exists())

    def test_submission_notifies_branch_directors_not_admins(self):
        c = self.claim(self.admin, submit=False)
        c.submit()
        from notifications.services import notify_expense_event
        notify_expense_event(c, "EXPENSE_SUBMITTED")
        recipients = set(NotificationEvent.objects.filter(event_type="EXPENSE_SUBMITTED").values_list("recipient__username", flat=True))
        self.assertEqual(recipients, {"dir1"})  # global director; Kochi director has no Dubai access

    def test_api_withdraw_direct_claim(self):
        c = self.claim(self.admin)
        api = APIClient()
        api.force_authenticate(self.admin)
        r = api.post(f"/api/v1/expenses/claims/{c.id}/withdraw/")
        self.assertEqual(r.status_code, 200, r.data)

    def test_admin_cannot_pay_own_claim(self):
        c = self.claim(self.admin)
        c.director_review(self.director, "APPROVED")
        api = APIClient()
        api.force_authenticate(self.admin)
        r = api.post(f"/api/v1/expenses/claims/{c.id}/record-payment/", {"payment_reference": "X"}, format="json")
        self.assertEqual(r.status_code, 403)
        api.force_authenticate(self.admin2)
        r = api.post(f"/api/v1/expenses/claims/{c.id}/record-payment/", {"payment_reference": "X"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)


class BranchAwarenessTests(Base):
    def test_admin_queue_only_shows_own_branches(self):
        dxb = self.claim(self.employee)
        kch = self.claim(self.kch_employee)
        ids = {i["id"] for i in pending_items_for(self.admin) if i["kind"] == "expense"}
        self.assertIn(dxb.id, ids)
        self.assertNotIn(kch.id, ids)

    def test_other_branch_admin_cannot_review(self):
        c = self.claim(self.employee)
        api = APIClient()
        api.force_authenticate(self.kch_admin)
        r = api.post(f"/api/v1/expenses/claims/{c.id}/admin-review/", {"action": "APPROVED"}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_scope_without_expense_rights_is_not_enough(self):
        UserBranchScope.objects.create(user=self.kch_admin, branch=self.dxb, can_manage_expenses=False)
        c = self.claim(self.employee)
        self.assertFalse(self.kch_admin.can_manage_expenses_for(self.dxb))
        self.assertNotIn(c.id, {i["id"] for i in pending_items_for(self.kch_admin) if i["kind"] == "expense"})
        UserBranchScope.objects.filter(user=self.kch_admin).update(can_manage_expenses=True)
        self.assertIn(c.id, {i["id"] for i in pending_items_for(self.kch_admin) if i["kind"] == "expense"})

    def test_branch_director_only_sees_branch(self):
        c = self.claim(self.employee)
        c.admin_review(self.admin, "APPROVED")
        self.assertNotIn(c.id, {i["id"] for i in pending_items_for(self.kch_director) if i["kind"] == "expense"})
        self.client.force_login(self.kch_director)
        self.client.post("/expenses/approvals/", {"claim_id": c.id, "action": "APPROVED"})
        c.refresh_from_db()
        self.assertEqual(c.status, ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW)

    def test_pending_feed_counts_by_kind(self):
        self.claim(self.employee)
        api = APIClient()
        api.force_authenticate(self.admin)
        r = api.get("/api/v1/approvals/pending/")
        self.assertEqual(r.data["expense_count"], 1)
        self.assertEqual(r.data["leave_count"], 0)
        self.assertEqual(r.data["items"][0]["branch_name"], "Dubai")


class TemplateRenderTests(Base):
    def test_review_pages_render(self):
        c = self.claim(self.employee)
        c.admin_review(self.admin, "APPROVED", remarks="trim", approved_amount="300")
        own = self.claim(self.admin2)
        self.client.force_login(self.director)
        for url in ("/expenses/approvals/", f"/expenses/{c.id}/", "/dashboard/", f"/expenses/{own.id}/"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertContains(r, 'id="expenseReviewModal"')
        r = self.client.get("/expenses/approvals/")
        self.assertContains(r, "of $500.00 claimed")
        self.assertContains(r, "Direct · Admin Manager")
        self.client.force_login(self.admin)
        r = self.client.get(f"/expenses/{c.id}/")
        self.assertNotContains(r, 'id="expenseReviewModal"')  # not admin's stage any more
        self.assertContains(r, "Partially approved")
        self.client.force_login(self.admin2)
        self.assertContains(self.client.get(f"/expenses/{own.id}/"), "goes directly to the Director")
