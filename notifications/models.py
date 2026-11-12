from django.db import models


class NotificationEvent(models.Model):
    recipient = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="notifications"
    )
    title = models.CharField(max_length=200)
    message = models.TextField()
    event_type = models.CharField(max_length=50, db_index=True)
    link_url = models.CharField(max_length=255, blank=True)
    is_read = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Notification Event"
        verbose_name_plural = "Notification Events"
        ordering = ["-created_at"]

    def __str__(self):
        return f"To {self.recipient.employee_id}: {self.title} (Read: {self.is_read})"


class DeviceRegistration(models.Model):
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="devices"
    )
    registration_token = models.CharField(max_length=500, unique=True, db_index=True)
    device_type = models.CharField(
        max_length=20,
        choices=[("ANDROID", "Android"), ("IOS", "iOS"), ("WEB", "Web Browser")],
        default="ANDROID"
    )
    device_model = models.CharField(max_length=100, blank=True)
    app_version = models.CharField(max_length=30, blank=True)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Device Registration"
        verbose_name_plural = "Device Registrations"

    def __str__(self):
        return f"{self.user.employee_id} - {self.device_type} ({self.device_model})"
