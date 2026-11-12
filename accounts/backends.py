from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model
from django.db.models import Q

User = get_user_model()


class EmployeeIdOrEmailBackend(ModelBackend):
    """
    Allows authentication using either employee_id, email, or username.
    """
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None:
            username = kwargs.get("employee_id") or kwargs.get("email")
        if not username or not password:
            return None

        username = username.strip()
        try:
            user = User.objects.filter(
                Q(employee_id__iexact=username) |
                Q(email__iexact=username) |
                Q(username__iexact=username)
            ).first()
            if user and user.check_password(password) and self.user_can_authenticate(user):
                return user
        except Exception:
            return None
        return None
