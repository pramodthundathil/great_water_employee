# Great Waters — Access Control & Role Permission Matrix

**Architecture:** Dual-Dimension Security Model  
**Dimension 1:** Business Role (`EMPLOYEE`, `LEAD_MANAGER`, `ADMIN_MANAGER`, `HR_COORDINATOR`, `DIRECTOR`)  
**Dimension 2:** Geographic Scope (`BRANCH_SCOPED`, `ORGANIZATION_WIDE`)  

---

## 1. Permission Matrix by Role & Function

| System Entity / Action | Employee | Lead / Manager | Admin Manager | HR Coordinator | Director | Global Admin |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| **Profile & Settings** |
| View own profile | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Update own profile contact/photo | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Organization & Branches** |
| View assigned branch details | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| View all branches (consolidated) | ❌ | Scope-based | Scope-based | Scope-based | ✅ | ✅ |
| Create / Edit Branches & Depts | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ |
| **User & Employee Management** |
| View reporting subordinates | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ |
| View branch employee directory | ❌ | ✅ (Branch) | ✅ (Branch) | ✅ (Branch) | ✅ | ✅ |
| Create / Edit Employee Records | ❌ | ❌ | ✅ | ✅ | ❌ | ✅ |
| Assign Roles & Branch Scopes | ❌ | ❌ | ✅ | ❌ | ❌ | ✅ |
| Deactivate Employee Accounts | ❌ | ❌ | ✅ | ❌ | ❌ | ✅ |
| **Expense Management** |
| Create / Draft / Edit Own Claim | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Upload Mandatory Receipts | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Submit / Withdraw Own Claim | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| View Own Expense Claims | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Manage Expense Categories | ❌ | ❌ | ✅ | ❌ | ❌ | ✅ |
| Stage 1 Review (Approve/Reject) | ❌ | ❌ | ✅ (Branch) | ❌ | ❌ | ❌ |
| Stage 2 Final Approval/Reject | ❌ | ❌ | ❌ | ❌ | ✅ (Org/Branch) | ❌ |
| Record Payment & UTR Reference | ❌ | ❌ | ✅ (Branch) | ❌ | ❌ | ❌ |
| Generate Expense Reports | Own only | ❌ | ✅ (Branch) | ❌ | ✅ (All) | ✅ (All) |
| Generate Reimbursement Reports | ❌ | ❌ | ✅ (Branch) | ❌ | ✅ (All) | ✅ (All) |
| **Leave Management** |
| Submit Own Leave Application | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| View Own Leave Balances & Ledger| ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Stage 1 Review (Recommend/Return) | ❌ | ✅ (Assigned) | ❌ | ❌ | ❌ | ❌ |
| Stage 2 Review (HR Policy Verify)| ❌ | ❌ | ❌ | ✅ (Branch) | ❌ | ❌ |
| Stage 3 Final Approval/Reject | ❌ | ❌ | ❌ | ❌ | ✅ (Org/Branch) | ❌ |
| Record Leave on Employee Behalf | ❌ | ❌ | ❌ | ✅ (Branch) | ❌ | ❌ |
| Manual Balance Adjustment (Remarks)| ❌ | ❌ | ❌ | ✅ (Branch) | ❌ | ❌ |
| Manage Leave Types & Accrual Rules| ❌ | ❌ | ❌ | ✅ | ❌ | ✅ |
| Manage Holiday Calendars | ❌ | ❌ | ❌ | ✅ (Branch) | ❌ | ✅ |
| Run Scheduled Leave Accrual | ❌ | ❌ | ❌ | ✅ | ❌ | System/Admin |
| Process Yearly Carry-Forward/Reset| ❌ | ❌ | ❌ | ✅ | ❌ | System/Admin |
| Generate Leave Summary Reports | Own only | Team only | ❌ | ✅ (Branch) | ✅ (All) | ✅ (All) |
| **Dashboards & Executive Views** |
| Personal Employee Dashboard | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Lead Manager Team Dashboard | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| Admin Manager Operations Dashboard| ❌ | ❌ | ✅ | ❌ | ❌ | ❌ |
| HR Coordinator Workforce Dashboard| ❌ | ❌ | ❌ | ✅ | ❌ | ❌ |
| Executive Cross-Branch Dashboard | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ |

---

## 2. Server-Side Enforcement Rules

### 2.1 Branch Scoping in Django Querysets
Every database query for models with branch affinity (`ExpenseClaim`, `LeaveApplication`, `EmployeeProfile`, `HolidayCalendar`) must filter against the user's authorized branch list:

```python
def get_user_branches(user):
    """
    Returns QuerySet of Branch objects accessible to the user.
    """
    if user.is_superuser or (user.is_global_scope and user.role in ['DIRECTOR', 'ADMIN_MANAGER', 'HR_COORDINATOR']):
        return Branch.objects.filter(is_active=True)
    
    # Primary branch plus any explicitly assigned branch scopes
    assigned_branch_ids = user.branch_scopes.values_list('branch_id', flat=True)
    branch_ids = set(assigned_branch_ids)
    if user.primary_branch_id:
        branch_ids.add(user.primary_branch_id)
    return Branch.objects.filter(id__in=branch_ids, is_active=True)
```

### 2.2 ID Manipulation & Privilege Escalation Protection
- Accessing `/expenses/claims/<id>/` or `/api/v1/expenses/<id>/` checks `claim.branch_id in user_branches` AND `claim.employee == user or user.has_approval_role()`.
- Modification forms reject any submission if `branch_id` does not belong to the user's authorized branches.
- Admin role assignments (`accounts.User.role`) can only be executed by users with `ADMIN_MANAGER` or `is_superuser`, and cannot assign a higher privilege level than their own.
