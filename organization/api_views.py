from rest_framework import serializers
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from organization.models import Organization, Branch, Department


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name", "code", "currency_code", "currency_symbol", "contact_email", "contact_phone"]


class BranchSerializer(serializers.ModelSerializer):
    class Meta:
        model = Branch
        fields = ["id", "code", "name", "address", "city", "country", "phone", "is_active"]


class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = ["id", "code", "name", "is_active"]


class UserBranchListView(APIView):
    """
    GET /api/v1/organization/branches/
    Returns branches accessible to current user.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        branches = request.user.get_accessible_branches()
        serializer = BranchSerializer(branches, many=True)
        return Response(serializer.data)


class DepartmentListView(APIView):
    """
    GET /api/v1/organization/departments/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        departments = Department.objects.filter(is_active=True)
        serializer = DepartmentSerializer(departments, many=True)
        return Response(serializer.data)
