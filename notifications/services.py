import logging
import threading

import requests
from django.core.mail import send_mail
from django.db import transaction
from django.conf import settings
from django.db.models import Q
from notifications.models import NotificationEvent, DeviceRegistration
from accounts.models import User, RoleChoices

logger = logging.getLogger(__name__)


def send_system_notification(recipient, title, message, event_type, link_url="", send_push=True, send_email=True):
    """
    Creates an in-app notification event and attempts push & email delivery.
    """
    if not recipient:
        return None

    # 1. Create In-App Notification Record
    event = NotificationEvent.objects.create(
        recipient=recipient,
        title=title,
        message=message,
        event_type=event_type,
        link_url=link_url
    )

    # 2. Email Delivery (Async or immediate via configured backend)
    if send_email and recipient.email:
        try:
            full_subject = f"[Great Waters] {title}"
            email_body = f"""Hello {recipient.first_name or recipient.employee_id},

{message}

Open in the portal: {getattr(settings, 'PORTAL_BASE_URL', 'http://localhost:8000')}{link_url}

Best regards,
Great Waters Automated Notification System
"""
            send_mail(
                subject=full_subject,
                message=email_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient.email],
                fail_silently=True
            )
        except Exception as e:
            logger.warning(f"Failed to send email to {recipient.email}: {e}")

    # 3. Mobile Push (Expo push service -> FCM / APNs)
    if send_push and getattr(settings, "PUSH_NOTIFICATIONS_ENABLED", True):
        tokens = list(
            DeviceRegistration.objects.filter(user=recipient, is_active=True)
            .values_list("registration_token", flat=True)
        )
        if tokens:
            data = {"event_type": event_type, "link_url": link_url, "notification_id": event.id}
            transaction.on_commit(lambda: _dispatch_push_async(tokens, title, message, data))

    return event


def expense_approvers_for(branch, role, exclude=None):
    """
    Active users holding `role` who may act on expense claims of `branch`:
    organisation-wide scope, the branch as primary location, or an active branch scope
    that grants expense management. Directors fall back to the superuser when none match.
    """
    qs = User.objects.filter(is_active=True, role=role).filter(
        Q(is_global_scope=True) | Q(primary_branch=branch) |
        Q(branch_scopes__branch=branch, branch_scopes__is_active=True, branch_scopes__can_manage_expenses=True)
    ).distinct()
    if exclude is not None:
        qs = qs.exclude(pk=exclude.pk)
    users = list(qs)
    if not users and role == RoleChoices.DIRECTOR:
        users = list(User.objects.filter(is_active=True, is_superuser=True))
    return users


def _money(value):
    return f"${value:,.2f}"


