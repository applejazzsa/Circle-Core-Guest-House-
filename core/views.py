import datetime
import json
import calendar
import csv
import uuid
import base64
import mimetypes
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth import authenticate
from django.contrib.auth.decorators import login_required
from django import forms
from django.core.cache import cache
from django.core.paginator import Paginator
from django.http import FileResponse, HttpResponse, JsonResponse
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Count, DecimalField, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.deletion import ProtectedError
from django.db.models.functions import Coalesce, ExtractHour
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt

from .forms import BookingForm, BookingRefundForm, ExpenseForm, GuestForm, GuestHouseSettingsForm, PaymentForm, RoomForm, validate_image_upload
from .models import (
    AuditLog,
    Booking,
    Property as GuestHouseProperty,
    BookingRefund,
    CommunicationLog,
    CleaningProof,
    DailyCloseLock,
    Expense,
    Guest,
    GuestHouseSettings,
    InventoryCategory,
    InventoryItem,
    InventoryTransaction,
    MaintenanceRequest,
    Payment,
    POSCategory,
    POSItem,
    POSSale,
    POSSaleItem,
    POSShift,
    POSTransaction,
    Room,
    Subscription,
    SubscriptionPlan,
    TrialEngagement,
    TrialLicense,
)
from .email_utils import (
    notify_booking_cancelled,
    notify_booking_checkin,
    notify_booking_checkout,
    notify_booking_created,
    notify_payment_received,
)
from .pdf_utils import generate_pdf
from .pdf_documents import render_booking_invoice_pdf, render_payment_receipt_pdf, render_pos_receipt_pdf
from .subscriptions import trial_end
from django.contrib.auth.models import Group, User as AuthUser
from .roles import is_cleaner, is_owner, STAFF_ROLES


SENSITIVE_DISCOUNT_AMOUNT = Decimal("50.00")
SENSITIVE_DISCOUNT_PERCENT = Decimal("5.00")
SHIFT_VARIANCE_APPROVAL_AMOUNT = Decimal("0.00")
MAX_CLEANING_PROOF_SIZE = 5 * 1024 * 1024
ALLOWED_CLEANING_PROOF_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _money(value):
    if value is None:
        return ""
    return str(value)


def _model_snapshot(obj, fields):
    return {field: _money(getattr(obj, field, "")) for field in fields}


def _audit(request, action, obj, before=None, after=None, reason="", approved_by=None):
    AuditLog.objects.create(
        actor=request.user if request.user.is_authenticated else None,
        approved_by=approved_by,
        action=action,
        object_type=obj.__class__.__name__,
        object_id=str(getattr(obj, "pk", "")),
        object_repr=str(obj)[:255],
        before=before or {},
        after=after or {},
        reason=reason,
        ip_address=_client_ip(request),
    )


def _manager_approval(request, reason):
    if is_owner(request.user):
        return request.user
    username = request.POST.get("manager_username", "").strip()
    password = request.POST.get("manager_password", "")
    approver = authenticate(request, username=username, password=password)
    if approver and is_owner(approver):
        AuditLog.objects.create(
            actor=request.user,
            approved_by=approver,
            action="login_override",
            object_type="ManagerApproval",
            object_repr=reason[:255],
            reason=reason,
            ip_address=_client_ip(request),
        )
        return approver
    return None


def _manager_approval_credentials(request, username, password, reason):
    if is_owner(request.user):
        return request.user
    approver = authenticate(request, username=(username or "").strip(), password=password or "")
    if approver and is_owner(approver):
        AuditLog.objects.create(
            actor=request.user,
            approved_by=approver,
            action="login_override",
            object_type="ManagerApproval",
            object_repr=reason[:255],
            reason=reason,
            ip_address=_client_ip(request),
        )
        return approver
    return None


def _approval_fields_html():
    return (
        '<div style="margin-top:12px; padding:12px; border:1px solid rgba(245,158,11,0.25); '
        'border-radius:var(--radius-md); background:rgba(245,158,11,0.06);">'
        '<div class="form-label" style="margin-bottom:8px;">Manager Approval Required</div>'
        '<div style="display:grid; grid-template-columns:1fr 1fr; gap:10px;">'
        '<input name="manager_username" placeholder="Owner username" autocomplete="username">'
        '<input name="manager_password" placeholder="Owner password" type="password" autocomplete="current-password">'
        '</div></div>'
    )


def _cash_shift_required(request):
    active_prop = get_active_property(request)
    if POSShift.objects.filter(prop=active_prop, closed_at__isnull=True).exists():
        return True
    messages.error(request, "Open a POS cash shift before recording cash transactions.")
    return False


def _is_date_locked(value):
    if isinstance(value, datetime.datetime):
        value = value.date()
    return bool(value and DailyCloseLock.objects.filter(close_date=value).exists())


def _locked_day_response(request, value, fallback="core:home", *args, **kwargs):
    messages.error(request, f"{value:%d %b %Y} is locked by daily close. Owner must reopen it before changes.")
    return redirect(fallback, *args, **kwargs)


def _owner_required(request):
    if is_owner(request.user):
        return None
    messages.error(request, "Owner access is required for that page.")
    return redirect("core:home")


def _cleaner_blocked(request):
    if not is_cleaner(request.user):
        return None
    messages.error(request, "Your role is limited to housekeeping tasks.")
    return redirect("core:cleaning")


def _current_subscription():
    return Subscription.objects.select_related("plan").first()


def _is_unlimited(value):
    return value is None or value >= 999


def _room_limit_context():
    subscription = _current_subscription()
    plan = subscription.plan if subscription else None
    limit = plan.max_rooms if plan else 0
    current = Room.objects.count()
    return subscription, plan, current, limit


def _user_limit_context():
    subscription = _current_subscription()
    plan = subscription.plan if subscription else None
    limit = plan.max_users if plan else 0
    current = AuthUser.objects.filter(is_active=True).count()
    return subscription, plan, current, limit


def _limit_reached_response(request, message, feature):
    messages.error(request, message)
    return redirect(f"{reverse('core:subscription_upgrade')}?feature={feature}")


def _validate_cleaning_photo(upload):
    try:
        validate_image_upload(upload)
    except ValidationError as exc:
        return " ".join(exc.messages)
    return ""


def _guest_queryset_for_active_property(request):
    active_prop = get_active_property(request)
    if not active_prop:
        return Guest.objects.none()
    return (
        Guest.objects.filter(Q(bookings__room__prop=active_prop) | Q(bookings__isnull=True))
        .distinct()
    )


def _api_property_from_request(request, payload=None):
    payload = payload or {}
    prop_id = (
        request.headers.get("X-Circle-Property-ID")
        or request.headers.get("X-Property-ID")
        or payload.get("property_id")
    )
    if prop_id:
        try:
            return GuestHouseProperty.objects.get(pk=prop_id, is_active=True)
        except (GuestHouseProperty.DoesNotExist, ValueError, TypeError):
            return None

    active_properties = GuestHouseProperty.objects.filter(is_active=True).order_by("sort_order", "pk")
    if active_properties.count() == 1:
        return active_properties.first()
    return None


@login_required
def notifications_feed(request):
    today = timezone.localdate()
    now = timezone.now()
    active_prop = get_active_property(request)
    alerts = []

    # Bookings checking out today still Checked In
    checkouts_due = (
        Booking.objects.select_related("guest", "room")
        .filter(room__prop=active_prop, check_out_date=today, status="Checked In")
    )
    for b in checkouts_due:
        alerts.append({
            "id": f"checkout-{b.pk}",
            "type": "checkout",
            "title": "Checkout Due Today",
            "body": f"{b.guest.full_name} · {b.room.name} — scheduled checkout today",
            "icon": "🔔",
        })

    # Bookings checking IN today not yet checked in
    checkins_due = (
        Booking.objects.select_related("guest", "room")
        .filter(room__prop=active_prop, check_in_date=today, status__in=["Pending", "Confirmed"])
    )
    for b in checkins_due:
        alerts.append({
            "id": f"checkin-{b.pk}",
            "type": "checkin",
            "title": "Guest Arriving Today",
            "body": f"{b.guest.full_name} · {b.room.name} — check-in expected today",
            "icon": "🏨",
        })

    # Rooms needing cleaning
    dirty_rooms = Room.objects.filter(prop=active_prop, cleaning_status="Needs Cleaning")
    for r in dirty_rooms:
        alerts.append({
            "id": f"cleaning-{r.pk}",
            "type": "needs_cleaning",
            "title": "Room Needs Cleaning",
            "body": f"{r.name} — needs to be cleaned",
            "icon": "🧹",
        })

    # Rooms cleaned in the last 2 hours (proof uploaded recently)
    two_hours_ago = now - datetime.timedelta(hours=2)
    recently_cleaned_room_ids = set(
        CleaningProof.objects.filter(
            room__prop=active_prop,
            created_at__gte=two_hours_ago,
        ).values_list("room_id", flat=True)
    )
    for r in Room.objects.filter(prop=active_prop, pk__in=recently_cleaned_room_ids, cleaning_status="Clean"):
        alerts.append({
            "id": f"cleaned-{r.pk}-{today}",
            "type": "cleaned",
            "title": "Room Ready",
            "body": f"{r.name} — cleaning complete, room is ready",
            "icon": "✅",
        })

    return JsonResponse({"alerts": alerts})


_ONBOARDING_FEATURES = [
    "Booking & reservation management",
    "Room status board",
    "Guest profiles & history",
    "Invoice & receipt PDF generation",
    "WhatsApp message templates",
    "Housekeeping & cleaning board",
    "Maintenance request tracker",
    "Expense tracking",
    "Point of Sale (POS) terminal",
    "Inventory management",
    "Daily close & cash shift",
    "Occupancy & revenue reports",
    "Staff roles & access control",
    "Multi-property support",
    "Availability calendar",
    "Subscription & billing management",
]


@login_required
def onboarding(request):
    if not is_owner(request.user):
        return redirect("core:home")
    if request.method == "POST":
        GuestHouseSettings.objects.filter(pk=1).update(onboarding_complete=True)
        return redirect("core:home")
    # Always render when visited directly (allows re-opening from Settings)
    settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
    return render(request, "core/onboarding.html", {
        "gh_settings": settings_obj,
        "features": _ONBOARDING_FEATURES,
    })


