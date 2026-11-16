"""Tests for scoping leave policies to all employees, branches, departments and individuals."""
from datetime import date
from decimal import Decimal

from django.test import TestCase

from accounts.models import User, RoleChoices, EmployeeProfile
from leaves.models import (
    LeaveType, LeavePolicy, LeavePolicyAssignment, LeaveLedgerEntry, PolicyScope,
)
from leaves.services import (
    assign_policy, unassign_policy, resolve_policy, policy_coverage, apply_policy_now,
    run_batch_monthly_accrual, run_yearly_leave_reset,
)
from organization.models import Organization, Branch, Department


class PolicyAssignmentTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="GW", code="GW")
        self.kochi = Branch.objects.create(organization=self.org, code="GW-KCH", name="Kochi", city="Kochi")
        self.dubai = Branch.objects.create(organization=self.org, code="GW-DXB", name="Dubai", city="Dubai")
        self.ops = Department.objects.create(organization=self.org, code="OPS", name="Operations")
        self.fin = Department.objects.create(organization=self.org, code="FIN", name="Finance")

        def mk(eid, branch, dept, role=RoleChoices.EMPLOYEE):
            u = User.objects.create_user(username=eid, employee_id=eid, email=f"{eid}@gw.test",
                                         password="Password@123", role=role, primary_branch=branch)
            EmployeeProfile.objects.create(user=u, department=dept, date_of_joining=date(2024, 1, 1))
            return u

        self.k_ops = mk("K-OPS", self.kochi, self.ops)
        self.k_fin = mk("K-FIN", self.kochi, self.fin)
        self.d_ops = mk("D-OPS", self.dubai, self.ops)
        self.d_fin = mk("D-FIN", self.dubai, self.fin)
        self.hr = mk("HR1", self.kochi, self.fin, role=RoleChoices.HR_COORDINATOR)
        self.hr.is_global_scope = True
        self.hr.save()

        self.cl = LeaveType.objects.create(organization=self.org, code="CL", name="Casual", is_paid=True)
        self.default = LeavePolicy.objects.create(leave_type=self.cl, name="Default CL", accrual_frequency="YEARLY",
                                                  yearly_entitlement=Decimal("12"))
        self.dubai_pol = LeavePolicy(leave_type=self.cl, name="Dubai CL", accrual_frequency="YEARLY",
                                     yearly_entitlement=Decimal("15"))
        self.dubai_pol.save(assign_to_all=False)
        self.ops_pol = LeavePolicy(leave_type=self.cl, name="Ops CL", accrual_frequency="YEARLY",
                                   yearly_entitlement=Decimal("18"))
        self.ops_pol.save(assign_to_all=False)
        self.vip_pol = LeavePolicy(leave_type=self.cl, name="VIP CL", accrual_frequency="YEARLY",
                                   yearly_entitlement=Decimal("25"))
        self.vip_pol.save(assign_to_all=False)

    def test_new_policy_defaults_to_all_employees(self):
        self.assertTrue(LeavePolicyAssignment.objects.filter(policy=self.default, scope=PolicyScope.ALL).exists())
        self.assertFalse(self.dubai_pol.assignments.exists())
        self.assertEqual(resolve_policy(self.d_fin, self.cl), self.default)

    def test_most_specific_assignment_wins(self):
        assign_policy(self.dubai_pol, PolicyScope.BRANCH, [self.dubai])
        assign_policy(self.ops_pol, PolicyScope.DEPARTMENT, [self.ops])
        assign_policy(self.vip_pol, PolicyScope.EMPLOYEE, [self.k_fin])

        self.assertEqual(resolve_policy(self.d_fin, self.cl), self.dubai_pol)   # branch beats all
        self.assertEqual(resolve_policy(self.d_ops, self.cl), self.ops_pol)     # department beats branch
        self.assertEqual(resolve_policy(self.k_ops, self.cl), self.ops_pol)
        self.assertEqual(resolve_policy(self.k_fin, self.cl), self.vip_pol)     # individual beats everything
        self.assertEqual(resolve_policy(self.hr, self.cl), self.default)

    def test_bulk_assign_is_idempotent_and_unassign_falls_back(self):
        self.assertEqual(assign_policy(self.dubai_pol, PolicyScope.BRANCH, [self.dubai, self.kochi]), 2)
        self.assertEqual(assign_policy(self.dubai_pol, PolicyScope.BRANCH, [self.dubai, self.kochi]), 0)
        coverage = policy_coverage()
        self.assertEqual(len(coverage[self.dubai_pol.id]), 5)
        self.assertEqual(coverage[self.default.id], [])

        kochi_row = self.dubai_pol.assignments.get(branch=self.kochi)
        self.assertEqual(unassign_policy(self.dubai_pol, [kochi_row.id]), 1)
        self.assertEqual(resolve_policy(self.k_ops, self.cl), self.default)

    def test_inactive_policy_is_ignored(self):
        assign_policy(self.vip_pol, PolicyScope.EMPLOYEE, [self.k_fin])
        self.vip_pol.is_active = False
        self.vip_pol.save()
        self.assertEqual(resolve_policy(self.k_fin, self.cl), self.default)

    def test_yearly_reset_grants_each_employee_only_their_policy(self):
        assign_policy(self.dubai_pol, PolicyScope.BRANCH, [self.dubai])
        assign_policy(self.vip_pol, PolicyScope.EMPLOYEE, [self.k_fin])
        run_yearly_leave_reset(2026)
        bal = lambda u: LeaveLedgerEntry.get_current_balance(u, self.cl)
        self.assertEqual(bal(self.k_ops), Decimal("12"))
        self.assertEqual(bal(self.k_fin), Decimal("25"))
        self.assertEqual(bal(self.d_ops), Decimal("15"))
        # Re-running never double-credits.
        run_yearly_leave_reset(2026)
        self.assertEqual(bal(self.d_ops), Decimal("15"))

    def test_apply_now_does_not_double_credit_after_switching_policy(self):
        apply_policy_now(self.default, as_of_date=date(2026, 3, 1))
        self.assertEqual(LeaveLedgerEntry.get_current_balance(self.d_ops, self.cl), Decimal("12"))
        assign_policy(self.dubai_pol, PolicyScope.BRANCH, [self.dubai])
        res = apply_policy_now(self.dubai_pol, as_of_date=date(2026, 3, 1))
        self.assertEqual(res["covered"], 2)
        self.assertEqual(res["credited"], 0)  # already granted CL for 2026 under the default policy
        self.assertEqual(LeaveLedgerEntry.get_current_balance(self.d_ops, self.cl), Decimal("12"))

    def test_monthly_accrual_uses_assigned_policy_only(self):
        el = LeaveType.objects.create(organization=self.org, code="EL", name="Earned", is_paid=True)
        LeavePolicy.objects.create(leave_type=el, name="EL std", accrual_frequency="MONTHLY",
                                   monthly_accrual_rate=Decimal("1.5"))
        dxb = LeavePolicy(leave_type=el, name="EL DXB", accrual_frequency="MONTHLY", monthly_accrual_rate=Decimal("2.5"))
        dxb.save(assign_to_all=False)
        assign_policy(dxb, PolicyScope.BRANCH, [self.dubai])
        run_batch_monthly_accrual(period_key="2026-03", as_of_date=date(2026, 3, 31))
        self.assertEqual(LeaveLedgerEntry.get_current_balance(self.k_ops, el), Decimal("1.5"))
        self.assertEqual(LeaveLedgerEntry.get_current_balance(self.d_ops, el), Decimal("2.5"))

    def test_settings_page_bulk_assign_and_specific_create(self):
        self.client.force_login(self.hr)
        resp = self.client.post("/leaves/settings/", {
            "action": "assign_policy", "policy_id": self.ops_pol.id, "scope": "EMPLOYEE",
            "employee_ids": [self.k_ops.id, self.d_ops.id], "apply_now": "1", "tab": "types",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.ops_pol.assignments.count(), 2)
        self.assertEqual(LeaveLedgerEntry.get_current_balance(self.d_ops, self.cl), Decimal("18"))

        resp = self.client.post("/leaves/settings/", {
            "action": "save_policy", "leave_type_id": self.cl.id, "name": "Finance CL",
            "accrual_frequency": "YEARLY", "yearly_entitlement": "10", "applies_to": "SPECIFIC", "tab": "types",
        })
        new_pol = LeavePolicy.objects.get(name="Finance CL")
        self.assertFalse(new_pol.assignments.exists())
        self.assertIn(f"open_assign={new_pol.id}", resp["Location"])

        page = self.client.get(resp["Location"])
        self.assertContains(page, "assignPolicyDialog")
        self.assertContains(page, "Individual employees")
