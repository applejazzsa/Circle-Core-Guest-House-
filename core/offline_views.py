import datetime
import hashlib
import json
import uuid
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_POST

from .models import Booking, DailyCloseLock, Guest, GuestHouseSettings, MaintenanceRequest, OfflineConflict, OfflineDevice, OfflineOperation, Payment, Property, Room, Subscription
from .roles import is_cleaner, is_manager, is_owner, is_reception, is_viewer


LEASE_HOURS = 72
ALLOWED_OPERATIONS = {"walk_in", "check_out", "cleaning", "maintenance", "cash_payment"}


class SyncConflict(Exception):
    def __init__(self, message, state=None):
        self.message = message
        self.state = state or {}


def _active_property(request):
    prop_id = request.session.get("active_property_id")
    return Property.objects.filter(pk=prop_id, is_active=True).first() or Property.objects.filter(is_active=True).order_by("sort_order", "pk").first()


def _device(request, client_id, active=True):
    try:
        client_id = uuid.UUID(str(client_id))
    except (TypeError, ValueError):
        return None
    query = OfflineDevice.objects.filter(client_id=client_id, prop=_active_property(request), user=request.user)
    if active:
        query = query.filter(is_active=True, revoked_at__isnull=True)
    return query.first()


def _lease(device):
    expires = timezone.now() + datetime.timedelta(hours=LEASE_HOURS)
    device.lease_expires_at = expires
    device.last_seen_at = timezone.now()
    device.save(update_fields=["lease_expires_at", "last_seen_at"])
    token = signing.dumps({"device": str(device.client_id), "user": device.user_id, "prop": device.prop_id, "expires": expires.isoformat()}, salt="offline-device")
    return token, expires


def _verify_lease(device, token, client_created_at):
    try:
        data = signing.loads(token, salt="offline-device")
        expires = parse_datetime(data["expires"])
    except (signing.BadSignature, KeyError, TypeError, ValueError) as exc:
        raise SyncConflict("Offline authorization is invalid. Reconnect and enroll this device again.") from exc
    if data.get("device") != str(device.client_id) or data.get("user") != device.user_id or data.get("prop") != device.prop_id:
        raise SyncConflict("Offline authorization does not match this device.")
    if client_created_at > expires:
        raise SyncConflict("This action was created after offline access expired.")
    if client_created_at > timezone.now() + datetime.timedelta(minutes=10):
        raise SyncConflict("The device clock is too far ahead.")


def _operation_allowed(user, operation_type):
    if is_owner(user) or is_manager(user):
        return True
    if is_reception(user):
        return operation_type in {"walk_in", "check_out", "cleaning", "maintenance"}
    if is_cleaner(user):
        return operation_type in {"cleaning", "maintenance"}
    if is_viewer(user):
        return False
    return False


def _serialize_state(prop):
    rooms = []
    for room in Room.objects.filter(prop=prop):
        rooms.append({
            "id": room.pk, "name": room.name, "type": room.room_type, "status": room.status,
            "cleaning_status": room.cleaning_status, "max_guests": room.max_guests,
            "rates": {key: str(room.get_price_for_duration(key) or "") for key in ("1_hour", "2_hours", "3_hours", "5_hours", "daily")},
        })
    settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
    bookings = Booking.objects.filter(room__prop=prop, status__in=["Pending", "Confirmed", "Checked In"]).select_related("room", "guest")
    booking_rows = []
    current_timezone = timezone.get_current_timezone()
    for booking in bookings:
        if booking.is_hourly:
            _, checkout_naive = booking._booking_window()
        else:
            checkout_naive = datetime.datetime.combine(booking.check_out_date, settings_obj.check_out_time)
        checkout_at = timezone.make_aware(checkout_naive, current_timezone).isoformat() if checkout_naive else None
        booking_rows.append({
            "id": booking.pk,
            "reference": booking.booking_reference,
            "room_id": booking.room_id,
            "room": booking.room.name,
            "guest": booking.guest.full_name,
            "vehicle_registration": booking.vehicle_registration,
            "status": booking.status,
            "balance": str(booking.balance_due),
            "checkout_at": checkout_at,
        })
    return {
        "rooms": rooms,
        "bookings": booking_rows,
        "guests": list(Guest.objects.filter(is_generic=False).values("id", "first_name", "last_name", "phone")[:500]),
    }


@login_required
def offline_console(request):
    return render(request, "core/offline_console.html")


@login_required
@require_POST
def offline_enroll(request):
    try:
        data = json.loads(request.body or "{}")
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({"error": "Invalid request."}, status=400)
    try:
        client_id = uuid.UUID(data.get("client_id", ""))
    except (TypeError, ValueError):
        return JsonResponse({"error": "Invalid device identifier."}, status=400)
    prop = _active_property(request)
    device, created = OfflineDevice.objects.get_or_create(client_id=client_id, defaults={"prop": prop, "user": request.user, "label": (data.get("label") or "Reception device")[:120]})
    if not created and (device.prop_id != prop.pk or device.user_id != request.user.pk):
        return JsonResponse({"error": "This device belongs to another account or property."}, status=403)
    if is_owner(request.user) and not device.is_active:
        OfflineDevice.objects.filter(prop=prop, is_active=True).exclude(pk=device.pk).update(is_active=False, revoked_at=timezone.now())
        device.is_active = True
        device.approved_by = request.user
        device.revoked_at = None
        device.save(update_fields=["is_active", "approved_by", "revoked_at"])
    return JsonResponse({"status": "active" if device.is_active else "pending", "device_id": str(device.client_id)})


