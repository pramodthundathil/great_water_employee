from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from accounts.models import User, EmployeeProfile, UserBranchScope


class EmployeeProfileInline(admin.StackedInline):
    model = EmployeeProfile
    fk_name = "user"
    can_delete = False


@admin.register(User)
class GWUserAdmin(UserAdmin):
    inlines = [EmployeeProfileInline]
    list_display = ("employee_id", "first_name", "last_name", "email", "role", "primary_branch", "is_active")
    list_filter = ("role", "primary_branch", "is_active", "is_global_scope")
    search_fields = ("employee_id", "email", "first_name", "last_name")
    ordering = ("employee_id",)
    fieldsets = UserAdmin.fieldsets + (
        ("Great Waters", {"fields": ("employee_id", "role", "is_global_scope", "primary_branch", "phone")}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("Great Waters", {"fields": ("employee_id", "email", "role", "primary_branch")}),
    )


admin.site.register(UserBranchScope)