def notify_expense_event(claim, event_type, remarks=""):
    """
    Dispatches role-aware, branch-aware notifications across expense workflow stages.
    """
    emp = claim.employee
    branch = claim.branch
    partial_note = (
        f" Approved {_money(claim.amount)} of {_money(claim.claimed_amount)} claimed."
        if claim.is_partially_approved else ""
    )

    if event_type == "EXPENSE_SUBMITTED":
        if claim.status == "UNDER_DIRECTOR_REVIEW":
            # Admin Manager's own claim goes straight to the Director(s) of that branch.
            for d in expense_approvers_for(branch, RoleChoices.DIRECTOR, exclude=emp):
                send_system_notification(
                    recipient=d,
                    title=f"Expense Claim Pending Director Approval: {claim.claim_number}",
                    message=f"{emp.full_name} ({emp.get_role_display()}) submitted a claim for {_money(claim.amount)} "
                            f"({claim.category.name}) at {branch.name}. It comes directly to you for approval.",
                    event_type=event_type,
                    link_url=f"/expenses/{claim.id}/"
                )
            return
        for adm in expense_approvers_for(branch, RoleChoices.ADMIN_MANAGER, exclude=emp):
            send_system_notification(
                recipient=adm,
                title=f"New Expense Claim {claim.claim_number}",
                message=f"{emp.full_name} ({emp.employee_id}) submitted a claim for {_money(claim.amount)} ({claim.category.name}) at {branch.name}.",
                event_type=event_type,
                link_url=f"/expenses/{claim.id}/"
            )

    elif event_type == "EXPENSE_ADMIN_APPROVED":
        for d in expense_approvers_for(branch, RoleChoices.DIRECTOR):
            send_system_notification(
                recipient=d,
                title=f"Expense Claim Pending Director Approval: {claim.claim_number}",
                message=f"Claim of {_money(claim.amount)} for {emp.full_name} ({branch.name}) reviewed by Admin and is awaiting your final approval.{partial_note}",
                event_type=event_type,
                link_url=f"/expenses/{claim.id}/"
            )
        if claim.is_partially_approved:
            send_system_notification(
                recipient=emp,
                title=f"Expense Claim Partially Approved by Admin: {claim.claim_number}",
                message=f"{partial_note.strip()} It now goes to the Director. Remarks: {remarks}",
                event_type=event_type,
                link_url=f"/expenses/{claim.id}/"
            )

    elif event_type == "EXPENSE_DIRECTOR_APPROVED":
        send_system_notification(
            recipient=emp,
            title=f"Expense Claim {'Partially ' if claim.is_partially_approved else ''}Approved: {claim.claim_number}",
            message=f"Your claim has been approved by the Director for {_money(claim.amount)} and is now queued for payment.{partial_note}"
                    + (f" Remarks: {remarks}" if claim.is_partially_approved and remarks else ""),
            event_type=event_type,
            link_url=f"/expenses/{claim.id}/"
        )
        for adm in expense_approvers_for(branch, RoleChoices.ADMIN_MANAGER, exclude=emp):
            send_system_notification(
                recipient=adm,
                title=f"Approved Claim Awaiting Payment: {claim.claim_number}",
                message=f"Claim of {_money(claim.amount)} for {emp.full_name} ({branch.name}) approved by Director. Ready for reimbursement payment.",
                event_type=event_type,
                link_url=f"/expenses/reimbursements/"
            )

    elif event_type in ["EXPENSE_ADMIN_REJECTED", "EXPENSE_DIRECTOR_REJECTED"]:
        send_system_notification(
            recipient=emp,
            title=f"Expense Claim Rejected: {claim.claim_number}",
            message=f"Your claim of {_money(claim.claimed_amount)} was rejected. Reason/Remarks: {remarks}",
            event_type=event_type,
            link_url=f"/expenses/{claim.id}/"
        )

    elif event_type == "EXPENSE_PAID":
        payment = getattr(claim, "payment_record", None)
        ref_text = f" Ref: {payment.payment_reference}" if payment else ""
        send_system_notification(
            recipient=emp,
            title=f"Expense Claim Reimbursed: {claim.claim_number}",
            message=f"Payment of {_money(claim.amount)} has been processed.{ref_text}",
            event_type=event_type,
            link_url=f"/expenses/{claim.id}/"
        )


def _hr_coordinators_for(branch):
    return User.objects.filter(is_active=True, role=RoleChoices.HR_COORDINATOR).filter(
        Q(is_global_scope=True) | Q(primary_branch=branch) |
        Q(branch_scopes__branch=branch, branch_scopes__is_active=True)
    ).distinct()


