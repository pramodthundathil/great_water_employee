from rest_framework.test import APIClient

from accounts.models import RoleChoices, User
from portal.tests import DirectorDashboardTests


class MobileRoleTests(DirectorDashboardTests):
    """Mobile admin: role analytics, Director cannot raise claims/leave, superuser is always Director."""

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def test_superuser_is_always_director(self):
        su = User.objects.create_superuser(
            username="root", employee_id="GW-ROOT", email="root@greatwaters.com", password="x"
        )
        self.assertEqual(su.role, RoleChoices.DIRECTOR)
        self.assertTrue(su.is_global_scope)
        # Even if someone tries to demote the superuser's role, it snaps back to Director.
        su.role = RoleChoices.EMPLOYEE
        su.save(update_fields=["role"])
        su.refresh_from_db()
        self.assertEqual(su.role, RoleChoices.DIRECTOR)
        # Promoting an existing employee to superuser makes them Director.
        self.emp.is_superuser = True
        self.emp.save()
        self.assertEqual(self.emp.role, RoleChoices.DIRECTOR)

    def test_director_cannot_create_expense_or_leave(self):
        c = self.client_for(self.director)
        r = c.post("/api/v1/expenses/claims/", {"category": self.category.id, "amount": "10", "expense_date": "2026-01-02"})
        self.assertEqual(r.status_code, 403)
        r = c.post("/api/v1/leaves/applications/", {"leave_type": self.leave_type.id, "start_date": "2026-12-01",
                                                    "end_date": "2026-12-01", "reason": "x"})
        self.assertEqual(r.status_code, 403)

    def test_staff_can_still_create_expense(self):
        r = self.client_for(self.admin_mgr).post(
            "/api/v1/expenses/claims/",
            {"category": self.category.id, "amount": "10", "expense_date": "2026-01-02"},
        )
        self.assertEqual(r.status_code, 201, r.content)

    def test_analytics_sections_by_role(self):
        expected = {
            self.director: {"expenses", "leaves", "workforce", "branches"},
            self.admin_mgr: {"expenses"},
            self.hr: {"expenses", "leaves", "workforce", "branches"},
            self.lead: {"leaves"},
        }
        for user, sections in expected.items():
            r = self.client_for(user).get("/api/v1/dashboard/analytics/")
            self.assertEqual(r.status_code, 200, (user.role, r.content))
            self.assertEqual(set(r.data["sections"]), sections, user.role)
            self.assertTrue(r.data["kpis"])
        d = self.client_for(self.director).get("/api/v1/dashboard/analytics/?months=3").data
        self.assertEqual(len(d["expense_trend"]["labels"]), 3)
        self.assertEqual(d["pipeline"]["expense"][1]["count"], 1)
        self.assertEqual(d["branch_breakdown"][0]["code"], "GW-DXB")
        self.assertIn("expense_trend", self.client_for(self.hr).get("/api/v1/dashboard/analytics/").data)
        self.assertNotIn("leave_trend", self.client_for(self.admin_mgr).get("/api/v1/dashboard/analytics/").data)
        self.assertNotIn("expense_trend", self.client_for(self.lead).get("/api/v1/dashboard/analytics/").data)

    def test_employee_has_no_analytics(self):
        self.assertEqual(self.client_for(self.emp).get("/api/v1/dashboard/analytics/").status_code, 403)

    def test_report_download_for_director(self):
        r = self.client_for(self.director).get("/api/v1/reports/expense-summary/export/?format=pdf")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")


class ReportAccessTests(MobileRoleTests):
    """Employee: no reports. Lead: leave only. Admin Manager: claims only. HR & Director: everything."""

    ALL = ["expenses", "leaves", "expense-summary", "payments", "leave-balances", "pending-approvals"]

    def _url(self, slug):
        return f"/api/v1/reports/{slug}/export/?format=excel"

    def test_report_matrix(self):
        expected = {
            self.emp: set(),
            self.lead: {"leaves", "leave-balances", "pending-approvals"},
            self.admin_mgr: {"expenses", "expense-summary", "payments", "pending-approvals"},
            self.hr: set(self.ALL),
            self.director: set(self.ALL),
        }
        for user, allowed in expected.items():
            c = self.client_for(user)
            for slug in self.ALL:
                code = c.get(self._url(slug)).status_code
                self.assertEqual(code, 200 if slug in allowed else 403, (user.role, slug, code))

    def test_reports_page_hidden_from_employee(self):
        self.client.force_login(self.emp)
        r = self.client.get("/reports/")
        self.assertEqual(r.status_code, 302)
        self.assertNotContains(self.client.get("/dashboard/"), 'href="/reports/"')

    def test_reports_page_shows_only_allowed_cards(self):
        self.client.force_login(self.admin_mgr)
        body = self.client.get("/reports/").content.decode()
        self.assertIn("/api/v1/reports/expenses/export/", body)
        self.assertNotIn("/api/v1/reports/leaves/export/", body)
        self.client.force_login(self.lead)
        body = self.client.get("/reports/").content.decode()
        self.assertIn("/api/v1/reports/leaves/export/", body)
        self.assertNotIn("/api/v1/reports/payments/export/", body)
