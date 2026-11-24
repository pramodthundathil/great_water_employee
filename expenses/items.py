"""
Multi-line, multi-currency expense items — shared by the mobile API and the web portal.

Every claim is reimbursed in its branch's currency (e.g. AED for Dubai, INR for Kochi). Each line records
what was spent, in the currency it was spent in, with a manually entered exchange rate:

    converted_amount = original_amount × exchange_rate      (in the claim / branch currency)

The claim's amount is the sum of the converted line amounts.
"""
import json
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import transaction

from expenses.models import ExpenseCategory, ExpenseItem
from organization.currencies import normalize_code

MAX_ITEMS = 50
MAX_RATE = Decimal("1000000")


class ItemError(ValueError):
    pass


def _dec(value):
    try:
        v = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    return v if v.is_finite() else None


def raw_items_from_request(data, claim=None):
    """
    Items from an API payload: `items` as a JSON list, or a JSON string (multipart forms).
    Falls back to the legacy single-line fields (category / amount / expense_date) for older app builds.
    Returns None when the payload carries neither.
    """
    raw = data.get("items")
    if isinstance(raw, str):
        raw = raw.strip()
        if raw:
            try:
                raw = json.loads(raw)
            except ValueError:
                raise ItemError("Expense items are not valid JSON.")
        else:
            raw = None
    if raw is not None:
        if not isinstance(raw, list):
            raise ItemError("Expense items must be a list.")
        return raw
    legacy_keys = ("amount", "category", "expense_date")
    if any(data.get(k) not in (None, "") for k in legacy_keys):
        row = {"category": None, "expense_date": None, "description": "", "currency": "", "amount": None,
               "exchange_rate": "1"}
        if claim is not None and claim.pk:
            existing = list(claim.items.all()[:2])
            if len(existing) > 1:
                raise ItemError("This claim has several items. Update the app to edit it.")
            if existing:
                it = existing[0]
                row.update(category=it.category_id, expense_date=it.expense_date.isoformat(),
                           description=it.description, currency=it.currency_code,
                           amount=str(it.original_amount), exchange_rate=str(it.exchange_rate))
            else:
                row.update(category=claim.category_id, expense_date=claim.expense_date.isoformat(),
                           amount=str(claim.claimed_amount))
        for key in legacy_keys:
            if data.get(key) not in (None, ""):
                row[key] = data.get(key)
        if data.get("currency"):
            row["currency"] = data.get("currency")
            row["exchange_rate"] = data.get("exchange_rate") or ""
        return [row]
    return None


def raw_items_from_post(post):
    """Items from the web portal's repeated form fields (item_category, item_amount, …)."""
    cols = {k: post.getlist(f"item_{k}") for k in ("category", "date", "description", "currency", "amount", "rate")}
    n = max((len(v) for v in cols.values()), default=0)
    rows = []
    for i in range(n):
        get = lambda k: (cols[k][i] if i < len(cols[k]) else "").strip()
        if not any(get(k) for k in ("category", "description", "amount")):
            continue  # completely blank row left in the form
        rows.append({
            "category": get("category"), "expense_date": get("date"), "description": get("description"),
            "currency": get("currency"), "amount": get("amount"), "exchange_rate": get("rate"),
        })
    return rows


def clean_items(raw_items, branch, base=None):
    """
    Validate raw item dicts for a claim of `branch`. Returns a list of clean dicts ready for `replace_items`.
    Raises ItemError with a message naming the offending line.
    """
    if not raw_items:
        raise ItemError("Add at least one expense item.")
    if len(raw_items) > MAX_ITEMS:
        raise ItemError(f"A claim can have at most {MAX_ITEMS} items.")
    base = base or branch.currency_code
    categories = {c.id: c for c in ExpenseCategory.available_for(branch)}
    today = date.today()
    clean = []
    for idx, row in enumerate(raw_items, start=1):
        if not isinstance(row, dict):
            raise ItemError(f"Item {idx}: invalid data.")
        label = f"Item {idx}"
        try:
            category = categories.get(int(row.get("category") or 0))
        except (TypeError, ValueError):
            category = None
        if not category:
            raise ItemError(f"{label}: choose a category.")
        try:
            d = date.fromisoformat(str(row.get("expense_date") or "")[:10])
        except ValueError:
            raise ItemError(f"{label}: enter a valid expense date (YYYY-MM-DD).")
        if d > today:
            raise ItemError(f"{label}: the expense date cannot be in the future.")
        currency = normalize_code(row.get("currency")) or base
        if currency != base and not branch.accepts_currency(currency):
            raise ItemError(f"{label}: {currency} is not an accepted currency for {branch.name}.")
        amount = _dec(row.get("amount"))
        if amount is None or amount <= 0:
            raise ItemError(f"{label}: enter an amount greater than zero.")
        amount = amount.quantize(Decimal("0.01"))
        if currency == base:
            rate = Decimal("1")
        else:
            rate = _dec(row.get("exchange_rate"))
            if rate is None or rate <= 0:
                raise ItemError(f"{label}: enter the exchange rate (1 {currency} = ? {base}).")
            if rate > MAX_RATE:
                raise ItemError(f"{label}: the exchange rate looks too large.")
            rate = rate.quantize(Decimal("0.000001"))
        converted = ExpenseItem.convert(amount, rate)
        if converted <= 0:
            raise ItemError(f"{label}: the converted amount is zero — check the exchange rate.")
        clean.append({
            "category": category, "expense_date": d,
            "description": str(row.get("description") or "").strip()[:255],
            "currency_code": currency, "original_amount": amount, "exchange_rate": rate,
            "converted_amount": converted,
        })
    total = sum((c["converted_amount"] for c in clean), Decimal("0"))
    if total >= Decimal("10000000000"):
        raise ItemError("The claim total is too large.")
    return clean


