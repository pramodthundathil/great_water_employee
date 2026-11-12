# Great Waters — Employee Expense & Leave Management System

A multi-branch enterprise management platform engineered for **Great Waters**, integrating automated expense reimbursements, multi-tier approval workflows, earned-leave accrual tracking, immutable balance ledgers, real-time notifications, executive reporting, and cross-platform mobile connectivity.

---

## Key Features

1. **Multi-Branch Organization Architecture**
   - Branch-scoped and Organization-wide permission layers.
   - Cross-branch consolidated view with instant branch switcher for authorized directors.
   - Full branch isolation: prevention of ID manipulation and cross-branch data leakage.

2. **Distinct Business Roles**
   - **Employee:** Submit expenses with mandatory receipt proofs, apply for leaves (full/half day), view personal leave ledger and live balances.
   - **Lead / Manager:** Review and recommend leave applications for direct reporting staff.
   - **Admin Manager:** Stage-1 expense claim review, record reimbursement payments (with UTR/reference), manage categories, user administration.
   - **HR Coordinator:** Stage-2 leave verification, leave type & policy administration, manual ledger adjustments with mandatory remarks, holiday calendar.
   - **Director:** Stage-2 final expense approval, Stage-3 final leave approval, cross-branch consolidated dashboards and reports.

3. **Multi-Stage Approval Workflows**
   - **Expense:** Employee -> Admin Manager (Stage 1) -> Director (Stage 2) -> Admin Manager Records Payment.
   - **Leave:** Employee -> Lead Manager Recommendation (Stage 1) -> HR Coordinator Verification (Stage 2) -> Director Final Approval (Stage 3) -> Real-time Ledger Deduction.

4. **Earned-Leave Accrual Engine**
   - Automated accrual calculation based on joining date and tenure.
   - Idempotent execution: safe to rerun scheduled runs without duplicate credits.
   - Immutable double-entry style leave ledger with running balance calculations.
   - Separation of official balance and pending days.

5. **Technology Stack**
   - **Backend:** Python 3.13, Django 5.1, Django REST Framework, PostgreSQL 15, SimpleJWT.
   - **Web Management Portal:** Django Server-Side Templates, Bootstrap 5, Chart.js, Custom CSS Design System with Great Waters official maritime branding (`#156B9E`).
   - **Mobile App:** React Native, cross-platform Android & iOS, secure Keychain/Keystore token storage, FCM push integration.
   - **Reporting & Exports:** ReportLab PDF generator and openpyxl Excel exporter.

---

## Documentation Index
- [`REQUIREMENTS.md`](file:///Users/pramodgopinath/Desktop/Projects/Great_waters_LM_EM/REQUIREMENTS.md): Detailed software requirements specification and business rules.
- [`DATABASE_DESIGN.md`](file:///Users/pramodgopinath/Desktop/Projects/Great_waters_LM_EM/DATABASE_DESIGN.md): PostgreSQL entity relationship diagram, table schemas, indexes, and constraints.
- [`PERMISSION_MATRIX.md`](file:///Users/pramodgopinath/Desktop/Projects/Great_waters_LM_EM/PERMISSION_MATRIX.md): Dual-dimension security matrix and access scopes.
- [`API_SPECIFICATION.md`](file:///Users/pramodgopinath/Desktop/Projects/Great_waters_LM_EM/API_SPECIFICATION.md): Complete REST API contract for mobile and external integrations.
- [`IMPLEMENTATION_PLAN.md`](file:///Users/pramodgopinath/Desktop/Projects/Great_waters_LM_EM/IMPLEMENTATION_PLAN.md): Stage-by-stage implementation milestones.
- [`TEST_PLAN.md`](file:///Users/pramodgopinath/Desktop/Projects/Great_waters_LM_EM/TEST_PLAN.md): Test suite, unit tests, integration tests, and QA coverage.
- [`DEPLOYMENT.md`](file:///Users/pramodgopinath/Desktop/Projects/Great_waters_LM_EM/DEPLOYMENT.md): Production deployment runbook, Gunicorn, Nginx, Systemd, backups.

---

## Quickstart (Development Setup)

### 1. Prerequisites
- Python 3.11+
- PostgreSQL 15+
- Node.js 18+ (for mobile app)

### 2. Environment Setup
```bash
# Clone and enter directory
cd /Users/pramodgopinath/Desktop/Projects/Great_waters_LM_EM

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run migrations
python manage.py migrate

# Seed initial data (branches, roles, demo users, policies)
python manage.py populate_initial_data

# Run dev server
python manage.py runserver 0.0.0.0:8000
```
Web portal is available at `http://localhost:8000/`.
