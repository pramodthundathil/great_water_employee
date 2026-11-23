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
    currency_code = models.CharField(
        max_length=3, default="USD",
        help_text="Reimbursement currency: every expense claim of this branch is approved and paid in it "
                  "(e.g. AED for Dubai, INR for Kochi).",
    )
    allowed_currencies = models.CharField(
        max_length=255, blank=True,
        help_text="Comma-separated currency codes employees may spend in. Blank = any currency.",
    )
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Branch"
        verbose_name_plural = "Branches"
        ordering = ["code"]

    def __str__(self):
        return f"{self.name} ({self.code})"

    @property
    def allowed_currency_list(self):
        """Explicitly configured expense currencies (empty list = any currency is allowed)."""
        from organization.currencies import parse_code_list
        return parse_code_list(self.allowed_currencies)

    def claim_currencies(self):
        """Currencies an employee of this branch can pick for an expense item (reimbursement currency first)."""
        from organization.currencies import CURRENCIES
        allowed = self.allowed_currency_list
        codes = allowed or [c[0] for c in CURRENCIES]
        return [self.currency_code] + [c for c in codes if c != self.currency_code]

    def accepts_currency(self, code):
        allowed = self.allowed_currency_list
        return not allowed or code == self.currency_code or code in allowed

    def currency_config(self):
        from organization.currencies import currency_json
        return {
            "currency": currency_json(self.currency_code),
            "allowed_currencies": self.allowed_currency_list,
            "claim_currencies": [currency_json(c) for c in self.claim_currencies()],
        }


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