def notify_leave_event(application, event_type, remarks="", partial=False, changes=None, actor=None):
    """
    Dispatches role-aware notifications across leave workflow stages.
    partial=True means the approver at this stage granted fewer days than were pending.
    """
    from leaves.models import fmt_days
    emp = application.employee
    branch = application.branch

    # Tell the employee as soon as an intermediate approver trims their dates.
    if partial and event_type in ("LEAVE_LEAD_RECOMMENDED", "LEAVE_HR_VERIFIED"):
        stage = "Lead / Manager" if event_type == "LEAVE_LEAD_RECOMMENDED" else "HR Coordinator"
        send_system_notification(
            recipient=emp,
            title=f"Leave Partially Recommended: {application.application_number}",
            message=(f"Your {stage} forwarded {fmt_days(application.number_of_days)} of "
                     f"{fmt_days(application.requested_number_of_days)} requested day(s) "
                     f"({application.start_date} to {application.end_date}). Remarks: {remarks}"),
            event_type="LEAVE_PARTIALLY_REDUCED",
            link_url=f"/leaves/{application.id}/"
        )

    if event_type in ("LEAVE_SUBMITTED", "LEAVE_RESUBMITTED"):
        verb = "resubmitted" if event_type == "LEAVE_RESUBMITTED" else "applied for"
        summary = (f"{emp.full_name} {verb} {application.number_of_days} days of {application.leave_type.name} "
                   f"({application.start_date} to {application.end_date}).")
        status = application.status
        if status == "PENDING_HR_VERIFICATION":
            recipients = _hr_coordinators_for(branch).exclude(id=emp.id)
            title = f"Leave from {emp.get_role_display()} {emp.full_name}: {application.application_number}"
            summary += " Sent directly to HR verification."
        elif status == "PENDING_DIRECTOR_APPROVAL":
            recipients = User.objects.filter(is_active=True, role=RoleChoices.DIRECTOR).exclude(id=emp.id)
            title = f"Leave from HR Coordinator {emp.full_name}: {application.application_number}"
            summary += " Sent directly for your approval."
        else:
            lead = getattr(emp.profile, "reporting_lead", None) if hasattr(emp, "profile") else None
            recipients = [lead] if lead else []
            title = (f"Leave Resubmitted by {emp.full_name}" if event_type == "LEAVE_RESUBMITTED"
                     else f"Leave Request from {emp.full_name}")
        for r in recipients:
            send_system_notification(
                recipient=r, title=title, message=summary, event_type=event_type,
                link_url=f"/leaves/{application.id}/",
            )

    elif event_type == "LEAVE_LEAD_RECOMMENDED":
        # Notify HR Coordinators
        hr_coordinators = User.objects.filter(
            is_active=True,
            role=RoleChoices.HR_COORDINATOR
        ).filter(
            Q(is_global_scope=True) |
            Q(primary_branch=branch) |
            Q(branch_scopes__branch=branch, branch_scopes__is_active=True)
        ).distinct()

        for hr in hr_coordinators:
            send_system_notification(
                recipient=hr,
                title=f"Leave Recommended by Lead: {application.application_number}",
                message=f"{emp.full_name}'s application for {application.number_of_days} days recommended by team lead and awaiting HR verification.",
                event_type=event_type,
                link_url=f"/leaves/{application.id}/"
            )

    elif event_type == "LEAVE_HR_VERIFIED":
        # Notify Directors
        directors = User.objects.filter(is_active=True, role=RoleChoices.DIRECTOR)
        for d in directors:
            send_system_notification(
                recipient=d,
                title=f"Leave Awaiting Final Approval: {application.application_number}",
                message=f"{emp.full_name}'s application verified by HR and is awaiting your final decision.",
                event_type=event_type,
                link_url=f"/leaves/{application.id}/"
            )

    elif event_type == "LEAVE_DIRECTOR_APPROVED":
        if application.is_reduced:
            title = f"Leave Application Partially Approved: {application.application_number}"
            message = (f"{fmt_days(application.number_of_days)} of {fmt_days(application.requested_number_of_days)} requested day(s) "
                       f"approved ({application.start_date} to {application.end_date}).")
            if remarks:
                message += f" Remarks: {remarks}"
        else:
            title = f"Leave Application Approved: {application.application_number}"
            message = f"Your application for {application.number_of_days} days ({application.start_date} to {application.end_date}) has been approved."
        send_system_notification(
            recipient=emp,
            title=title,
            message=message,
            event_type=event_type,
            link_url=f"/leaves/{application.id}/"
        )

    elif event_type == "LEAVE_RETURNED":
        # Sent back to an earlier approver instead of the employee: tell that stage's approvers.
        last = application.approval_history.filter(action="RETURNED").order_by("-created_at").first()
        target = getattr(last, "returned_to", "")
        by = last.approver.full_name if last else "An approver"
        if target == "LEAD_MANAGER":
            lead = getattr(getattr(emp, "profile", None), "reporting_lead", None)
            recipients = [lead] if lead else []
        elif target == "HR_COORDINATOR":
            recipients = list(_hr_coordinators_for(branch).exclude(id=emp.id))
        else:
            recipients = []
        for r in recipients:
            send_system_notification(
                recipient=r,
                title=f"Leave Returned for Review: {application.application_number}",
                message=(f"{by} returned {emp.full_name}'s leave ({fmt_days(application.number_of_days)} day(s), "
                         f"{application.start_date} to {application.end_date}) to you for another review. Remarks: {remarks}"),
                event_type=event_type,
                link_url=f"/leaves/{application.id}/",
            )

    elif event_type == "LEAVE_AMENDED":
        parts = []
        for field, label in (("leave_type", "type"), ("start_date", "from"), ("end_date", "to"), ("number_of_days", "days")):
            if changes and field in changes:
                parts.append(f"{label} {changes[field][0]} → {changes[field][1]}")
        send_system_notification(
            recipient=emp,
            title=f"Leave Changed by Approver: {application.application_number}",
            message=(f"{actor.full_name if actor else 'Your approver'} changed your leave: {', '.join(parts)}. "
                     f"Remarks: {remarks}"),
            event_type=event_type,
            link_url=f"/leaves/{application.id}/",
        )

    elif event_type in ["LEAVE_LEAD_REJECTED", "LEAVE_HR_REJECTED", "LEAVE_DIRECTOR_REJECTED"]:
        send_system_notification(
            recipient=emp,
            title=f"Leave Application Returned/Rejected: {application.application_number}",
            message=f"Your leave application was not approved. Remarks: {remarks}. You can edit and resubmit it from the leave details page.",
            event_type=event_type,
            link_url=f"/leaves/{application.id}/"
        )


