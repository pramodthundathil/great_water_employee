from rest_framework import serializers
from decimal import Decimal
from leaves.models import (
    LeaveType, LeavePolicy, LeaveApplication,
    LeaveApproval, LeaveLedgerEntry, HolidayCalendar, Holiday
)


class LeaveTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = LeaveType
        fields = ["id", "name", "code", "is_paid", "is_accrual_based", "requires_attachment"]


class LeaveApprovalSerializer(serializers.ModelSerializer):
    approver_name = serializers.CharField(source="approver.full_name", read_only=True)
    approver_id_str = serializers.CharField(source="approver.employee_id", read_only=True)

    class Meta:
        model = LeaveApproval
        fields = ["id", "stage", "approver_name", "approver_id_str", "action", "remarks", "created_at"]


class LeaveLedgerEntrySerializer(serializers.ModelSerializer):
    leave_type_code = serializers.CharField(source="leave_type.code", read_only=True)
    leave_type_name = serializers.CharField(source="leave_type.name", read_only=True)
    created_by_name = serializers.CharField(source="created_by.full_name", read_only=True)

    class Meta:
        model = LeaveLedgerEntry
        fields = [
            "id", "leave_type_code", "leave_type_name", "entry_type",
            "transaction_days", "balance_after", "reference_note",
            "created_by_name", "created_at"
        ]


class LeaveApplicationListSerializer(serializers.ModelSerializer):
    leave_type_code = serializers.CharField(source="leave_type.code", read_only=True)
    leave_type_name = serializers.CharField(source="leave_type.name", read_only=True)
    branch_code = serializers.CharField(source="branch.code", read_only=True)
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    employee_id_str = serializers.CharField(source="employee.employee_id", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = LeaveApplication
        fields = [
            "id", "application_number", "start_date", "end_date",
            "is_half_day", "half_day_period", "number_of_days", "reason",
            "status", "status_display", "leave_type_code", "leave_type_name",
            "branch_code", "employee_name", "employee_id_str", "submitted_at"
        ]


class LeaveApplicationDetailSerializer(serializers.ModelSerializer):
    leave_type = LeaveTypeSerializer(read_only=True)
    branch = serializers.SerializerMethodField()
    employee = serializers.SerializerMethodField()
    approval_history = LeaveApprovalSerializer(many=True, read_only=True)
    attachment_url = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = LeaveApplication
        fields = [
            "id", "application_number", "start_date", "end_date",
            "is_half_day", "half_day_period", "number_of_days", "reason",
            "attachment", "attachment_url", "status", "status_display",
            "leave_type", "branch", "employee", "approval_history",
            "submitted_at", "updated_at"
        ]

    def get_branch(self, obj):
        return {"id": obj.branch.id, "code": obj.branch.code, "name": obj.branch.name}

    def get_employee(self, obj):
        return {
            "id": obj.employee.id,
            "employee_id": obj.employee.employee_id,
            "full_name": obj.employee.full_name,
            "designation": getattr(obj.employee.profile, "designation", "") if hasattr(obj.employee, "profile") else ""
        }

    def get_attachment_url(self, obj):
        request = self.context.get("request")
        if obj.attachment:
            return request.build_absolute_uri(obj.attachment.url) if request else obj.attachment.url
        return None