@login_required
def home(request):
    if is_cleaner(request.user):
        return redirect("core:cleaning")
    if is_owner(request.user):
        _s = GuestHouseSettings.objects.filter(pk=1).only("onboarding_complete").first()
        if _s and not _s.onboarding_complete:
            return redirect("core:onboarding")
    _expire_elapsed_bookings()
    today = timezone.localdate()
    month_start = today.replace(day=1)
    month_end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    next_three_days = today + datetime.timedelta(days=3)
    active_status_filter = ~Q(status__in=Booking.INACTIVE_STATUSES)

    active_prop, rooms = _property_rooms(request)
    _, property_bookings = _property_bookings(request)
    _, property_payments = _property_payments(request)
    _, property_pos_sales = _property_pos_sales(request)
    _, property_pos_shifts = _property_pos_shifts(request)
    total_rooms = rooms.count()
    occupied_rooms = rooms.filter(status="Occupied").count()
    booked_rooms = rooms.filter(status="Booked").count()
    available_rooms = rooms.filter(status="Available").count()
    rooms_needing_cleaning = rooms.filter(cleaning_status="Needs Cleaning").count()

    monthly_revenue = (
        property_payments.filter(payment_date__gte=month_start, payment_date__lte=month_end)
        .aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    active_bookings = property_bookings.select_related("guest", "room").filter(active_status_filter)

    # Subqueries to detect fully-refunded bookings (total refunded >= total paid > 0)
    _refund_sq = (
        BookingRefund.objects.filter(booking_id=OuterRef("pk"))
        .values("booking_id").annotate(s=Sum("amount")).values("s")
    )
    _payment_sq = (
        Payment.objects.filter(booking_id=OuterRef("pk"))
        .values("booking_id").annotate(s=Sum("amount")).values("s")
    )

    def _genuinely_outstanding(qs):
        return (
            qs.filter(balance_due__gt=0)
            .annotate(
                _ann_refunded=Coalesce(Subquery(_refund_sq), Value(Decimal("0.00"), output_field=DecimalField())),
                _ann_paid=Coalesce(Subquery(_payment_sq), Value(Decimal("0.00"), output_field=DecimalField())),
            )
            .exclude(_ann_refunded__gte=F("_ann_paid"), _ann_paid__gt=0)
        )

    outstanding_balances = (
        _genuinely_outstanding(active_bookings).aggregate(total=Sum("balance_due"))["total"]
        or Decimal("0.00")
    )
    bookings_this_month = active_bookings.filter(
        check_in_date__gte=month_start,
        check_in_date__lte=month_end,
    ).count()

    monthly_bookings = property_bookings.select_related("guest", "room").filter(
        status__in=["Checked In", "Checked Out"],
        check_in_date__lt=month_end + datetime.timedelta(days=1),
        check_out_date__gt=month_start,
    )
    booked_room_nights = 0
    for booking in monthly_bookings:
        stay_start = max(booking.check_in_date, month_start)
        stay_end = min(booking.check_out_date, month_end + datetime.timedelta(days=1))
        booked_room_nights += max((stay_end - stay_start).days, 0)
    bookable_rooms = rooms.filter(status__in=["Available", "Booked", "Occupied", "Cleaning"]).count()
    possible_room_nights = bookable_rooms * month_end.day
    occupancy_rate = round((booked_room_nights / possible_room_nights) * 100, 1) if possible_room_nights else 0
    occupied_capacity_rate = round((occupied_rooms / total_rooms) * 100, 1) if total_rooms else 0

    last_6_months_revenue = []
    chart_month = month_start
    for _ in range(5):
        previous_month_end = chart_month - datetime.timedelta(days=1)
        chart_month = previous_month_end.replace(day=1)
    for offset in range(6):
        chart_month_end = chart_month.replace(day=calendar.monthrange(chart_month.year, chart_month.month)[1])
        revenue = (
            property_payments.filter(payment_date__gte=chart_month, payment_date__lte=chart_month_end)
            .aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )
        last_6_months_revenue.append(
            {
                "month_label": chart_month.strftime("%b"),
                "revenue": float(revenue),
            }
        )
        chart_month = chart_month_end + datetime.timedelta(days=1)

    todays_checkins = active_bookings.filter(check_in_date=today, status="Confirmed").order_by("room__name")
    todays_checkouts = active_bookings.filter(check_out_date=today, status="Checked In").order_by("room__name")
    in_house_bookings = active_bookings.filter(status="Checked In").order_by("check_out_date", "room__name")
    for booking in in_house_bookings:
        booking.nights_so_far = max((today - booking.check_in_date).days, 0)

    unpaid_bookings = _genuinely_outstanding(active_bookings).order_by("-balance_due", "check_out_date")[:8]
    unpaid_balance_count = _genuinely_outstanding(active_bookings).count()
    upcoming_arrivals = active_bookings.filter(
        status="Confirmed",
        check_in_date__gt=today,
        check_in_date__lte=next_three_days,
    ).order_by("check_in_date", "room__name")
    upcoming_arrivals_count = upcoming_arrivals.count()
    alert_count = unpaid_balance_count + upcoming_arrivals_count
    low_stock_inventory_items = InventoryItem.objects.filter(
        is_active=True,
        prop=active_prop,
        current_stock__lte=F("minimum_stock"),
    ).select_related("category").order_by("current_stock", "name")[:5]
    low_stock_inventory_count = InventoryItem.objects.filter(
        is_active=True,
        prop=active_prop,
        current_stock__lte=F("minimum_stock"),
    ).count()
    open_maintenance_requests = MaintenanceRequest.objects.select_related("room").filter(
        room__prop=active_prop,
        status__in=["open", "in_progress"],
    )
    urgent_maintenance_requests = open_maintenance_requests.filter(priority="urgent").order_by("scheduled_for", "-reported_at")[:5]
    urgent_room_ids = set(open_maintenance_requests.filter(priority="urgent", room__isnull=False).values_list("room_id", flat=True))
    owner_exception_count = 0
    owner_refund_count = 0
    owner_open_shift_count = 0
    owner_locked_today = False
    if is_owner(request.user):
        today_audits = AuditLog.objects.filter(created_at__date=today)
        owner_exception_count = today_audits.filter(
            Q(action__in=["delete", "refund", "login_override", "exception", "void"])
            | Q(reason__icontains="discount")
            | Q(reason__icontains="backdated")
            | Q(reason__icontains="cancel")
        ).count()
        owner_refund_count = (
            property_pos_sales.filter(created_at__date=today, status="refunded").count()
            + BookingRefund.objects.filter(refund_date=today, booking__room__prop=active_prop).count()
        )
        owner_open_shift_count = property_pos_shifts.filter(closed_at__isnull=True).count()
        owner_locked_today = DailyCloseLock.objects.filter(close_date=today).exists()

    checked_in_by_room = {booking.room_id: booking for booking in in_house_bookings}
    room_board = list(rooms)
    for room in room_board:
        room.current_booking = checked_in_by_room.get(room.pk)
        if room.current_booking:
            guest = room.current_booking.guest
            room.current_guest_name = guest.full_name
        else:
            room.current_guest_name = ""
        room.has_urgent_maintenance = room.pk in urgent_room_ids

    return render(
        request,
        "core/dashboard.html",
        {
            "total_rooms": total_rooms,
            "occupied_rooms": occupied_rooms,
            "booked_rooms": booked_rooms,
            "available_rooms": available_rooms,
            "rooms_needing_cleaning": rooms_needing_cleaning,
            "occupied_capacity_rate": occupied_capacity_rate,
            "monthly_revenue": monthly_revenue,
            "outstanding_balances": outstanding_balances,
            "bookings_this_month": bookings_this_month,
            "occupancy_rate": occupancy_rate,
            "last_6_months_revenue": last_6_months_revenue,
            "todays_checkins": todays_checkins,
            "todays_checkouts": todays_checkouts,
            "in_house_bookings": in_house_bookings,
            "unpaid_bookings": unpaid_bookings,
            "unpaid_balance_count": unpaid_balance_count,
            "upcoming_arrivals": upcoming_arrivals,
            "upcoming_arrivals_count": upcoming_arrivals_count,
            "alert_count": alert_count,
            "low_stock_inventory_items": low_stock_inventory_items,
            "low_stock_inventory_count": low_stock_inventory_count,
            "open_maintenance_count": open_maintenance_requests.count(),
            "urgent_maintenance_requests": urgent_maintenance_requests,
            "urgent_maintenance_count": open_maintenance_requests.filter(priority="urgent").count(),
            "owner_exception_count": owner_exception_count,
            "owner_refund_count": owner_refund_count,
            "owner_open_shift_count": owner_open_shift_count,
            "owner_locked_today": owner_locked_today,
            "room_board": room_board,
            "today": today,
        },
    )


@login_required
def search(request):
    query = request.GET.get("q", "").strip()
    active_prop = get_active_property(request)
    guests = Guest.objects.none()
    bookings = Booking.objects.none()
    rooms = Room.objects.none()
    if query:
        property_bookings = Booking.objects.filter(room__prop=active_prop)
        guests = Guest.objects.filter(
            bookings__room__prop=active_prop,
        ).filter(
            Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(phone__icontains=query)
            | Q(email__icontains=query)
        ).distinct()[:10]
        bookings = (
            property_bookings.select_related("guest", "room")
            .filter(
                Q(booking_reference__icontains=query)
                | Q(guest__first_name__icontains=query)
                | Q(guest__last_name__icontains=query)
            )[:10]
        )
        rooms = Room.objects.filter(prop=active_prop, name__icontains=query)[:10]
    return render(
        request,
        "search_results.html",
        {
            "query": query,
            "guest_results": guests,
            "booking_results": bookings,
            "room_results": rooms,
        },
    )


@login_required
def settings_view(request):
    settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
    generated_pos_api_key = request.session.pop("generated_pos_api_key", "")

    if request.method == "POST":
        if request.POST.get("action") == "regenerate_pos_api_key":
            generated_pos_api_key = settings_obj.regenerate_pos_api_key()
            request.session["generated_pos_api_key"] = generated_pos_api_key
            if request.POST.get("has_existing_key"):
                messages.warning(request, "POS API key regenerated. Your previous POS connection is now invalid.")
            else:
                messages.success(request, "POS API key generated.")
            return redirect("core:settings")
        form = GuestHouseSettingsForm(request.POST, request.FILES, instance=settings_obj)
        if form.is_valid():
            form.save()
            messages.success(request, "Settings updated successfully.")
            return redirect("core:settings")
    else:
        form = GuestHouseSettingsForm(instance=settings_obj)

    return render(
        request,
        "settings.html",
        {
            "form": form,
            "settings_obj": settings_obj,
            "generated_pos_api_key": generated_pos_api_key,
        },
    )


def get_active_property(request):
    """Return the Property the user is currently working in, falling back to first/only one."""
    prop_id = request.session.get("active_property_id")
    default_prop = GuestHouseProperty.objects.filter(is_active=True).order_by("sort_order", "pk").first()
    if default_prop:
        _backfill_unassigned_property_data(default_prop)
    if prop_id:
        prop = GuestHouseProperty.objects.filter(pk=prop_id, is_active=True).first()
        if prop:
            return prop
    # Fall back: first property (or create default)
    prop = GuestHouseProperty.objects.filter(is_active=True).order_by("sort_order", "pk").first()
    if prop:
        request.session["active_property_id"] = prop.pk
    return prop


def _backfill_unassigned_property_data(default_prop):
    if not default_prop:
        return
    Room.objects.filter(prop__isnull=True).update(prop=default_prop)
    Expense.objects.filter(prop__isnull=True).update(prop=default_prop)
    InventoryItem.objects.filter(prop__isnull=True).update(prop=default_prop)
    POSCategory.objects.filter(prop__isnull=True).update(prop=default_prop)
    POSItem.objects.filter(prop__isnull=True).update(prop=default_prop)
    POSSale.objects.filter(prop__isnull=True).update(prop=default_prop)
    POSShift.objects.filter(prop__isnull=True).update(prop=default_prop)


def _property_rooms(request):
    active_prop = get_active_property(request)
    if not active_prop:
        return active_prop, Room.objects.none()
    return active_prop, Room.objects.filter(prop=active_prop)


def _property_bookings(request):
    active_prop = get_active_property(request)
    if not active_prop:
        return active_prop, Booking.objects.none()
    return active_prop, Booking.objects.filter(room__prop=active_prop)


def _property_payments(request):
    active_prop = get_active_property(request)
    if not active_prop:
        return active_prop, Payment.objects.none()
    return active_prop, Payment.objects.filter(booking__room__prop=active_prop)


def _property_pos_shifts(request):
    active_prop = get_active_property(request)
    if not active_prop:
        return active_prop, POSShift.objects.none()
    return active_prop, POSShift.objects.filter(prop=active_prop)


def _property_pos_sales(request):
    active_prop = get_active_property(request)
    if not active_prop:
        return active_prop, POSSale.objects.none()
    return active_prop, POSSale.objects.filter(prop=active_prop)


@login_required
def property_switch(request, pk):
    if not is_owner(request.user):
        return redirect("core:home")
    prop = get_object_or_404(GuestHouseProperty, pk=pk, is_active=True)
    request.session["active_property_id"] = prop.pk
    return redirect(request.GET.get("next") or "core:home")


@login_required
def property_list(request):
    if not is_owner(request.user):
        return redirect("core:home")
    properties = GuestHouseProperty.objects.all()
    return render(request, "core/property_list.html", {"properties": properties})


@login_required
def property_add(request):
    if not is_owner(request.user):
        return redirect("core:home")
    form_values = {"name": "", "address": "", "phone": "", "email": "", "description": ""}
    subscription = _current_subscription()
    plan = subscription.plan if subscription else None
    active_property_count = GuestHouseProperty.objects.filter(is_active=True).count()
    if active_property_count >= 1 and not (subscription and subscription.has_feature("multi_property")):
        plan_name = plan.display_name if plan else "your current"
        return _limit_reached_response(
            request,
            f"{plan_name} plan allows one active property. Upgrade to Enterprise to add multiple properties.",
            "multi_property",
        )
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        address = request.POST.get("address", "").strip()
        phone = request.POST.get("phone", "").strip()
        email = request.POST.get("email", "").strip()
        description = request.POST.get("description", "").strip()
        form_values = {
            "name": name,
            "address": address,
            "phone": phone,
            "email": email,
            "description": description,
        }
        if not name:
            messages.error(request, "Property name is required.")
            return render(request, "core/property_form.html", {"form_values": form_values})
        prop = GuestHouseProperty.objects.create(
            name=name, address=address, phone=phone, email=email, description=description
        )
        request.session["active_property_id"] = prop.pk
        messages.success(request, f"Property '{name}' created and set as active.")
        return redirect("core:property_list")
    return render(request, "core/property_form.html", {"form_values": form_values})


@login_required
def property_edit(request, pk):
    if not is_owner(request.user):
        return redirect("core:home")
    prop = get_object_or_404(GuestHouseProperty, pk=pk)
    form_values = {
        "name": prop.name,
        "address": prop.address,
        "phone": prop.phone,
        "email": prop.email,
        "description": prop.description,
    }
    if request.method == "POST":
        prop.name = request.POST.get("name", prop.name).strip()
        prop.address = request.POST.get("address", "").strip()
        prop.phone = request.POST.get("phone", "").strip()
        prop.email = request.POST.get("email", "").strip()
        prop.description = request.POST.get("description", "").strip()
        prop.is_active = request.POST.get("is_active") == "on"
        subscription = _current_subscription()
        plan = subscription.plan if subscription else None
        if prop.is_active and not prop.__class__.objects.filter(pk=prop.pk, is_active=True).exists():
            active_property_count = GuestHouseProperty.objects.filter(is_active=True).exclude(pk=prop.pk).count()
            if active_property_count >= 1 and not (subscription and subscription.has_feature("multi_property")):
                plan_name = plan.display_name if plan else "your current"
                return _limit_reached_response(
                    request,
                    f"{plan_name} plan allows one active property. Upgrade to Enterprise to reactivate multiple properties.",
                    "multi_property",
                )
        form_values = {
            "name": prop.name,
            "address": prop.address,
            "phone": prop.phone,
            "email": prop.email,
            "description": prop.description,
        }
        if not prop.name:
            messages.error(request, "Property name is required.")
            return render(request, "core/property_form.html", {"prop": prop, "editing": True, "form_values": form_values})
        prop.save()
        messages.success(request, f"Property '{prop.name}' updated.")
        return redirect("core:property_list")
    return render(request, "core/property_form.html", {"prop": prop, "editing": True, "form_values": form_values})


@login_required
def staff_list(request):
    if not is_owner(request.user):
        return redirect("core:home")
    staff_users = (
        AuthUser.objects.filter(is_superuser=False)
        .prefetch_related("groups")
        .order_by("username")
    )
    return render(request, "core/staff_list.html", {"staff_users": staff_users, "roles": STAFF_ROLES})


@login_required
def staff_add(request):
    if not is_owner(request.user):
        return redirect("core:home")
    subscription, plan, current_users, max_users = _user_limit_context()
    if plan and not _is_unlimited(max_users) and current_users >= max_users:
        return _limit_reached_response(
            request,
            f"{plan.display_name} allows up to {max_users} active users. Upgrade your plan to add more staff accounts.",
            "users",
        )
    if request.method == "POST":
        username = request.POST.get("username", "").strip().lower()
        email = request.POST.get("email", "").strip()
        password = request.POST.get("password", "").strip()
        role_name = request.POST.get("role", "")
        first_name = request.POST.get("first_name", "").strip()
        last_name = request.POST.get("last_name", "").strip()

        if not username or not password or role_name not in STAFF_ROLES:
            messages.error(request, "Username, password, and a valid role are required.")
            return render(request, "core/staff_form.html", {"roles": STAFF_ROLES, "post": request.POST})

        if AuthUser.objects.filter(username=username).exists():
            messages.error(request, f"Username '{username}' is already taken.")
            return render(request, "core/staff_form.html", {"roles": STAFF_ROLES, "post": request.POST})

        user = AuthUser.objects.create_user(
            username=username, email=email, password=password,
            first_name=first_name, last_name=last_name,
        )
        group, _ = Group.objects.get_or_create(name=role_name)
        user.groups.add(group)
        messages.success(request, f"{role_name} account created for {username}.")
        return redirect("core:staff_list")

    return render(request, "core/staff_form.html", {
        "roles": STAFF_ROLES,
        "post": {"first_name": "", "last_name": "", "username": "", "email": "", "role": ""},
    })


@login_required
def staff_edit(request, pk):
    if not is_owner(request.user):
        return redirect("core:home")
    staff_user = get_object_or_404(AuthUser, pk=pk, is_superuser=False)
    if request.method == "POST":
        role_name = request.POST.get("role", "")
        new_password = request.POST.get("new_password", "").strip()
        is_active = request.POST.get("is_active") == "on"
        if is_active and not staff_user.is_active:
            subscription, plan, current_users, max_users = _user_limit_context()
            if plan and not _is_unlimited(max_users) and current_users >= max_users:
                return _limit_reached_response(
                    request,
                    f"{plan.display_name} allows up to {max_users} active users. Upgrade your plan to reactivate more staff accounts.",
                    "users",
                )

        if role_name in STAFF_ROLES:
            staff_user.groups.clear()
            group, _ = Group.objects.get_or_create(name=role_name)
            staff_user.groups.add(group)

        if new_password:
            staff_user.set_password(new_password)

        staff_user.first_name = request.POST.get("first_name", "").strip()
        staff_user.last_name = request.POST.get("last_name", "").strip()
        staff_user.email = request.POST.get("email", "").strip()
        staff_user.is_active = is_active
        staff_user.save()
        messages.success(request, f"Staff member {staff_user.username} updated.")
        return redirect("core:staff_list")

    current_role = next(
        (g.name for g in staff_user.groups.all() if g.name in STAFF_ROLES), None
    )
    return render(request, "core/staff_form.html", {
        "staff_user": staff_user,
        "roles": STAFF_ROLES,
        "current_role": current_role,
        "editing": True,
        "post": {"first_name": "", "last_name": "", "username": "", "email": "", "role": ""},
    })


@login_required
def staff_delete(request, pk):
    if not is_owner(request.user):
        return redirect("core:home")
    staff_user = get_object_or_404(AuthUser, pk=pk, is_superuser=False)
    if request.user.pk == staff_user.pk:
        messages.error(request, "You cannot delete your own account.")
        return redirect("core:staff_list")
    if request.method == "POST":
        name = staff_user.username
        staff_user.delete()
        messages.success(request, f"Staff member '{name}' removed.")
    return redirect("core:staff_list")


@login_required
def room_list(request):
    _expire_elapsed_bookings()
    active_prop = get_active_property(request)
    status_filter = request.GET.get("status", "")
    rooms = Room.objects.filter(prop=active_prop) if active_prop else Room.objects.none()
    if status_filter:
        rooms = rooms.filter(status=status_filter)
    base_qs = Room.objects.filter(prop=active_prop) if active_prop else Room.objects.none()
    status_counts = {
        "Available": base_qs.filter(status="Available").count(),
        "Booked": base_qs.filter(status="Booked").count(),
        "Occupied": base_qs.filter(status="Occupied").count(),
        "Cleaning": base_qs.filter(status="Cleaning").count(),
        "Maintenance": base_qs.filter(status="Maintenance").count(),
        "Blocked": base_qs.filter(status="Blocked").count(),
    }
    return render(
        request,
        "core/room_list.html",
        {
            "rooms": rooms,
            "status_filter": status_filter,
            "status_counts": status_counts,
            "is_cleaner": is_cleaner(request.user),
        },
    )


@login_required
def room_add(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    subscription, plan, current_rooms, max_rooms = _room_limit_context()
    if plan and not _is_unlimited(max_rooms) and current_rooms >= max_rooms:
        return _limit_reached_response(
            request,
            f"{plan.display_name} allows up to {max_rooms} rooms. Upgrade your plan to add more rooms.",
            "rooms",
        )
    if request.method == "POST":
        form = RoomForm(request.POST)
        if form.is_valid():
            room = form.save(commit=False)
            room.prop = active_prop
            room.save()
            messages.success(request, "Room added successfully.")
            return redirect("core:room_list")
    else:
        form = RoomForm()
    return render(request, "core/room_form.html", {"form": form, "title": "Add Room"})


@login_required
def room_edit(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    room = get_object_or_404(Room, pk=pk, prop=active_prop)
    if request.method == "POST":
        form = RoomForm(request.POST, instance=room)
        if form.is_valid():
            form.save()
            messages.success(request, "Room updated successfully.")
            return redirect("core:room_list")
    else:
        form = RoomForm(instance=room)
    return render(
        request,
        "core/room_form.html",
        {"form": form, "room": room, "title": f"Edit {room.name}"},
    )


@login_required
def room_detail(request, pk):
    active_prop = get_active_property(request)
    room = get_object_or_404(Room, pk=pk, prop=active_prop)
    _expire_elapsed_bookings(room=room)
    room.refresh_from_db()
    maintenance_requests = room.maintenance_requests.select_related("reported_by").all().order_by("-reported_at")
    today = timezone.localdate()
    history_period = request.GET.get("history", "month")
    if history_period not in ["day", "week", "month"]:
        history_period = "month"

    if history_period == "day":
        history_start = today
        history_end = today
        history_label = today.strftime("%d %b %Y")
    elif history_period == "week":
        history_start = today - datetime.timedelta(days=today.weekday())
        history_end = history_start + datetime.timedelta(days=6)
        history_label = f"{history_start.strftime('%d %b')} - {history_end.strftime('%d %b %Y')}"
    else:
        history_start = today.replace(day=1)
        history_end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
        history_label = today.strftime("%B %Y")

    booking_history = (
        room.bookings.select_related("guest")
        .filter(
            check_in_date__lte=history_end,
            check_out_date__gte=history_start,
        )
        .order_by("-check_in_date", "-created_at")
    )
    booking_history_count = booking_history.count()
    booking_history_revenue = booking_history.aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")
    active_booking = room.bookings.filter(status="Checked In").order_by("-check_in_time").first()

    return render(
        request,
        "core/room_detail.html",
        {
            "room": room,
            "maintenance_requests": maintenance_requests,
            "booking_history": booking_history,
            "booking_history_count": booking_history_count,
            "booking_history_revenue": booking_history_revenue,
            "history_period": history_period,
            "history_label": history_label,
            "active_booking": active_booking,
            "is_cleaner": is_cleaner(request.user),
        },
    )


@login_required
def room_delete(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    room = get_object_or_404(Room, pk=pk, prop=active_prop)
    if request.method == "POST":
        approver = _manager_approval(request, "Delete room")
        if not approver:
            messages.error(request, "Owner approval is required to delete rooms.")
            return redirect("core:room_detail", pk=room.pk)
        before = _model_snapshot(room, ["name", "room_type", "status", "cleaning_status"])
        name = room.name
        try:
            room.delete()
        except ProtectedError:
            messages.error(request, f'"{name}" cannot be deleted because it has bookings or related records. Mark it blocked or inactive instead.')
            return redirect("core:room_detail", pk=pk)
        _audit(request, "delete", room, before=before, reason="Room deleted", approved_by=approver)
        messages.success(request, f'"{name}" deleted successfully.')
        return redirect("core:room_list")
    return render(request, "rooms/room_delete.html", {"room": room})


@login_required
def cleaning_board(request):
    _expire_elapsed_bookings()
    today = timezone.localdate()
    clean_preview_limit = 12
    active_prop, rooms_qs = _property_rooms(request)
    rooms = list(rooms_qs)
    upcoming_bookings = (
        Booking.objects.select_related("guest")
        .filter(room__prop=active_prop, status="Confirmed", check_in_date__gte=today)
        .order_by("check_in_date")
    )
    next_booking_by_room = {}
    for booking in upcoming_bookings:
        next_booking_by_room.setdefault(booking.room_id, booking)

    columns = {
        "Needs Cleaning": [],
        "In Progress": [],
        "Clean": [],
    }
    for room in rooms:
        room.next_booking = next_booking_by_room.get(room.pk)
        columns[room.cleaning_status].append(room)

    return render(
        request,
        "cleaning.html",
        {
            "needs_cleaning_rooms": columns["Needs Cleaning"],
            "in_progress_rooms": columns["In Progress"],
            "clean_rooms": columns["Clean"],
            "clean_rooms_preview": columns["Clean"][:clean_preview_limit],
            "clean_rooms_extra_count": max(len(columns["Clean"]) - clean_preview_limit, 0),
        },
    )


@login_required
def cleaning_set_status(request, pk):
    active_prop = get_active_property(request)
    room = get_object_or_404(Room, pk=pk, prop=active_prop)
    if request.method == "POST":
        cleaning_status = request.POST.get("cleaning_status")
        valid_statuses = {choice[0] for choice in Room.CLEANING_STATUS_CHOICES}
        if cleaning_status not in valid_statuses:
            messages.error(request, "Invalid cleaning status.")
            return redirect("core:cleaning")

        before = _model_snapshot(room, ["status", "cleaning_status"])
        room.cleaning_status = cleaning_status
        if cleaning_status == "Clean" and room.status == "Cleaning":
            room.status = "Available"
        room.save()
        if cleaning_status == "Clean":
            _sync_room_status(room)
            if request.FILES.get("cleaning_photo"):
                validation_error = _validate_cleaning_photo(request.FILES["cleaning_photo"])
                if validation_error:
                    messages.error(request, validation_error)
                    return redirect("core:cleaning")
                proof = CleaningProof.objects.create(
                    room=room,
                    uploaded_by=request.user,
                    photo=request.FILES["cleaning_photo"],
                    notes=request.POST.get("cleaning_notes", ""),
                )
                _audit(
                    request,
                    "create",
                    proof,
                    after={"room": room.name, "photo": proof.photo.name},
                    reason="Cleaning proof uploaded",
                )
        _audit(
            request,
            "update",
            room,
            before=before,
            after=_model_snapshot(room, ["status", "cleaning_status"]),
            reason=f"Cleaning status set to {cleaning_status}",
        )
        messages.success(request, f"{room.name} marked as {cleaning_status}.")
    return redirect("core:cleaning")


@login_required
def cleaning_proof_review(request):
    if not is_owner(request.user):
        return redirect("core:cleaning")
    today = timezone.localdate()
    selected_date = request.GET.get("date", "")
    room_id = request.GET.get("room", "")
    active_prop = get_active_property(request)
    proofs = CleaningProof.objects.select_related("room", "uploaded_by").filter(room__prop=active_prop)
    if selected_date:
        try:
            proofs = proofs.filter(created_at__date=datetime.date.fromisoformat(selected_date))
        except ValueError:
            selected_date = ""
    if room_id:
        proofs = proofs.filter(room_id=room_id)
    paginator = Paginator(proofs, 24)
    page_obj = paginator.get_page(request.GET.get("page"))
    rooms = Room.objects.filter(prop=active_prop).order_by("name")
    return render(
        request,
        "core/cleaning_proof_review.html",
        {
            "page_obj": page_obj,
            "rooms": rooms,
            "selected_date": selected_date,
            "selected_room": room_id,
            "today": today,
            "proof_count": proofs.count(),
        },
    )


@login_required
def cleaning_proof_photo(request, pk):
    active_prop = get_active_property(request)
    proof = get_object_or_404(CleaningProof.objects.select_related("room"), pk=pk, room__prop=active_prop)
    if not proof.photo:
        return HttpResponse(status=404)
    content_type, _ = mimetypes.guess_type(proof.photo.name)
    return FileResponse(proof.photo.open("rb"), content_type=content_type or "application/octet-stream")


@login_required
def expense_list(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    today = timezone.localdate()
    selected_month = request.GET.get("month", today.strftime("%Y-%m"))
    selected_category = request.GET.get("category", "")

    try:
        month_start = datetime.date.fromisoformat(f"{selected_month}-01")
    except ValueError:
        selected_month = today.strftime("%Y-%m")
        month_start = today.replace(day=1)
    month_end = month_start.replace(day=calendar.monthrange(month_start.year, month_start.month)[1])

    active_prop = get_active_property(request)
    expenses = Expense.objects.filter(date__gte=month_start, date__lte=month_end, prop=active_prop)
    if selected_category:
        expenses = expenses.filter(category=selected_category)

    total_expenses = expenses.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    return render(
        request,
        "expenses/expense_list.html",
        {
            "expenses": expenses,
            "total_expenses": total_expenses,
            "selected_month": selected_month,
            "selected_category": selected_category,
            "category_choices": Expense.CATEGORY_CHOICES,
        },
    )


@login_required
def expense_add(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    if request.method == "POST":
        form = ExpenseForm(request.POST)
        if form.is_valid():
            if _is_date_locked(form.cleaned_data["date"]):
                return _locked_day_response(request, form.cleaned_data["date"], "core:expense_list")
            expense = form.save(commit=False)
            expense.prop = active_prop
            expense.save()
            messages.success(request, "Expense added successfully.")
            return redirect("core:expense_list")
    else:
        initial = {
            "date": timezone.localdate(),
            "category": request.GET.get("category", ""),
            "description": request.GET.get("description", ""),
            "amount": request.GET.get("amount", ""),
        }
        form = ExpenseForm(initial=initial)
    return render(request, "expenses/expense_form.html", {"form": form, "title": "Add Expense"})


@login_required
def expense_edit(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    expense = get_object_or_404(Expense, pk=pk, prop=active_prop)
    if _is_date_locked(expense.date):
        return _locked_day_response(request, expense.date, "core:expense_list")
    if request.method == "POST":
        form = ExpenseForm(request.POST, instance=expense)
        if form.is_valid():
            if _is_date_locked(form.cleaned_data["date"]):
                return _locked_day_response(request, form.cleaned_data["date"], "core:expense_list")
            form.save()
            messages.success(request, "Expense updated successfully.")
            return redirect("core:expense_list")
    else:
        form = ExpenseForm(instance=expense)
    return render(
        request,
        "expenses/expense_form.html",
        {"form": form, "expense": expense, "title": "Edit Expense"},
    )


@login_required
def expense_delete(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    expense = get_object_or_404(Expense, pk=pk, prop=active_prop)
    if _is_date_locked(expense.date):
        return _locked_day_response(request, expense.date, "core:expense_list")
    if request.method == "POST":
        approver = _manager_approval(request, "Delete expense")
        if not approver:
            messages.error(request, "Owner approval is required to delete expenses.")
            return redirect("core:expense_list")
        before = _model_snapshot(expense, ["date", "category", "description", "amount", "payment_method"])
        expense.delete()
        _audit(request, "delete", expense, before=before, reason="Expense deleted", approved_by=approver)
        messages.success(request, "Expense deleted successfully.")
    return redirect("core:expense_list")


@login_required
def reports(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    try:
        from django.db.models import F
        TrialEngagement.objects.filter(pk=1).update(reports_viewed=F("reports_viewed") + 1)
    except Exception:
        pass
    today = timezone.localdate()
    try:
        selected_month = int(request.GET.get("month", today.month))
        selected_year = int(request.GET.get("year", today.year))
        month_start = datetime.date(selected_year, selected_month, 1)
    except (TypeError, ValueError):
        selected_month = today.month
        selected_year = today.year
        month_start = today.replace(day=1)

    month_end = month_start.replace(day=calendar.monthrange(selected_year, selected_month)[1])
    next_month_start = month_end + datetime.timedelta(days=1)
    active_status_filter = ~Q(status__in=Booking.INACTIVE_STATUSES)
    active_prop, property_bookings = _property_bookings(request)
    _, property_payments = _property_payments(request)
    property_expenses = Expense.objects.filter(prop=active_prop)

    gross_revenue = (
        property_payments.filter(payment_date__gte=month_start, payment_date__lte=month_end)
        .aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    total_refunds = (
        BookingRefund.objects.filter(
            booking__room__prop=active_prop,
            refund_date__gte=month_start,
            refund_date__lte=month_end,
        )
        .aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    total_revenue = max(gross_revenue - total_refunds, Decimal("0.00"))
    total_expenses = (
        property_expenses.filter(date__gte=month_start, date__lte=month_end)
        .aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    net_profit = total_revenue - total_expenses
    _refund_sq_r = (
        BookingRefund.objects.filter(booking_id=OuterRef("pk"))
        .values("booking_id").annotate(s=Sum("amount")).values("s")
    )
    _payment_sq_r = (
        Payment.objects.filter(booking_id=OuterRef("pk"))
        .values("booking_id").annotate(s=Sum("amount")).values("s")
    )
    outstanding_balances = (
        property_bookings.filter(active_status_filter, balance_due__gt=0)
        .annotate(
            _ann_refunded=Coalesce(Subquery(_refund_sq_r), Value(Decimal("0.00"), output_field=DecimalField())),
            _ann_paid=Coalesce(Subquery(_payment_sq_r), Value(Decimal("0.00"), output_field=DecimalField())),
        )
        .exclude(_ann_refunded__gte=F("_ann_paid"), _ann_paid__gt=0)
        .aggregate(total=Sum("balance_due"))["total"]
        or Decimal("0.00")
    )

    total_rooms = Room.objects.filter(prop=active_prop).count()
    days_in_month = month_end.day
    total_possible_room_nights = total_rooms * days_in_month

    sold_bookings = property_bookings.select_related("room", "guest").filter(
        status__in=["Checked In", "Checked Out"],
        check_in_date__lt=next_month_start,
        check_out_date__gt=month_start,
    )
    room_nights_sold = 0
    total_room_revenue = Decimal("0.00")
    revenue_per_room = {}
    top_guest_counts = {}
    for booking in sold_bookings:
        stay_start = max(booking.check_in_date, month_start)
        stay_end = min(booking.check_out_date, next_month_start)
        nights = max((stay_end - stay_start).days, 0)
        if nights == 0:
            # Same-day / hourly booking: use the actual booking total and count as 1 room-day
            revenue = booking.total_amount
            occupancy_units = 1
        else:
            revenue = booking.rate_per_night * Decimal(nights)
            occupancy_units = nights
        room_nights_sold += occupancy_units
        total_room_revenue += revenue

        room_data = revenue_per_room.setdefault(
            booking.room_id,
            {
                "room": booking.room,
                "booking_ids": set(),
                "total_nights": 0,
                "total_revenue": Decimal("0.00"),
            },
        )
        room_data["booking_ids"].add(booking.pk)
        room_data["total_nights"] += occupancy_units
        room_data["total_revenue"] += revenue

        guest_data = top_guest_counts.setdefault(
            booking.guest_id,
            {
                "guest": booking.guest,
                "stays": 0,
            },
        )
        guest_data["stays"] += 1

    for room_data in revenue_per_room.values():
        room_data["booking_count"] = len(room_data["booking_ids"])
    revenue_per_room_rows = sorted(
        revenue_per_room.values(),
        key=lambda item: item["total_revenue"],
        reverse=True,
    )

    occupancy_rate = (
        round((room_nights_sold / total_possible_room_nights) * 100, 1)
        if total_possible_room_nights
        else 0
    )
    average_rate_per_night = (
        (total_room_revenue / Decimal(room_nights_sold)).quantize(Decimal("0.01"))
        if room_nights_sold
        else Decimal("0.00")
    )

    monthly_bookings = property_bookings.filter(
        check_in_date__gte=month_start,
        check_in_date__lte=month_end,
    )
    booking_source_rows = (
        monthly_bookings.values("booking_source")
        .annotate(count=Count("id"))
        .order_by("booking_source")
    )
    booking_source_data = []
    for row in booking_source_rows:
        source_bookings = monthly_bookings.filter(booking_source=row["booking_source"])
        source_revenue = (
            property_payments.filter(
                booking__in=source_bookings,
                payment_date__gte=month_start,
                payment_date__lte=month_end,
            ).aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )
        booking_source_data.append(
            {
                "source": row["booking_source"],
                "count": row["count"],
                "total_revenue": source_revenue,
            }
        )

    booking_status_data = []
    for status_value, status_label in Booking.STATUS_CHOICES:
        booking_status_data.append(
            {
                "status": status_label,
                "count": monthly_bookings.filter(status=status_value).count(),
            }
        )

    top_guests = sorted(top_guest_counts.values(), key=lambda item: item["stays"], reverse=True)[:5]
    years = range(today.year - 4, today.year + 2)
    months = [(number, datetime.date(2000, number, 1).strftime("%B")) for number in range(1, 13)]

    revenue_by_week = []
    expenses_by_week = []
    week_start = month_start
    week_number = 1
    while week_start <= month_end:
        week_end = min(week_start + datetime.timedelta(days=6), month_end)
        revenue_total = (
            property_payments.filter(payment_date__gte=week_start, payment_date__lte=week_end)
            .aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )
        expense_total = (
            property_expenses.filter(date__gte=week_start, date__lte=week_end)
            .aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )
        label = f"Week {week_number}"
        revenue_by_week.append(
            {
                "label": label,
                "start": week_start.strftime("%d %b"),
                "end": week_end.strftime("%d %b"),
                "revenue": float(revenue_total),
            }
        )
        expenses_by_week.append(
            {
                "label": label,
                "start": week_start.strftime("%d %b"),
                "end": week_end.strftime("%d %b"),
                "expenses": float(expense_total),
            }
        )
        week_start = week_end + datetime.timedelta(days=1)
        week_number += 1

    rooms_revenue = [
        {
            "room_name": row["room"].name,
            "revenue": float(row["total_revenue"]),
            "bookings_count": row["booking_count"],
            "nights": row["total_nights"],
        }
        for row in revenue_per_room_rows
    ]

    booking_sources = [
        {
            "source": row["source"] or "Unknown",
            "count": row["count"],
            "revenue": float(row["total_revenue"]),
        }
        for row in booking_source_data
    ]

    occupancy_heatmap = []
    for day_number in range(1, days_in_month + 1):
        current_day = datetime.date(selected_year, selected_month, day_number)
        occupied_room_count = (
            property_bookings.filter(
                active_status_filter,
                check_in_date__lte=current_day,
                check_out_date__gt=current_day,
            )
            .values("room_id")
            .distinct()
            .count()
        )
        occupancy_pct = round((occupied_room_count / total_rooms) * 100, 1) if total_rooms else 0
        occupancy_heatmap.append(
            {
                "date": current_day.isoformat(),
                "day": day_number,
                "label": current_day.strftime("%d %b %Y"),
                "occupancy_pct": occupancy_pct,
            }
        )

    return render(
        request,
        "core/reports.html",
        {
            "selected_month": selected_month,
            "selected_year": selected_year,
            "month_name": month_start.strftime("%B"),
            "selected_period": month_start.strftime("%B %Y"),
            "months": months,
            "years": years,
            "total_revenue": total_revenue,
            "total_expenses": total_expenses,
            "net_profit": net_profit,
            "outstanding_balances": outstanding_balances,
            "total_possible_room_nights": total_possible_room_nights,
            "room_nights_sold": room_nights_sold,
            "occupancy_rate": occupancy_rate,
            "average_rate_per_night": average_rate_per_night,
            "revenue_per_room_rows": revenue_per_room_rows,
            "booking_source_data": booking_source_data,
            "booking_status_data": booking_status_data,
            "top_guests": top_guests,
            "revenue_by_week": revenue_by_week,
            "expenses_by_week": expenses_by_week,
            "booking_sources": booking_sources,
            "rooms_revenue": rooms_revenue,
            "occupancy_heatmap": occupancy_heatmap,
            "room_revenue_chart_height": max(len(rooms_revenue) * 40, 240),
        },
    )


@login_required
def guest_list(request):
    query = request.GET.get("q", "").strip()
    active_prop = get_active_property(request)
    guests = Guest.objects.filter(Q(bookings__room__prop=active_prop) | Q(bookings__isnull=True)).distinct().order_by("last_name", "first_name")
    if query:
        guests = guests.filter(
            Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(phone__icontains=query)
        )
    paginator = Paginator(guests, 50)
    page_obj = paginator.get_page(request.GET.get("page"))
    filter_params = request.GET.copy()
    filter_params.pop("page", None)
    filter_query = filter_params.urlencode()
    return render(request, "core/guest_list.html", {"guests": page_obj, "page_obj": page_obj, "query": query, "filter_query": filter_query})


@login_required
def guest_add(request):
    if request.method == "POST":
        form = GuestForm(request.POST)
        if form.is_valid():
            guest = form.save()
            messages.success(request, f"{guest.full_name} added successfully.")
            return redirect("core:guest_detail", pk=guest.pk)
    else:
        form = GuestForm()
    return render(request, "guests/guest_form.html", {"form": form, "title": "Add Guest"})


@login_required
def guest_quick_create(request):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed."}, status=405)
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON."}, status=400)

    first_name = data.get("first_name", "").strip()
    last_name = data.get("last_name", "").strip()
    phone = data.get("phone", "").strip()

    errors = {}
    if not first_name:
        errors["first_name"] = "First name is required."
    if not last_name:
        errors["last_name"] = "Last name is required."
    if not phone:
        errors["phone"] = "Phone number is required."
    if errors:
        return JsonResponse({"errors": errors}, status=400)

    guest = Guest.objects.create(first_name=first_name, last_name=last_name, phone=phone)
    return JsonResponse({"id": guest.pk, "full_name": guest.full_name, "phone": guest.phone})


@login_required
def guest_detail(request, pk):
    active_prop = get_active_property(request)
    guest = get_object_or_404(_guest_queryset_for_active_property(request), pk=pk)
    bookings = guest.bookings.select_related("room").filter(room__prop=active_prop).order_by("-check_in_date")
    booking_list = list(bookings)
    first_stay = booking_list[-1].check_in_date if booking_list else None
    last_stay = booking_list[0].check_in_date if booking_list else None
    return render(
        request,
        "core/guest_detail.html",
        {
            "guest": guest,
            "bookings": booking_list,
            "first_stay": first_stay,
            "last_stay": last_stay,
            "stay_count": len(booking_list),
        },
    )


@login_required
def guest_edit(request, pk):
    guest = get_object_or_404(_guest_queryset_for_active_property(request), pk=pk)
    if request.method == "POST":
        form = GuestForm(request.POST, instance=guest)
        if form.is_valid():
            form.save()
            messages.success(request, "Guest updated successfully.")
            return redirect("core:guest_detail", pk=guest.pk)
    else:
        form = GuestForm(instance=guest)
    return render(
        request,
        "guests/guest_form.html",
        {"form": form, "guest": guest, "title": f"Edit {guest.full_name}"},
    )


def _sync_room_status(room):
    """Set room.status to reflect active bookings. Leaves Maintenance/Blocked/Cleaning alone."""
    if room.status in ("Maintenance", "Blocked", "Cleaning"):
        return
    today = timezone.localdate()
    active = Booking.objects.filter(
        room=room,
        status__in=("Confirmed", "Pending", "Checked In"),
        check_out_date__gte=today,
    )
    if active.filter(status="Checked In").exists():
        new_status = "Occupied"
    elif active.filter(status__in=("Confirmed", "Pending")).exists():
        new_status = "Booked"
    else:
        new_status = "Available"
    if room.status != new_status:
        room.status = new_status
        room.save(update_fields=["status"])


def _booking_end_datetime(booking, settings_obj):
    if booking.is_hourly:
        end_time = booking.booking_end_time or booking.compute_hourly_end_time()
        if not end_time:
            return None
        start_time = booking.booking_start_time
        end_date = booking.check_in_date
        end_dt = datetime.datetime.combine(end_date, end_time)
        # If end_time is before start_time the booking crosses midnight — add 1 day
        if start_time and end_time <= start_time:
            end_dt += datetime.timedelta(days=1)
    else:
        end_date = booking.check_out_date
        end_time = settings_obj.check_out_time
        end_dt = datetime.datetime.combine(end_date, end_time)

    if timezone.is_naive(end_dt):
        end_dt = timezone.make_aware(end_dt, timezone.get_current_timezone())
    return end_dt


def _expire_elapsed_bookings(room=None):
    # Throttle the global (no-room) pass to once per 5 minutes to avoid hammering
    # the DB on every page load across 8+ views.
    if room is None:
        cache_key = f"expire_bookings:{connection.schema_name}"
        if cache.get(cache_key):
            return
        cache.set(cache_key, True, 300)

    settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
    now = timezone.now()
    bookings = Booking.objects.select_related("room").filter(
        status__in=("Pending", "Confirmed", "Checked In"),
    )
    if room is not None:
        bookings = bookings.filter(room=room)

    for booking in bookings:
        booking_end = _booking_end_datetime(booking, settings_obj)
        if not booking_end or booking_end > now:
            continue

        booking_room = booking.room
        if booking.status == "Checked In":
            booking.status = "Checked Out"
            if not booking.check_out_time:
                booking.check_out_time = now
            booking.save(update_fields=["status", "check_out_time"])
            if booking_room.status not in ("Maintenance", "Blocked"):
                booking_room.status = "Cleaning"
                booking_room.cleaning_status = "Needs Cleaning"
                booking_room.save(update_fields=["status", "cleaning_status"])
        else:
            booking.status = "No Show"
            booking.save(update_fields=["status"])
            _sync_room_status(booking_room)


@login_required
def completed_booking_reminders_api(request):
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        since_ms = int(request.GET.get("since", "0"))
    except (TypeError, ValueError):
        since_ms = 0

    now = timezone.now()
    since = datetime.datetime.fromtimestamp(since_ms / 1000, tz=datetime.timezone.utc) if since_ms > 0 else now
    window_start = max(since, now - datetime.timedelta(hours=12))
    settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
    active_prop = get_active_property(request)
    candidate_bookings = Booking.objects.select_related("guest", "room").filter(
        room__prop=active_prop,
        status__in=("Pending", "Confirmed", "Checked In", "Checked Out", "No Show"),
        check_in_date__lte=now.date(),
        check_out_date__gte=(now - datetime.timedelta(days=1)).date(),
    )

    reminders = []
    for booking in candidate_bookings:
        end_dt = _booking_end_datetime(booking, settings_obj)
        if not end_dt or not (window_start < end_dt <= now):
            continue
        reminders.append(
            {
                "id": booking.pk,
                "reference": booking.booking_reference,
                "guest": booking.guest.full_name,
                "room": booking.room.name,
                "duration": booking.duration_label,
                "ended_at": end_dt.isoformat(),
            }
        )

    return JsonResponse(
        {
            "server_time": int(now.timestamp() * 1000),
            "reminders": reminders,
        }
    )


def _room_prices_json(active_prop=None):
    settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
    data = {}
    rooms = Room.objects.filter(prop=active_prop) if active_prop else Room.objects.none()
    for room in rooms:
        data[str(room.pk)] = {
            "daily": str(room.get_price_for_duration("daily") or settings_obj.default_price_per_night or ""),
            "24_hours": str(room.get_price_for_duration("24_hours") or settings_obj.default_price_24_hours or ""),
            "weekly": str(room.price_per_week or ""),
            "1_hour": str(room.price_1_hour or ""),
            "2_hours": str(room.get_price_for_duration("2_hours") or ""),
            "3_hours": str(room.get_price_for_duration("3_hours") or ""),
            "5_hours": str(room.price_5_hours or ""),
        }
    return json.dumps(data)


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


@login_required
def availability(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    _expire_elapsed_bookings()
    check_in = request.GET.get("check_in", "").strip()
    check_out = request.GET.get("check_out", "").strip()
    check_in_date = _parse_date(check_in)
    check_out_date = _parse_date(check_out)
    dates_submitted = bool(check_in or check_out)
    has_valid_dates = bool(check_in_date and check_out_date and check_out_date > check_in_date)
    nights = (check_out_date - check_in_date).days if has_valid_dates else 0

    active_prop, rooms = _property_rooms(request)
    available_rooms = []
    unavailable_rooms = []

    if dates_submitted and not has_valid_dates:
        messages.error(request, "Please choose a check-out date after the check-in date.")

    if has_valid_dates:
        active_booking_room_ids = set(
            Booking.objects.filter(
                room__prop=active_prop,
                check_in_date__lt=check_out_date,
                check_out_date__gt=check_in_date,
            )
            .exclude(status__in=Booking.INACTIVE_STATUSES)
            .values_list("room_id", flat=True)
        )

        for room in rooms:
            is_available = room.status not in ("Maintenance", "Blocked") and room.pk not in active_booking_room_ids
            room.estimated_total = room.price_per_night * Decimal(nights)
            if is_available:
                available_rooms.append(room)
            else:
                unavailable_rooms.append(room)

    return render(
        request,
        "availability.html",
        {
            "rooms": rooms,
            "available_rooms": available_rooms,
            "unavailable_rooms": unavailable_rooms,
            "check_in": check_in,
            "check_out": check_out,
            "dates_submitted": dates_submitted,
            "has_valid_dates": has_valid_dates,
            "nights": nights,
        },
    )


@login_required
def booking_list(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    _expire_elapsed_bookings()
    active_prop, property_bookings = _property_bookings(request)
    bookings = property_bookings.select_related("guest", "room").order_by("-check_in_date", "-created_at")
    status_filter = request.GET.get("status", "")
    source_filter = request.GET.get("source", "")
    month_filter = request.GET.get("month", "")

    if status_filter:
        bookings = bookings.filter(status=status_filter)
    if source_filter:
        bookings = bookings.filter(booking_source=source_filter)
    if month_filter:
        try:
            month_start = datetime.date.fromisoformat(f"{month_filter}-01")
            month_end = month_start.replace(day=calendar.monthrange(month_start.year, month_start.month)[1])
            bookings = bookings.filter(check_in_date__gte=month_start, check_in_date__lte=month_end)
        except ValueError:
            pass

    filtered_count = bookings.count()
    filtered_revenue = bookings.aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")
    status_counts = {
        status: property_bookings.filter(status=status).count()
        for status in ["Confirmed", "Checked In", "Checked Out", "Pending", "Cancelled"]
    }
    status_counts["checked_in"] = status_counts.get("Checked In", 0)
    active_filters = []
    query_params = request.GET.copy()
    for key, label in (("status", "Status"), ("source", "Source"), ("month", "Month")):
        value = request.GET.get(key)
        if value:
            params = query_params.copy()
            params.pop(key, None)
            active_filters.append(
                {
                    "label": label,
                    "value": value,
                    "remove_url": f"{request.path}?{params.urlencode()}" if params else request.path,
                }
            )

    paginator = Paginator(bookings, 50)
    page_obj = paginator.get_page(request.GET.get("page"))
    filter_params = request.GET.copy()
    filter_params.pop("page", None)
    filter_query = filter_params.urlencode()

    return render(
        request,
        "core/booking_list.html",
        {
            "bookings": page_obj,
            "page_obj": page_obj,
            "filter_query": filter_query,
            "status_filter": status_filter,
            "source_filter": source_filter,
            "month_filter": month_filter,
            "status_choices": Booking.STATUS_CHOICES,
            "source_choices": Booking.BOOKING_SOURCE_CHOICES,
            "active_filters": active_filters,
            "filtered_count": filtered_count,
            "filtered_revenue": filtered_revenue,
            "status_counts": status_counts,
        },
    )


@login_required
def booking_add(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    initial = {}
    locked_room = None
    active_prop, property_rooms = _property_rooms(request)
    guest_id = request.GET.get("guest")
    if guest_id:
        initial["guest"] = guest_id
    room_id = request.GET.get("room")
    check_in = _parse_date(request.GET.get("check_in", ""))
    check_out = _parse_date(request.GET.get("check_out", ""))
    if room_id:
        initial["room"] = room_id
        room = property_rooms.filter(pk=room_id).first()
        if room:
            locked_room = room
            initial["rate_per_night"] = room.price_per_night
        else:
            initial.pop("room", None)
    if check_in:
        initial["check_in_date"] = check_in
    if check_out:
        initial["check_out_date"] = check_out
    source = request.GET.get("source")
    status = request.GET.get("status")
    if source in dict(Booking.BOOKING_SOURCE_CHOICES):
        initial["booking_source"] = source
    if status in dict(Booking.STATUS_CHOICES):
        initial["status"] = status
    walk_in_mode = (
        initial.get("booking_source") == "Walk-in"
        and initial.get("status") == "Confirmed"
        and initial.get("check_in_date") == timezone.localdate()
    )

    if request.method == "POST":
        form = BookingForm(request.POST)
        form.fields["room"].queryset = property_rooms
        if form.is_valid():
            candidate = form.save(commit=False)
            needs_approval = candidate.check_in_date < timezone.localdate() or candidate.discount > SENSITIVE_DISCOUNT_AMOUNT
            approver = None
            if needs_approval:
                approver = _manager_approval(request, "Backdated booking or large discount")
                if not approver:
                    messages.error(request, "Owner approval is required for backdated bookings or large discounts.")
                    return redirect("core:booking_add")
            try:
                with transaction.atomic():
                    Room.objects.select_for_update().get(pk=candidate.room_id)
                    candidate.validate_room_available()
                    candidate.validate_room_conflict()
                    booking = form.save()
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                _audit(
                    request,
                    "create",
                    booking,
                    after=_model_snapshot(booking, ["check_in_date", "check_out_date", "booking_duration_type", "rate_per_night", "discount", "total_amount", "status"]),
                    approved_by=approver,
                )
                _sync_room_status(booking.room)
                settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
                notify_booking_created(booking, settings_obj)
                messages.success(request, f"Booking {booking.booking_reference} created.")
                return redirect("core:booking_detail", pk=booking.pk)
    else:
        form = BookingForm(initial=initial)
        form.fields["room"].queryset = property_rooms

    return render(
        request,
        "core/booking_form.html",
        {
            "form": form,
            "title": "Add Booking",
            "room_prices_json": _room_prices_json(active_prop),
            "walk_in_mode": walk_in_mode,
            "locked_room": locked_room,
        },
    )


@login_required
def booking_detail(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    booking = get_object_or_404(Booking.objects.select_related("guest", "room"), pk=pk, room__prop=active_prop)
    _expire_elapsed_bookings(room=booking.room)
    booking.refresh_from_db()
    payments = list(booking.payments.order_by("payment_date", "recorded_at"))
    refunds = list(booking.refunds.select_related("approved_by", "recorded_by").order_by("refund_date", "recorded_at"))
    running_balance = booking.total_amount
    for payment in payments:
        running_balance -= payment.amount
        payment.balance_after = running_balance
    for refund in refunds:
        running_balance += refund.amount
        refund.balance_after = running_balance
    latest_payment = payments[-1] if payments else None
    payment_totals = booking.payment_totals()
    subtotal = booking.rate_per_night if booking.is_hourly else booking.rate_per_night * Decimal(max(booking.num_nights, 0))
    can_mark_no_show = booking.status == "Confirmed" and booking.check_in_date < timezone.localdate()
    change_due = max(payment_totals["net_paid"] - booking.total_amount, Decimal("0.00"))
    return render(
        request,
        "core/booking_detail.html",
        {
            "booking": booking,
            "payments": payments,
            "refunds": refunds,
            "latest_payment": latest_payment,
            "payment_totals": payment_totals,
            "subtotal": subtotal,
            "can_mark_no_show": can_mark_no_show,
            "change_due": change_due,
        },
    )


@login_required
def booking_refund(request, booking_id):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    booking = get_object_or_404(Booking.objects.select_related("guest", "room"), pk=booking_id, room__prop=active_prop)
    payment_totals = booking.payment_totals()
    refundable_amount = max(payment_totals["net_paid"], Decimal("0.00"))
    if refundable_amount <= 0:
        messages.error(request, "There is no paid amount available to refund.")
        return redirect("core:booking_detail", pk=booking.pk)

    if request.method == "POST":
        form = BookingRefundForm(request.POST, max_refund=refundable_amount)
        if form.is_valid():
            if _is_date_locked(form.cleaned_data["refund_date"]):
                return _locked_day_response(request, form.cleaned_data["refund_date"], "core:booking_detail", pk=booking.pk)
            approver = _manager_approval(request, "Booking refund")
            if not approver:
                messages.error(request, "Owner approval is required to refund a booking payment.")
                return redirect("core:booking_refund", booking_id=booking.pk)
            refund = form.save(commit=False)
            refund.booking = booking
            refund.approved_by = approver
            refund.recorded_by = request.user
            refund.save()
            _audit(
                request,
                "refund",
                refund,
                after=_model_snapshot(refund, ["amount", "refund_method", "reference", "refund_date"]),
                reason=refund.reason,
                approved_by=approver,
            )
            messages.success(request, f"Refund recorded: R {refund.amount}.")
            return redirect("core:booking_detail", pk=booking.pk)
    else:
        form = BookingRefundForm(
            max_refund=refundable_amount,
            initial={"refund_date": timezone.localdate(), "refund_method": "Cash", "amount": refundable_amount},
        )

    return render(
        request,
        "core/booking_refund.html",
        {
            "booking": booking,
            "form": form,
            "refundable_amount": refundable_amount,
            "payment_totals": payment_totals,
        },
    )


@login_required
def payment_add(request, booking_id):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    booking = get_object_or_404(Booking.objects.select_related("guest", "room"), pk=booking_id, room__prop=active_prop)

    if request.method == "POST":
        form = PaymentForm(request.POST)
        if form.is_valid():
            payment = form.save(commit=False)
            if _is_date_locked(payment.payment_date):
                return _locked_day_response(request, payment.payment_date, "core:booking_detail", pk=booking.pk)
            if payment.payment_method == "Cash" and not _cash_shift_required(request):
                return redirect("core:payment_add", booking_id=booking.pk)
            payment.booking = booking
            totals = booking.payment_totals()
            balance_due = totals["balance_due"]
            total_paid_so_far = totals["total_paid"]
            tendered = payment.amount
            settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)

            if tendered <= 0:
                form.add_error("amount", "Payment amount must be greater than zero.")
            elif balance_due <= 0:
                form.add_error("amount", "This booking has no outstanding balance.")
            else:
                recorded_amount = tendered
                change = Decimal("0.00")
                if tendered > balance_due:
                    recorded_amount = balance_due
                    change = tendered - balance_due

                payment.amount = recorded_amount
                if total_paid_so_far == Decimal("0.00") and booking.deposit_required > 0 and recorded_amount <= booking.deposit_required:
                    payment.payment_type = "Deposit"
                payment.save()
                _audit(request, "create", payment, after=_model_snapshot(payment, ["amount", "payment_method", "payment_type", "reference"]))
                booking.recalculate_balance()
                notify_payment_received(booking, payment, settings_obj)

                if change > 0:
                    messages.success(request, f"Payment of R {recorded_amount:.2f} recorded. Change to give: R {change:.2f}.")
                elif recorded_amount < balance_due:
                    messages.success(request, f"Partial payment of R {recorded_amount:.2f} recorded. Remaining balance: R {balance_due - recorded_amount:.2f}.")
                else:
                    messages.success(request, "Payment recorded successfully.")
                return redirect("core:booking_detail", pk=booking.pk)
    else:
        form = PaymentForm(
            initial={
                "payment_date": timezone.localdate(),
                "payment_method": "Cash",
            }
        )

    return render(
        request,
        "core/payment_add.html",
        {
            "booking": booking,
            "form": form,
            "payment_totals": booking.payment_totals(),
            "open_shift": POSShift.objects.filter(prop=active_prop, closed_at__isnull=True).first(),
        },
    )


@login_required
def payment_list(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    today = timezone.localdate()
    selected_month = request.GET.get("month", today.strftime("%Y-%m"))
    selected_method = request.GET.get("method", "")

    try:
        month_start = datetime.date.fromisoformat(f"{selected_month}-01")
    except ValueError:
        selected_month = today.strftime("%Y-%m")
        month_start = today.replace(day=1)
    month_end = month_start.replace(day=calendar.monthrange(month_start.year, month_start.month)[1])

    active_prop, payments_qs = _property_payments(request)
    payments = payments_qs.select_related("booking", "booking__guest", "booking__room").filter(
        payment_date__gte=month_start,
        payment_date__lte=month_end,
    )
    if selected_method:
        payments = payments.filter(payment_method=selected_method)

    payments = list(payments.order_by("-payment_date", "-recorded_at"))
    total_received = sum((payment.amount for payment in payments), Decimal("0.00"))

    # Pre-compute cumulative paid per booking up to each payment's recorded_at to avoid N+1.
    from collections import defaultdict
    booking_ids = {p.booking_id for p in payments}
    all_booking_payments = (
        Payment.objects.filter(booking_id__in=booking_ids)
        .order_by("booking_id", "payment_date", "recorded_at", "pk")
        .values("pk", "booking_id", "amount", "payment_date", "recorded_at")
    )
    cumulative: dict = defaultdict(Decimal)
    payment_running: dict = {}
    for row in all_booking_payments:
        cumulative[row["booking_id"]] += row["amount"]
        payment_running[row["pk"]] = cumulative[row["booking_id"]]

    method_counts: dict = {}
    for payment in payments:
        method_counts[payment.payment_method] = method_counts.get(payment.payment_method, 0) + 1
        paid_cumulative = payment_running.get(payment.pk, payment.amount)
        payment.balance_after = payment.booking.total_amount - paid_cumulative
    most_used_method = max(method_counts, key=method_counts.get) if method_counts else "None"

    return render(
        request,
        "core/payment_list.html",
        {
            "payments": payments,
            "selected_month": selected_month,
            "selected_method": selected_method,
            "payment_method_choices": Payment.PAYMENT_METHOD_CHOICES,
            "total_received": total_received,
            "payment_count": len(payments),
            "most_used_method": most_used_method,
        },
    )


@login_required
def exception_report(request):
    if not is_owner(request.user):
        return redirect("core:home")
    today = timezone.localdate()
    selected_date = request.GET.get("date", today.isoformat())
    try:
        report_date = datetime.date.fromisoformat(selected_date)
    except ValueError:
        report_date = today
    active_prop = get_active_property(request)

    audits = AuditLog.objects.select_related("actor", "approved_by").filter(created_at__date=report_date)
    sensitive_audits = audits.filter(
        Q(action__in=["delete", "refund", "login_override", "exception"])
        | Q(reason__icontains="discount")
        | Q(reason__icontains="backdated")
        | Q(reason__icontains="cancel")
    )[:100]
    discounted_bookings = Booking.objects.select_related("guest", "room").filter(
        room__prop=active_prop,
        created_at__date=report_date,
        discount__gt=0,
    )
    refunded_sales = POSSale.objects.select_related("served_by").filter(
        prop=active_prop,
        created_at__date=report_date,
        status="refunded",
    )
    shift_variances = [
        shift
        for shift in POSShift.objects.select_related("opened_by", "closed_by").filter(prop=active_prop, closed_at__date=report_date)
        if shift.cash_variance and shift.cash_variance != 0
    ]

    return render(
        request,
        "core/exception_report.html",
        {
            "selected_date": report_date,
            "sensitive_audits": sensitive_audits,
            "discounted_bookings": discounted_bookings,
            "refunded_sales": refunded_sales,
            "shift_variances": shift_variances,
        },
    )


@login_required
def audit_timeline(request):
    if not is_owner(request.user):
        return redirect("core:home")
    today = timezone.localdate()
    selected_date = request.GET.get("date", "")
    action = request.GET.get("action", "").strip()
    query = request.GET.get("q", "").strip()

    logs = AuditLog.objects.select_related("actor", "approved_by").all()
    if selected_date:
        try:
            logs = logs.filter(created_at__date=datetime.date.fromisoformat(selected_date))
        except ValueError:
            selected_date = ""
    if action:
        logs = logs.filter(action=action)
    if query:
        logs = logs.filter(
            Q(object_type__icontains=query)
            | Q(object_repr__icontains=query)
            | Q(reason__icontains=query)
            | Q(actor__username__icontains=query)
            | Q(approved_by__username__icontains=query)
        )

    recent_sensitive = AuditLog.objects.filter(
        Q(action__in=["delete", "refund", "login_override", "exception", "void"])
        | Q(reason__icontains="discount")
        | Q(reason__icontains="backdated")
        | Q(reason__icontains="cancel")
    ).count()
    paginator = Paginator(logs, 30)
    page_obj = paginator.get_page(request.GET.get("page"))
    return render(
        request,
        "core/audit_timeline.html",
        {
            "page_obj": page_obj,
            "selected_date": selected_date,
            "today": today,
            "action": action,
            "query": query,
            "action_choices": AuditLog.ACTION_CHOICES,
            "total_logs": logs.count(),
            "recent_sensitive": recent_sensitive,
        },
    )


@login_required
def daily_close(request):
    if not is_owner(request.user):
        return redirect("core:home")
    today = timezone.localdate()
    selected = request.GET.get("date", today.isoformat())
    try:
        close_date = datetime.date.fromisoformat(selected)
    except ValueError:
        close_date = today
    close_lock = DailyCloseLock.objects.select_related("locked_by").filter(close_date=close_date).first()
    active_prop = get_active_property(request)

    payments = Payment.objects.filter(booking__room__prop=active_prop, payment_date=close_date)
    expenses = Expense.objects.filter(prop=active_prop, date=close_date)
    pos_sales = POSSale.objects.filter(prop=active_prop, created_at__date=close_date)
    shifts = POSShift.objects.select_related("opened_by", "closed_by").filter(prop=active_prop).filter(Q(opened_at__date=close_date) | Q(closed_at__date=close_date))
    bookings = Booking.objects.select_related("guest", "room").filter(room__prop=active_prop, check_in_date__lte=close_date, check_out_date__gte=close_date)
    audit_logs = AuditLog.objects.select_related("actor", "approved_by").filter(created_at__date=close_date)

    cash_payments = payments.filter(payment_method="Cash").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    card_payments = payments.filter(payment_method="Card").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    eft_payments = payments.filter(payment_method="EFT").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    pos_cash = pos_sales.filter(status="completed", payment_method__in=["cash", "split"]).aggregate(total=Sum("cash_amount"))["total"] or Decimal("0.00")
    pos_card = pos_sales.filter(status="completed", payment_method__in=["card", "split"]).aggregate(total=Sum("card_amount"))["total"] or Decimal("0.00")
    pos_eft = pos_sales.filter(status="completed", payment_method="eft").aggregate(total=Sum("total"))["total"] or Decimal("0.00")
    pos_wallet = pos_sales.filter(status="completed", payment_method__in=["snapscan", "zapper"]).aggregate(total=Sum("total"))["total"] or Decimal("0.00")
    pos_room_charge = pos_sales.filter(status="completed", payment_method="room_charge").aggregate(total=Sum("total"))["total"] or Decimal("0.00")
    pos_refunds = abs(pos_sales.filter(status="refunded").aggregate(total=Sum("total"))["total"] or Decimal("0.00"))
    booking_refunds_qs = BookingRefund.objects.filter(booking__room__prop=active_prop, refund_date=close_date)
    booking_refunds = booking_refunds_qs.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    refunds = pos_refunds + booking_refunds
    cash_expenses = expenses.filter(payment_method="Cash").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    cash_booking_refunds = booking_refunds_qs.filter(refund_method="Cash").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    pos_cash_refunds = abs(pos_sales.filter(status="refunded", payment_method__in=["cash", "split"]).aggregate(total=Sum("cash_amount"))["total"] or Decimal("0.00"))
    discounts = (pos_sales.filter(status="completed").aggregate(total=Sum("discount_amount"))["total"] or Decimal("0.00")) + (
        bookings.aggregate(total=Sum("discount"))["total"] or Decimal("0.00")
    )
    opening_float_total = sum((shift.opening_float for shift in shifts), Decimal("0.00"))
    counted_cash_total = sum((shift.closing_cash or Decimal("0.00") for shift in shifts if shift.closing_cash is not None), Decimal("0.00"))
    expected_cash_total = opening_float_total + cash_payments + pos_cash - cash_expenses - cash_booking_refunds - pos_cash_refunds
    cash_variance_total = counted_cash_total - expected_cash_total if counted_cash_total else None
    open_shifts = shifts.filter(closed_at__isnull=True).count()

    shift_rows = []
    for shift in shifts:
        shift_rows.append(
            {
                "shift": shift,
                "expected_cash": shift.expected_cash,
                "closing_cash": shift.closing_cash,
                "variance": shift.cash_variance,
            }
        )

    return render(
        request,
        "core/daily_close.html",
        {
            "close_date": close_date,
            "close_lock": close_lock,
            "payments_total": payments.aggregate(total=Sum("amount"))["total"] or Decimal("0.00"),
            "expenses_total": expenses.aggregate(total=Sum("amount"))["total"] or Decimal("0.00"),
            "cash_payments": cash_payments,
            "card_payments": card_payments + pos_card,
            "eft_payments": eft_payments + pos_eft,
            "pos_wallet": pos_wallet,
            "pos_room_charge": pos_room_charge,
            "pos_cash": pos_cash,
            "cash_expenses": cash_expenses,
            "cash_booking_refunds": cash_booking_refunds,
            "pos_cash_refunds": pos_cash_refunds,
            "opening_float_total": opening_float_total,
            "counted_cash_total": counted_cash_total,
            "expected_cash_total": expected_cash_total,
            "cash_variance_total": cash_variance_total,
            "open_shifts": open_shifts,
            "refunds": refunds,
            "discounts": discounts,
            "checkins": Booking.objects.filter(room__prop=active_prop, check_in_date=close_date).exclude(status__in=Booking.INACTIVE_STATUSES).count(),
            "checkouts": Booking.objects.filter(room__prop=active_prop, check_out_date=close_date).exclude(status__in=Booking.INACTIVE_STATUSES).count(),
            "shift_rows": shift_rows,
            "sensitive_actions": audit_logs.filter(action__in=["delete", "refund", "login_override", "exception"])[:20],
        },
    )


@login_required
def daily_close_lock(request):
    if not is_owner(request.user):
        return redirect("core:home")
    if request.method == "POST":
        close_date = _parse_date(request.POST.get("date", ""))
        if not close_date:
            messages.error(request, "Choose a valid date to lock.")
            return redirect("core:daily_close")
        lock, created = DailyCloseLock.objects.get_or_create(
            close_date=close_date,
            defaults={"locked_by": request.user, "notes": request.POST.get("notes", "")},
        )
        if created:
            _audit(request, "approve", lock, after={"close_date": close_date.isoformat()}, reason="Daily close locked")
            messages.success(request, f"Daily close locked for {close_date:%d %b %Y}.")
        else:
            messages.info(request, f"{close_date:%d %b %Y} is already locked.")
        return redirect(f"{reverse('core:daily_close')}?date={close_date.isoformat()}")
    return redirect("core:daily_close")


@login_required
def daily_close_unlock(request, pk):
    if not is_owner(request.user):
        return redirect("core:home")
    lock = get_object_or_404(DailyCloseLock, pk=pk)
    close_date = lock.close_date
    if request.method == "POST":
        approver = _manager_approval(request, "Unlock daily close")
        if not approver:
            messages.error(request, "Owner approval is required to unlock a closed day.")
            return redirect(f"{reverse('core:daily_close')}?date={close_date.isoformat()}")
        before = _model_snapshot(lock, ["close_date", "notes"])
        lock.delete()
        _audit(request, "delete", lock, before=before, reason="Daily close unlocked", approved_by=approver)
        messages.success(request, f"Daily close reopened for {close_date:%d %b %Y}.")
    return redirect(f"{reverse('core:daily_close')}?date={close_date.isoformat()}")


@login_required
def room_calendar(request):
    today = timezone.localdate()
    start = _parse_date(request.GET.get("start", "")) or today
    days = min(max(int(request.GET.get("days", "14") or 14), 7), 31)
    date_range = [start + datetime.timedelta(days=offset) for offset in range(days)]
    end = date_range[-1] + datetime.timedelta(days=1)
    active_prop, rooms_qs = _property_rooms(request)
    rooms = list(rooms_qs)
    bookings = Booking.objects.select_related("guest", "room").filter(
        room__prop=active_prop,
        check_in_date__lt=end,
        check_out_date__gt=start,
    ).exclude(status__in=Booking.INACTIVE_STATUSES)
    by_room_date = {}
    for booking in bookings:
        current = max(booking.check_in_date, start)
        last = min(booking.check_out_date, end)
        while current < last:
            by_room_date[(booking.room_id, current)] = booking
            current += datetime.timedelta(days=1)
    for room in rooms:
        room.calendar_cells = [{"date": day, "booking": by_room_date.get((room.pk, day))} for day in date_range]
    return render(request, "core/room_calendar.html", {"rooms": rooms, "date_range": date_range, "start": start, "days": days})


@login_required
def communications_center(request):
    templates = [
        {"name": "Confirmation", "url_name": "confirmation", "body": "Booking confirmed with room, dates, total, and deposit."},
        {"name": "Balance Reminder", "url_name": "balance_reminder", "body": "Outstanding balance reminder for guests before arrival/check-in."},
        {"name": "Welcome", "url_name": "welcome", "body": "Short welcome message after check-in."},
        {"name": "Thank You", "url_name": "thank_you", "body": "Thank-you message after checkout."},
    ]
    active_prop = get_active_property(request)
    recent_bookings = Booking.objects.select_related("guest", "room").filter(room__prop=active_prop).order_by("-created_at")[:20]
    logs = CommunicationLog.objects.select_related("booking", "guest", "created_by").filter(
        Q(booking__room__prop=active_prop) | Q(booking__isnull=True)
    )[:50]
    return render(request, "core/communications.html", {"templates": templates, "recent_bookings": recent_bookings, "logs": logs})


@login_required
def housekeeping_mobile(request):
    _expire_elapsed_bookings()
    active_prop = get_active_property(request)
    rooms = Room.objects.filter(prop=active_prop, cleaning_status__in=["Needs Cleaning", "In Progress"]).order_by("cleaning_status", "name")
    return render(request, "core/housekeeping_mobile.html", {"rooms": rooms})


def _csv_response(filename, headers, rows):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = csv.writer(response)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    return response


@login_required
def export_data(request, dataset):
    if not is_owner(request.user):
        return redirect("core:home")
    active_prop = get_active_property(request)
    if dataset == "bookings":
        rows = Booking.objects.select_related("guest", "room").filter(room__prop=active_prop).order_by("-created_at")
        return _csv_response(
            "bookings.csv",
            ["Reference", "Guest", "Room", "Check In", "Check Out", "Status", "Total", "Balance"],
            [[b.booking_reference, b.guest.full_name, b.room.name, b.check_in_date, b.check_out_date, b.status, b.total_amount, b.balance_due] for b in rows],
        )
    if dataset == "payments":
        rows = Payment.objects.select_related("booking", "booking__guest").filter(booking__room__prop=active_prop).order_by("-recorded_at")
        return _csv_response(
            "payments.csv",
            ["Date", "Reference", "Booking", "Guest", "Method", "Amount"],
            [[p.payment_date, p.reference, p.booking.booking_reference, p.booking.guest.full_name, p.payment_method, p.amount] for p in rows],
        )
    if dataset == "refunds":
        rows = BookingRefund.objects.select_related("booking", "booking__guest", "approved_by", "recorded_by").filter(booking__room__prop=active_prop).order_by("-refund_date", "-recorded_at")
        return _csv_response(
            "refunds.csv",
            ["Date", "Reference", "Booking", "Guest", "Method", "Amount", "Reason", "Approved By", "Recorded By"],
            [
                [
                    r.refund_date,
                    r.reference,
                    r.booking.booking_reference,
                    r.booking.guest.full_name,
                    r.refund_method,
                    r.amount,
                    r.reason,
                    r.approved_by.username if r.approved_by else "",
                    r.recorded_by.username if r.recorded_by else "",
                ]
                for r in rows
            ],
        )
    if dataset == "expenses":
        rows = Expense.objects.filter(prop=active_prop).order_by("-date", "-created_at")
        return _csv_response(
            "expenses.csv",
            ["Date", "Category", "Description", "Paid To", "Method", "Amount", "Reference"],
            [[e.date, e.category, e.description, e.paid_to, e.payment_method, e.amount, e.reference] for e in rows],
        )
    if dataset == "inventory":
        rows = InventoryItem.objects.select_related("category").filter(prop=active_prop).order_by("name")
        return _csv_response(
            "inventory.csv",
            ["Item", "Category", "Stock", "Unit", "Minimum", "Location", "Value"],
            [[i.name, i.category.name if i.category else "", i.current_stock, i.unit, i.minimum_stock, i.location, i.stock_value] for i in rows],
        )
    messages.error(request, "Unknown export.")
    return redirect("core:reports")


@login_required
def payment_delete(request, booking_id, payment_id):
    active_prop = get_active_property(request)
    booking = get_object_or_404(Booking, pk=booking_id, room__prop=active_prop)
    payment = get_object_or_404(Payment, pk=payment_id, booking=booking)
    if _is_date_locked(payment.payment_date):
        return _locked_day_response(request, payment.payment_date, "core:booking_detail", pk=booking.pk)
    if request.method == "POST":
        approver = _manager_approval(request, "Delete recorded payment")
        if not approver:
            messages.error(request, "Owner approval is required to delete a payment.")
            return redirect("core:booking_detail", pk=booking.pk)
        before = _model_snapshot(payment, ["amount", "payment_method", "reference", "payment_date"])
        payment.delete()
        _audit(request, "delete", payment, before=before, reason="Payment deleted", approved_by=approver)
        booking.recalculate_balance()
        messages.success(request, "Payment deleted successfully.")
    return redirect("core:booking_detail", pk=booking.pk)


def _pdf_settings_context():
    settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
    logo_url = ""
    if settings_obj.logo:
        try:
            logo_path = Path(settings_obj.logo.path)
            content_type = mimetypes.guess_type(str(logo_path))[0] or "image/png"
            logo_data = base64.b64encode(logo_path.read_bytes()).decode("ascii")
            logo_url = f"data:{content_type};base64,{logo_data}"
        except (OSError, ValueError, NotImplementedError):
            try:
                logo_url = settings_obj.logo.url
            except ValueError:
                logo_url = ""
    return {
        "settings": settings_obj,
        "logo_url": logo_url,
        "generated_at": timezone.now(),
    }


@login_required
def booking_confirmation_pdf(request, pk):
    active_prop = get_active_property(request)
    booking = get_object_or_404(Booking.objects.select_related("guest", "room"), pk=pk, room__prop=active_prop)
    context = _pdf_settings_context()
    context.update(
        {
            "booking": booking,
            "payment_totals": booking.payment_totals(),
        }
    )
    response = generate_pdf("pdfs/booking_confirmation.html", context)
    response["Content-Disposition"] = f'inline; filename="{booking.booking_reference}-confirmation.pdf"'
    return response


@login_required
def booking_invoice_pdf(request, pk):
    active_prop = get_active_property(request)
    booking = get_object_or_404(Booking.objects.select_related("guest", "room"), pk=pk, room__prop=active_prop)
    context = _pdf_settings_context()
    settings_obj = context["settings"]
    payment_totals = booking.payment_totals()
    vat_amount = Decimal("0.00")
    if settings_obj.vat_registered and booking.total_amount:
        divisor = Decimal("100.00") + settings_obj.vat_rate
        vat_amount = (booking.total_amount * settings_obj.vat_rate / divisor).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    return render_booking_invoice_pdf(booking, settings_obj, payment_totals, vat_amount)


@login_required
def payment_receipt_pdf(request, booking_id, payment_id):
    active_prop = get_active_property(request)
    booking = get_object_or_404(Booking.objects.select_related("guest", "room"), pk=booking_id, room__prop=active_prop)
    payment = get_object_or_404(Payment, pk=payment_id, booking=booking)
    payments = list(booking.payments.order_by("payment_date", "recorded_at", "pk"))
    running_balance = booking.total_amount
    balance_after_payment = booking.balance_due
    for row in payments:
        running_balance -= row.amount
        if row.pk == payment.pk:
            balance_after_payment = running_balance
            break
    settings_obj = _pdf_settings_context()["settings"]
    return render_payment_receipt_pdf(
        booking,
        payment,
        settings_obj,
        receipt_number=f"RCP-{payment.pk}",
        balance_after_payment=balance_after_payment,
    )


def _whatsapp_phone(phone):
    return "".join(character for character in phone if character.isdigit())


@login_required
def booking_whatsapp(request, pk, template_name):
    active_prop = get_active_property(request)
    booking = get_object_or_404(Booking.objects.select_related("guest", "room"), pk=pk, room__prop=active_prop)
    settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
    phone = _whatsapp_phone(booking.guest.phone)
    if not phone:
        messages.error(request, "No phone number on file for this guest.")
        return redirect("core:booking_detail", pk=booking.pk)

    guest_name = booking.guest.full_name
    guest_house_name = settings_obj.guest_house_name
    templates = {
        "confirmation": (
            f"Hi {guest_name}, your booking at {guest_house_name} is confirmed. "
            f"Room: {booking.room.name}. "
            f"Check-in: {booking.check_in_date.strftime('%d %b %Y')} at {settings_obj.check_in_time.strftime('%H:%M')}. "
            f"Check-out: {booking.check_out_date.strftime('%d %b %Y')}. "
            f"Total: R{booking.total_amount}. "
            f"Deposit required: R{booking.deposit_required}. "
            "Please reply if you have any questions."
        ),
        "balance_reminder": (
            f"Hi {guest_name}, just a reminder that your outstanding balance for your stay at "
            f"{guest_house_name} is R{booking.balance_due}. "
            "Please arrange payment at check-in. Thank you."
        ),
        "welcome": (
            f"Welcome to {guest_house_name}, {guest_name}! We hope you enjoy your stay. "
            "Please let us know if you need anything."
        ),
        "thank_you": (
            f"Thank you for staying with us at {guest_house_name}, {guest_name}! "
            "We hope to see you again soon."
        ),
    }
    message = templates.get(template_name)
    if not message:
        messages.error(request, "Unknown WhatsApp message template.")
        return redirect("core:booking_detail", pk=booking.pk)
    CommunicationLog.objects.create(
        booking=booking,
        guest=booking.guest,
        channel="WhatsApp",
        template_name=template_name,
        recipient=phone,
        message=message,
        status="opened",
        created_by=request.user,
    )
    return redirect(f"https://wa.me/{phone}?text={quote(message)}")


@login_required
def booking_checkin(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    booking = get_object_or_404(Booking, pk=pk, room__prop=active_prop)
    if _is_date_locked(booking.check_in_date):
        return _locked_day_response(request, booking.check_in_date, "core:booking_detail", pk=pk)
    if request.method == "POST":
        if booking.status not in ("Confirmed", "Pending"):
            messages.error(request, "Only pending or confirmed bookings can be checked in.")
            return redirect("core:booking_detail", pk=pk)
        from django.db import transaction as db_transaction
        from django.core.exceptions import ValidationError as DjangoValidationError
        before = _model_snapshot(booking, ["status", "check_in_time"])
        try:
            with db_transaction.atomic():
                booking.status = "Checked In"
                booking.check_in_time = timezone.now()
                booking.save()
                booking.room.status = "Occupied"
                booking.room.save()
        except DjangoValidationError as exc:
            messages.error(request, f"Check-in failed: {'; '.join(exc.messages)}")
            return redirect("core:booking_detail", pk=pk)
        _audit(request, "update", booking, before=before, after=_model_snapshot(booking, ["status", "check_in_time"]))
        settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
        notify_booking_checkin(booking, settings_obj)
        messages.success(request, f"{booking.booking_reference}: Guest checked in successfully.")
    return redirect("core:booking_detail", pk=pk)


@login_required
def booking_checkout(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    booking = get_object_or_404(Booking, pk=pk, room__prop=active_prop)
    if _is_date_locked(timezone.localdate()):
        return _locked_day_response(request, timezone.localdate(), "core:booking_detail", pk=pk)
    if request.method == "POST":
        if booking.status != "Checked In":
            messages.error(request, "Only checked-in bookings can be checked out.")
            return redirect("core:booking_detail", pk=pk)
        from django.db import transaction as db_transaction
        from django.core.exceptions import ValidationError as DjangoValidationError
        before = _model_snapshot(booking, ["status", "check_out_time", "balance_due"])
        try:
            with db_transaction.atomic():
                booking.status = "Checked Out"
                booking.check_out_time = timezone.now()
                booking.save()
                room = booking.room
                room.status = "Cleaning"
                room.cleaning_status = "Needs Cleaning"
                room.save()
        except DjangoValidationError as exc:
            messages.error(request, f"Check-out failed: {'; '.join(exc.messages)}")
            return redirect("core:booking_detail", pk=pk)
        _audit(request, "update", booking, before=before, after=_model_snapshot(booking, ["status", "check_out_time", "balance_due"]), reason="Checkout forced room to cleaning workflow")
        settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
        notify_booking_checkout(booking, settings_obj)
        if booking.balance_due > 0:
            messages.warning(
                request,
                f"{booking.booking_reference}: Checked out. Outstanding balance of "
                f"{booking.balance_due} remains — please collect payment.",
            )
        else:
            messages.success(request, f"{booking.booking_reference}: Checked out successfully.")
    return redirect("core:booking_detail", pk=pk)


@login_required
def booking_no_show(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    booking = get_object_or_404(Booking.objects.select_related("room"), pk=pk, room__prop=active_prop)
    if _is_date_locked(booking.check_in_date):
        return _locked_day_response(request, booking.check_in_date, "core:booking_detail", pk=pk)
    if request.method == "POST":
        if booking.status == "Confirmed" and booking.check_in_date < timezone.localdate():
            before = _model_snapshot(booking, ["status"])
            booking.status = "No Show"
            booking.save(update_fields=["status"])
            _sync_room_status(booking.room)
            _audit(request, "update", booking, before=before, after=_model_snapshot(booking, ["status"]), reason="Marked no-show")
            messages.success(request, "Booking marked as No Show. Room has been released.")
        else:
            messages.error(request, "Only overdue confirmed bookings can be marked as No Show.")
    return redirect("core:booking_detail", pk=pk)


@login_required
def booking_cancel(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    booking = get_object_or_404(Booking, pk=pk, room__prop=active_prop)
    if _is_date_locked(booking.check_in_date):
        return _locked_day_response(request, booking.check_in_date, "core:booking_detail", pk=pk)
    if booking.status in ("Cancelled", "Checked Out"):
        return redirect("core:booking_detail", pk=pk)
    if request.method == "POST":
        approver = _manager_approval(request, "Cancel booking")
        if not approver:
            messages.error(request, "Owner approval is required to cancel bookings.")
            return redirect("core:booking_cancel", pk=pk)
        from django.core.exceptions import ValidationError as DjangoValidationError
        room = booking.room
        before = _model_snapshot(booking, ["status", "balance_due", "deposit_paid"])
        try:
            booking.status = "Cancelled"
            booking.save()
        except DjangoValidationError as exc:
            messages.error(request, f"Cancellation failed: {'; '.join(exc.messages)}")
            return redirect("core:booking_detail", pk=pk)
        _sync_room_status(room)
        _audit(request, "update", booking, before=before, after=_model_snapshot(booking, ["status", "balance_due", "deposit_paid"]), reason="Booking cancelled", approved_by=approver)
        settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
        notify_booking_cancelled(booking, settings_obj)
        deposit_paid = booking.payment_totals()["net_paid"]
        if deposit_paid > 0:
            messages.warning(
                request,
                f"Booking {booking.booking_reference} cancelled. "
                f"Guest has paid R{deposit_paid:.2f} — process a refund if required.",
            )
        else:
            messages.success(request, f"Booking {booking.booking_reference} has been cancelled.")
        return redirect("core:booking_detail", pk=pk)
    return render(request, "bookings/booking_cancel.html", {"booking": booking})


@login_required
def booking_edit(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop, property_rooms = _property_rooms(request)
    booking = get_object_or_404(Booking, pk=pk, room__prop=active_prop)
    if _is_date_locked(booking.check_in_date) or _is_date_locked(booking.check_out_date):
        messages.error(request, "This booking touches a locked daily close date. Reopen the day before editing.")
        return redirect("core:booking_detail", pk=booking.pk)
    old_room = booking.room
    before = _model_snapshot(
        booking,
        ["room_id", "check_in_date", "check_out_date", "booking_duration_type", "rate_per_night", "discount", "total_amount", "status"],
    )
    if request.method == "POST":
        form = BookingForm(request.POST, instance=booking)
        form.fields["room"].queryset = property_rooms
        if form.is_valid():
            candidate = form.save(commit=False)
            if _is_date_locked(candidate.check_in_date) or _is_date_locked(candidate.check_out_date):
                messages.error(request, "The selected dates include a locked daily close date.")
                return redirect("core:booking_edit", pk=pk)
            needs_approval = (
                booking.status in ("Checked In", "Checked Out", "Cancelled")
                or candidate.check_in_date < timezone.localdate()
                or candidate.discount > SENSITIVE_DISCOUNT_AMOUNT
                or (candidate.total_amount and candidate.discount / candidate.total_amount * Decimal("100") > SENSITIVE_DISCOUNT_PERCENT)
            )
            approver = None
            if needs_approval:
                approver = _manager_approval(request, "Sensitive booking edit")
                if not approver:
                    messages.error(request, "Owner approval is required for completed/backdated bookings or large discounts.")
                    return redirect("core:booking_edit", pk=pk)
            try:
                with transaction.atomic():
                    Room.objects.select_for_update().get(pk=candidate.room_id)
                    if old_room.pk != candidate.room_id:
                        Room.objects.select_for_update().get(pk=old_room.pk)
                    candidate.validate_room_available()
                    candidate.validate_room_conflict()
                    updated = form.save()
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                _sync_room_status(updated.room)
                if old_room.pk != updated.room.pk:
                    _sync_room_status(old_room)
                _audit(
                    request,
                    "update",
                    updated,
                    before=before,
                    after=_model_snapshot(updated, ["room_id", "check_in_date", "check_out_date", "booking_duration_type", "rate_per_night", "discount", "total_amount", "status"]),
                    reason="Booking edited",
                    approved_by=approver,
                )
                messages.success(request, "Booking updated successfully.")
                return redirect("core:booking_detail", pk=booking.pk)
    else:
        form = BookingForm(instance=booking)
        form.fields["room"].queryset = property_rooms

    return render(
        request,
        "core/booking_form.html",
        {
            "form": form,
            "booking": booking,
            "title": f"Edit {booking.booking_reference}",
            "room_prices_json": _room_prices_json(active_prop),
        },
    )


@csrf_exempt
def pos_payment_api(request):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    settings_obj = GuestHouseSettings.objects.filter(pk=1).first()
    if not settings_obj or not settings_obj.pos_api_key:
        return JsonResponse({"error": "POS integration not configured"}, status=503)
    if request.headers.get("X-POS-API-Key") != settings_obj.pos_api_key:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    required = ["pos_reference", "amount", "payment_method", "status"]
    missing = [field for field in required if not payload.get(field)]
    if missing:
        return JsonResponse({"error": f"Missing fields: {', '.join(missing)}"}, status=400)

    booking = None
    active_prop = _api_property_from_request(request, payload)
    booking_reference = payload.get("booking_reference", "")
    if booking_reference:
        if not active_prop:
            return JsonResponse(
                {"error": "Property is required for POS booking payments. Send X-Circle-Property-ID or property_id."},
                status=400,
            )
        booking = Booking.objects.filter(booking_reference=booking_reference, room__prop=active_prop).first()

    try:
        amount = Decimal(str(payload["amount"]))
    except Exception:
        return JsonResponse({"error": "Invalid amount"}, status=400)

    transaction, created = POSTransaction.objects.get_or_create(
        pos_reference=payload["pos_reference"],
        defaults={
            "booking": booking,
            "pos_system": payload.get("pos_system") or "Unknown POS",
            "amount": amount,
            "currency": payload.get("currency") or "R",
            "payment_method": payload.get("payment_method"),
            "status": payload.get("status"),
            "raw_payload": payload,
        },
    )
    if not created:
        return JsonResponse({"success": True, "message": "Transaction already received"}, status=200)

    message = "Transaction recorded"
    if booking and transaction.status == "completed":
        payment_method = payload.get("payment_method")
        if payment_method not in dict(Payment.PAYMENT_METHOD_CHOICES):
            payment_method = "Cash"
        Payment.objects.create(
            booking=booking,
            amount=amount,
            payment_method=payment_method,
            payment_type="Payment",
            reference=transaction.pos_reference,
            notes=f"Received via POS: {transaction.pos_system}",
        )
        booking.recalculate_balance()
        transaction.processed = True
        transaction.processing_notes = "Payment recorded against booking."
        transaction.save(update_fields=["processed", "processing_notes"])
        message = "Payment recorded"
    elif booking_reference and not booking:
        transaction.processing_notes = "Booking reference was provided but not found."
        transaction.save(update_fields=["processing_notes"])

    return JsonResponse(
        {
            "success": True,
            "message": message,
            "booking_reference": booking.booking_reference if booking else booking_reference,
            "balance_due": float(booking.balance_due) if booking else None,
        }
    )


def pos_booking_lookup_api(request, reference):
    settings_obj = GuestHouseSettings.objects.filter(pk=1).first()
    if not settings_obj or not settings_obj.pos_api_key:
        return JsonResponse({"error": "POS integration not configured"}, status=503)
    if request.headers.get("X-POS-API-Key") != settings_obj.pos_api_key:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    active_prop = _api_property_from_request(request, request.GET)
    if not active_prop:
        return JsonResponse(
            {"error": "Property is required for POS booking lookup. Send X-Circle-Property-ID or property_id."},
            status=400,
        )
    booking = get_object_or_404(
        Booking.objects.select_related("guest", "room"),
        booking_reference=reference,
        room__prop=active_prop,
    )
    totals = booking.payment_totals()
    return JsonResponse(
        {
            "booking_reference": booking.booking_reference,
            "guest_name": booking.guest.full_name,
            "room": booking.room.name,
            "check_in": booking.check_in_date.isoformat(),
            "check_out": booking.check_out_date.isoformat(),
            "total_amount": float(booking.total_amount),
            "amount_paid": float(totals["net_paid"]),
            "balance_due": float(booking.balance_due),
            "status": booking.status,
        }
    )


@login_required
def pos_transactions(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    status_filter = request.GET.get("status", "")
    date_filter = request.GET.get("date", "")
    transactions = POSTransaction.objects.select_related("booking").all()
    if status_filter:
        transactions = transactions.filter(status=status_filter)
    if date_filter:
        try:
            selected_date = datetime.date.fromisoformat(date_filter)
            transactions = transactions.filter(received_at__date=selected_date)
        except ValueError:
            pass
    return render(
        request,
        "core/pos_transactions.html",
        {
            "transactions": transactions,
            "status_filter": status_filter,
            "date_filter": date_filter,
            "status_choices": POSTransaction.STATUS_CHOICES,
        },
    )


def trial_setup(request):
    if TrialLicense.objects.filter(is_active=True).exists():
        return redirect("core:home")
    if request.method == "POST":
        guest_house_name = request.POST.get("guest_house_name", "").strip()
        owner_email = request.POST.get("owner_email", "").strip()
        if not guest_house_name or not owner_email:
            messages.error(request, "Guest house name and owner email are required.")
        else:
            license_obj = TrialLicense.objects.create(
                guest_house_name=guest_house_name,
                owner_email=owner_email,
                expires_at=timezone.now() + datetime.timedelta(days=30),
                license_key=f"CCG-TRIAL-{uuid.uuid4().hex[:8].upper()}",
            )
            settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
            settings_obj.guest_house_name = guest_house_name
            settings_obj.email = owner_email
            settings_obj.save()
            messages.success(request, f"Trial activated. License: {license_obj.license_key}.")
            return redirect("core:home")
    return render(request, "trial/setup.html")


def trial_expired(request):
    license_obj = TrialLicense.objects.filter(is_active=True).first()
    days_since_expiry = 0
    if license_obj and license_obj.is_expired:
        days_since_expiry = max(0, (timezone.now() - license_obj.expires_at).days)
    return render(
        request,
        "trial/expired.html",
        {
            "license": license_obj,
            "days_since_expiry": days_since_expiry,
        },
    )


def plans_page(request):
    plans = {plan.name: plan for plan in SubscriptionPlan.objects.order_by("monthly_price")}
    feature_sets = {
        "starter": {
            "included": [
                "Dashboard & Overview",
                "Room Management",
                "Guest Profiles",
                "Bookings & Availability",
                "Check-in / Check-out",
                "Payment Tracking",
                "Booking PDFs",
                "Cleaning Board",
            ],
            "excluded": ["Expenses Module", "Inventory", "Full Reports", "POS Integration"],
            "cta": "Start Free Trial",
            "href": "core:trial_setup",
            "highlight": False,
        },
        "professional": {
            "included": [
                "Everything in Starter",
                "Expenses Module",
                "Inventory Management",
                "Full Reports & Charts",
                "Hourly & Weekly Bookings",
                "POS Integration",
                "Maintenance Requests",
                "Custom PDF Branding",
            ],
            "excluded": ["Multi-property", "API Access"],
            "cta": "Start Free Trial",
            "href": "core:trial_setup",
            "highlight": True,
        },
        "enterprise": {
            "included": [
                "Everything in Professional",
                "Unlimited Rooms & Users",
                "CSV & Excel Data Export",
                "Multi-property Management",
                "Full API Access",
                "Priority Support",
                "Dedicated Onboarding",
            ],
            "excluded": [],
            "cta": "Contact Us",
            "href": "mailto:sales@circlecoretech.co.za?subject=Circle Core Enterprise Enquiry",
            "highlight": False,
        },
    }
    plan_cards = []
    for key in ["starter", "professional", "enterprise"]:
        plan = plans.get(key)
        if not plan:
            continue
        annual_monthly = (plan.annual_price / Decimal("12")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        annual_savings = ((plan.monthly_price * Decimal("12")) - plan.annual_price).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        if plan.max_rooms < 999:
            limit = f"Up to {plan.max_rooms} rooms · {plan.max_users} user"
            if plan.max_users != 1:
                limit += "s"
        else:
            limit = "Unlimited rooms · Unlimited users"
        plan_cards.append(
            {
                "plan": plan,
                "annual_monthly": annual_monthly,
                "annual_savings": annual_savings,
                "limit": limit,
                **feature_sets[key],
            }
        )
    return render(request, "subscription/plans.html", {"plan_cards": plan_cards})


FEATURE_DISPLAY_NAMES = {
    "rooms": "More Rooms",
    "users": "More Staff Users",
    "expenses": "Expenses & Cost Tracking",
    "inventory": "Inventory Management",
    "full_reports": "Full Reports & Charts",
    "hourly_bookings": "Hourly Room Bookings",
    "weekly_bookings": "Weekly Room Bookings",
    "pos_integration": "POS Payment Integration",
    "staff_roles": "Staff Roles & Permissions",
    "export": "CSV & Excel Data Export",
    "multi_property": "Multi-property Management",
    "api_access": "API Access",
    "maintenance_requests": "Maintenance Request Tracking",
}


class SubscriptionSetupForm(forms.Form):
    guest_house_name = forms.CharField(max_length=150)
    owner_name = forms.CharField(max_length=200)
    owner_email = forms.EmailField()
    owner_phone = forms.CharField(max_length=20, required=False)
    plan = forms.ChoiceField(choices=[])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        plans = SubscriptionPlan.objects.order_by("monthly_price")
        self.fields["plan"].choices = [(plan.pk, plan.display_name) for plan in plans]
        for name, field in self.fields.items():
            if name == "plan":
                field.widget = forms.RadioSelect(choices=field.choices)
            else:
                field.widget.attrs.update({"class": "form-input"})


def subscription_upgrade(request):
    feature = request.GET.get("feature", "").strip()
    feature_display = FEATURE_DISPLAY_NAMES.get(feature, "This feature")
    subscription = Subscription.objects.select_related("plan").first()
    plans = {plan.name: plan for plan in SubscriptionPlan.objects.all()}
    professional = plans.get("professional")
    enterprise = plans.get("enterprise")
    current_plan = subscription.plan if subscription else None
    if feature == "rooms":
        current_limit = current_plan.max_rooms if current_plan else 0
        professional_has_feature = bool(professional and (_is_unlimited(professional.max_rooms) or professional.max_rooms > current_limit))
        enterprise_has_feature = bool(enterprise and (_is_unlimited(enterprise.max_rooms) or enterprise.max_rooms > current_limit))
    elif feature == "users":
        current_limit = current_plan.max_users if current_plan else 0
        professional_has_feature = bool(professional and (_is_unlimited(professional.max_users) or professional.max_users > current_limit))
        enterprise_has_feature = bool(enterprise and (_is_unlimited(enterprise.max_users) or enterprise.max_users > current_limit))
    else:
        professional_has_feature = bool(professional and getattr(professional, f"feature_{feature}", False))
        enterprise_has_feature = bool(enterprise and getattr(enterprise, f"feature_{feature}", False))
    return render(
        request,
        "subscription/upgrade_gate.html",
        {
            "feature": feature,
            "feature_display": feature_display,
            "subscription": subscription,
            "current_plan": current_plan,
            "professional": professional,
            "enterprise": enterprise,
            "professional_has_feature": professional_has_feature,
            "enterprise_has_feature": enterprise_has_feature,
        },
    )


def subscription_expired(request):
    subscription = Subscription.objects.select_related("plan").first()
    settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
    days_since_expiry = 0
    if subscription:
        days_since_expiry = max((timezone.now() - subscription.expires_at).days, 0)
    return render(
        request,
        "subscription/expired.html",
        {
            "subscription": subscription,
            "settings_obj": settings_obj,
            "days_since_expiry": days_since_expiry,
        },
    )


@login_required
def subscription_setup(request):
    if not is_owner(request.user):
        messages.error(request, "Only the account owner can configure the subscription.")
        return redirect("core:home")
    existing = Subscription.objects.filter(status__in=["active", "trial"]).first()
    if existing and request.method == "POST":
        messages.error(request, "An active subscription already exists. Contact support to change your plan.")
        return redirect("core:subscription_status")
    plans = SubscriptionPlan.objects.order_by("monthly_price")
    form = SubscriptionSetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        plan = get_object_or_404(SubscriptionPlan, pk=form.cleaned_data["plan"])
        now = timezone.now()
        trial_expires_at = trial_end(now)
        Subscription.objects.all().delete()
        subscription = Subscription.objects.create(
            plan=plan,
            billing_cycle="monthly",
            status="trial",
            expires_at=trial_expires_at,
            trial_ends_at=trial_expires_at,
            owner_name=form.cleaned_data["owner_name"],
            owner_email=form.cleaned_data["owner_email"],
            owner_phone=form.cleaned_data.get("owner_phone", ""),
            next_billing_date=trial_expires_at.date(),
        )
        settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
        settings_obj.guest_house_name = form.cleaned_data["guest_house_name"]
        settings_obj.email = form.cleaned_data["owner_email"]
        settings_obj.phone = form.cleaned_data.get("owner_phone", "")
        settings_obj.save()
        messages.success(request, f"Welcome to Circle Core Guest House. Your {subscription.plan.display_name} trial is active.")
        return redirect("core:home")
    return render(request, "subscription/setup.html", {"form": form, "plans": plans})


@login_required
def subscription_status(request):
    subscription = get_object_or_404(Subscription.objects.select_related("plan"))
    feature_keys = [
        "expenses",
        "full_reports",
        "export",
        "hourly_bookings",
        "weekly_bookings",
        "inventory",
        "staff_roles",
        "pos_integration",
        "multi_property",
        "api_access",
        "custom_pdf_branding",
        "maintenance_requests",
        "priority_support",
    ]
    feature_labels = {
        **FEATURE_DISPLAY_NAMES,
        "custom_pdf_branding": "Custom PDF Branding",
        "priority_support": "Priority Support",
    }
    features = [
        {
            "label": feature_labels.get(key, key.replace("_", " ").title()),
            "included": subscription.has_feature(key),
        }
        for key in feature_keys
    ]
    progress_pct = min(100, round((subscription.days_remaining / 30) * 100)) if subscription.days_remaining else 0
    billing_rows = []
    if subscription.last_payment_date or subscription.last_payment_amount or subscription.last_payment_reference:
        billing_rows.append(
            {
                "date": subscription.last_payment_date,
                "amount": subscription.last_payment_amount,
                "reference": subscription.last_payment_reference,
            }
        )
    return render(
        request,
        "subscription/status.html",
        {
            "subscription": subscription,
            "features": features,
            "progress_pct": progress_pct,
            "billing_rows": billing_rows,
        },
    )


class InventoryItemForm(forms.ModelForm):
    class Meta:
        model = InventoryItem
        fields = [
            "name",
            "category",
            "sku",
            "unit",
            "current_stock",
            "minimum_stock",
            "reorder_quantity",
            "cost_per_unit",
            "supplier_name",
            "supplier_phone",
            "location",
            "notes",
            "is_active",
        ]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field_class = "w-full rounded-xl border border-gray-200 bg-white px-4 py-3 text-sm text-gray-900 focus:border-gold focus:outline-none focus:ring-1 focus:ring-gold"
        for name, field in self.fields.items():
            if name == "is_active":
                field.widget.attrs.update({"class": "h-4 w-4 rounded border-gray-300 text-gold focus:ring-gold"})
            else:
                field.widget.attrs.update({"class": field_class})


class InventoryCategoryForm(forms.ModelForm):
    class Meta:
        model = InventoryCategory
        fields = ["name", "description", "color"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2}), "color": forms.TextInput(attrs={"type": "color"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": "w-full rounded-xl border border-gray-200 bg-white px-4 py-3 text-sm text-gray-900 focus:border-gold focus:outline-none focus:ring-1 focus:ring-gold"})


class StockInForm(forms.Form):
    quantity = forms.DecimalField(min_value=Decimal("0.01"), max_digits=10, decimal_places=2)
    reference = forms.CharField(required=False, max_length=100)
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    date = forms.DateField(initial=timezone.localdate, widget=forms.DateInput(attrs={"type": "date"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": "w-full rounded-xl border border-gray-200 bg-white px-4 py-3 text-sm text-gray-900 focus:border-gold focus:outline-none focus:ring-1 focus:ring-gold"})


class StockOutForm(forms.Form):
    quantity = forms.DecimalField(min_value=Decimal("0.01"), max_digits=10, decimal_places=2)
    reason = forms.CharField(required=False, max_length=100)
    room = forms.ModelChoiceField(queryset=Room.objects.all(), required=False)
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    confirm_negative = forms.BooleanField(required=False, label="Allow stock to go below zero")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name == "confirm_negative":
                field.widget.attrs.update({"class": "h-4 w-4 rounded border-gray-300 text-gold focus:ring-gold"})
            else:
                field.widget.attrs.update({"class": "w-full rounded-xl border border-gray-200 bg-white px-4 py-3 text-sm text-gray-900 focus:border-gold focus:outline-none focus:ring-1 focus:ring-gold"})


class MaintenanceRequestForm(forms.ModelForm):
    class Meta:
        model = MaintenanceRequest
        fields = [
            "room",
            "category",
            "priority",
            "title",
            "description",
            "assigned_to",
            "assigned_phone",
            "estimated_cost",
            "scheduled_for",
            "block_room_until_resolved",
        ]
        widgets = {
            "scheduled_for": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field_class = "w-full rounded-xl border border-gray-200 bg-white px-4 py-3 text-sm text-gray-900 focus:border-gold focus:outline-none focus:ring-1 focus:ring-gold"
        for name, field in self.fields.items():
            if name == "block_room_until_resolved":
                field.widget.attrs.update({"class": "h-4 w-4 rounded border-gray-300 text-gold focus:ring-gold"})
            else:
                field.widget.attrs.update({"class": field_class})


class MaintenanceResolutionForm(forms.Form):
    resolution_notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))
    actual_cost = forms.DecimalField(required=False, min_value=Decimal("0.00"), max_digits=10, decimal_places=2)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": "w-full rounded-xl border border-gray-200 bg-white px-4 py-3 text-sm text-gray-900 focus:border-gold focus:outline-none focus:ring-1 focus:ring-gold"})


def _maintenance_form_for_property(active_prop, *args, **kwargs):
    form = MaintenanceRequestForm(*args, **kwargs)
    form.fields["room"].queryset = Room.objects.filter(prop=active_prop)
    return form


@login_required
def maintenance_board(request):
    active_prop = get_active_property(request)
    requests = MaintenanceRequest.objects.select_related("room", "reported_by").filter(room__prop=active_prop)
    columns = {
        "open": requests.filter(status="open"),
        "in_progress": requests.filter(status="in_progress"),
        "resolved": requests.filter(status="resolved"),
        "closed": requests.filter(status="closed"),
    }
    today = timezone.localdate()
    month_start = today.replace(day=1)
    month_end = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    open_count = requests.filter(status__in=["open", "in_progress"]).count()
    urgent_count = requests.filter(status__in=["open", "in_progress"], priority="urgent").count()
    overdue_count = sum(1 for request_obj in requests.filter(status__in=["open", "in_progress"]) if request_obj.is_overdue)
    this_month_cost = (
        requests.filter(reported_at__date__gte=month_start, reported_at__date__lte=month_end)
        .aggregate(total=Sum("actual_cost"))["total"]
        or Decimal("0.00")
    )
    return render(
        request,
        "core/maintenance_board.html",
        {
            "columns": columns,
            "open_count": open_count,
            "urgent_count": urgent_count,
            "overdue_count": overdue_count,
            "this_month_cost": this_month_cost,
        },
    )


@login_required
def maintenance_add(request):
    active_prop = get_active_property(request)
    form = _maintenance_form_for_property(active_prop, request.POST or None, initial={"room": request.GET.get("room", "")})
    if request.method == "POST" and form.is_valid():
        maintenance_request = form.save(commit=False)
        maintenance_request.reported_by = request.user
        maintenance_request.save()
        messages.success(request, "Maintenance request logged.")
        return redirect("core:maintenance_detail", pk=maintenance_request.pk)
    return render(request, "core/maintenance_form.html", {"form": form, "title": "Log Maintenance Request"})


@login_required
def maintenance_edit(request, pk):
    active_prop = get_active_property(request)
    maintenance_request = get_object_or_404(MaintenanceRequest, pk=pk, room__prop=active_prop)
    form = _maintenance_form_for_property(active_prop, request.POST or None, instance=maintenance_request)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Maintenance request updated.")
        return redirect("core:maintenance_detail", pk=maintenance_request.pk)
    return render(
        request,
        "core/maintenance_form.html",
        {"form": form, "title": f"Edit {maintenance_request.title}", "maintenance_request": maintenance_request},
    )


@login_required
def maintenance_detail(request, pk):
    active_prop = get_active_property(request)
    maintenance_request = get_object_or_404(MaintenanceRequest.objects.select_related("room", "reported_by"), pk=pk, room__prop=active_prop)
    resolution_form = MaintenanceResolutionForm(
        initial={
            "resolution_notes": maintenance_request.resolution_notes,
            "actual_cost": maintenance_request.actual_cost,
        }
    )
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "mark_in_progress":
            maintenance_request.status = "in_progress"
            maintenance_request.save(update_fields=["status"])
            messages.success(request, "Maintenance request marked in progress.")
            return redirect("core:maintenance_detail", pk=maintenance_request.pk)
        if action == "mark_resolved":
            resolution_form = MaintenanceResolutionForm(request.POST)
            if resolution_form.is_valid():
                maintenance_request.status = "resolved"
                maintenance_request.resolution_notes = resolution_form.cleaned_data.get("resolution_notes", "")
                maintenance_request.actual_cost = resolution_form.cleaned_data.get("actual_cost")
                maintenance_request.resolved_at = timezone.now()
                maintenance_request.save()
                messages.success(request, "Maintenance request resolved.")
                return redirect("core:maintenance_detail", pk=maintenance_request.pk)
        if action == "close":
            maintenance_request.status = "closed"
            maintenance_request.save(update_fields=["status"])
            messages.success(request, "Maintenance request closed.")
            return redirect("core:maintenance_detail", pk=maintenance_request.pk)
    return render(
        request,
        "core/maintenance_detail.html",
        {
            "maintenance_request": maintenance_request,
            "resolution_form": resolution_form,
        },
    )


@login_required
def maintenance_set_status(request, pk):
    active_prop = get_active_property(request)
    maintenance_request = get_object_or_404(MaintenanceRequest, pk=pk, room__prop=active_prop)
    if request.method == "POST":
        status = request.POST.get("status")
        valid_statuses = {choice[0] for choice in MaintenanceRequest.STATUS_CHOICES}
        if status not in valid_statuses:
            messages.error(request, "Invalid maintenance status.")
        else:
            maintenance_request.status = status
            if status == "resolved":
                maintenance_request.resolved_at = timezone.now()
            maintenance_request.save()
            messages.success(request, f"Maintenance request marked {maintenance_request.get_status_display()}.")
    return redirect("core:maintenance_board")


_DEFAULT_INVENTORY_CATEGORIES = [
    ("Cleaning Supplies", "#22c55e"),
    ("Toiletries & Amenities", "#c9a84c"),
    ("Linen & Towels", "#3b82f6"),
    ("Kitchen & Dining", "#f97316"),
    ("Maintenance & Tools", "#ef4444"),
    ("Office & Admin", "#8b5cf6"),
    ("Food & Beverages", "#14b8a6"),
    ("Other", "#6b7280"),
]


def _seed_default_inventory_categories():
    if InventoryCategory.objects.exists():
        return
    for name, color in _DEFAULT_INVENTORY_CATEGORIES:
        InventoryCategory.objects.get_or_create(name=name, defaults={"color": color})


@login_required
def inventory_dashboard(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    items = InventoryItem.objects.select_related("category").filter(is_active=True, prop=active_prop)
    category_filter = request.GET.get("category", "")
    location_filter = request.GET.get("location", "")
    status_filter = request.GET.get("status", "")
    query = request.GET.get("q", "").strip()

    if category_filter:
        items = items.filter(category_id=category_filter)
    if location_filter:
        items = items.filter(location=location_filter)
    if query:
        items = items.filter(Q(name__icontains=query) | Q(sku__icontains=query))
    if status_filter == "low":
        items = items.filter(current_stock__lte=F("minimum_stock"), current_stock__gt=0)
    elif status_filter == "out":
        items = items.filter(current_stock__lte=0)
    elif status_filter == "sufficient":
        items = items.filter(current_stock__gt=F("minimum_stock"))

    all_active = InventoryItem.objects.select_related("category").filter(is_active=True, prop=active_prop)
    low_stock_items = all_active.filter(current_stock__lte=F("minimum_stock")).order_by("current_stock", "name")
    total_stock_value = sum((item.stock_value for item in all_active), Decimal("0.00"))
    categories = InventoryCategory.objects.all()
    locations = (
        InventoryItem.objects.filter(is_active=True, prop=active_prop)
        .exclude(location="")
        .values_list("location", flat=True)
        .distinct()
        .order_by("location")
    )

    return render(
        request,
        "core/inventory_dashboard.html",
        {
            "items": items,
            "categories": categories,
            "locations": locations,
            "low_stock_items": low_stock_items,
            "total_items": all_active.count(),
            "low_stock_count": low_stock_items.count(),
            "total_stock_value": total_stock_value,
            "categories_count": categories.count(),
            "category_filter": category_filter,
            "location_filter": location_filter,
            "status_filter": status_filter,
            "query": query,
        },
    )


@login_required
def inventory_add(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    _seed_default_inventory_categories()
    active_prop = get_active_property(request)
    form = InventoryItemForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.prop = active_prop
        item.save()
        messages.success(request, "Inventory item added.")
        return redirect("core:inventory_detail", pk=item.pk)
    return render(request, "core/inventory_form.html", {
        "form": form,
        "title": "Add Inventory Item",
        "has_categories": InventoryCategory.objects.exists(),
    })


@login_required
def inventory_edit(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    item = get_object_or_404(InventoryItem, pk=pk, prop=active_prop)
    form = InventoryItemForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Inventory item updated.")
        return redirect("core:inventory_detail", pk=item.pk)
    return render(request, "core/inventory_form.html", {"form": form, "item": item, "title": f"Edit {item.name}"})


@login_required
def inventory_detail(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    item = get_object_or_404(InventoryItem.objects.select_related("category"), pk=pk, prop=active_prop)
    transactions = item.transactions.select_related("room", "booking", "recorded_by").all()[:50]
    return render(request, "core/inventory_detail.html", {"item": item, "transactions": transactions})


def _record_inventory_transaction(
    item,
    transaction_type,
    quantity,
    user,
    reference="",
    notes="",
    room=None,
    booking=None,
    recorded_date=None,
):
    stock_before = item.current_stock
    stock_after = stock_before + quantity if transaction_type == "in" else stock_before - quantity
    item.current_stock = stock_after
    item.save(update_fields=["current_stock"])
    transaction = InventoryTransaction.objects.create(
        item=item,
        transaction_type=transaction_type,
        quantity=quantity,
        stock_before=stock_before,
        stock_after=stock_after,
        reference=reference,
        notes=notes,
        room=room,
        booking=booking,
        recorded_by=user,
    )
    if recorded_date:
        recorded_time = timezone.localtime().time()
        transaction.recorded_at = timezone.make_aware(datetime.datetime.combine(recorded_date, recorded_time))
        transaction.save(update_fields=["recorded_at"])
    return transaction


@login_required
def inventory_stock_in(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    item = get_object_or_404(InventoryItem, pk=pk, prop=active_prop)
    form = StockInForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        recorded_date = form.cleaned_data.get("date")
        if _is_date_locked(recorded_date):
            return _locked_day_response(request, recorded_date, "core:inventory_detail", pk=item.pk)
        _record_inventory_transaction(
            item,
            "in",
            form.cleaned_data["quantity"],
            request.user,
            reference=form.cleaned_data.get("reference", ""),
            notes=form.cleaned_data.get("notes", ""),
            recorded_date=recorded_date,
        )
        messages.success(request, "Stock received and inventory updated.")
        return redirect("core:inventory_detail", pk=item.pk)
    return render(request, "core/inventory_stock_in.html", {"form": form, "item": item, "mode": "in"})


@login_required
def inventory_stock_out(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    item = get_object_or_404(InventoryItem, pk=pk, prop=active_prop)
    if request.method == "POST":
        form = StockOutForm(request.POST)
        form.fields["room"].queryset = Room.objects.filter(prop=active_prop)
    else:
        form = StockOutForm()
    if request.method == "POST" and form.is_valid():
        quantity = form.cleaned_data["quantity"]
        if item.current_stock - quantity < 0 and not form.cleaned_data.get("confirm_negative"):
            form.add_error("confirm_negative", "Stock will go below zero. Tick this box to confirm.")
        else:
            _record_inventory_transaction(
                item,
                "out",
                quantity,
                request.user,
                reference=form.cleaned_data.get("reason", ""),
                notes=form.cleaned_data.get("notes", ""),
                room=form.cleaned_data.get("room"),
            )
            messages.success(request, "Stock used and inventory updated.")
            return redirect("core:inventory_detail", pk=item.pk)
    return render(request, "core/inventory_stock_in.html", {"form": form, "item": item, "mode": "out"})


@login_required
def inventory_transactions(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    transactions = InventoryTransaction.objects.select_related("item", "item__category", "room", "recorded_by").filter(item__prop=active_prop)
    item_filter = request.GET.get("item", "")
    type_filter = request.GET.get("type", "")
    start = request.GET.get("start", "")
    end = request.GET.get("end", "")
    if item_filter:
        transactions = transactions.filter(item_id=item_filter)
    if type_filter:
        transactions = transactions.filter(transaction_type=type_filter)
    if start:
        transactions = transactions.filter(recorded_at__date__gte=start)
    if end:
        transactions = transactions.filter(recorded_at__date__lte=end)
    return render(
        request,
        "core/inventory_transactions.html",
        {
            "transactions": transactions,
            "items": InventoryItem.objects.filter(prop=active_prop),
            "transaction_types": InventoryTransaction.TRANSACTION_TYPES,
            "item_filter": item_filter,
            "type_filter": type_filter,
            "start": start,
            "end": end,
        },
    )


@login_required
def inventory_categories(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    form = InventoryCategoryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Inventory category added.")
        return redirect("core:inventory_categories")
    return render(
        request,
        "core/inventory_form.html",
        {
            "form": form,
            "title": "Inventory Categories",
            "categories": InventoryCategory.objects.all(),
            "is_category_form": True,
        },
    )


# ─── POS ITEM MANAGEMENT ────────────────────────────────────

class POSItemForm(forms.ModelForm):
    class Meta:
        model = POSItem
        fields = [
            'name', 'category', 'price', 'emoji', 'barcode', 'sku',
            'is_quick_item', 'is_active', 'track_inventory',
            'stock_quantity', 'low_stock_alert', 'color', 'sort_order',
        ]
        widgets = {'color': forms.TextInput(attrs={'type': 'color'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field_style = "padding:10px 14px; width:100%; box-sizing:border-box;"
        for name, field in self.fields.items():
            if name not in ('is_quick_item', 'is_active', 'track_inventory'):
                field.widget.attrs.update({'style': field_style})


class POSCategoryForm(forms.ModelForm):
    class Meta:
        model = POSCategory
        fields = ['name', 'icon', 'color', 'sort_order', 'is_active']
        widgets = {'color': forms.TextInput(attrs={'type': 'color'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name != 'is_active':
                field.widget.attrs.update({'style': 'padding:10px 14px; width:100%; box-sizing:border-box;'})


_DEFAULT_POS_CATEGORIES = [
    ("Food & Snacks",     "🍔", "#f97316"),
    ("Drinks",            "🥤", "#3b82f6"),
    ("Alcohol",           "🍺", "#f59e0b"),
    ("Toiletries",        "🧴", "#8b5cf6"),
    ("Cleaning",          "🧹", "#22c55e"),
    ("Miscellaneous",     "📦", "#6b7280"),
]


def _seed_default_pos_categories(active_prop):
    if POSCategory.objects.filter(prop=active_prop).exists():
        return
    for i, (name, icon, color) in enumerate(_DEFAULT_POS_CATEGORIES):
        POSCategory.objects.get_or_create(
            name=name,
            prop=active_prop,
            defaults={"icon": icon, "color": color, "sort_order": i},
        )


def _pos_item_form_for_property(active_prop, *args, **kwargs):
    form = POSItemForm(*args, **kwargs)
    form.fields["category"].queryset = POSCategory.objects.filter(prop=active_prop)
    return form


@login_required
def pos_items_list(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    categories = POSCategory.objects.filter(prop=active_prop, is_active=True)
    category_filter = request.GET.get('category', '')
    items = POSItem.objects.select_related('category').filter(prop=active_prop)
    if category_filter:
        items = items.filter(category_id=category_filter)
    quick_items = POSItem.objects.filter(prop=active_prop, is_quick_item=True, is_active=True).order_by('sort_order', 'name')
    return render(request, 'pos/items_list.html', {
        'items': items,
        'categories': categories,
        'category_filter': category_filter,
        'quick_items': quick_items,
        'total_items': POSItem.objects.filter(prop=active_prop, is_active=True).count(),
        'quick_count': quick_items.count(),
    })


@login_required
def pos_item_add(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    _seed_default_pos_categories(active_prop)
    cat_form = POSCategoryForm()
    if request.method == 'POST':
        form = _pos_item_form_for_property(active_prop, request.POST)
        if form.is_valid():
            item = form.save(commit=False)
            item.prop = active_prop
            item.save()
            messages.success(request, f'"{item.name}" added to POS menu.')
            return redirect('core:pos_items_list')
    else:
        form = _pos_item_form_for_property(active_prop)
    return render(request, 'pos/item_form.html', {
        'form': form, 'cat_form': cat_form, 'title': 'Add POS Item',
    })


@login_required
def pos_item_edit(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    item = get_object_or_404(POSItem, pk=pk, prop=active_prop)
    cat_form = POSCategoryForm()
    if request.method == 'POST':
        form = _pos_item_form_for_property(active_prop, request.POST, instance=item)
        if form.is_valid():
            form.save()
            messages.success(request, f'"{item.name}" updated.')
            return redirect('core:pos_items_list')
    else:
        form = _pos_item_form_for_property(active_prop, instance=item)
    return render(request, 'pos/item_form.html', {
        'form': form, 'cat_form': cat_form, 'item': item, 'title': f'Edit {item.name}',
    })


@login_required
def pos_item_delete(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    item = get_object_or_404(POSItem, pk=pk, prop=active_prop)
    if request.method == 'POST':
        approver = _manager_approval(request, "Delete POS item")
        if not approver:
            messages.error(request, "Owner approval is required to remove POS menu items.")
            return redirect('core:pos_items_list')
        before = _model_snapshot(item, ["name", "price", "stock_quantity", "track_inventory"])
        name = item.name
        item.delete()
        _audit(request, "delete", item, before=before, reason="POS item deleted", approved_by=approver)
        messages.success(request, f'"{name}" removed from POS menu.')
    return redirect('core:pos_items_list')


@login_required
def pos_category_add(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    if request.method == 'POST':
        form = POSCategoryForm(request.POST)
        if form.is_valid():
            cat = form.save(commit=False)
            cat.prop = get_active_property(request)
            cat.save()
            messages.success(request, f'Category "{cat.name}" created.')
    return redirect('core:pos_items_list')


@login_required
def pos_items_reorder(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            ids = data.get('ids', [])
            active_prop = get_active_property(request)
            for order, item_id in enumerate(ids):
                POSItem.objects.filter(pk=item_id, prop=active_prop).update(sort_order=order)
            return JsonResponse({'success': True})
        except Exception:
            return JsonResponse({'success': False}, status=400)
    return JsonResponse({'error': 'POST only'}, status=405)


# ─── POS TERMINAL ───────────────────────────────────────────

@login_required
def pos_terminal(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    categories = POSCategory.objects.filter(prop=active_prop, is_active=True).prefetch_related('items')
    items = POSItem.objects.select_related('category').filter(prop=active_prop, is_active=True)
    quick_items = items.filter(is_quick_item=True)
    settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
    open_shift = POSShift.objects.filter(prop=active_prop, closed_at__isnull=True).first()
    items_json = json.dumps([
        {
            'id': item.pk,
            'name': item.name,
            'price': float(item.price),
            'emoji': item.emoji,
            'category_id': item.category_id,
            'barcode': item.barcode,
            'is_quick_item': item.is_quick_item,
            'color': item.color,
            'track_inventory': item.track_inventory,
            'stock_quantity': float(item.stock_quantity),
            'is_out_of_stock': item.is_out_of_stock,
            'is_low_stock': item.is_low_stock,
        }
        for item in items
    ])
    categories_json = json.dumps([
        {'id': cat.pk, 'name': cat.name, 'icon': cat.icon, 'color': cat.color}
        for cat in categories
    ])
    recent_sales = (
        POSSale.objects
        .select_related('guest')
        .prefetch_related('items')
        .filter(prop=active_prop, created_at__date=timezone.localdate())
        .order_by('-created_at')[:20]
    )
    active_bookings = (
        Booking.objects
        .select_related('guest', 'room')
        .filter(room__prop=active_prop, status='Checked In')
        .order_by('room__name')
    )
    active_bookings_json = json.dumps([
        {
            'id': b.pk,
            'reference': b.booking_reference,
            'guest_name': b.guest.full_name,
            'room_name': b.room.name,
            'balance_due': float(b.balance_due),
        }
        for b in active_bookings
    ])
    return render(request, 'pos/terminal.html', {
        'categories': categories,
        'items': items,
        'quick_items': quick_items,
        'items_json': items_json,
        'categories_json': categories_json,
        'settings': settings_obj,
        'open_shift': open_shift,
        'vat_rate': float(settings_obj.vat_rate) if settings_obj.vat_registered else 0,
        'vat_registered': settings_obj.vat_registered,
        'recent_sales': recent_sales,
        'active_bookings': active_bookings,
        'active_bookings_json': active_bookings_json,
    })


@login_required
def pos_active_bookings_api(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    bookings = Booking.objects.select_related('guest', 'room').filter(room__prop=active_prop, status='Checked In')
    data = [
        {
            'id': b.pk,
            'reference': b.booking_reference,
            'guest_name': b.guest.full_name,
            'room_name': b.room.name,
            'balance_due': float(b.balance_due),
        }
        for b in bookings
    ]
    return JsonResponse({'bookings': data})


@login_required
def pos_barcode_lookup(request, barcode):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    item = POSItem.objects.filter(prop=active_prop, barcode=barcode, is_active=True).select_related('category').first()
    if not item:
        return JsonResponse({'found': False})
    return JsonResponse({
        'found': True,
        'item': {
            'id': item.pk,
            'name': item.name,
            'price': float(item.price),
            'emoji': item.emoji,
            'category': item.category.name if item.category else '',
            'is_out_of_stock': item.is_out_of_stock,
        }
    })


@login_required
def pos_complete_sale(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    items_data = data.get('items', [])
    if not items_data:
        return JsonResponse({'error': 'No items in cart'}, status=400)

    settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
    active_prop = get_active_property(request)
    booking = None
    booking_id = data.get('booking_id')
    if booking_id:
        booking = Booking.objects.select_related('guest').filter(pk=booking_id, room__prop=active_prop).first()

    discount_pct = Decimal(str(data.get('discount_pct', 0)))
    discount_amount = Decimal(str(data.get('discount_amount', 0)))
    payment_method = data.get('payment_method', 'cash')
    if payment_method in ("cash", "split") and not POSShift.objects.filter(prop=active_prop, closed_at__isnull=True).exists():
        return JsonResponse({'error': 'Open a POS shift before taking cash payments.'}, status=400)

    subtotal = Decimal('0.00')
    sale_items_to_create = []
    for item_data in items_data:
        qty = Decimal(str(item_data.get('quantity', 1)))
        unit_price = Decimal(str(item_data.get('unit_price', 0)))
        item_discount_pct = Decimal(str(item_data.get('discount_pct', 0)))
        discount_per_unit = unit_price * (item_discount_pct / 100)
        line_total = (unit_price - discount_per_unit) * qty
        subtotal += line_total
        sale_items_to_create.append({
            'pos_item_id': item_data.get('pos_item_id'),
            'name': item_data.get('name', ''),
            'quantity': qty,
            'unit_price': unit_price,
            'discount_pct': item_discount_pct,
            'line_total': line_total,
        })

    if discount_amount == 0 and discount_pct > 0:
        discount_amount = (subtotal * discount_pct / 100).quantize(Decimal('0.01'))
    approver = None
    if discount_amount > SENSITIVE_DISCOUNT_AMOUNT or discount_pct > SENSITIVE_DISCOUNT_PERCENT:
        approver = _manager_approval_credentials(
            request,
            data.get("manager_username", ""),
            data.get("manager_password", ""),
            "POS large discount",
        )
        if not approver:
            return JsonResponse(
                {
                    'error': 'Owner approval is required for this discount.',
                    'approval_required': True,
                    'reason': f'Discount R{discount_amount:.2f} / {discount_pct:.2f}%',
                },
                status=403,
            )

    after_discount = subtotal - discount_amount
    tax_amount = Decimal('0.00')
    if settings_obj.vat_registered and after_discount > 0:
        vat_rate = settings_obj.vat_rate
        divisor = Decimal('100') + vat_rate
        tax_amount = (after_discount * vat_rate / divisor).quantize(Decimal('0.01'))
    total = after_discount

    cash_tendered = Decimal(str(data.get('cash_tendered', 0)))
    change_given = Decimal(str(data.get('change_given', 0)))
    cash_amount = Decimal(str(data.get('cash_amount', 0)))
    card_amount = Decimal(str(data.get('card_amount', 0)))

    open_shift = POSShift.objects.filter(prop=active_prop, closed_at__isnull=True).first()

    sale = POSSale.objects.create(
        prop=active_prop,
        booking=booking,
        guest=booking.guest if booking else None,
        shift=open_shift,
        subtotal=subtotal,
        discount_amount=discount_amount,
        discount_pct=discount_pct,
        tax_amount=tax_amount,
        total=total,
        payment_method=payment_method,
        cash_amount=cash_amount if payment_method == 'split' else (total if payment_method == 'cash' else Decimal('0')),
        card_amount=card_amount if payment_method == 'split' else (total if payment_method == 'card' else Decimal('0')),
        cash_tendered=cash_tendered,
        change_given=change_given,
        notes=data.get('notes', ''),
        served_by=request.user,
        status='completed',
    )
    _audit(
        request,
        "create",
        sale,
        after=_model_snapshot(sale, ["subtotal", "discount_amount", "discount_pct", "total", "payment_method", "status"]),
        approved_by=approver,
        reason="POS sale completed",
    )

    for item_data in sale_items_to_create:
        pos_item = None
        if item_data['pos_item_id']:
            pos_item = POSItem.objects.filter(pk=item_data['pos_item_id'], prop=active_prop).first()
        POSSaleItem.objects.create(
            sale=sale,
            pos_item=pos_item,
            name=item_data['name'],
            quantity=item_data['quantity'],
            unit_price=item_data['unit_price'],
            discount_pct=item_data['discount_pct'],
            line_total=item_data['line_total'],
        )
        if pos_item and pos_item.track_inventory:
            pos_item.stock_quantity = max(pos_item.stock_quantity - item_data['quantity'], Decimal('0'))
            pos_item.save(update_fields=['stock_quantity'])

    if payment_method == 'room_charge' and booking:
        # Room charge increases what the guest owes — use update() to bypass compute_totals()
        from django.db.models import F as DbF
        Booking.objects.filter(pk=booking.pk).update(
            total_amount=DbF('total_amount') + total,
            balance_due=DbF('balance_due') + total,
        )

    return JsonResponse({
        'success': True,
        'sale_reference': sale.sale_reference,
        'sale_id': sale.pk,
        'total': float(sale.total),
        'change_given': float(sale.change_given),
        'receipt_url': f'/pos/sales/{sale.pk}/pdf/receipt/',
    })


# ─── POS SALES HISTORY ──────────────────────────────────────

@login_required
def pos_sales_list(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    today = timezone.localdate()
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    method_filter = request.GET.get('method', '')
    query = request.GET.get('q', '').strip()
    active_prop = get_active_property(request)

    sales = POSSale.objects.select_related('guest', 'booking', 'served_by').prefetch_related('items').filter(prop=active_prop)
    if date_from:
        sales = sales.filter(created_at__date__gte=date_from)
    if date_to:
        sales = sales.filter(created_at__date__lte=date_to)
    if method_filter:
        sales = sales.filter(payment_method=method_filter)
    if query:
        sales = sales.filter(
            Q(sale_reference__icontains=query)
            | Q(guest__first_name__icontains=query)
            | Q(guest__last_name__icontains=query)
        )

    today_sales = POSSale.objects.filter(prop=active_prop, created_at__date=today, status='completed')
    today_revenue = today_sales.aggregate(t=Sum('total'))['t'] or Decimal('0.00')
    today_count = today_sales.count()
    avg_sale = (today_revenue / today_count).quantize(Decimal('0.01')) if today_count else Decimal('0.00')
    top_item_qs = (
        POSSaleItem.objects
        .filter(sale__prop=active_prop, sale__created_at__date=today, sale__status='completed')
        .values('name')
        .annotate(cnt=Sum('quantity'))
        .order_by('-cnt')
        .first()
    )
    top_item = top_item_qs['name'] if top_item_qs else '—'

    return render(request, 'pos/sales_list.html', {
        'sales': sales[:200],
        'today_revenue': today_revenue,
        'today_count': today_count,
        'avg_sale': avg_sale,
        'top_item': top_item,
        'date_from': date_from,
        'date_to': date_to,
        'method_filter': method_filter,
        'query': query,
        'payment_methods': POSSale.PAYMENT_METHOD_CHOICES,
    })


@login_required
def pos_sale_detail(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    sale = get_object_or_404(
        POSSale.objects.select_related('guest', 'booking', 'booking__room', 'served_by')
        .prefetch_related('items', 'items__pos_item'),
        pk=pk,
        prop=active_prop,
    )
    return render(request, 'pos/sale_detail.html', {'sale': sale})


@login_required
def pos_sale_refund(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    sale = get_object_or_404(POSSale, pk=pk, prop=active_prop)
    if request.method == 'POST':
        reason = request.POST.get('refund_reason', '').strip()
        approver = _manager_approval(request, "POS refund")
        if not approver:
            messages.error(request, 'Owner approval is required to refund a POS sale.')
            return redirect('core:pos_sale_detail', pk=pk)
        if sale.status != 'completed':
            messages.error(request, 'Only completed sales can be refunded.')
            return redirect('core:pos_sale_detail', pk=pk)

        refund = POSSale.objects.create(
            prop=active_prop,
            booking=sale.booking,
            guest=sale.guest,
            subtotal=-sale.subtotal,
            discount_amount=-sale.discount_amount,
            discount_pct=sale.discount_pct,
            tax_amount=-sale.tax_amount,
            total=-sale.total,
            payment_method=sale.payment_method,
            cash_amount=-sale.cash_amount,
            card_amount=-sale.card_amount,
            cash_tendered=Decimal('0'),
            change_given=Decimal('0'),
            status='refunded',
            notes=f"Refund of {sale.sale_reference}",
            refund_reason=reason,
            original_sale=sale,
            served_by=request.user,
        )
        for item in sale.items.all():
            POSSaleItem.objects.create(
                sale=refund,
                pos_item=item.pos_item,
                name=item.name,
                quantity=-item.quantity,
                unit_price=item.unit_price,
                discount_pct=item.discount_pct,
                line_total=-item.line_total,
            )
            if item.pos_item and item.pos_item.track_inventory:
                item.pos_item.stock_quantity += item.quantity
                item.pos_item.save(update_fields=['stock_quantity'])

        sale.status = 'refunded'
        sale.save(update_fields=['status'])
        _audit(
            request,
            "refund",
            sale,
            before={"status": "completed", "total": _money(sale.total)},
            after={"status": "refunded", "refund_reference": refund.sale_reference},
            reason=reason,
            approved_by=approver,
        )

        if sale.payment_method == 'room_charge' and sale.booking:
            refund_payment = Payment.objects.filter(
                booking=sale.booking,
                reference=sale.sale_reference,
            ).first()
            if refund_payment:
                refund_payment.delete()

        messages.success(request, f'Refund processed: {refund.sale_reference}')
        return redirect('core:pos_sale_detail', pk=refund.pk)
    return render(request, 'pos/sale_detail.html', {'sale': sale, 'show_refund': True})


@login_required
def pos_receipt_pdf(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    sale = get_object_or_404(
        POSSale.objects.select_related('guest', 'booking', 'booking__room', 'served_by')
        .prefetch_related('items'),
        pk=pk,
        prop=active_prop,
    )
    settings_obj = _pdf_settings_context()["settings"]
    return render_pos_receipt_pdf(sale, settings_obj)


# ─── POS SHIFTS ─────────────────────────────────────────────

@login_required
def pos_shifts(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    open_shift = POSShift.objects.filter(prop=active_prop, closed_at__isnull=True).first()
    past_shifts = POSShift.objects.filter(prop=active_prop, closed_at__isnull=False).select_related('opened_by', 'closed_by')[:20]
    return render(request, 'pos/shifts.html', {
        'open_shift': open_shift,
        'past_shifts': past_shifts,
    })


@login_required
def pos_open_shift(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    next_url = request.POST.get('next') or request.GET.get('next')
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        next_url = reverse('core:pos_shifts')

    if request.method == 'POST':
        active_prop = get_active_property(request)
        if POSShift.objects.filter(prop=active_prop, closed_at__isnull=True).exists():
            messages.error(request, 'A shift is already open.')
        else:
            opening_float_str = request.POST.get('opening_float', '0')
            try:
                opening_float = Decimal(opening_float_str)
            except Exception:
                opening_float = Decimal('0')
            shift = POSShift.objects.create(
                prop=active_prop,
                opened_by=request.user,
                opening_float=opening_float,
                notes=request.POST.get('notes', ''),
            )
            _audit(request, "create", shift, after=_model_snapshot(shift, ["opening_float", "notes"]))
            messages.success(request, f'Shift opened with R{opening_float} float.')
    return redirect(next_url)


@login_required
def pos_close_shift(request, pk):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    active_prop = get_active_property(request)
    shift = get_object_or_404(POSShift, pk=pk, prop=active_prop)
    if request.method == 'POST':
        if shift.closed_at:
            messages.error(request, 'Shift is already closed.')
        else:
            closing_cash_str = request.POST.get('closing_cash', '0')
            try:
                closing_cash = Decimal(closing_cash_str)
            except Exception:
                closing_cash = Decimal('0')
            shift.closed_at = timezone.now()
            shift.closed_by = request.user
            shift.closing_cash = closing_cash
            shift.notes = request.POST.get('notes', shift.notes)
            variance = closing_cash - shift.expected_cash
            if variance != SHIFT_VARIANCE_APPROVAL_AMOUNT and not shift.notes.strip():
                messages.error(request, 'Explain the cash variance in the notes before closing the shift.')
                return redirect('core:pos_shifts')
            shift.save()
            _audit(
                request,
                "update",
                shift,
                after={
                    "closing_cash": _money(shift.closing_cash),
                    "expected_cash": _money(shift.expected_cash),
                    "variance": _money(shift.cash_variance),
                    "notes": shift.notes,
                },
                reason="Shift closed",
            )
            messages.success(request, 'Shift closed successfully.')
    return redirect('core:pos_shifts')


# ─── POS REPORTS ────────────────────────────────────────────

@login_required
def pos_reports(request):
    blocked = _cleaner_blocked(request)
    if blocked:
        return blocked
    today = timezone.localdate()
    month_start = today.replace(day=1)
    active_prop = get_active_property(request)

    date_from_str = request.GET.get('date_from', month_start.isoformat())
    date_to_str = request.GET.get('date_to', today.isoformat())
    try:
        date_from = datetime.date.fromisoformat(date_from_str)
        date_to = datetime.date.fromisoformat(date_to_str)
    except ValueError:
        date_from = month_start
        date_to = today

    sales = POSSale.objects.filter(
        prop=active_prop,
        created_at__date__gte=date_from,
        created_at__date__lte=date_to,
        status='completed',
    )

    total_revenue = sales.aggregate(t=Sum('total'))['t'] or Decimal('0.00')
    total_count = sales.count()
    avg_sale_val = (total_revenue / total_count).quantize(Decimal('0.01')) if total_count else Decimal('0.00')

    revenue_by_method = list(
        sales.values('payment_method')
        .annotate(total=Sum('total'), count=Count('id'))
        .order_by('-total')
    )

    top_items = list(
        POSSaleItem.objects
        .filter(sale__in=sales)
        .values('name')
        .annotate(qty=Sum('quantity'), revenue=Sum('line_total'))
        .order_by('-revenue')[:10]
    )

    revenue_by_category = list(
        POSSaleItem.objects
        .filter(sale__in=sales, pos_item__category__isnull=False)
        .values('pos_item__category__name')
        .annotate(revenue=Sum('line_total'))
        .order_by('-revenue')
    )

    sales_by_hour = list(
        sales.annotate(hour=ExtractHour('created_at'))
        .values('hour')
        .annotate(count=Count('id'), revenue=Sum('total'))
        .order_by('hour')
    )
    hourly_labels = [str(h) for h in range(24)]
    hourly_revenue = [0.0] * 24
    for row in sales_by_hour:
        if row['hour'] is not None:
            hourly_revenue[int(row['hour'])] = float(row['revenue'] or 0)

    room_charges = list(
        sales.filter(payment_method='room_charge', booking__isnull=False)
        .values('booking__booking_reference', 'booking__guest__first_name', 'booking__guest__last_name', 'booking__room__name')
        .annotate(total=Sum('total'), count=Count('id'))
        .order_by('-total')[:10]
    )

    return render(request, 'pos/reports.html', {
        'total_revenue': total_revenue,
        'total_count': total_count,
        'avg_sale': avg_sale_val,
        'date_from': date_from_str,
        'date_to': date_to_str,
        'revenue_by_method': revenue_by_method,
        'top_items': top_items,
        'revenue_by_category': revenue_by_category,
        'hourly_labels_json': json.dumps(hourly_labels),
        'hourly_revenue_json': json.dumps(hourly_revenue),
        'revenue_by_method_json': json.dumps([
            {'label': r['payment_method'], 'value': float(r['total'])}
            for r in revenue_by_method
        ]),
        'top_items_json': json.dumps([
            {'label': i['name'], 'value': float(i['revenue'])}
            for i in top_items
        ]),
        'room_charges': room_charges,
    })


# ── Command Center impersonation entry point ──────────────────────────────────

def command_enter(request):
    """
    Receives a short-lived signed token from the Command Center and logs in
    the tenant's first superuser so admins can operate as the tenant.
    Token expires after 60 seconds.
    """
    from django.core import signing
    from django.contrib.auth import get_user_model, login as auth_login

    token = request.GET.get("token", "")
    try:
        data = signing.loads(token, salt="command-impersonation", max_age=60)
    except (signing.BadSignature, signing.SignatureExpired):
        return render(request, "core/command_enter_invalid.html", status=400)

    if data.get("schema") != connection.schema_name:
        return render(request, "core/command_enter_invalid.html", status=403)

    User = get_user_model()
    user = User.objects.filter(is_superuser=True).order_by("pk").first()
    if not user:
        return render(request, "core/command_enter_invalid.html", status=403)

    auth_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    AuditLog.objects.create(
        actor=user,
        action="login_override",
        object_type="CommandCenter",
        object_repr=f"Command Center impersonation for {connection.schema_name}",
        reason=f"Command user: {data.get('command_username', 'unknown')}",
        ip_address=_client_ip(request),
    )
    return redirect("/")
