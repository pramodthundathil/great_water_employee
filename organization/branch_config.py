"""
Branch ownership rules for configuration records (leave types, leave policies, expense
categories, holiday calendars).

A record with branch=None is organisation-wide: everyone can see it, but only users with access
to all branches (superuser, or an organisation-wide role) may change it. A record with a branch
applies to that branch only and may be changed by anyone who configures that branch.
"""
from django.core.exceptions import ValidationError
from django.db.models import Q


class BranchScopeError(ValidationError):
    """The user tried to configure something outside the branches they can access."""

    def __str__(self):
        return " ".join(self.messages)


def can_configure_branch(user, branch_id):
    """May `user` change configuration owned by this branch (None = organisation-wide)?"""
    if user.has_all_branch_access:
        return True
    if branch_id is None:
        return False
    return user.get_accessible_branches().filter(pk=branch_id).exists()


def require_configurable(user, branch_id):
    if not can_configure_branch(user, branch_id):
        raise BranchScopeError(
            "This is an organisation-wide setting. Only users with access to all branches can change it."
            if branch_id is None else "This setting belongs to a branch outside your access scope."
        )


def visible_to(user, qs, field="branch"):
    """Organisation-wide records plus those of the user's branches."""
    if user.has_all_branch_access:
        return qs
    return qs.filter(Q(**{f"{field}__isnull": True}) | Q(**{f"{field}__in": user.get_accessible_branches()}))


def available_in(branch_id, qs, field="branch"):
    """Records usable by staff of one branch: organisation-wide ones plus that branch's own."""
    scope = Q(**{f"{field}__isnull": True})
    if branch_id:
        scope |= Q(**{f"{field}_id": branch_id})
    return qs.filter(scope)


def resolve_config_branch(user, branch_id):
    """
    The branch a new configuration record should belong to. Users with access to all branches may
    leave it blank (organisation-wide); everyone else always gets one of their own branches.
    """
    accessible = user.get_accessible_branches()
    if branch_id not in (None, ""):
        branch = accessible.filter(pk=branch_id).first() if str(branch_id).isdigit() else None
        if branch is None:
            raise BranchScopeError("You cannot configure that branch.")
        return branch
    if user.has_all_branch_access:
        return None
    branch = accessible.filter(pk=user.primary_branch_id).first() or accessible.first()
    if branch is None:
        raise BranchScopeError("Your account has no branch assigned. Contact the administrator.")
    return branch


def can_manage_all_branches(user):
    """Create / deactivate / delete offices and manage departments: needs access to all branches."""
    return user.has_all_branch_access and user.role in ("DIRECTOR", "ADMIN_MANAGER", "HR_COORDINATOR")


def can_edit_branch(user, branch):
    """Edit an office's details. A branch-level Director may do so for their own offices."""
    return can_manage_all_branches(user) or (user.is_director and user.can_access_branch(branch))
