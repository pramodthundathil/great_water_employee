from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404
from django.utils import timezone

from notifications.models import NotificationEvent, DeviceRegistration
from notifications.serializers import NotificationEventSerializer, DeviceRegistrationSerializer


class NotificationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        notifications = NotificationEvent.objects.filter(recipient=request.user).order_by("is_read", "-created_at")[:50]
        unread_count = NotificationEvent.objects.filter(recipient=request.user, is_read=False).count()
        return Response({
            "unread_count": unread_count,
            "notifications": NotificationEventSerializer(notifications, many=True).data
        })


class NotificationMarkReadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        item = get_object_or_404(NotificationEvent, pk=pk, recipient=request.user)
        item.is_read = True
        item.save()
        return Response({"detail": "Notification marked as read."})


class NotificationMarkAllReadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        NotificationEvent.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
        return Response({"detail": "All notifications marked as read."})


class DeviceRegisterView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        token = request.data.get("registration_token")
        if not token:
            return Response({"detail": "registration_token is required."}, status=status.HTTP_400_BAD_REQUEST)

        device_type = request.data.get("device_type", "ANDROID")
        device_model = request.data.get("device_model", "")
        app_version = request.data.get("app_version", "")

        device, created = DeviceRegistration.objects.update_or_create(
            registration_token=token,
            defaults={
                "user": request.user,
                "device_type": device_type,
                "device_model": device_model,
                "app_version": app_version,
                "is_active": True,
                "last_seen_at": timezone.now()
            }
        )
        return Response({"detail": "Device registered successfully.", "created": created})


class DeviceUnregisterView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        token = request.data.get("registration_token")
        if token:
            DeviceRegistration.objects.filter(registration_token=token, user=request.user).update(is_active=False)
        return Response({"detail": "Device unregistered."})
