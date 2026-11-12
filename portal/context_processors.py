from django.conf import settings
from notifications.models import NotificationEvent
from accounts.models import RoleChoices


def great_waters_context(request):
    """
    Supplies global branding, active user scopes, unread notifications,
    and branch selector context to all Django templates.
    """
    context = {
        "brand_name": "Great Waters",
        "brand_primary": "#156B9E",
        "brand_secondary": "#0D4D73",
        "brand_dark": "#21201E",
        "unread_notifications_count": 0,
        "user_accessible_branches": [],
        "selected_branch_id": None,
        "is_global_user": False,
        "show_demo_logins": settings.DEBUG,
    }

    if request.user.is_authenticated:
        context["unread_notifications_count"] = NotificationEvent.objects.filter(
            recipient=request.user,
            is_read=False
        ).count()

        accessible_branches = list(request.user.get_accessible_branches())
        context["user_accessible_branches"] = accessible_branches

        is_global = request.user.is_superuser or (
            request.user.is_global_scope and request.user.role in [
                RoleChoices.DIRECTOR, RoleChoices.ADMIN_MANAGER, RoleChoices.HR_COORDINATOR
            ]
        )
        context["is_global_user"] = is_global

        # Session or parameter based branch filter
        selected = request.GET.get("branch_filter") or request.session.get("selected_branch_id")
        if selected and (selected == "all" or any(str(b.id) == str(selected) for b in accessible_branches)):
            context["selected_branch_id"] = selected
            request.session["selected_branch_id"] = selected
        elif not is_global and request.user.primary_branch_id:
            context["selected_branch_id"] = str(request.user.primary_branch_id)
        else:
            context["selected_branch_id"] = "all"

    return context
