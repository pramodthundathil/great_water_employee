"""
Fix branch reimbursement currencies and re-label claims to match.

    python manage.py sync_branch_currencies                      # report only (dry run)
    python manage.py sync_branch_currencies --set GW-DXB=AED --set GW-KCH=INR --apply
    python manage.py sync_branch_currencies --guess --apply      # infer from country / city (Dubai→AED, India→INR)
    python manage.py sync_branch_currencies --apply --include-closed   # also approved / paid claims

Claim amounts are never converted: claims whose lines are all in the old currency are re-labelled
with the branch currency. Claims with real foreign-currency lines are listed and left alone.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from expenses.items import retag_claims_to_branch_currency
from organization.currencies import guess_currency, normalize_code
from organization.models import Branch


class Command(BaseCommand):
    help = "Set branch reimbursement currencies and re-label claims to the branch currency."

    def add_arguments(self, parser):
        parser.add_argument("--set", action="append", default=[], metavar="CODE=CUR",
                            help="Branch code and currency, e.g. GW-DXB=AED (repeatable).")
        parser.add_argument("--guess", action="store_true",
                            help="Infer currency from the branch country / city when it is recognised.")
        parser.add_argument("--include-closed", action="store_true",
                            help="Also re-label approved and paid claims.")
        parser.add_argument("--apply", action="store_true", help="Write changes (default is a dry run).")

    def handle(self, *args, **opts):
        wanted = {}
        for pair in opts["set"]:
            if "=" not in pair:
                raise CommandError(f"Use BRANCH=CUR, got '{pair}'.")
            code, cur = pair.split("=", 1)
            cur = normalize_code(cur)
            if not cur:
                raise CommandError(f"Unknown currency in '{pair}'.")
            if not Branch.objects.filter(code=code.strip().upper()).exists():
                raise CommandError(f"No branch with code '{code}'.")
            wanted[code.strip().upper()] = cur

        apply = opts["apply"]
        with transaction.atomic():
            for b in Branch.objects.order_by("code"):
                new = wanted.get(b.code) or (guess_currency(b.country, b.city) if opts["guess"] else None)
                line = f"{b.code:<10} {b.name[:30]:<30} {b.country or '-':<22} {b.currency_code}"
                if new and new != b.currency_code:
                    line += f" -> {new}"
                    b.currency_code = new
                    b.save(update_fields=["currency_code", "updated_at"])
                moved, skipped = retag_claims_to_branch_currency(b, include_closed=opts["include_closed"])
                if moved:
                    line += f" | {len(moved)} claim(s) re-labelled"
                if skipped:
                    line += f" | skipped (foreign-currency lines): {', '.join(skipped)}"
                self.stdout.write(line)
            if not apply:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Dry run: nothing saved. Add --apply to write the changes."))
            else:
                self.stdout.write(self.style.SUCCESS("Saved."))
