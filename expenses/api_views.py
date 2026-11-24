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
    ExpenseClaimStatus, ReimbursementPayment, review_message
)
from expenses.serializers import (
    ExpenseCategorySerializer, ExpenseClaimListSerializer,
    ExpenseClaimDetailSerializer, ExpenseAttachmentSerializer
)
from accounts.models import RoleChoices
from notifications.services import notify_expense_event
from portal.admin_views import validate_upload, validate_receipt_count, EDITABLE_EXPENSE_STATUSES
from expenses.items import ItemError, clean_items, raw_items_from_request, replace_items, summary_header
from organization.currencies import all_currencies_json


class ExpenseCategoryListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        categories = ExpenseCategory.available_for(request.user.primary_branch)
        return Response(ExpenseCategorySerializer(categories, many=True).data)


class ExpenseCurrencyConfigView(APIView):
    """
    GET /api/v1/expenses/currencies/
    The current user's reimbursement currency (their branch's), the currencies they may claim in,
    and the full ISO-4217 list (for branch configuration screens).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Claims are always filed under the employee's own (primary) branch, so its currency is the payout currency.
        branch = request.user.primary_branch
        data = {"all_currencies": all_currencies_json()}
        if branch:
            data.update(branch.currency_config())
            data["branch"] = {"id": branch.id, "code": branch.code, "name": branch.name}
        return Response(data)


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

        qs = qs.select_related("category", "branch", "employee").prefetch_related("attachments", "items")
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

        description = (request.data.get("description") or "").strip()
        project_ref = request.data.get("project_reference", "")
        submit_now = str(request.data.get("submit_now", "false")).lower() in ("true", "1")

        try:
            raw = raw_items_from_request(request.data)
            if raw is None:
                return Response({"detail": "Add at least one expense item (category, date, amount)."},
                                status=status.HTTP_400_BAD_REQUEST)
            items = clean_items(raw, branch)
        except ItemError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        if not description:
            description = items[0]["description"] or "Expense claim"

        files = request.FILES.getlist("receipts") or request.FILES.getlist("file")
        count_err = validate_receipt_count(len(files))
        if count_err:
            return Response({"detail": count_err}, status=status.HTTP_400_BAD_REQUEST)
        for f in files:
            err = validate_upload(f)
            if err:
                return Response({"detail": err}, status=status.HTTP_400_BAD_REQUEST)

        header = summary_header(items)
        claim = ExpenseClaim.objects.create(
            employee=user,
            branch=branch,
            currency_code=branch.currency_code,
            category=header["category"],
            amount=header["amount"],
            expense_date=header["expense_date"],
            description=description,
            project_reference=project_ref,
            status=ExpenseClaimStatus.DRAFT
        )
        replace_items(claim, items)

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
        try:
            raw = raw_items_from_request(data, claim=claim)
            if raw is not None:
                replace_items(claim, clean_items(raw, claim.branch, base=claim.currency_code))
        except ItemError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
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
        count_err = validate_receipt_count(len(files), claim.attachments.count())
        if count_err:
            return Response({"detail": count_err}, status=status.HTTP_400_BAD_REQUEST)
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
        if not user.can_manage_expenses_for(claim.branch):
            return Response({"detail": "You are not an expense approver for this branch."}, status=status.HTTP_403_FORBIDDEN)

        action = request.data.get("action", "").upper()
        remarks = request.data.get("remarks", "")
        approved_amount = request.data.get("approved_amount")

        try:
            recorded = claim.admin_review(approver=user, action=action, remarks=remarks, approved_amount=approved_amount)
            event_type = "EXPENSE_ADMIN_REJECTED" if recorded == "REJECTED" else "EXPENSE_ADMIN_APPROVED"
            notify_expense_event(claim, event_type, remarks=remarks)
            return Response({"detail": review_message(claim, recorded), "status": claim.status, "amount": str(claim.amount)})
        except ValidationError as e:
            return Response({"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)


class ExpenseClaimDirectorApprovalView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        user = request.user
        if not (user.is_director or user.is_superuser):
            return Response({"detail": "Only Directors can grant final expense approval."}, status=status.HTTP_403_FORBIDDEN)

        claim = get_object_or_404(ExpenseClaim, pk=pk)
        if not user.can_manage_expenses_for(claim.branch):
            return Response({"detail": "You are not an expense approver for this branch."}, status=status.HTTP_403_FORBIDDEN)

        action = request.data.get("action", "").upper()
        remarks = request.data.get("remarks", "")
        approved_amount = request.data.get("approved_amount")

        try:
            recorded = claim.director_review(approver=user, action=action, remarks=remarks, approved_amount=approved_amount)
            event_type = "EXPENSE_DIRECTOR_REJECTED" if recorded == "REJECTED" else "EXPENSE_DIRECTOR_APPROVED"
            notify_expense_event(claim, event_type, remarks=remarks)
            return Response({"detail": review_message(claim, recorded), "status": claim.status, "amount": str(claim.amount)})
        except ValidationError as e:
            return Response({"detail": " ".join(e.messages)}, status=status.HTTP_400_BAD_REQUEST)


class ExpenseClaimRecordPaymentView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        user = request.user
        if not (user.is_admin_manager or user.is_superuser):
            return Response({"detail": "Only Admin Managers can record reimbursement payments."}, status=status.HTTP_403_FORBIDDEN)

        claim = get_object_or_404(ExpenseClaim, pk=pk)
        if not user.can_manage_expenses_for(claim.branch):
            return Response({"detail": "You are not an expense approver for this branch."}, status=status.HTTP_403_FORBIDDEN)
        if claim.employee_id == user.id and not user.is_superuser:
            return Response({"detail": "Another Admin Manager must record payment of your own claim."}, status=status.HTTP_403_FORBIDDEN)

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
