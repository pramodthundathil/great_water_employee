from rest_framework import serializers
from notifications.models import NotificationEvent, DeviceRegistration


class NotificationEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationEvent
        fields = ["id", "title", "message", "event_type", "link_url", "is_read", "created_at"]


class DeviceRegistrationSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceRegistration
        fields = ["registration_token", "device_type", "device_model", "app_version"]
