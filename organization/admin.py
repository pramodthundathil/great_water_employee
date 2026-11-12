from django.contrib import admin
from organization.models import Organization, Branch, Department

admin.site.register(Organization)
admin.site.register(Branch, list_display=("code", "name", "city", "country", "is_active"))
admin.site.register(Department, list_display=("code", "name", "is_active"))
