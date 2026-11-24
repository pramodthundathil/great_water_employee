"""
Single source of truth for who may see which report / analytics section.

  EMPLOYEE        no reports, no analytics
  LEAD_MANAGER    leave reports only (their team)
  ADMIN_MANAGER   expense / claim reports only (their branches)
  HR_COORDINATOR  every report (their branches — all branches when global scope)
  DIRECTOR        every report (their branches — all branches when global scope; superusers always)

Branch scope comes from User.get_accessible_branches(): a global-scope Admin Manager /
HR Coordinator sees every branch, a branch-assigned one only their primary + scoped branches.
"""
from accounts.models import RoleChoices

EXPENSE = "expense"
LEAVE = "leave"

# report slug -> domain
REPORT_DOMAINS = {
    "expenses": EXPENSE,
    "expense-summary": EXPENSE,
    "payments": EXPENSE,
    "leaves": LEAVE,
    "leave-balances": LEAVE,
    "pending-approvals": None,  # mixed: filtered down to the user's domains
}

ROLE_DOMAINS = {
    RoleChoices.LEAD_MANAGER: {LEAVE},
    RoleChoices.ADMIN_MANAGER: {EXPENSE},
    RoleChoices.HR_COORDINATOR: {EXPENSE, LEAVE},
    RoleChoices.DIRECTOR: {EXPENSE, LEAVE},
}


def effective_role(user):
    return RoleChoices.DIRECTOR if user.is_superuser else user.role


def report_domains(user):
    """Domains ('expense' / 'leave') this user may report on. Empty set = no reports."""
    return set(ROLE_DOMAINS.get(effective_role(user), set()))


def can_view_reports(user):
    return bool(report_domains(user))


def allowed_reports(user):
    domains = report_domains(user)
    if not domains:
        return set()
    return {slug for slug, dom in REPORT_DOMAINS.items() if dom is None or dom in domains}


def can_access_report(user, slug):
    return slug in allowed_reports(user)
