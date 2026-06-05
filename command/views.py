"""
Circle Core Command Center views.

All views operate on the public schema.
Tenant data is read by switching schemas via schema_context().
"""

import logging

from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.core.cache import cache
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_tenants.utils import schema_context

from tenants.models import Domain, GuestHouseTenant, Lead

from .decorators import command_required
from .models import CommandAuditLog, CommandUser

logger = logging.getLogger(__name__)


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def _command_login_limited(request, username):
    key = f"command_login_rl:{_client_ip(request)}:{username.lower()}"
    attempts = cache.get(key, 0)
    if attempts >= 5:
        return True
    cache.set(key, attempts + 1, timeout=15 * 60)
    return False


def _command_user(request):
    user_id = request.session.get("command_user_id")
    if not user_id:
        return None
    return CommandUser.objects.filter(pk=user_id, is_active=True).first()


def _audit_command(request, action, schema_name="", object_repr="", before=None, after=None, reason=""):
    user = _command_user(request)
    CommandAuditLog.objects.create(
        actor=user,
        actor_username=request.session.get("command_username", ""),
        action=action,
        schema_name=schema_name,
        object_repr=str(object_repr)[:255],
        before=before or {},
        after=after or {},
        reason=reason,
        ip_address=_client_ip(request),
    )


# ── Auth ──────────────────────────────────────────────────────────────────────

def command_login(request):
    if request.session.get("command_user_id"):
        return redirect("/command/")

    error = None
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        if _command_login_limited(request, username or "blank"):
            logger.warning("Command Center login rate-limited for %s from %s", username or "blank", _client_ip(request))
            return render(
                request,
                "command/login.html",
                {"error": "Too many login attempts. Try again in 15 minutes."},
                status=429,
            )
        try:
            user = CommandUser.objects.get(username=username, is_active=True)
            if user.check_password(password):
                user.last_login_at = timezone.now()
                user.save(update_fields=["last_login_at"])
                request.session.cycle_key()
                request.session["command_user_id"] = user.pk
                request.session["command_username"] = user.username
                cache.delete(f"command_login_rl:{_client_ip(request)}:{username.lower()}")
                logger.info("Command Center login for %s from %s", user.username, _client_ip(request))
                return redirect("/command/")
            error = "Invalid username or password."
        except CommandUser.DoesNotExist:
            error = "Invalid username or password."
        logger.warning("Failed Command Center login for %s from %s", username or "blank", _client_ip(request))

    return render(request, "command/login.html", {"error": error})


def command_logout(request):
    request.session.pop("command_user_id", None)
    request.session.pop("command_username", None)
    return redirect("/command/login/")


# ── Dashboard ─────────────────────────────────────────────────────────────────

@command_required
def dashboard(request):
    tenants = (
        GuestHouseTenant.objects
        .exclude(schema_name="public")
        .prefetch_related("domains")
        .order_by("-created_at")
    )

    total = tenants.count()
    stats = {
        "total_tenants": total,
        "trial_count": 0,
        "active_count": 0,
        "expired_count": 0,
        "suspended_count": 0,
        "new_leads": Lead.objects.filter(status="new").count(),
        "total_leads": Lead.objects.count(),
    }

    expiring_soon = []
    recent = []
    now = timezone.now()

    for i, tenant in enumerate(tenants):
        try:
            with schema_context(tenant.schema_name):
                from core.models import Subscription
                sub = Subscription.objects.select_related("plan").first()
        except Exception:
            sub = None

        if sub:
            if sub.status == "trial":
                stats["trial_count"] += 1
                if 0 <= sub.days_remaining <= 5:
                    expiring_soon.append({"tenant": tenant, "subscription": sub})
            elif sub.status == "active":
                stats["active_count"] += 1
            elif sub.status in ("expired", "cancelled", "suspended"):
                if sub.status == "suspended":
                    stats["suspended_count"] += 1
                else:
                    stats["expired_count"] += 1

        if i < 10:
            domain = tenant.domains.filter(is_primary=True).first()
            recent.append({
                "tenant": tenant,
                "subscription": sub,
                "domain": domain.domain if domain else None,
            })

    return render(request, "command/dashboard.html", {
        "stats": stats,
        "expiring_soon": expiring_soon,
        "recent_tenants": recent,
        "username": request.session.get("command_username"),
    })


# ── Tenants ───────────────────────────────────────────────────────────────────

