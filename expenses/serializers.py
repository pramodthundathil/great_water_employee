from rest_framework import serializers
from decimal import Decimal
from expenses.models import (
    ExpenseCategory, ExpenseClaim, ExpenseAttachment,
    ExpenseApproval, ReimbursementPayment, ExpenseClaimStatus
)
from organization.models import Branch
from accounts.models import User


class ExpenseCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ExpenseCategory
        fields = ["id", "name", "code", "description"]


class ExpenseAttachmentSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = ExpenseAttachment
        fields = ["id", "file", "file_url", "original_filename", "file_type", "file_size", "uploaded_at"]

    def get_file_url(self, obj):
        request = self.context.get("request")
        if obj.file:
            return request.build_absolute_uri(obj.file.url) if request else obj.file.url
        return None


class ExpenseApprovalSerializer(serializers.ModelSerializer):
    approver_name = serializers.CharField(source="approver.full_name", read_only=True)
    approver_id_str = serializers.CharField(source="approver.employee_id", read_only=True)

    class Meta:
        model = ExpenseApproval
        fields = ["id", "stage", "approver_name", "approver_id_str", "action", "remarks", "approved_amount", "created_at"]


class ReimbursementPaymentSerializer(serializers.ModelSerializer):
    recorded_by_name = serializers.CharField(source="recorded_by.full_name", read_only=True)

    class Meta:
        model = ReimbursementPayment
        fields = [
            "id", "payment_date", "payment_mode", "payment_reference",
            "remarks", "recorded_by_name", "created_at"
        ]


class ExpenseClaimListSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    branch_code = serializers.CharField(source="branch.code", read_only=True)
    employee_name = serializers.CharField(source="employee.full_name", read_only=True)
    employee_id_str = serializers.CharField(source="employee.employee_id", read_only=True)
    attachments_count = serializers.IntegerField(source="attachments.count", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    claimed_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    is_partially_approved = serializers.BooleanField(read_only=True)

    class Meta:
        model = ExpenseClaim
        fields = [
            "id", "claim_number", "expense_date", "amount", "claimed_amount", "is_partially_approved",
            "description", "project_reference", "status", "status_display", "category_name",
            "branch_code", "employee_name", "employee_id_str", "attachments_count",
            "submitted_at", "created_at"
        ]


class ExpenseClaimDetailSerializer(serializers.ModelSerializer):
    category = ExpenseCategorySerializer(read_only=True)
    branch = serializers.SerializerMethodField()
    employee = serializers.SerializerMethodField()
    attachments = ExpenseAttachmentSerializer(many=True, read_only=True)
    approval_history = ExpenseApprovalSerializer(many=True, read_only=True)
    payment_record = ReimbursementPaymentSerializer(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    claimed_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    is_partially_approved = serializers.BooleanField(read_only=True)
    skips_admin_stage = serializers.BooleanField(read_only=True)
    can_withdraw = serializers.BooleanField(read_only=True)

    class Meta:
        model = ExpenseClaim
        fields = [
            "id", "claim_number", "expense_date", "amount", "claimed_amount", "is_partially_approved",
            "skips_admin_stage", "can_withdraw", "description",
            "project_reference", "status", "status_display", "category",
            "branch", "employee", "attachments", "approval_history",
            "payment_record", "submitted_at", "created_at", "updated_at"
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
