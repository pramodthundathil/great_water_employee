import time

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect


class InactivityLogoutMiddleware:
    """
    Signs web-portal users out after SESSION_IDLE_TIMEOUT_MINUTES of inactivity.
    API requests (JWT, used by the mobile app) are not affected.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/api/") or request.path.startswith("/static/") or request.path.startswith("/media/"):
            return self.get_response(request)

        if request.user.is_authenticated:
            timeout = getattr(settings, "SESSION_IDLE_TIMEOUT_MINUTES", 30) * 60
            now = int(time.time())
            last_seen = request.session.get("last_activity_ts")
            if last_seen and now - last_seen > timeout:
                logout(request)
                messages.info(request, "You were signed out after a period of inactivity. Please sign in again.")
                return redirect(settings.LOGIN_URL)
            request.session["last_activity_ts"] = now

        return self.get_response(request)
