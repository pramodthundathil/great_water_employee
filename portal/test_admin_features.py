from datetime import date
from decimal import Decimal

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User, RoleChoices, EmployeeProfile
from expenses.models import ExpenseCategory, ExpenseClaim, ExpenseClaimStatus
from leaves.models import LeaveType, LeavePolicy, Holiday
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


class LeaveSettingsViewTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="GW", code="GW")
        self.branch = Branch.objects.create(organization=self.org, code="GW-KCH", name="Kochi")
        self.hr = User.objects.create_user(
            username="HR1", employee_id="HR1", email="hr1@gw.test", password="Password@123",
            role=RoleChoices.HR_COORDINATOR, is_global_scope=True, primary_branch=self.branch,
        )
        self.emp = User.objects.create_user(
            username="EMP1", employee_id="EMP1", email="emp1@gw.test", password="Password@123",
            role=RoleChoices.EMPLOYEE, primary_branch=self.branch,
        )

    def test_leave_settings_access_and_invalid_query_params(self):
        self.client.force_login(self.emp)
        resp = self.client.get("/leaves/settings/")
        self.assertEqual(resp.status_code, 302)

        self.client.force_login(self.hr)
        resp = self.client.get("/leaves/settings/?tab=holidays&year=invalid")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Leave Settings")
        self.assertContains(resp, "Public Holidays")

    def test_create_update_leave_type_and_save_policy_preserves_open_type(self):
        self.client.force_login(self.hr)
        resp = self.client.post("/leaves/settings/", {
            "action": "create_leave_type",
            "code": "ml",
            "name": "Maternity Leave",
            "is_paid": "1",
            "requires_attachment": "1",
        })
        self.assertEqual(resp.status_code, 302)
        lt = LeaveType.objects.get(code="ML")
        self.assertIn(f"open_type={lt.id}", resp["Location"])

        resp2 = self.client.post("/leaves/settings/", {
            "action": "save_policy",
            "leave_type_id": lt.id,
            "name": "Standard Maternity Policy",
            "accrual_frequency": "NONE",
            "yearly_entitlement": "90",
            "monthly_accrual_rate": "0",
            "max_carry_forward": "0",
            "probation_period_months": "6",
            "is_active": "1",
        })
        self.assertEqual(resp2.status_code, 302)
        self.assertTrue(LeavePolicy.objects.filter(leave_type=lt, yearly_entitlement=Decimal("90.00")).exists())

    def test_create_leave_type_auto_creates_default_organization_when_empty(self):
        Organization.objects.all().delete()
        hr_no_branch = User.objects.create_user(
            username="HR2", employee_id="HR2", email="hr2@gw.test", password="Password@123",
            role=RoleChoices.HR_COORDINATOR, is_global_scope=True,
        )
        self.client.force_login(hr_no_branch)
        resp = self.client.post("/leaves/settings/", {
            "action": "create_leave_type",
            "code": "CL",
            "name": "Casual Leave",
            "is_paid": "1",
        })
        self.assertEqual(resp.status_code, 302)
        lt = LeaveType.objects.get(code="CL")
        self.assertIsNotNone(lt.organization_id)

    def test_add_update_and_delete_holiday_preserves_tab_and_year(self):
        self.client.force_login(self.hr)
        resp = self.client.post("/leaves/settings/", {
            "action": "add_holiday",
            "date": "2027-01-26",
            "name": "Republic Day",
            "branch": self.branch.id,
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn("tab=holidays", resp["Location"])
        self.assertIn("year=2027", resp["Location"])
        holiday = Holiday.objects.get(name="Republic Day")

        # Duplicate date warning (does not overwrite existing holiday)
        dup_resp = self.client.post("/leaves/settings/", {
            "action": "add_holiday",
            "date": "2027-01-26",
            "name": "Duplicate Day",
            "branch": self.branch.id,
        }, follow=True)
        self.assertContains(dup_resp, "already exists")

        # Update holiday
        upd_resp = self.client.post("/leaves/settings/", {
            "action": "update_holiday",
            "holiday_id": holiday.id,
            "date": "2027-01-26",
            "name": "Republic Day (India)",
            "branch": "",
            "is_optional": "1",
        })
        self.assertEqual(upd_resp.status_code, 302)
        holiday.refresh_from_db()
        self.assertEqual(holiday.name, "Republic Day (India)")
        self.assertTrue(holiday.is_optional)
        self.assertIsNone(holiday.calendar.branch)

        # Delete holiday
        del_resp = self.client.post("/leaves/settings/", {
            "action": "delete_holiday",
            "holiday_id": holiday.id,
        })
        self.assertEqual(del_resp.status_code, 302)
        self.assertIn("tab=holidays", del_resp["Location"])
        self.assertFalse(Holiday.objects.filter(pk=holiday.id).exists())


class BranchCrudAndDetailTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="GW", code="GW")
        self.branch = Branch.objects.create(
            organization=self.org, code="GW-KCH", name="Kochi", city="Kochi", country="India", phone="+91 484 12345"
        )
        self.other_branch = Branch.objects.create(
            organization=self.org, code="GW-DXB", name="Dubai", city="Dubai", country="UAE"
        )
        self.director = User.objects.create_user(
            username="DIR1", employee_id="DIR1", email="dir1@gw.test", password="Password@123",
            role=RoleChoices.DIRECTOR, is_global_scope=True, primary_branch=self.branch,
        )
        self.emp = User.objects.create_user(
            username="EMP1", employee_id="EMP1", email="emp1@gw.test", password="Password@123",
            role=RoleChoices.EMPLOYEE, primary_branch=self.branch,
        )

    def test_branch_list_and_single_detail_view_permissions(self):
        self.client.force_login(self.emp)
        self.assertEqual(self.client.get(f"/branches/{self.branch.id}/").status_code, 200)
        # Cannot view unauthorized branch or run mutations
        self.assertEqual(self.client.get(f"/branches/{self.other_branch.id}/").status_code, 302)
        self.client.post("/branches/", {"action": "create", "code": "GW-SGP", "name": "Singapore"})
        self.assertFalse(Branch.objects.filter(code="GW-SGP").exists())

        self.client.force_login(self.director)
        resp = self.client.get(f"/branches/{self.branch.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Kochi")
        self.assertContains(resp, "EMP1")

    def test_branch_create_update_toggle_and_delete_lifecycle(self):
        self.client.force_login(self.director)

        # Create + redirect to single view
        resp = self.client.post("/branches/", {
            "action": "create",
            "code": "gw-sgp",
            "name": "Singapore Hub",
            "city": "Singapore",
            "country": "Singapore",
            "phone": "+65 6123 4567",
            "address": "1 Marina Blvd",
            "is_active": "1",
            "open_after_create": "1",
        })
        created = Branch.objects.get(code="GW-SGP")
        self.assertRedirects(resp, f"/branches/{created.id}/")

        # Reject duplicate code
        dup = self.client.post("/branches/", {
            "action": "create", "code": "GW-SGP", "name": "Duplicate", "is_active": "1"
        }, follow=True)
        self.assertContains(dup, "already exists")

        # Update from detail page
        upd = self.client.post(f"/branches/{created.id}/", {
            "action": "update",
            "code": "GW-SIN",
            "name": "Singapore Maritime Hub",
            "city": "Singapore",
            "country": "SG",
            "phone": "+65 6999 0000",
            "address": "2 Maritime Sq",
            "is_active": "1",
        })
        self.assertRedirects(upd, f"/branches/{created.id}/")
        created.refresh_from_db()
        self.assertEqual(created.code, "GW-SIN")
        self.assertEqual(created.name, "Singapore Maritime Hub")

        # Toggle inactive and verify manager still sees it on /branches/
        self.client.post(f"/branches/{created.id}/", {"action": "toggle_active"})
        created.refresh_from_db()
        self.assertFalse(created.is_active)
        list_resp = self.client.get("/branches/")
        self.assertContains(list_resp, "Singapore Maritime Hub")

        # Cannot delete branch that still has employees
        blocked = self.client.post(f"/branches/{self.branch.id}/", {"action": "delete"}, follow=True)
        self.assertContains(blocked, "Cannot delete")
        self.assertTrue(Branch.objects.filter(pk=self.branch.id).exists())

        # Can permanently delete empty branch
        deleted = self.client.post(f"/branches/{created.id}/", {"action": "delete"})
        self.assertRedirects(deleted, "/branches/")
        self.assertFalse(Branch.objects.filter(pk=created.id).exists())
