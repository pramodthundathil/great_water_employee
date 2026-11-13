from django.test import TestCase
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from decimal import Decimal
from datetime import date
from organization.models import Organization, Branch
from accounts.models import User, RoleChoices
from expenses.models import (
    ExpenseCategory, ExpenseClaim, ExpenseAttachment,
    ExpenseClaimStatus, ReimbursementPayment
)


class ExpenseWorkflowTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Great Waters", code="GW")
        self.branch = Branch.objects.create(organization=self.org, code="GW-DXB", name="Dubai Branch")
        self.category = ExpenseCategory.objects.create(organization=self.org, code="TRAVEL", name="Travel")

        self.employee = User.objects.create_user(
            username="emp1", employee_id="GW-EMP-001", email="emp1@greatwaters.com",
            password="Password@123", role=RoleChoices.EMPLOYEE, primary_branch=self.branch
        )
        self.admin_manager = User.objects.create_user(
            username="adm1", employee_id="GW-ADM-001", email="adm1@greatwaters.com",
            password="Password@123", role=RoleChoices.ADMIN_MANAGER, primary_branch=self.branch
        )
        self.director = User.objects.create_user(
            username="dir1", employee_id="GW-DIR-001", email="dir1@greatwaters.com",
            password="Password@123", role=RoleChoices.DIRECTOR, is_global_scope=True
        )

    def test_mandatory_proof_requirement_on_submit(self):
        claim = ExpenseClaim.objects.create(
            employee=self.employee,
            branch=self.branch,
            category=self.category,
            expense_date=date.today(),
            amount=Decimal("150.00"),
            description="Taxi to shipyard",
            status=ExpenseClaimStatus.DRAFT
        )

        # Attempting to submit without attachment must raise ValidationError
        with self.assertRaises(ValidationError) as ctx:
            claim.submit()
        self.assertIn("mandatory", str(ctx.exception).lower())

        # Now attach a receipt proof
        dummy_file = ContentFile(b"%PDF-1.4 sample receipt", name="taxi_receipt.pdf")
        ExpenseAttachment.objects.create(
            claim=claim, file=dummy_file, original_filename="taxi_receipt.pdf"
        )

        # Now submit should succeed
        claim.submit()
        self.assertEqual(claim.status, ExpenseClaimStatus.UNDER_ADMIN_REVIEW)

    def test_full_expense_approval_and_payment_cycle(self):
        claim = ExpenseClaim.objects.create(
            employee=self.employee,
            branch=self.branch,
            category=self.category,
            expense_date=date.today(),
            amount=Decimal("250.00"),
            description="Client engineering dinner",
            status=ExpenseClaimStatus.DRAFT
        )
        dummy_file = ContentFile(b"image data", name="bill.jpg")
        ExpenseAttachment.objects.create(claim=claim, file=dummy_file, original_filename="bill.jpg")
        claim.submit()

        # Step 1: Admin Manager Stage 1 Review
        claim.admin_review(approver=self.admin_manager, action="APPROVED", remarks="Receipt verified")
        self.assertEqual(claim.status, ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW)

        # Step 2: Director Stage 2 Review
        claim.director_review(approver=self.director, action="APPROVED", remarks="Approved per policy")
        self.assertEqual(claim.status, ExpenseClaimStatus.APPROVED_BY_DIRECTOR)

        # Step 3: Admin Manager records payment
        payment = claim.record_payment(
            recorded_by=self.admin_manager,
            payment_date=date.today(),
            payment_mode="BANK_TRANSFER",
            payment_reference="UTR-2026-9999",
            remarks="Transferred via corporate bank"
        )
        self.assertEqual(claim.status, ExpenseClaimStatus.PAID)
        self.assertEqual(payment.payment_reference, "UTR-2026-9999")

    def test_rejection_requires_remarks(self):
        claim = ExpenseClaim.objects.create(
            employee=self.employee,
            branch=self.branch,
            category=self.category,
            expense_date=date.today(),
            amount=Decimal("50.00"),
            description="Personal lunch",
            status=ExpenseClaimStatus.UNDER_ADMIN_REVIEW
        )

        # Rejection without remarks must fail
        with self.assertRaises(ValidationError):
            claim.admin_review(approver=self.admin_manager, action="REJECTED", remarks="")

        # Rejection with remarks succeeds and updates status
        claim.admin_review(approver=self.admin_manager, action="REJECTED", remarks="Personal expenses cannot be claimed")
        self.assertEqual(claim.status, ExpenseClaimStatus.REJECTED_BY_ADMIN)


