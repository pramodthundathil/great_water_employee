# Great Waters — Module Implementation & Execution Plan

**Author:** Antigravity Engineering Team  
**Architecture:** Django 5 + PostgreSQL + Bootstrap 5 + React Native  
**Document Version:** 1.0  

---

## Stage-by-Stage Execution Roadmap

### Stage 1: Discovery, Architecture & Baseline
- [x] Inspect directory and system tools (Python 3.13, Node v25, PostgreSQL 15 running on port 5433).
- [x] Establish PostgreSQL database `great_waters_db`.
- [x] Secure and inspect official branding asset (`great_waters_logo.jpg` with `#156B9E` maritime blue and `#21201E` charcoal).
- [x] Create project specification documents:
  - [x] `REQUIREMENTS.md`
  - [x] `DATABASE_DESIGN.md`
  - [x] `PERMISSION_MATRIX.md`
  - [x] `API_SPECIFICATION.md`
  - [x] `IMPLEMENTATION_PLAN.md`
  - [x] `TEST_PLAN.md`
  - [x] `DEPLOYMENT.md`

---

### Stage 2: Backend Foundation & Multi-Branch Architecture
- [ ] Initialize Django project (`config`) with modular apps:
  - `accounts`: Custom User, EmployeeProfile, UserBranchScope, ReportingRelationship.
  - `organization`: Organization, Branch, Department.
  - `expenses`: Categories, Claims, Attachments, Approvals, Reimbursements.
  - `leaves`: LeaveType, LeavePolicy, AccrualRule, AccrualRun, LeaveLedgerEntry, LeaveApplication, LeaveApproval, HolidayCalendar.
  - `notifications`: NotificationEvent, DeviceRegistration, EmailDelivery, FCM dispatcher.
  - `reports`: PDF and Excel export engine.
- [ ] Implement Custom User model extending `AbstractUser` before initial migration.
- [ ] Configure JWT authentication with SimpleJWT, token rotation, and blacklisting.
- [ ] Create and verify initial PostgreSQL migrations.
- [ ] Implement seed data script (`populate_initial_data.py`) with sample branches (Dubai, Kochi, Singapore), departments, roles, and test users.
- [ ] Write and run automated authentication and permission tests.

---

### Stage 3: Core Business Modules & Business Logic
- [ ] **Expenses Module:**
  - Mandatory receipt file upload and size/type validation.
  - State machine transitions (Draft -> Under Admin Review -> Under Director Review -> Approved -> Paid).
  - Admin Manager review & rejection remarks.
  - Director final approval & rejection remarks.
  - Admin Manager payment recording (Date, Mode, Reference).
- [ ] **Leave Module:**
  - Date overlap validation, half-day support, working days / holiday calendar deduction.
  - 4-Stage Approval Workflow (Employee -> Lead Manager -> HR Coordinator -> Director -> Deduction).
  - Earned-Leave Accrual Engine with idempotent execution and monthly/yearly service tracking.
  - Real-time immutable Leave Ledger with opening, accrual, deduction, and closing balances.
  - HR manual adjustment with mandatory audit remarks.
  - HR record leave on behalf of employees.

---

### Stage 4: Django Template Web Portal
- [ ] Design System & Layouts:
  - Responsive base templates (`base.html`, `auth_base.html`).
  - Great Waters header, sidebar, profile dropdown, unread notification badge.
  - Official Great Waters logo integration and `#156b9e` design tokens.
  - Bootstrap 5 + Vanilla CSS + Chart.js dashboard charts.
- [ ] Role-Tailored Portals:
  - Employee Portal: Personal expense claims, receipt viewer, leave applications, live balance card.
  - Lead / Manager Portal: Assigned team leave recommendations queue, team calendar.
  - Admin Manager Portal: Stage-1 expense approval queue, payment recording form, category management, user admin.
  - HR Coordinator Portal: Stage-2 leave verification queue, leave types/policies, manual balance adjustment, holiday calendar.
  - Director Portal: Executive cross-branch dashboard, final expense approval queue, final leave approval queue.
- [ ] Consolidated Branch View for Global Administrators with real-time branch switching.

---

### Stage 5: React Native Mobile Application
- [ ] Setup clean React Native mobile codebase supporting iOS and Android.
- [ ] Authentication & Secure Storage:
  - JWT storage, session refresh mutex, transparent access token renewal.
- [ ] Screen Architecture:
  - Auth: Login, Forgot Password.
  - Home: Role-tailored dashboard, greeting, quick action buttons, live balances.
  - Expenses: Claim list, Create Claim with multi-proof camera/gallery/PDF picker, Claim Detail with approval trail.
  - Leaves: Leave list, Apply Leave with half-day toggle and date picker, Live Balance view, Leave Detail.
  - Notifications: In-app notification center with read/unread status.
  - Profile: Employee profile, branch info, reporting lead, logout.

---

### Stage 6: Notifications, Reporting & Exports
- [ ] **Notification Engine:**
  - Web in-app notifications linked to relevant records.
  - FCM push notification dispatcher with mock/live service integration.
  - Email notification templates for workflow approvals and rejections.
- [ ] **Reports Engine:**
  - PDF generation with Great Waters branding header using ReportLab.
  - Excel generation with formatted headers using openpyxl.
  - Reports: Expense claims, Leave utilization, Reimbursement status, Pending approval aging.

---

### Stage 7: QA, Verification, Testing & Handover Documentation
- [ ] Comprehensive test suite execution (Auth, Permissions, Expenses, Leaves, Accrual, Reports).
- [ ] Verify CSRF protection, SQL injection resistance, and branch boundary isolation.
- [ ] Verify mobile session lifecycle (token expiration, concurrent requests, background/foreground).
- [ ] Create production deployment guides (`DEPLOYMENT.md`, `README.md`, `.env.example`, systemd/gunicorn/nginx configs).
- [ ] Final handover review against user acceptance criteria.
