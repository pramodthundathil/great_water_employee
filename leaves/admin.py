from django.contrib import admin
from leaves.models import (
    LeaveType, LeavePolicy, LeaveAccrualRun, LeaveLedgerEntry,
    LeaveApplication, LeaveApproval, HolidayCalendar, Holiday, LeaveAuditLog,
)


class LeaveApprovalInline(admin.TabularInline):
    model = LeaveApproval
    extra = 0
    readonly_fields = ("stage", "approver", "action", "remarks", "approved_start_date", "approved_end_date",
                       "approved_days", "created_at")


class LeaveAuditLogInline(admin.TabularInline):
    model = LeaveAuditLog
    extra = 0
    can_delete = False
    readonly_fields = ("created_at", "action", "actor", "stage", "from_status", "to_status", "remarks", "changes",
                       "source", "ip_address")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(LeaveAuditLog)
class LeaveAuditLogAdmin(admin.ModelAdmin):
    list_display = ("application", "action", "actor", "from_status", "to_status", "source", "created_at")
    list_filter = ("action", "source")
    search_fields = ("application__application_number", "actor__employee_id")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LeaveApplication)
class LeaveApplicationAdmin(admin.ModelAdmin):
    list_display = ("application_number", "employee", "leave_type", "start_date", "end_date", "number_of_days",
                    "requested_number_of_days", "status", "resubmission_count")
    list_filter = ("status", "leave_type", "branch")
    search_fields = ("application_number", "employee__employee_id", "employee__first_name")
    inlines = [LeaveApprovalInline, LeaveAuditLogInline]


@admin.register(LeaveLedgerEntry)
class LeaveLedgerEntryAdmin(admin.ModelAdmin):
    list_display = ("employee", "leave_type", "entry_type", "transaction_days", "balance_after", "created_at")
    list_filter = ("entry_type", "leave_type")
    search_fields = ("employee__employee_id",)

    def has_change_permission(self, request, obj=None):
        return False  # ledger is immutable

    def has_delete_permission(self, request, obj=None):
        return False


class HolidayInline(admin.TabularInline):
    model = Holiday
    extra = 1


@admin.register(HolidayCalendar)
class HolidayCalendarAdmin(admin.ModelAdmin):
    list_display = ("name", "year", "branch", "is_active")
    inlines = [HolidayInline]


admin.site.register(LeaveType, list_display=("code", "name", "is_paid", "is_accrual_based", "is_active"))
admin.site.register(LeavePolicy, list_display=("name", "leave_type", "accrual_frequency", "yearly_entitlement", "monthly_accrual_rate"))
admin.site.register(LeaveAccrualRun, list_display=("employee", "policy", "period_key", "days_credited"))