@login_required
@require_GET
def offline_bootstrap(request):
    device = _device(request, request.GET.get("device_id", ""))
    if not device:
        return JsonResponse({"error": "Device is not approved for offline access."}, status=403)
    token, expires = _lease(device)
    state = _serialize_state(device.prop)
    state.update({"lease": token, "lease_expires": expires.isoformat(), "server_time": timezone.now().isoformat(), "property": {"id": device.prop_id, "name": device.prop.name}})
    return JsonResponse(state)


def _apply_operation(request, device, operation_type, payload, occurred_at):
    prop = device.prop
    if operation_type == "walk_in":
        room = Room.objects.select_for_update().get(pk=payload.get("room_id"), prop=prop)
        if room.status != "Available" or room.cleaning_status != "Clean":
            raise SyncConflict(f"{room.name} is no longer available and clean.", {"room_status": room.status, "cleaning_status": room.cleaning_status})
        duration = payload.get("duration")
        rate = room.get_price_for_duration(duration)
        if not rate or str(rate) != str(payload.get("rate")):
            raise SyncConflict("The room rate changed while this device was offline.", {"current_rate": str(rate or "")})
        local_time = timezone.localtime(occurred_at)
        check_in = local_time.date()
        hourly = duration in ("1_hour", "2_hours", "3_hours", "5_hours")
        identity = payload.get("identity_mode", "walk_in")
        vehicle_registration = (payload.get("vehicle_registration") or "")[:20].strip().upper()
        if identity == "existing":
            guest = Guest.objects.filter(pk=payload.get("guest_id"), is_generic=False).first()
            if guest and guest.vehicle_registration:
                vehicle_registration = guest.vehicle_registration
        elif identity == "plate" and vehicle_registration:
            guest = Guest.get_or_create_for_vehicle(vehicle_registration)
        else:
            guest = Guest.get_generic()
        if not guest:
            raise SyncConflict("The selected guest no longer exists.")
        booking = Booking(guest=guest, room=room, check_in_date=check_in, check_out_date=check_in if hourly else check_in + datetime.timedelta(days=1), booking_duration_type=duration, booking_start_time=local_time.time().replace(second=0, microsecond=0) if hourly else None, num_guests=max(1, min(int(payload.get("num_guests", 1)), room.max_guests)), rate_per_night=rate, booking_source="Walk-in", status="Checked In", vehicle_registration=vehicle_registration, check_in_time=occurred_at)
        booking.save()
        room.status = "Occupied"; room.save(update_fields=["status"])
        return {"booking_id": booking.pk, "reference": booking.booking_reference}
    if operation_type == "check_out":
        booking = Booking.objects.select_for_update().select_related("room").get(pk=payload.get("booking_id"), room__prop=prop)
        if booking.status != "Checked In": raise SyncConflict("Booking is no longer checked in.", {"status": booking.status})
        booking.status = "Checked Out"; booking.check_out_time = occurred_at; booking.save()
        booking.room.status = "Cleaning"; booking.room.cleaning_status = "Needs Cleaning"; booking.room.save(update_fields=["status", "cleaning_status"])
        return {"booking_id": booking.pk}
    if operation_type == "cleaning":
        room = Room.objects.select_for_update().get(pk=payload.get("room_id"), prop=prop)
        status = payload.get("status")
        if status not in dict(Room.CLEANING_STATUS_CHOICES): raise ValidationError("Invalid cleaning status.")
        if status == "Clean" and room.status == "Occupied": raise SyncConflict("An occupied room cannot be marked clean.")
        room.cleaning_status = status
        if status == "Clean" and room.status == "Cleaning": room.status = "Available"
        room.save(update_fields=["cleaning_status", "status"])
        return {"room_id": room.pk, "status": room.status, "cleaning_status": status}
    if operation_type == "maintenance":
        room = Room.objects.get(pk=payload.get("room_id"), prop=prop)
        category, priority = payload.get("category", "other"), payload.get("priority", "medium")
        if category not in dict(MaintenanceRequest.CATEGORY_CHOICES) or priority not in dict(MaintenanceRequest.PRIORITY_CHOICES): raise ValidationError("Invalid maintenance category or priority.")
        item = MaintenanceRequest.objects.create(room=room, category=category, priority=priority, title=(payload.get("title") or "Offline maintenance report")[:200], description=payload.get("description") or "Reported while offline", reported_by=request.user, block_room_until_resolved=bool(payload.get("block_room")))
        return {"maintenance_id": item.pk}
    if operation_type == "cash_payment":
        booking = Booking.objects.select_for_update().get(pk=payload.get("booking_id"), room__prop=prop)
        if DailyCloseLock.objects.filter(close_date=timezone.localtime(occurred_at).date()).exists(): raise SyncConflict("The payment date is locked by daily close.")
        try: amount = Decimal(str(payload.get("amount")))
        except InvalidOperation as exc: raise ValidationError("Invalid payment amount.") from exc
        if amount <= 0 or booking.balance_due <= 0: raise SyncConflict("This booking cannot accept that payment.", {"balance": str(booking.balance_due)})
        payment = Payment.objects.create(booking=booking, amount=min(amount, booking.balance_due), payment_date=timezone.localtime(occurred_at).date(), payment_method="Cash", payment_type="Payment", notes="Recorded offline")
        return {"payment_id": payment.pk, "amount": str(payment.amount)}
    raise ValidationError("Unsupported offline operation.")


