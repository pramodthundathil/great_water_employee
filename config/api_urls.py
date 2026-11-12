from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView, TokenBlacklistView

from accounts.api_views import (
    CustomTokenObtainPairView, CurrentUserView, ProfileUpdateView,
    ChangePasswordView, ForgotPasswordView,
)
from portal.api_views import DashboardSummaryView, PendingApprovalsView
from portal.analytics_api import DashboardAnalyticsView
from organization.api_views import UserBranchListView, DepartmentListView
from organization.config_api import (
    EmployeeListView, EmployeeDetailView, EmployeeToggleActiveView, EmployeeSetPasswordView,
    EmployeeFormOptionsView, BranchOverviewView, BranchUpdateView,
    ExpenseCategoryConfigView, ExpenseCategoryUpdateView,
    LeaveSettingsView, LeaveTypeUpdateView, LeavePolicyCreateView, LeavePolicyUpdateView,
)
from expenses.api_views import (
    ExpenseCategoryListView, ExpenseClaimListCreateView,
    ExpenseClaimDetailView, ExpenseClaimSubmitView, ExpenseClaimWithdrawView,
    ExpenseClaimAdminReviewView, ExpenseClaimDirectorApprovalView,
    ExpenseClaimRecordPaymentView, ExpenseAttachmentView
)
from leaves.api_views import (
    LeaveTypeListView, EmployeeLeaveBalanceView, EmployeeLeaveLedgerView,
    LeaveApplicationListCreateView, LeaveApplicationDetailView,
    LeaveApplicationWithdrawView, LeaveApplicationLeadRecommendView,
    LeaveApplicationHrVerifyView, LeaveApplicationDirectorApprovalView,
    LeaveManualAdjustmentView, LeavePreviewView, HolidayListView
)
from notifications.api_views import (
    NotificationListView, NotificationMarkReadView,
    NotificationMarkAllReadView, DeviceRegisterView, DeviceUnregisterView
)
from reports.api_views import ExpenseReportExportView, LeaveReportExportView, ReportExportView

