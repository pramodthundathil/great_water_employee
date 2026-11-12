from datetime import date
from django.core.management.base import BaseCommand
from leaves.services import run_yearly_leave_reset


class Command(BaseCommand):
    help = "Year-start leave processing: lapse balances above carry-forward limits and grant yearly entitlements. Safe to re-run."

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, help="Leave year to open (default: current year)")

    def handle(self, *args, **options):
        year = options.get("year") or date.today().year
        res = run_yearly_leave_reset(year)
        self.stdout.write(self.style.SUCCESS(
            f"Yearly leave reset for {year}: lapsed={res['lapsed']} granted={res['granted']} skipped={res['skipped']}"
        ))
