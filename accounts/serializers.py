from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from django.contrib.auth import authenticate
from accounts.models import User, EmployeeProfile, UserBranchScope, RoleChoices
from organization.models import Branch, Department


class BranchBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Branch
        fields = ["id", "code", "name", "city", "country"]


class DepartmentBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = ["id", "code", "name"]


class EmployeeProfileSerializer(serializers.ModelSerializer):
    department = DepartmentBriefSerializer(read_only=True)
    reporting_lead_name = serializers.CharField(source="reporting_lead.full_name", read_only=True)
    reporting_lead_id_str = serializers.CharField(source="reporting_lead.employee_id", read_only=True)

    class Meta:
        model = EmployeeProfile
        fields = [
            "designation", "department", "reporting_lead_name",
            "reporting_lead_id_str", "date_of_joining", "emergency_contact",
            "bank_name", "bank_account_no", "bank_ifsc_or_swift", "address"
        ]


class UserDetailSerializer(serializers.ModelSerializer):
    primary_branch = BranchBriefSerializer(read_only=True)
    profile = EmployeeProfileSerializer(read_only=True)
    accessible_branches = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "employee_id", "email", "username", "first_name", "last_name",
            "role", "is_global_scope", "primary_branch", "phone", "avatar",
            "profile", "accessible_branches"
        ]

    def get_accessible_branches(self, obj):
        branches = obj.get_accessible_branches()
        return BranchBriefSerializer(branches, many=True).data


class CustomTokenObtainPairSerializer(serializers.Serializer):
    """
    Custom JWT serializer supporting employee ID, username, or email login.
    Returns access, refresh tokens, and serialized user profile payload.
    """
    username = serializers.CharField(required=False)
    employee_id = serializers.CharField(required=False)
    email = serializers.CharField(required=False)
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        login_id = attrs.get("username") or attrs.get("employee_id") or attrs.get("email")
        password = attrs.get("password")

        if not login_id:
            raise serializers.ValidationError({"username": ["This field is required."]})

        user = authenticate(
            request=self.context.get("request"),
            username=login_id,
            password=password
        )

        if not user:
            raise serializers.ValidationError({"detail": "Invalid credentials. Please verify your Employee ID/Email and password."})

        if not user.is_active:
            raise serializers.ValidationError({"detail": "This employee account has been deactivated."})

        from rest_framework_simplejwt.tokens import RefreshToken
        refresh = RefreshToken.for_user(user)

        return {
            "refresh": str(refresh),
            "access": str(refresh.access_token),
            "user": UserDetailSerializer(user).data
        }