urlpatterns = [
    # Auth
    path("auth/token/", CustomTokenObtainPairView.as_view(), name="api_token_obtain_pair"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="api_token_refresh"),
    path("auth/token/blacklist/", TokenBlacklistView.as_view(), name="api_token_blacklist"),
    path("auth/me/", CurrentUserView.as_view(), name="api_current_user"),
    path("auth/profile/", ProfileUpdateView.as_view(), name="api_profile_update"),
    path("auth/change-password/", ChangePasswordView.as_view(), name="api_change_password"),
    path("auth/forgot-password/", ForgotPasswordView.as_view(), name="api_forgot_password"),

    # Mobile aggregates
    path("dashboard/summary/", DashboardSummaryView.as_view(), name="api_dashboard_summary"),
    path("dashboard/analytics/", DashboardAnalyticsView.as_view(), name="api_dashboard_analytics"),
    path("approvals/pending/", PendingApprovalsView.as_view(), name="api_approvals_pending"),

    # Organization
    path("organization/branches/", UserBranchListView.as_view(), name="api_branches"),
    path("organization/departments/", DepartmentListView.as_view(), name="api_departments"),
    path("organization/branches/overview/", BranchOverviewView.as_view(), name="api_branches_overview"),
    path("organization/employees/", EmployeeListView.as_view(), name="api_employees"),
    path("organization/branches/<int:pk>/", BranchUpdateView.as_view(), name="api_branch_update"),
    path("organization/employees/form-options/", EmployeeFormOptionsView.as_view(), name="api_employee_form_options"),
    path("organization/employees/<int:pk>/", EmployeeDetailView.as_view(), name="api_employee_detail"),
    path("organization/employees/<int:pk>/toggle-active/", EmployeeToggleActiveView.as_view(), name="api_employee_toggle_active"),
    path("organization/employees/<int:pk>/set-password/", EmployeeSetPasswordView.as_view(), name="api_employee_set_password"),

    # Configuration (read-only)
    path("settings/expense-categories/", ExpenseCategoryConfigView.as_view(), name="api_settings_expense_categories"),
    path("settings/expense-categories/<int:pk>/", ExpenseCategoryUpdateView.as_view(), name="api_settings_expense_category"),
    path("settings/leave-types/", LeaveSettingsView.as_view(), name="api_settings_leave_types"),
    path("settings/leave-types/<int:pk>/", LeaveTypeUpdateView.as_view(), name="api_settings_leave_type"),
    path("settings/leave-types/<int:pk>/policies/", LeavePolicyCreateView.as_view(), name="api_settings_leave_policy_create"),
    path("settings/leave-policies/<int:pk>/", LeavePolicyUpdateView.as_view(), name="api_settings_leave_policy"),

    # Expenses
    path("expenses/categories/", ExpenseCategoryListView.as_view(), name="api_expense_categories"),
    path("expenses/claims/", ExpenseClaimListCreateView.as_view(), name="api_expense_claims"),
    path("expenses/claims/<int:pk>/", ExpenseClaimDetailView.as_view(), name="api_expense_claim_detail"),
    path("expenses/claims/<int:pk>/attachments/", ExpenseAttachmentView.as_view(), name="api_expense_attachments"),
    path("expenses/claims/<int:pk>/attachments/<int:att_id>/", ExpenseAttachmentView.as_view(), name="api_expense_attachment_delete"),
    path("expenses/claims/<int:pk>/submit/", ExpenseClaimSubmitView.as_view(), name="api_expense_claim_submit"),
    path("expenses/claims/<int:pk>/withdraw/", ExpenseClaimWithdrawView.as_view(), name="api_expense_claim_withdraw"),
    path("expenses/claims/<int:pk>/admin-review/", ExpenseClaimAdminReviewView.as_view(), name="api_expense_claim_admin_review"),
    path("expenses/claims/<int:pk>/director-approval/", ExpenseClaimDirectorApprovalView.as_view(), name="api_expense_claim_director_approval"),
    path("expenses/claims/<int:pk>/record-payment/", ExpenseClaimRecordPaymentView.as_view(), name="api_expense_claim_record_payment"),

    # Leaves
    path("leaves/types/", LeaveTypeListView.as_view(), name="api_leave_types"),
    path("leaves/balance/", EmployeeLeaveBalanceView.as_view(), name="api_leave_balance"),
    path("leaves/ledger/", EmployeeLeaveLedgerView.as_view(), name="api_leave_ledger"),
    path("leaves/applications/", LeaveApplicationListCreateView.as_view(), name="api_leave_applications"),
    path("leaves/applications/<int:pk>/", LeaveApplicationDetailView.as_view(), name="api_leave_application_detail"),
    path("leaves/applications/<int:pk>/withdraw/", LeaveApplicationWithdrawView.as_view(), name="api_leave_application_withdraw"),
    path("leaves/applications/<int:pk>/lead-recommend/", LeaveApplicationLeadRecommendView.as_view(), name="api_leave_lead_recommend"),
    path("leaves/applications/<int:pk>/hr-verify/", LeaveApplicationHrVerifyView.as_view(), name="api_leave_hr_verify"),
    path("leaves/applications/<int:pk>/director-approval/", LeaveApplicationDirectorApprovalView.as_view(), name="api_leave_director_approval"),
    path("leaves/preview/", LeavePreviewView.as_view(), name="api_leave_preview"),
    path("leaves/holidays/", HolidayListView.as_view(), name="api_leave_holidays"),
    path("leaves/adjust-balance/", LeaveManualAdjustmentView.as_view(), name="api_leave_adjust_balance"),

    # Notifications & Devices
    path("notifications/", NotificationListView.as_view(), name="api_notifications"),
    path("notifications/<int:pk>/mark-read/", NotificationMarkReadView.as_view(), name="api_notification_mark_read"),
    path("notifications/mark-all-read/", NotificationMarkAllReadView.as_view(), name="api_notifications_mark_all_read"),
    path("devices/register/", DeviceRegisterView.as_view(), name="api_device_register"),
    path("devices/unregister/", DeviceUnregisterView.as_view(), name="api_device_unregister"),

    # Reports Export
    path("reports/expenses/export/", ExpenseReportExportView.as_view(), name="api_reports_expenses_export"),
    path("reports/leaves/export/", LeaveReportExportView.as_view(), name="api_reports_leaves_export"),
    path("reports/<slug:report>/export/", ReportExportView.as_view(), name="api_reports_export"),
]
