from django.contrib import admin
from expenses.models import ExpenseCategory, ExpenseClaim, ExpenseAttachment, ExpenseApproval, ReimbursementPayment


class AttachmentInline(admin.TabularInline):
    model = ExpenseAttachment
    extra = 0


class ApprovalInline(admin.TabularInline):
    model = ExpenseApproval
    extra = 0
    readonly_fields = ("stage", "approver", "action", "remarks", "created_at")


@admin.register(ExpenseClaim)
class ExpenseClaimAdmin(admin.ModelAdmin):
    list_display = ("claim_number", "employee", "branch", "category", "amount", "expense_date", "status")
    list_filter = ("status", "branch", "category")
    search_fields = ("claim_number", "employee__employee_id", "employee__first_name")
    inlines = [AttachmentInline, ApprovalInline]


admin.site.register(ExpenseCategory, list_display=("code", "name", "is_active"))
admin.site.register(ReimbursementPayment, list_display=("claim", "payment_date", "payment_mode", "payment_reference"))
