from datetime import timedelta
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from expenses.models import ExpenseClaim
from leaves.models import LeaveApplication
from notifications.models import NotificationEvent
from portal.dashboard_services import (
    PENDING_EXPENSE_STATUSES, PENDING_LEAVE_STATUSES, send_approver_reminder, get_item_approver_info,
)


class Command(BaseCommand):
    help = "Remind approvers about expense claims / leave applications pending longer than N days (run daily via cron)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=getattr(settings, "APPROVAL_REMINDER_DAYS", 2))

    def handle(self, *args, **options):
        days = options["days"]
        cutoff = timezone.now() - timedelta(days=days)
        sent = 0

        class _System:
            full_name = "Great Waters automated reminder"

        items = [
            ("expense", c) for c in ExpenseClaim.objects.filter(status__in=PENDING_EXPENSE_STATUSES, updated_at__lte=cutoff)
            .select_related("branch", "category", "employee")
        ] + [
            ("leave", a) for a in LeaveApplication.objects.filter(status__in=PENDING_LEAVE_STATUSES, updated_at__lte=cutoff)
            .select_related("branch", "leave_type", "employee__profile__reporting_lead")
        ]

        for item_type, item in items:
            info = get_item_approver_info(item, item_type)
            ref = item.claim_number if item_type == "expense" else item.application_number
            # Do not remind the same approvers more than once a day for the same request
            already = NotificationEvent.objects.filter(
                event_type="APPROVAL_REMINDER", title__contains=ref, created_at__gte=timezone.now() - timedelta(hours=23)
            ).exists()
            if already or not info["approvers"]:
                continue
            count, _ = send_approver_reminder(item, item_type, actor=_System())
            sent += 1 if count else 0

        self.stdout.write(self.style.SUCCESS(f"Sent reminders for {sent} request(s) pending more than {days} day(s)."))
