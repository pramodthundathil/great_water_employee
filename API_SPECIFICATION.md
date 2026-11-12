# Great Waters — REST API Specification

**Base URL:** `/api/v1`  
**Authentication:** Bearer JWT (`Authorization: Bearer <access_token>`)  
**Data Format:** JSON (`Content-Type: application/json` or `multipart/form-data` for file uploads)  

---

## 1. Authentication & Session Management

### `POST /api/v1/auth/token/`
Obtain JWT token pair. Rate limited (5 attempts/min).
- **Request Body:**
  ```json
  {
    "username": "GW-001 or employee@greatwaters.com",
    "password": "SecretPassword123"
  }
  ```
- **Response (200 OK):**
  ```json
  {
    "access": "eyJhbGciOi...",
    "refresh": "eyJhbGciOi...",
    "user": {
      "id": 1,
      "employee_id": "GW-001",
      "email": "employee@greatwaters.com",
      "first_name": "John",
      "last_name": "Doe",
      "role": "EMPLOYEE",
      "is_global_scope": false,
      "branch": {
        "id": 1,
        "code": "GW-DXB",
        "name": "Dubai Headquarters"
      }
    }
  }
  ```

### `POST /api/v1/auth/token/refresh/`
Rotate and refresh access token.
- **Request Body:**
  ```json
  {
    "refresh": "eyJhbGciOi..."
  }
  ```
- **Response (200 OK):**
  ```json
  {
    "access": "eyJhbGciOi...",
    "refresh": "eyJhbGciOi..."
  }
  ```

### `POST /api/v1/auth/token/blacklist/` (Logout)
Blacklist the active refresh token and invalidate the mobile session.
- **Request Body:**
  ```json
  {
    "refresh": "eyJhbGciOi..."
  }
  ```
- **Response (200 OK):**
  ```json
  {
    "detail": "Token successfully invalidated."
  }
  ```

### `GET /api/v1/auth/me/`
Fetch authenticated user profile, permissions, and assigned branches.

---

## 2. Organization & Branches

### `GET /api/v1/organization/branches/`
List branches authorized for the current user. Global users receive all active branches.

### `GET /api/v1/organization/departments/`
List departments in the organization.

---

## 3. Expense Management

### `GET /api/v1/expenses/categories/`
List active expense categories.

### `GET /api/v1/expenses/claims/`
Query parameters: `?branch_id=&status=&category_id=&start_date=&end_date=`
Returns paginated list of claims. Filtered by role and branch scope.

### `POST /api/v1/expenses/claims/` (multipart/form-data)
Create a new expense claim.
- **Fields:**
  - `expense_date`: `2026-10-15`
  - `category`: `1` (ID)
  - `amount`: `125.50`
  - `description`: `Client dinner and transportation`
  - `project_reference`: `GW-PROJ-89` (Optional)
  - `status`: `DRAFT` or `UNDER_ADMIN_REVIEW`
  - `receipts`: `[binary file 1, binary file 2]` (Mandatory if submitting directly)

### `GET /api/v1/expenses/claims/{id}/`
Retrieve full claim details, receipts list, and approval history trail.

### `POST /api/v1/expenses/claims/{id}/submit/`
Submit a draft claim. Verifies that at least one receipt attachment exists.

### `POST /api/v1/expenses/claims/{id}/withdraw/`
Withdraw a submitted claim before review.

### `POST /api/v1/expenses/claims/{id}/admin-review/`
Admin Manager Stage 1 approval/rejection.
- **Request Body:**
  ```json
  {
    "action": "APPROVED", // or "REJECTED"
    "remarks": "Verified taxi receipt and tax invoice."
  }
  ```

### `POST /api/v1/expenses/claims/{id}/director-approval/`
Director Stage 2 final approval/rejection.
- **Request Body:**
  ```json
  {
    "action": "APPROVED", // or "REJECTED"
    "remarks": "Approved as per travel budget allowance."
  }
  ```

### `POST /api/v1/expenses/claims/{id}/record-payment/`
Admin Manager marks claim as paid.
- **Request Body:**
  ```json
  {
    "payment_date": "2026-10-18",
    "payment_mode": "BANK_TRANSFER",
    "payment_reference": "TXN9876543210",
    "remarks": "Disbursed via corporate payroll account."
  }
  ```

