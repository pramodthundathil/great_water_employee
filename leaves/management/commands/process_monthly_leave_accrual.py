from django.core.management.base import BaseCommand
from leaves.services import run_batch_monthly_accrual


class Command(BaseCommand):
    help = "Run scheduled monthly earned-leave accrual across active employees."

    def add_arguments(self, parser):
        parser.add_argument("--period", type=str, help="Period key format YYYY-MM (default: current month)")

    def handle(self, *args, **options):
        period_key = options.get("period")
        self.stdout.write(f"Executing earned-leave accrual for period: {period_key or 'CURRENT'}...")
        res = run_batch_monthly_accrual(period_key=period_key)

        self.stdout.write(self.style.SUCCESS(
            f"Accrual finished. Total Employees: {res['total_employees']} | Credited: {res['credited']} | Skipped: {res['skipped']}"
        ))
        for line in res["details"][:10]:
            self.stdout.write(f"  {line}")
        if len(res["details"]) > 10:
            self.stdout.write(f"  ...and {len(res['details']) - 10} more.")
