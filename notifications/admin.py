from django.contrib import admin
from notifications.models import NotificationEvent, DeviceRegistration

admin.site.register(NotificationEvent, list_display=("recipient", "title", "event_type", "is_read", "created_at"))
admin.site.register(DeviceRegistration, list_display=("user", "device_type", "device_model", "is_active", "last_seen_at"))
