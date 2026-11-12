from datetime import date
from decimal import Decimal

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User, RoleChoices, EmployeeProfile
from expenses.models import ExpenseCategory, ExpenseClaim, ExpenseClaimStatus
from organization.models import Organization, Branch


class AdminFeatureTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="GW", code="GW")
        self.branch = Branch.objects.create(organization=self.org, code="GW-KCH", name="Kochi")
        self.admin = User.objects.create_user(username="A1", employee_id="A1", email="a1@gw.test", password="Password@123",
                                              role=RoleChoices.ADMIN_MANAGER, primary_branch=self.branch)
        self.emp = User.objects.create_user(username="E1", employee_id="E1", email="e1@gw.test", password="Password@123",
                                            role=RoleChoices.EMPLOYEE, primary_branch=self.branch)
        EmployeeProfile.objects.create(user=self.emp, date_of_joining=date(2024, 1, 1))
        self.cat = ExpenseCategory.objects.create(organization=self.org, name="Travel", code="TRAVEL")

    def test_admin_can_deactivate_employee_and_login_is_blocked(self):
        self.client.force_login(self.admin)
        self.client.post(f"/employees/{self.emp.id}/edit/", {"action": "toggle_active"})
        self.emp.refresh_from_db()
        self.assertFalse(self.emp.is_active)
        resp = APIClient().post("/api/v1/auth/token/", {"username": "E1", "password": "Password@123"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_employee_can_edit_draft_but_not_after_review(self):
        claim = ExpenseClaim.objects.create(employee=self.emp, branch=self.branch, category=self.cat,
                                            expense_date=date(2026, 1, 5), amount=Decimal("10"), description="taxi")
        api = APIClient()
        api.force_authenticate(self.emp)
        r = api.patch(f"/api/v1/expenses/claims/{claim.id}/", {"amount": "25.50"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["amount"], "25.50")
        claim.status = ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW
        claim.save()
        self.assertEqual(api.patch(f"/api/v1/expenses/claims/{claim.id}/", {"amount": "1"}, format="json").status_code, 400)

    def test_upload_rejects_non_image_files_and_accepts_images(self):
        claim = ExpenseClaim.objects.create(employee=self.emp, branch=self.branch, category=self.cat,
                                            expense_date=date(2026, 1, 5), amount=Decimal("10"), description="taxi")
        api = APIClient()
        api.force_authenticate(self.emp)
        bad = SimpleUploadedFile("evil.exe", b"MZ", content_type="application/octet-stream")
        self.assertEqual(api.post(f"/api/v1/expenses/claims/{claim.id}/attachments/", {"receipts": bad}).status_code, 400)
        generic = SimpleUploadedFile("scan.pdf", b"%PDF-1.4", content_type="application/octet-stream")
        self.assertEqual(api.post(f"/api/v1/expenses/claims/{claim.id}/attachments/", {"receipts": generic}).status_code, 201)
        good = SimpleUploadedFile("bill.jpg", b"\xff\xd8\xff", content_type="image/jpeg")
        self.assertEqual(api.post(f"/api/v1/expenses/claims/{claim.id}/attachments/", {"receipts": good}).status_code, 201)

    def test_admin_cannot_review_own_claim(self):
        claim = ExpenseClaim.objects.create(employee=self.admin, branch=self.branch, category=self.cat,
                                            expense_date=date(2026, 1, 5), amount=Decimal("10"), description="x",
                                            status=ExpenseClaimStatus.UNDER_ADMIN_REVIEW)
        api = APIClient()
        api.force_authenticate(self.admin)
        r = api.post(f"/api/v1/expenses/claims/{claim.id}/admin-review/", {"action": "APPROVED"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_forgot_password_sends_email_without_revealing_accounts(self):
        api = APIClient()
        r1 = api.post("/api/v1/auth/forgot-password/", {"email": "e1@gw.test"}, format="json")
        r2 = api.post("/api/v1/auth/forgot-password/", {"email": "nobody@gw.test"}, format="json")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.data, r2.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("/reset/", mail.outbox[0].body)

    def test_web_pages_render_for_roles(self):
        self.client.force_login(self.admin)
        for url in ["/expenses/categories/", f"/employees/{self.emp.id}/edit/", "/reports/", "/password-reset/"]:
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_new_report_exports(self):
        api = APIClient()
        api.force_authenticate(self.admin)
        for rep in ["pending-approvals", "payments", "leave-balances", "expense-summary"]:
            for fmt in ["excel", "pdf"]:
                self.assertEqual(api.get(f"/api/v1/reports/{rep}/export/?format={fmt}").status_code, 200, (rep, fmt))
