from rest_framework import serializers
from decimal import Decimal
from leaves.models import (
    LeaveType, LeavePolicy, LeaveApplication,
    LeaveApproval, LeaveLedgerEntry, HolidayCalendar, Holiday, LeaveAuditLog,
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
        fields = [
            "id", "stage", "approver_name", "approver_id_str", "action", "remarks",
            "approved_start_date", "approved_end_date", "approved_days", "created_at",
        ]


class LeaveAuditLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()
    actor_id_str = serializers.SerializerMethodField()
    action_display = serializers.CharField(source="get_action_display", read_only=True)
    source_display = serializers.CharField(source="get_source_display", read_only=True)
    from_status_display = serializers.SerializerMethodField()
    to_status_display = serializers.SerializerMethodField()

    class Meta:
        model = LeaveAuditLog
        fields = [
            "id", "action", "action_display", "stage", "actor_name", "actor_id_str",
            "from_status", "from_status_display", "to_status", "to_status_display",
            "remarks", "changes", "source", "source_display", "created_at",
        ]

    def get_actor_name(self, obj):
        return obj.actor.full_name if obj.actor else "System"

    def get_actor_id_str(self, obj):
        return obj.actor.employee_id if obj.actor else ""

    @staticmethod
    def _status_label(value):
        from leaves.models import LeaveApplicationStatus
        return dict(LeaveApplicationStatus.choices).get(value, value) if value else ""

    def get_from_status_display(self, obj):
        return self._status_label(obj.from_status)

    def get_to_status_display(self, obj):
        return self._status_label(obj.to_status)


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
    status_display = serializers.CharField(source="display_status", read_only=True)
    my_last_action = serializers.SerializerMethodField()
    last_action = serializers.SerializerMethodField()

    class Meta:
        model = LeaveApplication
        fields = [
            "id", "application_number", "start_date", "end_date",
            "is_half_day", "half_day_period", "number_of_days", "reason",
            "status", "status_display", "leave_type_code", "leave_type_name",
            "branch_code", "employee_name", "employee_id_str", "submitted_at",
            "requested_number_of_days", "is_partially_approved", "resubmission_count",
            "current_stage", "starts_at_stage", "my_last_action", "last_action",
        ]

    def _history(self, obj):
        # Uses the prefetched approval_history when available.
        return list(obj.approval_history.all())

    def get_my_last_action(self, obj):
        request = self.context.get("request")
        if not request or obj.employee_id == request.user.id:
            return None
        mine = [h for h in self._history(obj) if h.approver_id == request.user.id]
        if not mine:
            return None
        h = mine[-1]
        return {"action": h.action, "label": h.action_label, "stage": h.stage, "created_at": h.created_at}

    def get_last_action(self, obj):
        history = self._history(obj)
        if not history:
            return None
        h = history[-1]
        return {"action": h.action, "label": h.action_label, "stage": h.stage_label,
                "by": h.approver.full_name, "created_at": h.created_at}


class LeaveApplicationDetailSerializer(serializers.ModelSerializer):
    leave_type = LeaveTypeSerializer(read_only=True)
    branch = serializers.SerializerMethodField()
    employee = serializers.SerializerMethodField()
    approval_history = LeaveApprovalSerializer(many=True, read_only=True)
    audit_trail = LeaveAuditLogSerializer(source="audit_logs", many=True, read_only=True)
    attachment_url = serializers.SerializerMethodField()
    status_display = serializers.CharField(source="display_status", read_only=True)
    can_resubmit = serializers.SerializerMethodField()

    class Meta:
        model = LeaveApplication
        fields = [
            "id", "application_number", "start_date", "end_date",
            "is_half_day", "half_day_period", "number_of_days", "reason",
            "attachment", "attachment_url", "status", "status_display",
            "leave_type", "branch", "employee", "approval_history", "audit_trail",
            "requested_start_date", "requested_end_date", "requested_number_of_days",
            "is_partially_approved", "is_reduced", "resubmission_count", "can_resubmit",
            "starts_at_stage", "skipped_stages", "route_note", "current_stage",
            "review",
            "submitted_at", "updated_at"
        ]

    review = serializers.SerializerMethodField()

    def get_review(self, obj):
        """Decision the signed-in user can take on this application now (null if none)."""
        from leaves.services import leave_review_options
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return None
        return leave_review_options(request.user, obj)

    def get_can_resubmit(self, obj):
        request = self.context.get("request")
        return bool(request and request.user.is_authenticated and obj.can_resubmit(request.user))

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
