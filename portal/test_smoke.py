"""Renders every portal page and mobile endpoint for every seeded role."""
import io
import shutil
import tempfile

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import User
from expenses.models import ExpenseClaim
from leaves.models import LeaveApplication
from organization.models import Branch

ROLES = ["GW-DIR-001", "GW-ADM-001", "GW-HRC-001", "GW-ENG-001", "GW-EMP-001"]
PAGES = [
    "/dashboard/", "/expenses/", "/expenses/approvals/", "/expenses/reimbursements/", "/expenses/categories/",
    "/leaves/", "/leaves/approvals/", "/leaves/ledger/", "/leaves/settings/", "/leaves/record/",
    "/branches/", "/employees/", "/reports/", "/notifications/", "/profile/",
]
API = [
    "/api/v1/auth/me/", "/api/v1/dashboard/summary/", "/api/v1/approvals/pending/",
    "/api/v1/expenses/claims/", "/api/v1/expenses/claims/?scope=mine", "/api/v1/expenses/categories/",
    "/api/v1/leaves/applications/", "/api/v1/leaves/balance/", "/api/v1/leaves/ledger/", "/api/v1/leaves/types/",
    "/api/v1/leaves/holidays/", "/api/v1/notifications/",
]

MEDIA = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=MEDIA)
class PortalSmokeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("populate_initial_data", stdout=io.StringIO())

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    def test_every_page_renders_for_every_role(self):
        claim = ExpenseClaim.objects.first()
        leave = LeaveApplication.objects.first()
        branch = Branch.objects.first()
        for emp_id in ROLES:
            user = User.objects.get(employee_id=emp_id)
            self.client.force_login(user)
            pages = PAGES + [f"/expenses/{claim.id}/", f"/leaves/{leave.id}/", f"/branches/{branch.id}/"]
            for url in pages:
                resp = self.client.get(url)
                self.assertIn(resp.status_code, (200, 302), f"{emp_id} {url} -> {resp.status_code}")

    def test_every_mobile_endpoint_responds_for_every_role(self):
        api = APIClient()
        for emp_id in ROLES:
            api.force_authenticate(User.objects.get(employee_id=emp_id))
            for url in API:
                resp = api.get(url)
                self.assertEqual(resp.status_code, 200, f"{emp_id} {url} -> {resp.status_code}")

    def test_jwt_login_with_seeded_password(self):
        resp = APIClient().post("/api/v1/auth/token/", {"username": "GW-EMP-001", "password": "Password@123"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("access", resp.data)


class StaticFilesWhiteNoiseTests(SimpleTestCase):
    def test_whitenoise_configured_and_serves_static_files(self):
        self.assertIn("whitenoise.middleware.WhiteNoiseMiddleware", settings.MIDDLEWARE)
        self.assertIn("whitenoise.runserver_nostatic", settings.INSTALLED_APPS)
        sec_idx = settings.MIDDLEWARE.index("django.middleware.security.SecurityMiddleware")
        wn_idx = settings.MIDDLEWARE.index("whitenoise.middleware.WhiteNoiseMiddleware")
        self.assertEqual(wn_idx, sec_idx + 1)

        with tempfile.TemporaryDirectory() as static_root:
            storages = {
                **settings.STORAGES,
                "staticfiles": {
                    "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
                },
            }
            with override_settings(DEBUG=False, STATIC_ROOT=static_root, STORAGES=storages):
                call_command("collectstatic", interactive=False, verbosity=0)
                resp = self.client.get("/static/css/portal.css")
                self.assertEqual(resp.status_code, 200)
                resp.close()
