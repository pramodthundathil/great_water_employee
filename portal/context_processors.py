from django.conf import settings
from notifications.models import NotificationEvent


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

        is_global = request.user.has_all_branch_access
        context["is_global_user"] = is_global

        # Session or parameter based branch filter
        selected = request.GET.get("branch_filter") or request.session.get("selected_branch_id")
        if selected and (selected == "all" or any(str(b.id) == str(selected) for b in accessible_branches)):
            context["selected_branch_id"] = selected
            request.session["selected_branch_id"] = selected
        elif not is_global and len(accessible_branches) == 1:
            context["selected_branch_id"] = str(accessible_branches[0].id)
        else:
            context["selected_branch_id"] = "all"

    return context
