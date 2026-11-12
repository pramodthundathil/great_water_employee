from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from decimal import Decimal

from expenses.models import (
    ExpenseCategory, ExpenseClaim, ExpenseAttachment,
    ExpenseClaimStatus, ReimbursementPayment
)
from expenses.serializers import (
    ExpenseCategorySerializer, ExpenseClaimListSerializer,
    ExpenseClaimDetailSerializer, ExpenseAttachmentSerializer
)
from accounts.models import RoleChoices
from notifications.services import notify_expense_event
from portal.admin_views import validate_upload, EDITABLE_EXPENSE_STATUSES
from datetime import date as _date


def _validate_claim_fields(amount, expense_date):
    try:
        amount_dec = Decimal(str(amount))
    except Exception:
        return "Amount must be a number."
    if amount_dec <= 0:
        return "Amount must be greater than zero."
    try:
        d = _date.fromisoformat(str(expense_date)[:10])
    except ValueError:
        return "Invalid expense date. Use YYYY-MM-DD."
    if d > _date.today():
        return "Expense date cannot be in the future."
    return None


class ExpenseCategoryListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        categories = ExpenseCategory.objects.filter(is_active=True)
        return Response(ExpenseCategorySerializer(categories, many=True).data)


class ExpenseClaimListCreateView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        user = request.user
        accessible_branches = user.get_accessible_branches()

        # Query filtering based on role and branch scope
        if user.role == RoleChoices.EMPLOYEE or request.query_params.get("scope") == "mine":
            qs = ExpenseClaim.objects.filter(employee=user)
        else:
            # Managers and Approvers see claims in their authorized branches
            qs = ExpenseClaim.objects.filter(branch__in=accessible_branches)

        # Apply query params
        branch_id = request.query_params.get("branch_id")
        if branch_id and user.can_access_branch(get_object_or_404(accessible_branches, id=branch_id)):
            qs = qs.filter(branch_id=branch_id)

        status_param = request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)

        category_id = request.query_params.get("category_id")
        if category_id:
            qs = qs.filter(category_id=category_id)

        qs = qs.select_related("category", "branch", "employee").prefetch_related("attachments")
        serializer = ExpenseClaimListSerializer(qs[:50], many=True)
        return Response(serializer.data)

    @transaction.atomic
    def post(self, request):
        user = request.user
        if user.is_director:
            return Response({"detail": "Directors approve expense claims; they cannot raise their own."},
                            status=status.HTTP_403_FORBIDDEN)
        branch = user.primary_branch
        if not branch:
            return Response({"detail": "User has no primary branch assigned."}, status=status.HTTP_400_BAD_REQUEST)

        category_id = request.data.get("category")
        amount = request.data.get("amount")
        expense_date = request.data.get("expense_date")
        description = request.data.get("description", "")
        project_ref = request.data.get("project_reference", "")
        submit_now = str(request.data.get("submit_now", "false")).lower() in ("true", "1")

        if not category_id or not amount or not expense_date:
            return Response({"detail": "Category, amount, and expense date are required."}, status=status.HTTP_400_BAD_REQUEST)

        category = get_object_or_404(ExpenseCategory, id=category_id, is_active=True)
        error = _validate_claim_fields(amount, expense_date)
        if error:
            return Response({"detail": error}, status=status.HTTP_400_BAD_REQUEST)
        files = request.FILES.getlist("receipts") or request.FILES.getlist("file")
        for f in files:
            err = validate_upload(f)
            if err:
                return Response({"detail": err}, status=status.HTTP_400_BAD_REQUEST)

        claim = ExpenseClaim.objects.create(
            employee=user,
            branch=branch,
            category=category,
            amount=Decimal(str(amount)),
            expense_date=expense_date,
            description=description,
            project_reference=project_ref,
            status=ExpenseClaimStatus.DRAFT
        )

        # Handle uploaded proof files (mandatory if submit_now is true)
        for f in files:
            ExpenseAttachment.objects.create(
                claim=claim,
                file=f,
                original_filename=f.name,
                file_type=f.content_type,
                file_size=f.size
            )

        if submit_now:
            try:
                claim.submit()
                notify_expense_event(claim, "EXPENSE_SUBMITTED")
            except ValidationError as e:
                transaction.set_rollback(True)
                return Response({"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)

        detail_serializer = ExpenseClaimDetailSerializer(claim, context={"request": request})
        return Response(detail_serializer.data, status=status.HTTP_201_CREATED)


class ExpenseClaimDetailView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def patch(self, request, pk):
        """Edit a claim before the Admin Manager reviews it."""
        claim = get_object_or_404(ExpenseClaim, pk=pk, employee=request.user)
        if claim.status not in EDITABLE_EXPENSE_STATUSES:
            return Response({"detail": "This claim can no longer be edited because review has started."}, status=status.HTTP_400_BAD_REQUEST)
        data = request.data
        if "category" in data:
            claim.category = get_object_or_404(ExpenseCategory, id=data.get("category"), is_active=True)
        amount = data.get("amount", claim.amount)
        expense_date = data.get("expense_date", claim.expense_date.isoformat())
        error = _validate_claim_fields(amount, expense_date)
        if error:
            return Response({"detail": error}, status=status.HTTP_400_BAD_REQUEST)
        claim.amount = Decimal(str(amount))
        claim.expense_date = str(expense_date)[:10]
        if "description" in data:
            claim.description = data.get("description", "")
        if "project_reference" in data:
            claim.project_reference = data.get("project_reference", "")
        claim.save()
        claim.refresh_from_db()
        return Response(ExpenseClaimDetailSerializer(claim, context={"request": request}).data)

    def get(self, request, pk):
        user = request.user
        accessible_branches = user.get_accessible_branches()
        claim = get_object_or_404(ExpenseClaim, pk=pk)

        # Enforce security: user must be the claim owner or have branch access as reviewer
        if claim.employee != user and not (user.can_access_branch(claim.branch) and user.role != RoleChoices.EMPLOYEE):
            return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)

        serializer = ExpenseClaimDetailSerializer(claim, context={"request": request})
        return Response(serializer.data)


class ExpenseAttachmentView(APIView):
    """
    POST   /api/v1/expenses/claims/<pk>/attachments/          (multipart: receipts[])
    DELETE /api/v1/expenses/claims/<pk>/attachments/<att_id>/
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def _get_claim(self, request, pk):
        claim = get_object_or_404(ExpenseClaim, pk=pk, employee=request.user)
        if claim.status not in EDITABLE_EXPENSE_STATUSES:
            return None
        return claim

    def post(self, request, pk):
        claim = self._get_claim(request, pk)
        if not claim:
            return Response({"detail": "Proofs can no longer be changed for this claim."}, status=status.HTTP_400_BAD_REQUEST)
        files = request.FILES.getlist("receipts") or request.FILES.getlist("file")
        if not files:
            return Response({"detail": "No file uploaded."}, status=status.HTTP_400_BAD_REQUEST)
        for f in files:
            err = validate_upload(f)
            if err:
                return Response({"detail": err}, status=status.HTTP_400_BAD_REQUEST)
        for f in files:
            ExpenseAttachment.objects.create(
                claim=claim, file=f, original_filename=f.name, file_type=f.content_type or "", file_size=f.size
            )
        return Response(ExpenseClaimDetailSerializer(claim, context={"request": request}).data, status=status.HTTP_201_CREATED)

    def delete(self, request, pk, att_id):
        claim = self._get_claim(request, pk)
        if not claim:
            return Response({"detail": "Proofs can no longer be changed for this claim."}, status=status.HTTP_400_BAD_REQUEST)
        att = get_object_or_404(ExpenseAttachment, pk=att_id, claim=claim)
        att.file.delete(save=False)
        att.delete()
        return Response(ExpenseClaimDetailSerializer(claim, context={"request": request}).data)


class ExpenseClaimSubmitView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        claim = get_object_or_404(ExpenseClaim, pk=pk, employee=request.user)
        try:
            claim.submit()
            notify_expense_event(claim, "EXPENSE_SUBMITTED")
            return Response({"detail": "Claim submitted successfully.", "status": claim.status})
        except ValidationError as e:
            return Response({"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)


class ExpenseClaimWithdrawView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        claim = get_object_or_404(ExpenseClaim, pk=pk, employee=request.user)
        try:
            claim.withdraw(request.user)
            return Response({"detail": "Claim withdrawn.", "status": claim.status})
        except ValidationError as e:
            return Response({"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)


class ExpenseClaimAdminReviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        user = request.user
        if not (user.is_admin_manager or user.is_superuser):
            return Response({"detail": "Only Admin Managers can perform Stage 1 review."}, status=status.HTTP_403_FORBIDDEN)

        claim = get_object_or_404(ExpenseClaim, pk=pk)
        if not user.can_access_branch(claim.branch):
            return Response({"detail": "You do not have access to this branch."}, status=status.HTTP_403_FORBIDDEN)

        action = request.data.get("action", "").upper()
        remarks = request.data.get("remarks", "")

        try:
            claim.admin_review(approver=user, action=action, remarks=remarks)
            event_type = "EXPENSE_ADMIN_APPROVED" if action == "APPROVED" else "EXPENSE_ADMIN_REJECTED"
            notify_expense_event(claim, event_type, remarks=remarks)
            return Response({"detail": f"Claim {action.lower()} successfully.", "status": claim.status})
        except ValidationError as e:
            return Response({"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)


class ExpenseClaimDirectorApprovalView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        user = request.user
        if not (user.is_director or user.is_superuser):
            return Response({"detail": "Only Directors can grant final expense approval."}, status=status.HTTP_403_FORBIDDEN)

        claim = get_object_or_404(ExpenseClaim, pk=pk)
        if not user.can_access_branch(claim.branch):
            return Response({"detail": "You do not have access to this branch."}, status=status.HTTP_403_FORBIDDEN)

        action = request.data.get("action", "").upper()
        remarks = request.data.get("remarks", "")

        try:
            claim.director_review(approver=user, action=action, remarks=remarks)
            event_type = "EXPENSE_DIRECTOR_APPROVED" if action == "APPROVED" else "EXPENSE_DIRECTOR_REJECTED"
            notify_expense_event(claim, event_type, remarks=remarks)
            return Response({"detail": f"Claim {action.lower()} successfully.", "status": claim.status})
        except ValidationError as e:
            return Response({"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)


class ExpenseClaimRecordPaymentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        user = request.user
        if not (user.is_admin_manager or user.is_superuser):
            return Response({"detail": "Only Admin Managers can record reimbursement payments."}, status=status.HTTP_403_FORBIDDEN)

        claim = get_object_or_404(ExpenseClaim, pk=pk)
        if not user.can_access_branch(claim.branch):
            return Response({"detail": "You do not have access to this branch."}, status=status.HTTP_403_FORBIDDEN)

        payment_date = request.data.get("payment_date") or timezone.now().date()
        payment_mode = request.data.get("payment_mode", "BANK_TRANSFER")
        payment_reference = request.data.get("payment_reference")
        remarks = request.data.get("remarks", "")

        if not payment_reference:
            return Response({"detail": "Payment transaction reference number is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            payment = claim.record_payment(
                recorded_by=user,
                payment_date=payment_date,
                payment_mode=payment_mode,
                payment_reference=payment_reference,
                remarks=remarks
            )
            notify_expense_event(claim, "EXPENSE_PAID")
            return Response({"detail": "Payment recorded successfully.", "status": claim.status})
        except ValidationError as e:
            return Response({"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)
