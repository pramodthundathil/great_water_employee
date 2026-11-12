# Great Waters — Automated Test Plan & Quality Assurance Strategy

**Test Framework:** Django Test Suite / pytest  
**Database:** PostgreSQL (`great_waters_db`)  

---

## 1. Test Matrix Overview

### 1.1 Authentication & Session Lifecycle Tests
- `test_user_jwt_login_valid_credentials`: Verifies employee ID and email login returns valid access and refresh tokens.
- `test_user_jwt_login_invalid_credentials`: Verifies rate limiting, error codes, and failure responses.
- `test_jwt_token_refresh`: Verifies refresh token exchange rotates token and issues fresh access token.
- `test_jwt_revoked_token_rejection`: Verifies blacklisted refresh token cannot be reused.
- `test_web_session_csrf_protection`: Verifies session cookies and CSRF tokens on Django web template forms.

### 1.2 Multi-Branch Scoping & Permission Tests
- `test_employee_cannot_view_peer_expenses`: Verifies normal employee can only access their own claims.
- `test_branch_scoped_admin_cannot_access_other_branch`: Verifies Admin Manager in Branch A cannot view or approve claims from Branch B.
- `test_global_director_can_access_all_branches`: Verifies user with `is_global_scope=True` can view cross-branch records.
- `test_url_tampering_id_enumeration_rejected`: Verifies 404 or 403 when tampering with object IDs in URL or API payloads.
- `test_privilege_escalation_prevention`: Verifies non-admin cannot alter `role` or `is_global_scope` attributes.

### 1.3 Expense Workflow & Business Rules Tests
- `test_expense_requires_at_least_one_receipt`: Verifies submission fails without attachment.
- `test_expense_status_transitions`: Verifies `DRAFT` -> `UNDER_ADMIN_REVIEW` -> `UNDER_DIRECTOR_REVIEW` -> `APPROVED_BY_DIRECTOR` -> `PAID`.
- `test_expense_cannot_be_approved_by_unauthorized_role`: Verifies Employee or Lead Manager cannot execute Stage-1 or Stage-2 approvals.
- `test_admin_manager_rejection_with_remarks`: Verifies rejected claim moves to `REJECTED_BY_ADMIN` and returns to employee.
- `test_payment_recorded_only_on_director_approved_claims`: Verifies unapproved claims cannot have payment registered.

### 1.4 Leave Workflow & Accrual Engine Tests
- `test_leave_overlap_validation`: Verifies employee cannot apply for overlapping date ranges.
- `test_four_stage_leave_workflow`:
  1. Employee submits -> `PENDING_LEAD_RECOMMENDATION`
  2. Lead Manager recommends -> `PENDING_HR_VERIFICATION`
  3. HR Coordinator verifies -> `PENDING_DIRECTOR_APPROVAL`
  4. Director approves -> `APPROVED` & Ledger deduction.
- `test_pending_leave_does_not_deduct_official_balance`: Verifies leave days are only deducted upon Director final approval.
- `test_earned_leave_accrual_calculation`: Verifies accurate pro-rata or monthly days credited based on `date_of_joining`.
- `test_idempotent_accrual_execution`: Verifies running monthly accrual command twice does not generate duplicate credits.
- `test_hr_manual_adjustment_with_mandatory_remarks`: Verifies ledger adjustment records audit trail and adjusts closing balance.

### 1.5 Notification & Reporting Export Tests
- `test_notification_event_creation_on_workflow_step`: Verifies notifications created for next approver and employee.
- `test_device_registration_and_cleanup`: Verifies FCM tokens are registered and pruned on logout.
- `test_pdf_report_generation`: Verifies ReportLab generates valid non-empty PDF binary with Great Waters headers.
- `test_excel_report_generation`: Verifies openpyxl generates structured XLSX spreadsheet.