class PaymentAuditTrailTests(TestCase):
    """Disbursing a reimbursement must appear in the claim's audit trail (web + API)."""

    def setUp(self):
        org = Organization.objects.create(name="Great Waters", code="GW")
        self.branch = Branch.objects.create(organization=org, code="GW-DXB", name="Dubai Branch")
        self.category = ExpenseCategory.objects.create(organization=org, code="TRAVEL", name="Travel")
        self.employee = User.objects.create_user(
            username="emp1", employee_id="GW-EMP-001", email="emp1@greatwaters.com",
            password="Password@123", role=RoleChoices.EMPLOYEE, primary_branch=self.branch,
        )
        self.admin = User.objects.create_user(
            username="adm1", employee_id="GW-ADM-001", email="adm1@greatwaters.com",
            password="Password@123", role=RoleChoices.ADMIN_MANAGER, primary_branch=self.branch,
            is_global_scope=True,
        )

    def _approved_claim(self):
        return ExpenseClaim.objects.create(
            employee=self.employee, branch=self.branch, category=self.category,
            expense_date=date.today(), amount=Decimal("120.00"), description="Taxi",
            status=ExpenseClaimStatus.APPROVED_BY_DIRECTOR,
        )

    def _assert_trail(self, claim, mode_label, ref):
        entry = claim.approval_history.last()
        self.assertIsNotNone(entry)
        self.assertEqual((entry.stage, entry.action, entry.approver), ("PAYMENT", "PAID", self.admin))
        self.assertIn(mode_label, entry.remarks)
        self.assertIn(ref, entry.remarks)

    def test_model_record_payment_writes_trail_entry(self):
        claim = self._approved_claim()
        claim.record_payment(self.admin, date(2026, 11, 12), "CASH", "CASH-1", remarks="Petty cash")
        self._assert_trail(claim, "Cash", "CASH-1")
        self.assertIn("12 Nov 2026", claim.approval_history.last().remarks)

    def test_api_record_payment_shows_in_approval_history(self):
        from rest_framework.test import APIClient
        claim = self._approved_claim()
        api = APIClient()
        api.force_authenticate(self.admin)
        r = api.post(f"/api/v1/expenses/claims/{claim.pk}/record-payment/", {
            "payment_date": "2026-11-12", "payment_mode": "BANK_TRANSFER", "payment_reference": "TRX-881",
        }, format="json")
        self.assertLess(r.status_code, 300, r.data)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ExpenseClaimStatus.PAID)
        self._assert_trail(claim, "Bank Transfer", "TRX-881")
        detail = api.get(f"/api/v1/expenses/claims/{claim.pk}/").data
        self.assertEqual(detail["approval_history"][-1]["action"], "PAID")

    def test_web_record_payment_shows_on_detail_page(self):
        claim = self._approved_claim()
        self.client.force_login(self.admin)
        self.client.post("/expenses/reimbursements/", {
            "claim_id": claim.pk, "payment_date": "2026-11-13", "payment_mode": "CHEQUE", "payment_reference": "CHQ-12",
        })
        claim.refresh_from_db()
        self.assertEqual(claim.status, ExpenseClaimStatus.PAID)
        self._assert_trail(claim, "Cheque", "CHQ-12")
        page = self.client.get(f"/expenses/{claim.pk}/")
        self.assertContains(page, "DISBURSED")
        self.assertContains(page, "CHQ-12")

    def test_backfill_migration_adds_missing_entries_once(self):
        import importlib
        from django.apps import apps as global_apps
        from expenses.models import ExpenseApproval
        claim = self._approved_claim()
        ReimbursementPayment.objects.create(
            claim=claim, recorded_by=self.admin, payment_date=date(2026, 10, 1),
            payment_mode="BANK_TRANSFER", payment_reference="OLD-1",
        )
        claim.status = ExpenseClaimStatus.PAID
        claim.save()
        mig = importlib.import_module("expenses.migrations.0002_backfill_payment_audit_trail")
        mig.backfill(global_apps, None)
        mig.backfill(global_apps, None)
        self.assertEqual(ExpenseApproval.objects.filter(claim=claim, stage="PAYMENT").count(), 1)
        self._assert_trail(claim, "Bank Transfer", "OLD-1")