@login_required
@require_POST
def offline_sync(request):
    try:
        data = json.loads(request.body or "{}")
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({"error": "Invalid request."}, status=400)
    device = _device(request, data.get("device_id", ""))
    if not device: return JsonResponse({"error": "Device is not approved."}, status=403)
    subscription = Subscription.objects.first()
    if not subscription or subscription.status not in ("active", "trial") or subscription.expires_at <= timezone.now():
        return JsonResponse({"error": "Subscription changes cannot synchronize until the account is active."}, status=402)
    results = []
    for row in data.get("operations", [])[:100]:
        operation_id = row.get("id", "")
        try:
            operation_uuid = uuid.UUID(str(operation_id))
        except (TypeError, ValueError):
            results.append({"id": operation_id, "status": "rejected", "result": {}, "error": "Invalid operation identifier."})
            continue
        payload = row.get("payload") or {}
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        existing = OfflineOperation.objects.filter(device=device, operation_id=operation_uuid).first()
        if existing:
            if existing.payload_hash != payload_hash:
                results.append({"id": operation_id, "status": "rejected", "result": {}, "error": "Operation ID was reused with different data."})
            else:
                results.append({"id": operation_id, "status": existing.status, "result": existing.result, "error": existing.error})
            continue
        occurred_at = parse_datetime(row.get("created_at", ""))
        status, result, error, conflict_state = "rejected", {}, "Invalid operation.", {}
        try:
            if row.get("type") not in ALLOWED_OPERATIONS or not occurred_at: raise ValidationError("Invalid operation.")
            if not _operation_allowed(request.user, row["type"]): raise ValidationError("Your role cannot perform this offline action.")
            _verify_lease(device, data.get("lease", ""), occurred_at)
            with transaction.atomic(): result = _apply_operation(request, device, row["type"], payload, occurred_at)
            status, error = "applied", ""
        except SyncConflict as exc:
            status, error, conflict_state = "conflict", exc.message, exc.state
        except (ValidationError, Room.DoesNotExist, Booking.DoesNotExist, ValueError, TypeError) as exc:
            error = "; ".join(getattr(exc, "messages", [str(exc)]))[:255]
        operation = OfflineOperation.objects.create(device=device, operation_id=operation_uuid, operation_type=row.get("type", "")[:40], payload=payload, payload_hash=payload_hash, status=status, result=result, error=error, client_created_at=occurred_at or timezone.now())
        if status == "conflict": OfflineConflict.objects.create(operation=operation, reason=error, server_state=conflict_state)
        results.append({"id": operation_id, "status": status, "result": result, "error": error})
    device.last_seen_at = timezone.now(); device.save(update_fields=["last_seen_at"])
    return JsonResponse({"results": results, "state": _serialize_state(device.prop), "server_time": timezone.now().isoformat()})


@login_required
def offline_management(request):
    if not is_owner(request.user): return redirect("core:home")
    prop = _active_property(request)
    if request.method == "POST":
        device = get_object_or_404(OfflineDevice, pk=request.POST.get("device_id"), prop=prop)
        action = request.POST.get("action")
        if action == "approve":
            OfflineDevice.objects.filter(prop=prop, is_active=True).exclude(pk=device.pk).update(is_active=False, revoked_at=timezone.now())
            device.is_active=True; device.revoked_at=None; device.approved_by=request.user; device.save(update_fields=["is_active", "revoked_at", "approved_by"])
        elif action == "revoke":
            device.is_active=False; device.revoked_at=timezone.now(); device.save(update_fields=["is_active", "revoked_at"])
        return redirect("core:offline_management")
    conflicts = OfflineConflict.objects.filter(operation__device__prop=prop, is_resolved=False).select_related("operation", "operation__device")
    return render(request, "core/offline_management.html", {"devices": OfflineDevice.objects.filter(prop=prop).select_related("user"), "conflicts": conflicts})


@login_required
@require_POST
def offline_conflict_resolve(request, pk):
    if not is_owner(request.user): return redirect("core:home")
    conflict = get_object_or_404(OfflineConflict, pk=pk, operation__device__prop=_active_property(request))
    conflict.is_resolved=True; conflict.resolution="keep_server"; conflict.resolved_by=request.user; conflict.resolved_at=timezone.now(); conflict.save()
    return redirect("core:offline_management")
