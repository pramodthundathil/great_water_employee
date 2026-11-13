from django.db import models
from django.contrib.auth.models import AbstractUser
from django.utils import timezone


class RoleChoices(models.TextChoices):
    EMPLOYEE = "EMPLOYEE", "Employee"
    LEAD_MANAGER = "LEAD_MANAGER", "Lead / Manager"
    ADMIN_MANAGER = "ADMIN_MANAGER", "Admin Manager"
    HR_COORDINATOR = "HR_COORDINATOR", "HR Coordinator"
    DIRECTOR = "DIRECTOR", "Director"


class User(AbstractUser):
    employee_id = models.CharField(
        max_length=30,
        unique=True,
        db_index=True,
        help_text="Unique company employee ID (e.g., GW-001)"
    )
    email = models.EmailField(unique=True, db_index=True)
    role = models.CharField(
        max_length=30,
        choices=RoleChoices.choices,
        default=RoleChoices.EMPLOYEE,
        db_index=True
    )
    is_global_scope = models.BooleanField(
        default=False,
        help_text="If True, user can view and manage organization-wide records across all branches (subject to role)"
    )
    primary_branch = models.ForeignKey(
        "organization.Branch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="primary_employees"
    )
    phone = models.CharField(max_length=30, blank=True)
    avatar = models.ImageField(upload_to="avatars/", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "employee_id"
    REQUIRED_FIELDS = ["email", "username"]

    class Meta:
        verbose_name = "User"
        verbose_name_plural = "Users"
        ordering = ["employee_id"]

    def __str__(self):
        name = self.get_full_name() or self.username
        return f"{name} ({self.employee_id}) - {self.get_role_display()}"

    def save(self, *args, **kwargs):
        # The system administrator (superuser) is always the Director with organisation-wide scope,
        # whether created via createsuperuser, the Django admin, or promoted later.
        if self.is_superuser:
            self.role = RoleChoices.DIRECTOR
            self.is_global_scope = True
            self.is_staff = True
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = set(update_fields) | {"role", "is_global_scope", "is_staff"}
        super().save(*args, **kwargs)

    @property
    def full_name(self):
        name = f"{self.first_name} {self.last_name}".strip()
        return name if name else self.username

    def get_accessible_branches(self):
        """
        Returns QuerySet of Branch objects accessible to this user.
        Enforces dual-dimension security: Role + Scope.
        """
        from organization.models import Branch

        if self.is_superuser or (self.is_global_scope and self.role in [
            RoleChoices.DIRECTOR,
            RoleChoices.ADMIN_MANAGER,
            RoleChoices.HR_COORDINATOR,
        ]):
            return Branch.objects.filter(is_active=True)

        branch_ids = set()
        if self.primary_branch_id:
            branch_ids.add(self.primary_branch_id)

        scoped_branch_ids = self.branch_scopes.filter(is_active=True).values_list("branch_id", flat=True)
        branch_ids.update(scoped_branch_ids)

        return Branch.objects.filter(id__in=branch_ids, is_active=True)

    def can_access_branch(self, branch):
        """
        Verifies if user has authorization to access the specified branch.
        """
        if not branch:
            return False
        if self.is_superuser or (self.is_global_scope and self.role in [
            RoleChoices.DIRECTOR,
            RoleChoices.ADMIN_MANAGER,
            RoleChoices.HR_COORDINATOR,
        ]):
            return True
        if self.primary_branch_id == branch.id:
            return True
        return self.branch_scopes.filter(branch=branch, is_active=True).exists()

    def get_expense_branches(self):
        """
        Branches whose expense claims this user may review / pay.
        Like get_accessible_branches(), but a cross-branch scope only counts when it grants
        expense management (UserBranchScope.can_manage_expenses).
        """
        from organization.models import Branch

        if self.is_superuser or (self.is_global_scope and self.role in [
            RoleChoices.DIRECTOR,
            RoleChoices.ADMIN_MANAGER,
        ]):
            return Branch.objects.filter(is_active=True)

        branch_ids = set()
        if self.primary_branch_id:
            branch_ids.add(self.primary_branch_id)
        branch_ids.update(
            self.branch_scopes.filter(is_active=True, can_manage_expenses=True).values_list("branch_id", flat=True)
        )
        return Branch.objects.filter(id__in=branch_ids, is_active=True)

    def can_manage_expenses_for(self, branch):
        if not branch:
            return False
        return self.get_expense_branches().filter(id=branch.id).exists()

    @property
    def is_lead_manager(self):
        return self.role == RoleChoices.LEAD_MANAGER or self.is_superuser

    @property
    def is_admin_manager(self):
        return self.role == RoleChoices.ADMIN_MANAGER or self.is_superuser

    @property
    def is_hr_coordinator(self):
        return self.role == RoleChoices.HR_COORDINATOR or self.is_superuser

    @property
    def is_director(self):
        return self.role == RoleChoices.DIRECTOR or self.is_superuser


class UserBranchScope(models.Model):
    """
    Explicit cross-branch access assignment for Managers and Coordinators.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="branch_scopes")
    branch = models.ForeignKey("organization.Branch", on_delete=models.CASCADE, related_name="scoped_users")
    can_manage_expenses = models.BooleanField(default=True)
    can_manage_leaves = models.BooleanField(default=True)
    effective_from = models.DateField(default=timezone.now)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "User Branch Scope"
        verbose_name_plural = "User Branch Scopes"
        unique_together = ("user", "branch")

    def __str__(self):
        return f"{self.user.employee_id} -> {self.branch.code} (Active: {self.is_active})"


class EmployeeProfile(models.Model):
    """
    Detailed profile and reporting relationship for an employee.
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    department = models.ForeignKey(
        "organization.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="employees"
    )
    designation = models.CharField(max_length=100, blank=True)
    reporting_lead = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="direct_reports",
        help_text="Lead / Manager who reviews leave applications for this employee"
    )
    date_of_joining = models.DateField(default=timezone.now, db_index=True)
    emergency_contact = models.CharField(max_length=100, blank=True)
    pan_or_national_id = models.CharField(max_length=50, blank=True)
    bank_name = models.CharField(max_length=100, blank=True)
    bank_account_no = models.CharField(max_length=50, blank=True)
    bank_ifsc_or_swift = models.CharField(max_length=50, blank=True)
    address = models.TextField(blank=True)

    class Meta:
        verbose_name = "Employee Profile"
        verbose_name_plural = "Employee Profiles"

    def __str__(self):
        return f"Profile of {self.user.employee_id} - {self.designation}"
