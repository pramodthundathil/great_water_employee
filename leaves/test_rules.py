"""Tests for the leave rules added for the specification review."""
from datetime import date
from decimal import Decimal

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import User, RoleChoices, EmployeeProfile
from leaves.models import (
    LeaveType, LeavePolicy, LeaveApplication, LeaveApplicationStatus,
    LeaveLedgerEntry, LeaveLedgerEntryType, HolidayCalendar, Holiday,
)
from leaves.services import (
    calculate_leave_days, validate_leave_request, LeaveRequestError,
    record_leave_on_behalf, run_yearly_leave_reset,
)
from organization.models import Organization, Branch


class LeaveRuleTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="GW", code="GW")
        self.branch = Branch.objects.create(organization=self.org, code="GW-KCH", name="Kochi")
        self.other_branch = Branch.objects.create(organization=self.org, code="GW-DXB", name="Dubai")
        mk = lambda eid, role, **kw: User.objects.create_user(
            username=eid, employee_id=eid, email=f"{eid}@gw.test", password="Password@123", role=role, **kw)
        self.lead = mk("L1", RoleChoices.LEAD_MANAGER, primary_branch=self.branch)
        self.other_lead = mk("L2", RoleChoices.LEAD_MANAGER, primary_branch=self.branch)
        self.hr = mk("H1", RoleChoices.HR_COORDINATOR, primary_branch=self.branch)
        self.emp = mk("E1", RoleChoices.EMPLOYEE, primary_branch=self.branch)
        EmployeeProfile.objects.create(user=self.emp, reporting_lead=self.lead, date_of_joining=date(2024, 1, 1))
        self.el = LeaveType.objects.create(organization=self.org, code="EL", name="Earned", is_paid=True, is_accrual_based=True)
        self.cl = LeaveType.objects.create(organization=self.org, code="CL", name="Casual", is_paid=True)
        self.lwp = LeaveType.objects.create(organization=self.org, code="LWP", name="Unpaid", is_paid=False)
        self.el_policy = LeavePolicy.objects.create(leave_type=self.el, name="EL", accrual_frequency="MONTHLY",
                                                    monthly_accrual_rate=Decimal("1.5"), max_carry_forward=Decimal("5"))
        LeavePolicy.objects.create(leave_type=self.cl, name="CL", accrual_frequency="NONE",
                                   yearly_entitlement=Decimal("12"), max_carry_forward=Decimal("0"))
        LeaveLedgerEntry.objects.create(employee=self.emp, leave_type=self.el, entry_type=LeaveLedgerEntryType.OPENING_BALANCE,
                                        transaction_days=Decimal("8"), balance_after=Decimal("8"), reference_note="Opening")
        cal = HolidayCalendar.objects.create(organization=self.org, branch=self.branch, name="KCH", year=2026)
        Holiday.objects.create(calendar=cal, date=date(2026, 8, 15), name="Independence Day")
        dxb = HolidayCalendar.objects.create(organization=self.org, branch=self.other_branch, name="DXB", year=2026)
        Holiday.objects.create(calendar=dxb, date=date(2026, 8, 14), name="Dubai only")

    def test_public_holiday_of_own_branch_is_not_counted(self):
        days = calculate_leave_days(self.branch, date(2026, 8, 13), date(2026, 8, 16))
        self.assertEqual(days, Decimal("3"))  # 15th excluded, Dubai's 14th still counted

    @override_settings(LEAVE_WEEKEND_DAYS=[5, 6])
    def test_configured_weekly_offs_are_excluded(self):
        # 2026-08-10 is a Monday; Mon..Sun with Sat/Sun off = 5 days
        self.assertEqual(calculate_leave_days(None, date(2026, 8, 10), date(2026, 8, 16)), Decimal("5"))

    def test_overlap_is_blocked(self):
        LeaveApplication.objects.create(employee=self.emp, branch=self.branch, leave_type=self.el,
                                        start_date=date(2026, 9, 1), end_date=date(2026, 9, 3),
                                        number_of_days=Decimal("3"), reason="x")
        with self.assertRaises(LeaveRequestError) as ctx:
            validate_leave_request(self.emp, self.el, date(2026, 9, 3), date(2026, 9, 4))
        self.assertEqual(ctx.exception.code, "overlap")

    def test_insufficient_balance_counts_pending_and_can_be_acknowledged(self):
        LeaveApplication.objects.create(employee=self.emp, branch=self.branch, leave_type=self.el,
                                        start_date=date(2026, 9, 1), end_date=date(2026, 9, 6),
                                        number_of_days=Decimal("6"), reason="x")
        with self.assertRaises(LeaveRequestError) as ctx:
            validate_leave_request(self.emp, self.el, date(2026, 10, 1), date(2026, 10, 3))  # 3 > 8-6
        self.assertTrue(ctx.exception.extra["insufficient_balance"])
        days, _, warnings = validate_leave_request(self.emp, self.el, date(2026, 10, 1), date(2026, 10, 3),
                                                   acknowledge_insufficient_balance=True)
        self.assertEqual(days, Decimal("3"))
        self.assertEqual(len(warnings), 1)

    def test_hr_records_leave_on_behalf_and_balance_updates(self):
        app, _ = record_leave_on_behalf(self.hr, self.emp, self.el, date(2026, 9, 7), date(2026, 9, 8), "Phoned in sick")
        self.assertEqual(app.status, LeaveApplicationStatus.APPROVED)
        self.assertEqual(LeaveLedgerEntry.get_current_balance(self.emp, self.el), Decimal("6"))

    def test_unpaid_leave_does_not_create_negative_balance(self):
        app = LeaveApplication.objects.create(employee=self.emp, branch=self.branch, leave_type=self.lwp,
                                              start_date=date(2026, 9, 1), end_date=date(2026, 9, 2),
                                              number_of_days=Decimal("2"), reason="x",
                                              status=LeaveApplicationStatus.PENDING_DIRECTOR_APPROVAL)
        director = User.objects.create_user(username="D1", employee_id="D1", email="d1@gw.test", password="x",
                                            role=RoleChoices.DIRECTOR, is_global_scope=True)
        app.director_review(director, "APPROVED")
        self.assertEqual(LeaveLedgerEntry.get_current_balance(self.emp, self.lwp), Decimal("0"))

    def test_yearly_reset_lapses_excess_grants_entitlement_and_is_idempotent(self):
        run_yearly_leave_reset(2027)
        run_yearly_leave_reset(2027)
        self.assertEqual(LeaveLedgerEntry.get_current_balance(self.emp, self.el), Decimal("5"))   # 8 capped to 5
        self.assertEqual(LeaveLedgerEntry.get_current_balance(self.emp, self.cl), Decimal("12"))  # granted once

    def test_only_reporting_lead_can_recommend_via_api(self):
        app = LeaveApplication.objects.create(employee=self.emp, branch=self.branch, leave_type=self.el,
                                              start_date=date(2026, 9, 1), end_date=date(2026, 9, 1),
                                              number_of_days=Decimal("1"), reason="x")
        client = APIClient()
        client.force_authenticate(self.other_lead)
        resp = client.post(f"/api/v1/leaves/applications/{app.id}/lead-recommend/", {"action": "RECOMMENDED"}, format="json")
        self.assertEqual(resp.status_code, 403)
        client.force_authenticate(self.lead)
        resp = client.post(f"/api/v1/leaves/applications/{app.id}/lead-recommend/", {"action": "RECOMMENDED"}, format="json")
        self.assertEqual(resp.status_code, 200)

    def test_other_lead_cannot_read_employee_balance(self):
        client = APIClient()
        client.force_authenticate(self.other_lead)
        self.assertEqual(client.get(f"/api/v1/leaves/balance/?employee_id={self.emp.id}").status_code, 403)
        client.force_authenticate(self.lead)
        self.assertEqual(client.get(f"/api/v1/leaves/balance/?employee_id={self.emp.id}").status_code, 200)

    def test_mobile_endpoints_respond(self):
        client = APIClient()
        client.force_authenticate(self.emp)
        r = client.post("/api/v1/leaves/preview/", {"leave_type": self.el.id, "start_date": "2026-08-14",
                                                    "end_date": "2026-08-16"}, format="json")
        self.assertEqual(r.data["number_of_days"], 2.0)
        self.assertEqual(client.get("/api/v1/dashboard/summary/").status_code, 200)
        self.assertEqual(client.get("/api/v1/leaves/holidays/?year=2026").data[0]["name"], "Independence Day")
        client.force_authenticate(self.lead)
        LeaveApplication.objects.create(employee=self.emp, branch=self.branch, leave_type=self.el,
                                        start_date=date(2026, 9, 1), end_date=date(2026, 9, 1),
                                        number_of_days=Decimal("1"), reason="x")
        self.assertEqual(client.get("/api/v1/approvals/pending/").data["count"], 1)