def _dispatch_push_async(tokens, title, body, data):
    if getattr(settings, "TESTING", False):
        return
    threading.Thread(target=send_expo_push, args=(tokens, title, body, data), daemon=True).start()


def send_expo_push(tokens, title, body, data=None):
    """
    Sends push notifications through the Expo push service.
    Tokens that Expo reports as no longer registered are deactivated.
    """
    expo_tokens = [t for t in tokens if t.startswith("ExponentPushToken") or t.startswith("ExpoPushToken")]
    if not expo_tokens:
        return
    messages_payload = [
        {"to": t, "title": title, "body": body[:240], "data": data or {}, "sound": "default", "priority": "high",
         "channelId": "approvals"}
        for t in expo_tokens
    ]
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if getattr(settings, "EXPO_ACCESS_TOKEN", ""):
        headers["Authorization"] = f"Bearer {settings.EXPO_ACCESS_TOKEN}"
    try:
        for i in range(0, len(messages_payload), 100):
            chunk = messages_payload[i:i + 100]
            resp = requests.post(settings.EXPO_PUSH_URL, json=chunk, headers=headers, timeout=10)
            tickets = resp.json().get("data", []) if resp.ok else []
            for msg, ticket in zip(chunk, tickets):
                if ticket.get("status") == "error" and ticket.get("details", {}).get("error") == "DeviceNotRegistered":
                    DeviceRegistration.objects.filter(registration_token=msg["to"]).update(is_active=False)
    except Exception as e:  # network failures must never break the workflow
        logger.warning(f"Expo push delivery failed: {e}")
