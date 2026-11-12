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


def notify_expense_event(claim, event_type, remarks=""):
    """
    Dispatches role-aware notifications across expense workflow stages.
    """
    emp = claim.employee
    branch = claim.branch

    if event_type == "EXPENSE_SUBMITTED":
        # Notify Admin Managers who have access to this branch
        admin_managers = User.objects.filter(
            is_active=True,
            role=RoleChoices.ADMIN_MANAGER
        ).filter(
            Q(is_global_scope=True) |
            Q(primary_branch=branch) |
            Q(branch_scopes__branch=branch, branch_scopes__is_active=True)
        ).distinct()

        for adm in admin_managers:
            send_system_notification(
                recipient=adm,
                title=f"New Expense Claim {claim.claim_number}",
                message=f"{emp.full_name} ({emp.employee_id}) submitted a claim for ${claim.amount} ({claim.category.name}) at {branch.name}.",
                event_type=event_type,
                link_url=f"/expenses/{claim.id}/"
            )

    elif event_type == "EXPENSE_ADMIN_APPROVED":
        # Notify Directors
        directors = User.objects.filter(is_active=True, role=RoleChoices.DIRECTOR)
        for d in directors:
            send_system_notification(
                recipient=d,
                title=f"Expense Claim Pending Director Approval: {claim.claim_number}",
                message=f"Claim of ${claim.amount} for {emp.full_name} reviewed by Admin and is awaiting your final approval.",
                event_type=event_type,
                link_url=f"/expenses/{claim.id}/"
            )

    elif event_type == "EXPENSE_DIRECTOR_APPROVED":
        # Notify Employee & Admin Managers for reimbursement disbursement
        send_system_notification(
            recipient=emp,
            title=f"Expense Claim Approved: {claim.claim_number}",
            message=f"Your claim of ${claim.amount} has been approved by the Director and is now queued for payment.",
            event_type=event_type,
            link_url=f"/expenses/{claim.id}/"
        )

        admin_managers = User.objects.filter(
            is_active=True,
            role=RoleChoices.ADMIN_MANAGER
        ).filter(
            Q(is_global_scope=True) |
            Q(primary_branch=branch) |
            Q(branch_scopes__branch=branch, branch_scopes__is_active=True)
        ).distinct()
        for adm in admin_managers:
            send_system_notification(
                recipient=adm,
                title=f"Approved Claim Awaiting Payment: {claim.claim_number}",
                message=f"Claim of ${claim.amount} for {emp.full_name} approved by Director. Ready for reimbursement payment.",
                event_type=event_type,
                link_url=f"/expenses/reimbursements/"
            )

    elif event_type in ["EXPENSE_ADMIN_REJECTED", "EXPENSE_DIRECTOR_REJECTED"]:
        send_system_notification(
            recipient=emp,
            title=f"Expense Claim Rejected: {claim.claim_number}",
            message=f"Your claim of ${claim.amount} was rejected. Reason/Remarks: {remarks}",
            event_type=event_type,
            link_url=f"/expenses/{claim.id}/"
        )

    elif event_type == "EXPENSE_PAID":
        payment = getattr(claim, "payment_record", None)
        ref_text = f" Ref: {payment.payment_reference}" if payment else ""
        send_system_notification(
            recipient=emp,
            title=f"Expense Claim Reimbursed: {claim.claim_number}",
            message=f"Payment of ${claim.amount} has been processed.{ref_text}",
            event_type=event_type,
            link_url=f"/expenses/{claim.id}/"
        )


def notify_leave_event(application, event_type, remarks=""):
    """
    Dispatches role-aware notifications across leave workflow stages.
    """
    emp = application.employee
    branch = application.branch

    if event_type == "LEAVE_SUBMITTED":
        # Notify direct reporting lead
        lead = getattr(emp.profile, "reporting_lead", None) if hasattr(emp, "profile") else None
        if lead:
            send_system_notification(
                recipient=lead,
                title=f"Leave Request from {emp.full_name}",
                message=f"{emp.full_name} applied for {application.number_of_days} days of {application.leave_type.name} ({application.start_date} to {application.end_date}).",
                event_type=event_type,
                link_url=f"/leaves/{application.id}/"
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
        send_system_notification(
            recipient=emp,
            title=f"Leave Application Approved: {application.application_number}",
            message=f"Your application for {application.number_of_days} days ({application.start_date} to {application.end_date}) has been approved.",
            event_type=event_type,
            link_url=f"/leaves/{application.id}/"
        )

    elif event_type in ["LEAVE_LEAD_REJECTED", "LEAVE_HR_REJECTED", "LEAVE_DIRECTOR_REJECTED"]:
        send_system_notification(
            recipient=emp,
            title=f"Leave Application Returned/Rejected: {application.application_number}",
            message=f"Your leave application was not approved. Remarks: {remarks}",
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