@command_required
def tenant_list(request):
    status_filter = request.GET.get("status", "")
    search = request.GET.get("q", "").strip()

    qs = GuestHouseTenant.objects.exclude(schema_name="public").prefetch_related("domains")
    if search:
        qs = qs.filter(name__icontains=search) | GuestHouseTenant.objects.filter(
            owner_email__icontains=search
        ).exclude(schema_name="public")

    qs = qs.order_by("-created_at")

    rows = []
    for tenant in qs:
        try:
            with schema_context(tenant.schema_name):
                from core.models import Subscription, TrialEngagement
                sub = Subscription.objects.select_related("plan").first()
                engagement = TrialEngagement.objects.filter(pk=1).first()
        except Exception:
            sub = None
            engagement = None

        if status_filter and sub and sub.status != status_filter:
            continue
        if status_filter and not sub:
            continue

        domain = tenant.domains.filter(is_primary=True).first()
        rows.append({
            "tenant": tenant,
            "subscription": sub,
            "engagement": engagement,
            "domain": domain.domain if domain else None,
        })

    return render(request, "command/tenants.html", {
        "rows": rows,
        "status_filter": status_filter,
        "search": search,
        "username": request.session.get("command_username"),
    })


@command_required
def tenant_detail(request, schema_name):
    tenant = get_object_or_404(GuestHouseTenant, schema_name=schema_name)
    domain = tenant.domains.filter(is_primary=True).first()

    try:
        with schema_context(schema_name):
            from core.models import (
                Booking, Guest, Room, Subscription, TrialEngagement,
            )
            sub = Subscription.objects.select_related("plan").first()
            engagement = TrialEngagement.objects.filter(pk=1).first()
            room_count = Room.objects.count()
            guest_count = Guest.objects.count()
            booking_count = Booking.objects.count()
            active_bookings = Booking.objects.filter(
                status__in=["Confirmed", "Checked In"]
            ).count()
    except Exception as exc:
        logger.error("tenant_detail error for %s: %s", schema_name, exc)
        sub = engagement = None
        room_count = guest_count = booking_count = active_bookings = 0

    from core.subscriptions import GRACE_DAYS, TRIAL_DAYS

    return render(request, "command/tenant_detail.html", {
        "tenant": tenant,
        "domain": domain.domain if domain else None,
        "subscription": sub,
        "engagement": engagement,
        "room_count": room_count,
        "guest_count": guest_count,
        "booking_count": booking_count,
        "active_bookings": active_bookings,
        "grace_days": GRACE_DAYS,
        "trial_days": TRIAL_DAYS,
        "username": request.session.get("command_username"),
    })


# ── Tenant actions ────────────────────────────────────────────────────────────

@command_required
@require_POST
def extend_trial(request, schema_name):
    try:
        days = int(request.POST.get("days", 7))
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, 90))
    try:
        with schema_context(schema_name):
            from core.models import Subscription
            sub = Subscription.objects.first()
            if sub:
                before = {
                    "status": sub.status,
                    "expires_at": sub.expires_at.isoformat() if sub.expires_at else "",
                    "trial_ends_at": sub.trial_ends_at.isoformat() if sub.trial_ends_at else "",
                }
                sub.expires_at = sub.expires_at + timezone.timedelta(days=days)
                if sub.trial_ends_at:
                    sub.trial_ends_at = sub.trial_ends_at + timezone.timedelta(days=days)
                sub.next_billing_date = sub.expires_at.date()
                sub.save()
                _audit_command(
                    request,
                    "extend_trial",
                    schema_name,
                    object_repr=f"Trial extended by {days} days",
                    before=before,
                    after={
                        "status": sub.status,
                        "expires_at": sub.expires_at.isoformat(),
                        "trial_ends_at": sub.trial_ends_at.isoformat() if sub.trial_ends_at else "",
                    },
                )
        messages.success(request, f"Trial extended by {days} days for {schema_name}.")
    except Exception as exc:
        messages.error(request, f"Failed to extend trial: {exc}")
    return redirect(f"/command/tenants/{schema_name}/")


@command_required
@require_POST
def suspend_tenant(request, schema_name):
    try:
        tenant = GuestHouseTenant.objects.get(schema_name=schema_name)
        before = {"tenant_is_active": tenant.is_active, "subscription_status": ""}
        with schema_context(schema_name):
            from core.models import Subscription
            sub = Subscription.objects.first()
            if sub:
                before["subscription_status"] = sub.status
                sub.status = "suspended"
                sub.save(update_fields=["status"])
        tenant.is_active = False
        tenant.save(update_fields=["is_active"])
        _audit_command(
            request,
            "suspend_tenant",
            schema_name,
            object_repr=tenant.name,
            before=before,
            after={"tenant_is_active": tenant.is_active, "subscription_status": "suspended"},
        )
        messages.success(request, f"{schema_name} suspended.")
    except Exception as exc:
        messages.error(request, f"Failed to suspend: {exc}")
    return redirect(f"/command/tenants/{schema_name}/")