@transaction.atomic
def replace_items(claim, clean_items_list):
    """
    Replace the claim's lines and roll the header up from them: amount = Σ converted amounts,
    expense date = earliest line date, category = first line's category. Saves the claim.
    """
    if not claim.currency_code:
        claim.currency_code = claim.branch.currency_code
    claim.items.all().delete()
    for pos, c in enumerate(clean_items_list):
        ExpenseItem.objects.create(
            claim=claim, position=pos, category=c["category"], expense_date=c["expense_date"],
            description=c["description"], currency_code=c["currency_code"],
            original_amount=c["original_amount"], exchange_rate=c["exchange_rate"],
        )
    total = sum((c["converted_amount"] for c in clean_items_list), Decimal("0"))
    claim.set_claimed_amount(total)
    claim.expense_date = min(c["expense_date"] for c in clean_items_list)
    claim.category = clean_items_list[0]["category"]
    claim.save()
    return claim


def summary_header(clean_items_list):
    """Header fields needed to create a claim before its items exist."""
    return {
        "category": clean_items_list[0]["category"],
        "expense_date": min(c["expense_date"] for c in clean_items_list),
        "amount": sum((c["converted_amount"] for c in clean_items_list), Decimal("0")),
    }


def expense_currency_context(branch):
    """Template context for the web claim editor: the reimbursement currency and selectable currencies."""
    from organization.currencies import all_currencies_json, currency_json
    if not branch:
        return {"claim_currency": None, "claim_currencies": all_currencies_json()}
    return {
        "claim_currency": currency_json(branch.currency_code),
        "claim_currencies": [currency_json(c) for c in branch.claim_currencies()],
    }


# Claims that are still open: their currency follows the branch when the branch currency changes.
OPEN_CLAIM_STATUSES = (
    "DRAFT", "UNDER_ADMIN_REVIEW", "UNDER_DIRECTOR_REVIEW", "REJECTED_BY_ADMIN", "REJECTED_BY_DIRECTOR", "WITHDRAWN",
)


@transaction.atomic
def retag_claims_to_branch_currency(branch, include_closed=False):
    """
    Move claims of `branch` onto the branch's reimbursement currency.

    Only claims whose items are all in the claim's old currency (rate 1, i.e. no real conversion) are moved:
    their amounts are kept and simply re-labelled, which is right when the old label was wrong (e.g. the
    "USD" default before branch currencies existed). Claims with genuine foreign-currency lines are skipped,
    because their exchange rates were entered against the old currency.
    Approved / paid claims are only touched with include_closed=True.
    Returns (moved_claim_numbers, skipped_claim_numbers).
    """
    from expenses.models import ExpenseClaim

    target = branch.currency_code
    qs = ExpenseClaim.objects.filter(branch=branch).exclude(currency_code=target).prefetch_related("items")
    if not include_closed:
        qs = qs.filter(status__in=OPEN_CLAIM_STATUSES)
    moved, skipped = [], []
    for claim in qs:
        old = claim.currency_code
        items = list(claim.items.all())
        if any(i.currency_code != old or i.exchange_rate != Decimal("1") for i in items):
            skipped.append(claim.claim_number)
            continue
        for i in items:
            i.currency_code = target
            i.save(update_fields=["currency_code"])
        claim.currency_code = target
        claim.save(update_fields=["currency_code", "updated_at"])
        moved.append(claim.claim_number)
    return moved, skipped
