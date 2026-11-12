from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy
from portal import views, admin_views

urlpatterns = [
    # Auth
    path("login/", views.login_view, name="portal_login"),
    path("logout/", views.logout_view, name="portal_logout"),
    path("password-reset/", auth_views.PasswordResetView.as_view(
        template_name="portal/auth/password_reset.html",
        email_template_name="portal/emails/password_reset_email.txt",
        subject_template_name="portal/emails/password_reset_subject.txt",
        success_url=reverse_lazy("password_reset_done"),
    ), name="password_reset"),
    path("password-reset/sent/", auth_views.PasswordResetDoneView.as_view(
        template_name="portal/auth/password_reset_done.html",
    ), name="password_reset_done"),
    path("reset/<uidb64>/<token>/", auth_views.PasswordResetConfirmView.as_view(
        template_name="portal/auth/password_reset_confirm.html",
        success_url=reverse_lazy("password_reset_complete"),
    ), name="password_reset_confirm"),
    path("reset/done/", auth_views.PasswordResetCompleteView.as_view(
        template_name="portal/auth/password_reset_complete.html",
    ), name="password_reset_complete"),
    path("profile/password/", admin_views.password_change_view, name="portal_password_change"),

    # Core Dashboard
    path("", views.dashboard_view, name="portal_home"),
    path("dashboard/", views.dashboard_view, name="portal_dashboard"),

    # Expenses
    path("expenses/", views.expenses_list_view, name="portal_expenses"),
    path("expenses/<int:pk>/", views.expense_detail_view, name="portal_expense_detail"),
    path("expenses/<int:pk>/edit/", admin_views.expense_edit_view, name="portal_expense_edit"),
    path("expenses/approvals/", views.expense_approvals_view, name="portal_expense_approvals"),
    path("expenses/reimbursements/", views.reimbursements_view, name="portal_reimbursements"),
    path("expenses/categories/", admin_views.expense_categories_view, name="portal_expense_categories"),

    # Leaves
    path("leaves/", views.leaves_list_view, name="portal_leaves"),
    path("leaves/<int:pk>/", views.leave_detail_view, name="portal_leave_detail"),
    path("leaves/approvals/", views.leave_approvals_view, name="portal_leave_approvals"),
    path("leaves/ledger/", views.leave_ledger_view, name="portal_leave_ledger"),
    path("leaves/settings/", admin_views.leave_settings_view, name="portal_leave_settings"),
    path("leaves/record/", admin_views.leave_record_on_behalf_view, name="portal_leave_record"),

    # Organization & Staff
    path("branches/", views.branches_view, name="portal_branches"),
    path("branches/<int:pk>/", views.branch_detail_view, name="portal_branch_detail"),
    path("employees/", views.employees_view, name="portal_employees"),
    path("employees/<int:pk>/edit/", admin_views.employee_edit_view, name="portal_employee_edit"),

    # Reports
    path("reports/", views.reports_view, name="portal_reports"),

    # Notifications & Profile
    path("notifications/", views.notifications_view, name="portal_notifications"),
    path("profile/", views.profile_view, name="portal_profile"),
]
