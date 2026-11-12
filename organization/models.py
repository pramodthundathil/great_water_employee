from django.db import models


class Organization(models.Model):
    name = models.CharField(max_length=255, default="Great Waters")
    code = models.CharField(max_length=50, unique=True, default="GW")
    currency_code = models.CharField(max_length=10, default="USD")
    currency_symbol = models.CharField(max_length=10, default="$")
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Organization"
        verbose_name_plural = "Organizations"

    def __str__(self):
        return f"{self.name} ({self.code})"


class Branch(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="branches")
    code = models.CharField(max_length=20, unique=True, db_index=True)  # e.g., GW-DXB, GW-KCH
    name = models.CharField(max_length=255, db_index=True)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    country = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Branch"
        verbose_name_plural = "Branches"
        ordering = ["code"]

    def __str__(self):
        return f"{self.name} ({self.code})"


class Department(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="departments")
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Department"
        verbose_name_plural = "Departments"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.code})"
