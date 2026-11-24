"""Users without access to all branches see and configure only their own branch(es)."""
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import EmployeeProfile, RoleChoices, User, UserBranchScope
from expenses.models import ExpenseCategory, ExpenseClaim, ExpenseClaimStatus
from leaves.models import (
    Holiday, HolidayCalendar, LeaveAccrualRun, LeaveApplication, LeaveApplicationStatus, LeavePolicy,
    LeavePolicyAssignment, LeaveType,
)
from leaves.services import get_employee_leave_balances, resolve_policy
from notifications.models import NotificationEvent
from notifications.services import notify_leave_event
from organization.models import Branch, Department, Organization
from portal.api_views import pending_items_for
from portal.dashboard_services import build_my_action_center, leave_calendar_scope

S = LeaveApplicationStatus


class BranchScopeBase(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Great Waters", code="GW")
        self.dxb = Branch.objects.create(organization=self.org, code="DXB", name="Dubai", currency_code="AED")
        self.kch = Branch.objects.create(organization=self.org, code="KCH", name="Kochi", currency_code="INR")
        R = RoleChoices
        self.su = User.objects.create_superuser(
            username="su", employee_id="SU", email="su@x.test", password="Password@123")
        self.director = self.user("dir", R.DIRECTOR, self.dxb)
        self.hr = self.user("hr", R.HR_COORDINATOR, self.dxb)
        self.admin = self.user("adm", R.ADMIN_MANAGER, self.dxb)
        self.lead = self.user("lead", R.LEAD_MANAGER, self.dxb)
        self.all_hr = self.user("ghr", R.HR_COORDINATOR, self.kch, is_global_scope=True)
        self.e_dxb = self.user("ed", R.EMPLOYEE, self.dxb, lead=self.lead)
        # A report that sits in another branch.
        self.e_kch = self.user("ek", R.EMPLOYEE, self.kch, lead=self.lead)
        self.el = LeaveType.objects.create(organization=self.org, code="EL", name="Earned")
        self.policy = LeavePolicy.objects.create(leave_type=self.el, name="Org EL", effective_date=date(2025, 1, 1))
        self.cat = ExpenseCategory.objects.create(organization=self.org, code="TRV", name="Travel")

    def user(self, name, role, branch, lead=None, **kw):
        u = User.objects.create_user(
            username=name, employee_id=name.upper(), email=f"{name}@x.test", password="Password@123",
            first_name=name, role=role, primary_branch=branch, **kw)
        EmployeeProfile.objects.create(user=u, reporting_lead=lead, date_of_joining=date(2024, 1, 1))
        return u

    def leave(self, emp, status):
        d = date.today() + timedelta(days=3)
        return LeaveApplication.objects.create(
            employee=emp, branch=emp.primary_branch, leave_type=self.el, start_date=d, end_date=d,
            number_of_days=1, reason="x", status=status)

    def claim(self, emp, status):
        return ExpenseClaim.objects.create(
            employee=emp, branch=emp.primary_branch, category=self.cat, expense_date=date.today(),
            amount=Decimal("10"), description="x", status=status)

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def settings(self, user, **data):
        self.client.force_login(user)
        return self.client.post("/leaves/settings/", data)


class RequestScopeTests(BranchScopeBase):
    def test_pending_queues_are_limited_to_own_branch(self):
        for status, approver in ((S.PENDING_LEAD_RECOMMENDATION, self.lead), (S.PENDING_HR_VERIFICATION, self.hr),
                                 (S.PENDING_DIRECTOR_APPROVAL, self.director)):
            own = self.leave(self.e_dxb, status)
            self.leave(self.e_kch, status)
            ids = {i["id"] for i in pending_items_for(approver) if i["kind"] == "leave"}
            self.assertEqual(ids, {own.id}, approver.role)
            self.assertEqual({i["pk"] for i in build_my_action_center(approver)["items"]}, {own.id})
        for status, approver in ((ExpenseClaimStatus.UNDER_ADMIN_REVIEW, self.admin),
                                 (ExpenseClaimStatus.UNDER_DIRECTOR_REVIEW, self.director)):
            own = self.claim(self.e_dxb, status)
            self.claim(self.e_kch, status)
            ids = {i["id"] for i in pending_items_for(approver) if i["kind"] == "expense"}
            self.assertEqual(ids, {own.id}, approver.role)

    def test_superuser_and_org_wide_users_see_all(self):
        apps = {self.leave(e, S.PENDING_HR_VERIFICATION).id for e in (self.e_dxb, self.e_kch)}
        for u in (self.su, self.all_hr):
            self.assertEqual({i["id"] for i in pending_items_for(u) if i["kind"] == "leave"}, apps)
            self.assertEqual(leave_calendar_scope(u).count(), 2)

    def test_extra_branch_scope_opens_that_branch(self):
        self.leave(self.e_kch, S.PENDING_HR_VERIFICATION)
        self.assertEqual(pending_items_for(self.hr), [])
        UserBranchScope.objects.create(user=self.hr, branch=self.kch)
        self.assertEqual(len(pending_items_for(self.hr)), 1)

    def test_leave_lists_details_and_decisions(self):
        own, other = self.leave(self.e_dxb, S.PENDING_LEAD_RECOMMENDATION), self.leave(self.e_kch, S.PENDING_LEAD_RECOMMENDATION)
        for u in (self.lead, self.hr, self.admin, self.director):
            api = self.api(u)
            self.assertEqual({r["id"] for r in api.get("/api/v1/leaves/track/").data["results"]}, {own.id}, u.role)
            self.assertEqual(api.get(f"/api/v1/leaves/applications/{other.id}/").status_code, 403)
            self.client.force_login(u)
            r = self.client.get("/leaves/approvals/?view=tracker&track=all")
            self.assertEqual([a.id for a in r.context["rows"]], [own.id])
            self.assertEqual(leave_calendar_scope(u).count(), 1)
        r = self.api(self.lead).post(f"/api/v1/leaves/applications/{other.id}/lead-recommend/", {"action": "RECOMMENDED"})
        self.assertEqual(r.status_code, 403)
        self.client.force_login(self.lead)
        self.client.post("/leaves/approvals/", {"app_id": other.id, "action": "RECOMMENDED"})
        other.refresh_from_db()
        self.assertEqual(other.status, S.PENDING_LEAD_RECOMMENDATION)

    def test_lead_ledger_and_leave_balance_of_other_branch_denied(self):
        self.assertEqual(self.api(self.lead).get(f"/api/v1/leaves/balance/?employee_id={self.e_kch.id}").status_code, 403)
        self.client.force_login(self.hr)
        r = self.client.post(f"/leaves/ledger/?emp_id={self.e_kch.id}",
                             {"leave_type_id": self.el.id, "days": "2", "remarks": "x"})
        self.assertEqual(r.status_code, 302)
        self.assertFalse(self.hr.leave_ledger_entries.exists())

    def test_stale_session_branch_never_widens_scope(self):
        self.claim(self.e_kch, ExpenseClaimStatus.UNDER_ADMIN_REVIEW)
        self.client.force_login(self.admin)
        s = self.client.session
        s["selected_branch_id"] = str(self.kch.id)
        s.save()
        self.assertEqual(self.client.get("/expenses/").context["paginator"].count, 0)
        self.assertEqual(self.client.get("/dashboard/").context["pending_expense_claims"], 0)

    def test_director_notifications_follow_branch(self):
        kch_dir = self.user("kdir", RoleChoices.DIRECTOR, self.kch)
        notify_leave_event(self.leave(self.e_dxb, S.PENDING_DIRECTOR_APPROVAL), "LEAVE_HR_VERIFIED")
        got = set(NotificationEvent.objects.values_list("recipient_id", flat=True))
        self.assertEqual(got, {self.director.id, self.su.id})
        self.assertNotIn(kch_dir.id, got)


class AccountScopeTests(BranchScopeBase):
    def test_branch_director_cannot_grant_or_take_over_org_wide_access(self):
        api = self.api(self.director)
        api.patch(f"/api/v1/organization/employees/{self.hr.id}/", {"is_global_scope": True}, format="json")
        api.patch(f"/api/v1/organization/employees/{self.director.id}/", {"is_global_scope": True}, format="json")
        self.hr.refresh_from_db(); self.director.refresh_from_db()
        self.assertFalse(self.hr.is_global_scope or self.director.is_global_scope)
        boss = self.user("boss", RoleChoices.DIRECTOR, self.dxb, is_global_scope=True)
        url = f"/api/v1/organization/employees/{boss.id}/"
        self.assertEqual(api.post(url + "set-password/", {"new_password": "Zx9!qwerty77"}).status_code, 403)
        self.assertEqual(api.patch(url, {"email": "a@b.test"}, format="json").status_code, 400)
        self.assertEqual(api.post(url + "toggle-active/").status_code, 400)

    def test_lead_must_cover_employee_branch(self):
        r = self.api(self.su).patch(f"/api/v1/organization/employees/{self.e_dxb.id}/",
                                    {"primary_branch": self.kch.id}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_branch_director_branch_management(self):
        api = self.api(self.director)
        self.assertEqual([b["code"] for b in api.get("/api/v1/organization/branches/overview/").data], ["DXB"])
        url = "/api/v1/organization/branches/"
        self.assertEqual(api.post(url + "overview/", {"code": "N", "name": "N"}).status_code, 403)
        self.assertEqual(api.patch(f"{url}{self.kch.id}/", {"name": "Z"}).status_code, 403)
        self.assertEqual(api.patch(f"{url}{self.dxb.id}/", {"phone": "1", "is_active": False}).status_code, 200)
        self.dxb.refresh_from_db()
        self.assertTrue(self.dxb.is_active and self.dxb.phone == "1")
        self.client.force_login(self.director)
        self.client.post("/branches/", {"action": "update", "branch_id": self.kch.id, "code": "KCH", "name": "Z"})
        self.client.post(f"/branches/{self.dxb.id}/", {"action": "toggle_active"})
        self.client.post("/branches/", {"action": "dept_create", "code": "D", "name": "D"})
        self.kch.refresh_from_db(); self.dxb.refresh_from_db()
        self.assertEqual(self.kch.name, "Kochi")
        self.assertTrue(self.dxb.is_active)
        self.assertFalse(Department.objects.exists())
        self.assertEqual([b.code for b in self.client.get("/branches/").context["branches"]], ["DXB"])
        self.assertEqual(self.client.get(f"/branches/{self.kch.id}/").status_code, 302)
        self.assertEqual(self.client.get(f"/branches/{self.dxb.id}/").status_code, 200)


class ConfigurationScopeTests(BranchScopeBase):
    def holiday(self, branch, name):
        cal = HolidayCalendar.objects.create(organization=self.org, branch=branch, name=name, year=date.today().year)
        return Holiday.objects.create(calendar=cal, date=date.today(), name=name)

    def test_holidays(self):
        org_h, own, other = self.holiday(None, "Org"), self.holiday(self.dxb, "Own"), self.holiday(self.kch, "Other")
        for u in (self.hr, self.director):
            self.client.force_login(u)
            r = self.client.get("/leaves/settings/?tab=holidays")
            self.assertEqual({h.name for h in r.context["holidays"]}, {"Org", "Own"})
            self.assertNotContains(r, "All branches (Organization-wide)")
            for h in (org_h, other):
                self.settings(u, action="delete_holiday", holiday_id=h.id)
                self.settings(u, action="update_holiday", holiday_id=h.id, date=str(h.date), name="Hacked")
                h.refresh_from_db()
                self.assertNotEqual(h.name, "Hacked")
            # Blank branch means their own; another branch is refused rather than becoming organisation-wide.
            self.settings(u, action="add_holiday", date="2030-01-0%d" % (1 + (u == self.hr)), name="New")
            self.settings(u, action="add_holiday", date="2030-02-01", name="Bad", branch=self.kch.id)
        self.assertEqual({h.calendar.branch_id for h in Holiday.objects.filter(name="New")}, {self.dxb.id})
        self.assertFalse(Holiday.objects.filter(name="Bad").exists())
        self.settings(self.hr, action="update_holiday", holiday_id=own.id, date=str(own.date), name="Own2")
        own.refresh_from_db()
        self.assertEqual((own.name, own.calendar.branch_id), ("Own2", self.dxb.id))
        self.settings(self.su, action="add_holiday", date="2030-03-01", name="Everyone")
        self.assertIsNone(Holiday.objects.get(name="Everyone").calendar.branch)

    def test_leave_types_and_policies_web(self):
        self.settings(self.hr, action="create_leave_type", code="HAJ", name="Hajj", is_paid="1")
        haj = LeaveType.objects.get(code="HAJ")
        self.assertEqual(haj.branch, self.dxb)
        self.settings(self.hr, action="save_policy", leave_type_id=self.el.id, name="Dubai EL", yearly_entitlement="30")
        mine = LeavePolicy.objects.get(name="Dubai EL")
        self.assertEqual(mine.branch, self.dxb)
        # Organisation-wide records cannot be changed.
        self.settings(self.hr, action="update_leave_type", leave_type_id=self.el.id, name="Hacked")
        self.settings(self.hr, action="save_policy", leave_type_id=self.el.id, policy_id=self.policy.id, name="Hacked",
                      yearly_entitlement="1")
        self.el.refresh_from_db(); self.policy.refresh_from_db()
        self.assertEqual((self.el.name, self.policy.name), ("Earned", "Org EL"))
        # The branch policy overrides the default in its branch only.
        self.assertEqual(resolve_policy(self.e_dxb, self.el), mine)
        self.assertEqual(resolve_policy(self.e_kch, self.el), self.policy)
        # The branch type reaches its branch only.
        self.assertIn(haj, LeaveType.available_for(self.e_dxb))
        self.assertNotIn(haj.id, {t["id"] for t in self.api(self.e_kch).get("/api/v1/leaves/types/").data})
        self.assertNotIn(haj, [b["leave_type"] for b in get_employee_leave_balances(self.e_kch)])
        d = str(date.today() + timedelta(days=5))
        r = self.api(self.e_kch).post("/api/v1/leaves/applications/", {
            "leave_type": haj.id, "start_date": d, "end_date": d, "reason": "x", "acknowledge_insufficient_balance": "1"})
        self.assertEqual(r.status_code, 400)
        kch_hr = self.user("khr", RoleChoices.HR_COORDINATOR, self.kch)
        self.client.cookies.clear()  # drop the previous user's flash messages
        self.client.force_login(kch_hr)
        r = self.client.get("/leaves/settings/")
        self.assertEqual([t.code for t in r.context["leave_types"]], ["EL"])
        self.assertNotContains(r, "Dubai EL")
        self.client.force_login(self.su)
        self.assertContains(self.client.get("/leaves/settings/"), "Dubai EL")
        self.settings(self.su, action="update_leave_type", leave_type_id=self.el.id, name="Earned+")
        self.el.refresh_from_db()
        self.assertEqual(self.el.name, "Earned+")

    def test_policy_assignment_and_accrual_stay_in_branch(self):
        kch_a = LeavePolicyAssignment.objects.create(policy=self.policy, scope="BRANCH", branch=self.kch)
        org_a = self.policy.assignments.get(scope="ALL")
        dept = Department.objects.create(organization=self.org, code="D", name="D")
        post = dict(action="assign_policy", policy_id=self.policy.id)
        self.settings(self.hr, **post, scope="DEPARTMENT", department_ids=[dept.id])
        self.settings(self.hr, **post, scope="BRANCH", branch_ids=[self.kch.id, self.dxb.id])
        self.settings(self.hr, **post, scope="EMPLOYEE", employee_ids=[self.e_kch.id])
        self.settings(self.hr, action="unassign_policy", policy_id=self.policy.id, assignment_ids=[kch_a.id, org_a.id])
        self.assertEqual(set(self.policy.assignments.values_list("scope", "branch_id")),
                         {("ALL", None), ("BRANCH", self.kch.id), ("BRANCH", self.dxb.id)})
        self.settings(self.hr, action="run_accrual")
        self.settings(self.hr, action="apply_policy_now", policy_id=self.policy.id)
        self.assertEqual(set(LeaveAccrualRun.objects.values_list("employee__primary_branch", flat=True)), {self.dxb.id})
        self.settings(self.su, action="run_accrual")
        self.assertTrue(LeaveAccrualRun.objects.filter(employee=self.e_kch).exists())

    def test_configuration_api(self):
        api = self.api(self.director)
        base = "/api/v1/settings/"
        self.assertEqual(api.patch(f"{base}leave-types/{self.el.id}/", {"name": "Z"}).status_code, 403)
        self.assertEqual(api.patch(f"{base}leave-policies/{self.policy.id}/", {"name": "Z"}).status_code, 403)
        self.assertEqual(api.patch(f"{base}expense-categories/{self.cat.id}/", {"name": "Z"}).status_code, 403)
        r = api.post(f"{base}leave-types/", {"code": "X", "name": "X", "branch": self.kch.id})
        self.assertEqual(r.status_code, 403)
        r = api.post(f"{base}leave-types/", {"code": "X", "name": "X"})
        self.assertEqual((r.status_code, r.data["branch"]["id"], r.data["can_edit"]), (201, self.dxb.id, True))
        r = api.post(f"{base}leave-types/{self.el.id}/policies/", {"yearly_entitlement": "5"})
        self.assertEqual(r.data["branch"]["id"], self.dxb.id)
        other = LeaveType.objects.create(organization=self.org, code="KL", name="K", branch=self.kch)
        self.assertEqual(api.patch(f"{base}leave-types/{other.id}/", {"name": "Z"}).status_code, 404)
        self.assertEqual({t["code"] for t in api.get(f"{base}leave-types/").data}, {"EL", "X"})
        su = self.api(self.su)
        self.assertEqual({t["code"] for t in su.get(f"{base}leave-types/").data}, {"EL", "X", "KL"})
        self.assertEqual(su.patch(f"{base}leave-types/{other.id}/", {"name": "Z"}).status_code, 200)
        self.assertIsNone(su.post(f"{base}leave-types/", {"code": "ORG", "name": "O"}).data["branch"])

    def test_expense_categories(self):
        self.client.force_login(self.admin)
        self.client.post("/expenses/categories/", {"action": "create", "code": "SALIK", "name": "Salik"})
        salik = ExpenseCategory.objects.get(code="SALIK")
        self.assertEqual(salik.branch, self.dxb)
        self.client.post("/expenses/categories/", {"action": "toggle", "category_id": self.cat.id})
        self.cat.refresh_from_db()
        self.assertTrue(self.cat.is_active)
        self.assertEqual(self.client.get("/expenses/categories/").status_code, 200)
        codes = lambda u: {c["code"] for c in self.api(u).get("/api/v1/expenses/categories/").data}
        self.assertEqual(codes(self.e_dxb), {"TRV", "SALIK"})
        self.assertEqual(codes(self.e_kch), {"TRV"})

    def test_every_portal_page_renders_for_each_role(self):
        self.leave(self.e_dxb, S.PENDING_HR_VERIFICATION)
        pages = ["/dashboard/", "/expenses/", "/expenses/approvals/", "/expenses/reimbursements/", "/expenses/categories/",
                 "/leaves/", "/leaves/approvals/", "/leaves/ledger/", "/leaves/settings/", "/leaves/record/",
                 "/branches/", "/employees/", "/reports/"]
        for u in (self.su, self.director, self.hr, self.admin, self.lead, self.all_hr, self.e_dxb):
            self.client.force_login(u)
            for p in pages:
                self.assertIn(self.client.get(p).status_code, (200, 302), f"{u.role} {p}")
            self.assertIn(self.api(u).get("/api/v1/dashboard/analytics/").status_code, (200, 403))