@command_required
@require_POST
def activate_tenant(request, schema_name):
    try:
        tenant = GuestHouseTenant.objects.get(schema_name=schema_name)
        before = {"tenant_is_active": tenant.is_active, "subscription_status": "", "expires_at": ""}
        tenant.is_active = True
        tenant.save(update_fields=["is_active"])
        with schema_context(schema_name):
            from core.models import Subscription
            from core.subscriptions import apply_paid_renewal
            sub = Subscription.objects.first()
            if sub:
                before["subscription_status"] = sub.status
                before["expires_at"] = sub.expires_at.isoformat() if sub.expires_at else ""
                apply_paid_renewal(sub)
                sub.save()
                after = {
                    "tenant_is_active": tenant.is_active,
                    "subscription_status": sub.status,
                    "expires_at": sub.expires_at.isoformat() if sub.expires_at else "",
                }
            else:
                after = {"tenant_is_active": tenant.is_active}
        _audit_command(request, "activate_tenant", schema_name, object_repr=tenant.name, before=before, after=after)
        messages.success(request, f"{schema_name} activated.")
    except Exception as exc:
        messages.error(request, f"Failed to activate: {exc}")
    return redirect(f"/command/tenants/{schema_name}/")


@command_required
@require_POST
def record_payment(request, schema_name):
    amount = request.POST.get("amount", "").strip()
    reference = request.POST.get("reference", "").strip()
    billing_cycle = request.POST.get("billing_cycle", "monthly")
    try:
        sub = None
        before = {}
        with schema_context(schema_name):
            from core.models import Subscription
            from core.subscriptions import apply_paid_renewal
            sub = Subscription.objects.first()
            if sub:
                before = {
                    "billing_cycle": sub.billing_cycle,
                    "status": sub.status,
                    "expires_at": sub.expires_at.isoformat() if sub.expires_at else "",
                    "last_payment_amount": str(sub.last_payment_amount or ""),
                    "last_payment_reference": sub.last_payment_reference,
                }
                sub.billing_cycle = billing_cycle
                apply_paid_renewal(sub)
                sub.last_payment_date = timezone.localdate()
                sub.last_payment_amount = amount or None
                sub.last_payment_reference = reference
                sub.save()
                _audit_command(
                    request,
                    "record_subscription_payment",
                    schema_name,
                    object_repr=reference or f"Manual payment {amount}",
                    before=before,
                    after={
                        "billing_cycle": sub.billing_cycle,
                        "status": sub.status,
                        "expires_at": sub.expires_at.isoformat() if sub.expires_at else "",
                        "last_payment_amount": str(sub.last_payment_amount or ""),
                        "last_payment_reference": sub.last_payment_reference,
                    },
                )
        if not sub:
            raise ValueError("No subscription found.")
        messages.success(
            request,
            f"Payment recorded for {schema_name}. Subscription activated until "
            f"{sub.expires_at.strftime('%d %b %Y')}."
        )
    except Exception as exc:
        messages.error(request, f"Failed to record payment: {exc}")
    return redirect(f"/command/tenants/{schema_name}/")


@command_required
def impersonate_tenant(request, schema_name):
    """Generate a short-lived signed token and redirect into the tenant's domain."""
    tenant = get_object_or_404(GuestHouseTenant, schema_name=schema_name)
    token = signing.dumps(
        {
            "schema": schema_name,
            "ts": timezone.now().isoformat(),
            "command_user_id": request.session.get("command_user_id"),
            "command_username": request.session.get("command_username"),
        },
        salt="command-impersonation",
    )
    domain = tenant.domains.filter(is_primary=True).first()
    scheme = "http" if settings.DEBUG else "https"
    port = request.get_port()
    port_suffix = f":{port}" if settings.DEBUG and port not in ("80", "443") else ""
    if settings.DEBUG:
        host = f"{schema_name}.localhost{port_suffix}"
    else:
        host = domain.domain if domain else f"{schema_name}.{settings.BASE_DOMAIN}"
    logger.warning(
        "Command Center impersonation token issued by %s for tenant %s from %s",
        request.session.get("command_username"),
        schema_name,
        _client_ip(request),
    )
    _audit_command(
        request,
        "issue_impersonation_token",
        schema_name,
        object_repr=tenant.name,
        reason="Command Center tenant impersonation token issued",
    )
    return redirect(f"{scheme}://{host}/command-enter/?token={token}")


# ── Leads ─────────────────────────────────────────────────────────────────────

@command_required
def lead_list(request):
    status_filter = request.GET.get("status", "")
    qs = Lead.objects.select_related("tenant").order_by("-created_at")
    if status_filter:
        qs = qs.filter(status=status_filter)

    return render(request, "command/leads.html", {
        "leads": qs,
        "status_filter": status_filter,
        "status_choices": Lead.STATUS_CHOICES,
        "username": request.session.get("command_username"),
    })


@command_required
@require_POST
def lead_update_status(request, pk):
    lead = get_object_or_404(Lead, pk=pk)
    new_status = request.POST.get("status", "")
    valid = [s[0] for s in Lead.STATUS_CHOICES]
    if new_status in valid:
        lead.status = new_status
        notes = request.POST.get("notes_internal", "").strip()
        if notes:
            lead.notes_internal = notes
        lead.save(update_fields=["status", "notes_internal", "updated_at"])
        messages.success(request, f"Lead updated to {lead.get_status_display()}.")
    return redirect("/command/leads/")