---

## 4. Leave Management & Accrual

### `GET /api/v1/leaves/types/`
List active leave types (Earned Leave, Casual Leave, Sick Leave, Unpaid Leave).

### `GET /api/v1/leaves/balance/`
Retrieve leave balances for the authenticated employee or specified employee (for HR/Lead).
- **Response (200 OK):**
  ```json
  {
    "employee_id": "GW-001",
    "balances": [
      {
        "leave_type_id": 1,
        "leave_type_name": "Earned Leave",
        "earned_days": 18.0,
        "taken_days": 4.0,
        "pending_days": 2.0,
        "available_balance": 14.0
      },
      {
        "leave_type_id": 2,
        "leave_type_name": "Casual Leave",
        "earned_days": 12.0,
        "taken_days": 1.0,
        "pending_days": 0.0,
        "available_balance": 11.0
      }
    ]
  }
  ```

### `GET /api/v1/leaves/ledger/`
Returns ledger entry transactions for the employee with running balances.

### `GET /api/v1/leaves/applications/`
Query parameters: `?branch_id=&status=&leave_type=&start_date=&end_date=`
Paginated list of leave applications.

### `POST /api/v1/leaves/applications/` (multipart/form-data)
Submit leave application.
- **Fields:**
  - `leave_type`: `1` (ID)
  - `start_date`: `2026-11-01`
  - `end_date`: `2026-11-03`
  - `is_half_day`: `false`
  - `half_day_period`: `FIRST_HALF` (Optional)
  - `reason`: `Annual family vacation`
  - `attachment`: `[binary file]` (Optional)

### `POST /api/v1/leaves/applications/{id}/lead-recommend/`
Lead / Manager Stage 1 recommendation.
- **Request Body:**
  ```json
  {
    "action": "RECOMMENDED", // or "REJECTED"
    "remarks": "Project deadlines covered during this duration."
  }
  ```

### `POST /api/v1/leaves/applications/{id}/hr-verify/`
HR Coordinator Stage 2 policy verification.
- **Request Body:**
  ```json
  {
    "action": "VERIFIED", // or "REJECTED"
    "remarks": "Policy balance verified. Eligible for 3 days."
  }
  ```

### `POST /api/v1/leaves/applications/{id}/director-approval/`
Director Stage 3 final approval/rejection.
- **Request Body:**
  ```json
  {
    "action": "APPROVED", // or "REJECTED"
    "remarks": "Final approval granted."
  }
  ```

### `POST /api/v1/leaves/adjust-balance/`
HR Coordinator manual ledger adjustment with mandatory audit remarks.
- **Request Body:**
  ```json
  {
    "employee_id": 1,
    "leave_type_id": 1,
    "days": 2.0, // positive to credit, negative to debit
    "remarks": "Compensatory off credit approved for weekend deployment."
  }
  ```

---

## 5. Notifications & Push Device Registration

### `GET /api/v1/notifications/`
List user notifications (unread first).

### `POST /api/v1/notifications/{id}/mark-read/`
Mark single notification as read.

### `POST /api/v1/notifications/mark-all-read/`
Mark all user notifications as read.

### `POST /api/v1/devices/register/`
Register/update mobile device FCM push token.
- **Request Body:**
  ```json
  {
    "registration_token": "fcm_token_string_here",
    "device_type": "ANDROID", // or "IOS"
    "device_model": "Pixel 8 Pro",
    "app_version": "1.0.0"
  }
  ```

### `POST /api/v1/devices/unregister/`
Remove device token on logout.

---

## 6. Reports & Analytics

### `GET /api/v1/reports/expenses/export/`
Query params: `?format=pdf|excel&branch_id=&start_date=&end_date=`

### `GET /api/v1/reports/leaves/export/`
Query params: `?format=pdf|excel&branch_id=&start_date=&end_date=`

### `GET /api/v1/reports/reimbursements/export/`
Query params: `?format=pdf|excel&branch_id=&start_date=&end_date=`

### `GET /api/v1/reports/pending-approvals/`
Query params: `?format=pdf|excel&branch_id=`
