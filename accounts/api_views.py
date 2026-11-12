from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.views import TokenObtainPairView
from accounts.serializers import CustomTokenObtainPairSerializer, UserDetailSerializer


class CustomTokenObtainPairView(TokenObtainPairView):
    """
    POST /api/v1/auth/token/
    """
    serializer_class = CustomTokenObtainPairSerializer


class CurrentUserView(APIView):
    """
    GET /api/v1/auth/me/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = UserDetailSerializer(request.user)
        return Response(serializer.data)


from django.conf import settings
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.throttling import AnonRateThrottle


class ProfileUpdateView(APIView):
    """
    PATCH /api/v1/auth/profile/  — employee updates own contact & bank details.
    """
    permission_classes = [IsAuthenticated]
    PROFILE_FIELDS = ["emergency_contact", "address", "bank_name", "bank_account_no", "bank_ifsc_or_swift"]

    def patch(self, request):
        user = request.user
        if "phone" in request.data:
            user.phone = str(request.data.get("phone", ""))[:30]
            user.save(update_fields=["phone"])
        profile = getattr(user, "profile", None)
        if profile:
            changed = []
            for f in self.PROFILE_FIELDS:
                if f in request.data:
                    setattr(profile, f, str(request.data.get(f, "")))
                    changed.append(f)
            if changed:
                profile.save(update_fields=changed)
        return Response(UserDetailSerializer(user, context={"request": request}).data)


class ChangePasswordView(APIView):
    """
    POST /api/v1/auth/change-password/  {old_password, new_password}
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        old_password = request.data.get("old_password", "")
        new_password = request.data.get("new_password", "")
        if not user.check_password(old_password):
            return Response({"detail": "Your current password is incorrect."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            validate_password(new_password, user)
        except DjangoValidationError as e:
            return Response({"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)
        user.set_password(new_password)
        user.save()
        return Response({"detail": "Password changed. Please sign in again on your other devices."})


class PasswordResetThrottle(AnonRateThrottle):
    scope = "password_reset"
    rate = "5/hour"


class ForgotPasswordView(APIView):
    """
    POST /api/v1/auth/forgot-password/  {email}
    Sends the same secure reset link as the web portal. Always returns 200 so
    the endpoint cannot be used to discover which e-mails are registered.
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PasswordResetThrottle]

    def post(self, request):
        email = str(request.data.get("email", "")).strip()
        form = PasswordResetForm({"email": email})
        if email and form.is_valid():
            base = getattr(settings, "PORTAL_BASE_URL", "http://localhost:8000")
            form.save(
                domain_override=base.split("://", 1)[-1],
                use_https=base.startswith("https"),
                subject_template_name="portal/emails/password_reset_subject.txt",
                email_template_name="portal/emails/password_reset_email.txt",
                request=None,
            )
        return Response({"detail": "If an active account exists for that e-mail, a password reset link has been sent."})
