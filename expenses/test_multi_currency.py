"""Branch reimbursement currency, multi-row / multi-currency expense items and multiple receipts."""
import json
from datetime import date
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import RoleChoices, User
from expenses.models import ExpenseCategory, ExpenseClaim, ExpenseClaimStatus, ExpenseItem
from organization.models import Branch, Organization


def pdf(name="r.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4 test", content_type="application/pdf")


class MultiCurrencyTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Great Waters", code="GW")
        self.dxb = Branch.objects.create(organization=self.org, code="GW-DXB", name="Dubai", currency_code="AED")
        self.kch = Branch.objects.create(organization=self.org, code="GW-KCH", name="Kochi", currency_code="INR",
                                         allowed_currencies="INR,USD,AED")
        self.travel = ExpenseCategory.objects.create(organization=self.org, code="TRAVEL", name="Travel")
        self.meals = ExpenseCategory.objects.create(organization=self.org, code="MEALS", name="Meals")
        self.dxb_emp = self.user("e1", RoleChoices.EMPLOYEE, self.dxb)
        self.kch_emp = self.user("e2", RoleChoices.EMPLOYEE, self.kch)
        self.director = self.user("d1", RoleChoices.DIRECTOR, None, is_global_scope=True)

    def user(self, name, role, branch, **extra):
        return User.objects.create_user(
            username=name, employee_id=f"GW-{name.upper()}", email=f"{name}@gw.test", password="Password@123",
            role=role, primary_branch=branch, **extra,
        )

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def test_api_create_multi_item_multi_currency_claim(self):
        items = [
            {"category": self.travel.id, "expense_date": "2026-01-05", "description": "Taxi",
             "currency": "AED", "amount": "50"},
            {"category": self.meals.id, "expense_date": "2026-01-03", "description": "Dinner in London",
             "currency": "GBP", "amount": "20.00", "exchange_rate": "4.65"},
            {"category": self.travel.id, "expense_date": "2026-01-04", "description": "Uber in Kochi",
             "currency": "INR", "amount": "1000", "exchange_rate": "0.044"},
        ]
        r = self.api(self.dxb_emp).post("/api/v1/expenses/claims/", {
            "items": json.dumps(items), "description": "Client visit", "submit_now": "true",
            "receipts": [pdf("a.pdf"), pdf("b.pdf"), pdf("c.pdf")],
        }, format="multipart")
        self.assertEqual(r.status_code, 201, r.data)
        claim = ExpenseClaim.objects.get(pk=r.data["id"])
        self.assertEqual(claim.currency_code, "AED")
        # 50 + 20×4.65 (93.00) + 1000×0.044 (44.00)
        self.assertEqual(claim.amount, Decimal("187.00"))
        self.assertEqual(claim.claimed_amount, Decimal("187.00"))
        self.assertEqual(claim.expense_date, date(2026, 1, 3))
        self.assertEqual(claim.category, self.travel)
        self.assertEqual(claim.items.count(), 3)
        self.assertEqual(claim.attachments.count(), 3)
        self.assertEqual(claim.status, ExpenseClaimStatus.UNDER_ADMIN_REVIEW)
        self.assertEqual(r.data["currency_code"], "AED")
        self.assertEqual([i["currency"] for i in r.data["items"]], ["AED", "GBP", "INR"])
        self.assertEqual(r.data["items"][1]["converted_amount"], "93.00")
        # Same-currency rows are always at rate 1 regardless of input.
        self.assertEqual(claim.items.first().exchange_rate, Decimal("1"))

    def test_foreign_currency_requires_rate(self):
        items = [{"category": self.travel.id, "expense_date": "2026-01-05", "currency": "USD", "amount": "10"}]
        r = self.api(self.dxb_emp).post("/api/v1/expenses/claims/", {"items": items, "description": "x"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("exchange rate", r.data["detail"])

    def test_branch_allowed_currencies_are_enforced(self):
        items = [{"category": self.travel.id, "expense_date": "2026-01-05", "currency": "EUR", "amount": "10",
                  "exchange_rate": "90"}]
        r = self.api(self.kch_emp).post("/api/v1/expenses/claims/", {"items": items, "description": "x"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("not an accepted currency", r.data["detail"])
        items[0].update(currency="USD", exchange_rate="83.5")
        r = self.api(self.kch_emp).post("/api/v1/expenses/claims/", {"items": items, "description": "x"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["currency_code"], "INR")
        self.assertEqual(r.data["amount"], "835.00")

    def test_currency_config_endpoint(self):
        r = self.api(self.kch_emp).get("/api/v1/expenses/currencies/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["currency"]["code"], "INR")
        self.assertEqual([c["code"] for c in r.data["claim_currencies"]], ["INR", "USD", "AED"])
        self.assertGreater(len(r.data["all_currencies"]), 150)
        r = self.api(self.dxb_emp).get("/api/v1/expenses/currencies/")
        self.assertEqual(r.data["claim_currencies"][0]["code"], "AED")
        self.assertGreater(len(r.data["claim_currencies"]), 150)  # no restriction = every currency

    def test_patch_replaces_items_and_legacy_patch_still_works(self):
        api = self.api(self.dxb_emp)
        r = api.post("/api/v1/expenses/claims/", {"items": [
            {"category": self.travel.id, "expense_date": "2026-01-05", "currency": "AED", "amount": "10"},
            {"category": self.meals.id, "expense_date": "2026-01-05", "currency": "USD", "amount": "10",
             "exchange_rate": "3.6725"},
        ], "description": "x"}, format="json")
        cid = r.data["id"]
        self.assertEqual(r.data["amount"], "46.73")
        r = api.patch(f"/api/v1/expenses/claims/{cid}/", {"items": [
            {"category": self.meals.id, "expense_date": "2026-01-06", "currency": "AED", "amount": "99"},
        ]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["amount"], "99.00")
        self.assertEqual(len(r.data["items"]), 1)
        # Older app builds send just the amount: it updates the single item.
        r = api.patch(f"/api/v1/expenses/claims/{cid}/", {"amount": "120"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["amount"], "120.00")
        self.assertEqual(r.data["items"][0]["category"]["id"], self.meals.id)

    def test_partial_approval_messages_use_claim_currency(self):
        r = self.api(self.kch_emp).post("/api/v1/expenses/claims/", {
            "items": json.dumps([{"category": self.travel.id, "expense_date": "2026-01-05", "currency": "INR",
                                  "amount": "5000"}]),
            "description": "x", "submit_now": "true", "receipts": [pdf()],
        }, format="multipart")
        claim = ExpenseClaim.objects.get(pk=r.data["id"])
        admin = self.user("a1", RoleChoices.ADMIN_MANAGER, self.kch)
        r = self.api(admin).post(f"/api/v1/expenses/claims/{claim.id}/admin-review/",
                                 {"action": "PARTIALLY_APPROVED", "approved_amount": "4000", "remarks": "cap"},
                                 format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("INR 4,000.00 of INR 5,000.00", r.data["detail"])

    def test_web_create_with_rows_and_detail_page(self):
        self.client.force_login(self.dxb_emp)
        r = self.client.post("/expenses/", {
            "item_category": [self.travel.id, self.meals.id],
            "item_date": ["2026-01-05", "2026-01-06"],
            "item_description": ["Taxi", "Lunch in Mumbai"],
            "item_currency": ["AED", "INR"],
            "item_amount": ["40", "2000"],
            "item_rate": ["", "0.0441"],
            "description": "Site visit", "receipts": [pdf("1.pdf"), pdf("2.pdf")],
        })
        self.assertEqual(r.status_code, 302)
        claim = ExpenseClaim.objects.get(description="Site visit")
        self.assertEqual(claim.amount, Decimal("128.20"))
        self.assertEqual(claim.attachments.count(), 2)
        page = self.client.get(f"/expenses/{claim.id}/")
        self.assertContains(page, "AED 128.20")
        self.assertContains(page, "1 INR = 0.0441 AED")
        edit = self.client.get(f"/expenses/{claim.id}/edit/")
        self.assertContains(edit, "gwItemsInitial")

    def test_web_branch_currency_configuration(self):
        self.client.force_login(self.director)
        self.client.post("/branches/", {
            "action": "create", "code": "gw-sg", "name": "Singapore", "country": "Singapore",
            "currency_code": "SGD", "allowed_currencies": ["USD", "MYR"], "is_active": "1",
        })
        b = Branch.objects.get(code="GW-SG")
        self.assertEqual(b.currency_code, "SGD")
        self.assertEqual(b.allowed_currency_list, ["USD", "MYR"])
        self.assertTrue(b.accepts_currency("SGD"))
        self.assertFalse(b.accepts_currency("EUR"))

    def test_api_branch_currency_configuration(self):
        api = self.api(self.director)
        r = api.post("/api/v1/organization/branches/overview/",
                     {"code": "GW-AUH", "name": "Abu Dhabi", "country": "UAE"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["currency_code"], "AED")  # inferred from the country
        r = api.patch(f"/api/v1/organization/branches/{r.data['id']}/",
                      {"currency_code": "usd", "allowed_currencies": ["AED", "EUR", "BAD"]}, format="json")
        self.assertEqual(r.data["currency_code"], "USD")
        self.assertEqual(r.data["allowed_currencies"], ["AED", "EUR"])
        r = api.patch(f"/api/v1/organization/branches/{r.data['id']}/", {"currency_code": "ZZZ"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_item_conversion_rounding(self):
        self.assertEqual(ExpenseItem.convert(Decimal("10.00"), Decimal("3.672500")), Decimal("36.73"))


class BranchCurrencyChangeTests(MultiCurrencyTests):
    """Changing a branch's currency moves its open claims to it; approved / paid claims keep theirs."""

    def _claim(self, status, items):
        c = ExpenseClaim.objects.create(employee=self.dxb_emp, branch=self.dxb, category=self.travel,
                                        expense_date=date(2026, 1, 5), amount=Decimal("1"), description="x",
                                        status=status, currency_code="USD")
        for cur, amt, rate in items:
            ExpenseItem.objects.create(claim=c, category=self.travel, expense_date=date(2026, 1, 5),
                                       currency_code=cur, original_amount=Decimal(amt), exchange_rate=Decimal(rate))
        return c

    def test_web_currency_change_relabels_open_claims_only(self):
        self.dxb.currency_code = "USD"
        self.dxb.save()
        open_claim = self._claim(ExpenseClaimStatus.UNDER_ADMIN_REVIEW, [("USD", "100", "1")])
        paid = self._claim(ExpenseClaimStatus.PAID, [("USD", "50", "1")])
        foreign = self._claim(ExpenseClaimStatus.DRAFT, [("EUR", "10", "1.1")])
        self.client.force_login(self.director)
        self.client.post(f"/branches/{self.dxb.id}/", {
            "action": "update", "branch_id": self.dxb.id, "code": "GW-DXB", "name": "Dubai",
            "country": "UAE", "currency_code": "AED", "is_active": "1",
        })
        self.dxb.refresh_from_db()
        self.assertEqual(self.dxb.currency_code, "AED")
        open_claim.refresh_from_db(); paid.refresh_from_db(); foreign.refresh_from_db()
        self.assertEqual(open_claim.currency_code, "AED")
        self.assertEqual(open_claim.items.get().currency_code, "AED")
        self.assertEqual(paid.currency_code, "USD")
        self.assertEqual(foreign.currency_code, "USD")

    def test_currency_endpoint_uses_primary_branch_only(self):
        r = self.api(self.director).get("/api/v1/expenses/currencies/")
        self.assertNotIn("currency", r.data)  # no own branch, so no payout currency is implied

    def test_sync_command(self):
        from io import StringIO
        from django.core.management import call_command
        self.kch.currency_code = "USD"
        self.kch.save()
        c = ExpenseClaim.objects.create(employee=self.kch_emp, branch=self.kch, category=self.travel,
                                        expense_date=date(2026, 1, 5), amount=Decimal("10"), description="x",
                                        status=ExpenseClaimStatus.PAID, currency_code="USD")
        out = StringIO()
        call_command("sync_branch_currencies", "--set", "GW-KCH=INR", stdout=out)
        self.kch.refresh_from_db()
        self.assertEqual(self.kch.currency_code, "USD")  # dry run
        call_command("sync_branch_currencies", "--set", "GW-KCH=INR", "--include-closed", "--apply", stdout=out)
        self.kch.refresh_from_db(); c.refresh_from_db()
        self.assertEqual(self.kch.currency_code, "INR")
        self.assertEqual(c.currency_code, "INR")


class ReceiptUploaderTests(TestCase):
    def setUp(self):
        org = Organization.objects.create(name="GW", code="GW")
        self.branch = Branch.objects.create(organization=org, code="GW-DXB", name="Dubai", currency_code="AED")
        self.cat = ExpenseCategory.objects.create(organization=org, code="T", name="Travel")
        self.emp = User.objects.create_user(username="e", employee_id="GW-E", email="e@gw.test",
                                            password="Password@123", role=RoleChoices.EMPLOYEE,
                                            primary_branch=self.branch)
        self.client.force_login(self.emp)

    def post(self, n):
        return self.client.post("/expenses/", {
            "item_category": self.cat.id, "item_date": "2026-01-05", "item_currency": "AED", "item_amount": "10",
            "item_rate": "1", "description": f"claim-{n}", "receipts": [pdf(f"{i}.pdf") for i in range(n)],
        })

    def test_page_renders_uploader(self):
        r = self.client.get("/expenses/")
        self.assertContains(r, 'class="gw-uploader"')
        self.assertContains(r, 'data-required="1"')

    def test_multiple_files_saved_and_limit_enforced(self):
        self.post(5)
        self.assertEqual(ExpenseClaim.objects.get(description="claim-5").attachments.count(), 5)
        self.post(21)
        self.assertFalse(ExpenseClaim.objects.filter(description="claim-21").exists())
        self.post(0)
        self.assertFalse(ExpenseClaim.objects.filter(description="claim-0").exists())


class ReceiptViewerTests(ReceiptUploaderTests):
    def test_detail_page_lists_receipts_as_gallery(self):
        self.post(3)
        claim = ExpenseClaim.objects.get(description="claim-3")
        r = self.client.get(f"/expenses/{claim.id}/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, f'data-gw-gallery="claim-{claim.id}"', count=4)  # "View all" + 3 cards
        self.assertContains(r, "View all (3)")
