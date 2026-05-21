from django.db import models
from django.db.utils import OperationalError, ProgrammingError

from .models import GuestHouseSettings, InventoryItem, MaintenanceRequest, Property, Room, Subscription
from .roles import can_manage_business, can_manage_system, is_cleaner, is_operator, is_owner, primary_role


def guest_house_settings(request):
    try:
        settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
    except (OperationalError, ProgrammingError):
        settings_obj = None
    return {
        "guest_house_settings": settings_obj,
    }


def active_property_context(request):
    try:
        if not getattr(request, "user", None) or not request.user.is_authenticated:
            return {"active_property": None, "all_properties": []}
        prop_id = request.session.get("active_property_id")
        properties = list(Property.objects.filter(is_active=True).order_by("sort_order", "name"))
        active = None
        if prop_id:
            active = next((p for p in properties if p.pk == prop_id), None)
        if not active and properties:
            active = properties[0]
            request.session["active_property_id"] = active.pk
        return {"active_property": active, "all_properties": properties}
    except (OperationalError, ProgrammingError):
        return {"active_property": None, "all_properties": []}


def subscription_context(request):
    try:
        subscription = Subscription.objects.select_related("plan").first()
    except (OperationalError, ProgrammingError):
        subscription = None
    return {
        "subscription": subscription,
        "plan": subscription.plan if subscription else None,
    }


def inventory_context(request):
    try:
        low_stock_count = InventoryItem.objects.filter(is_active=True, current_stock__lte=models.F("minimum_stock")).count()
    except Exception:
        low_stock_count = 0
    return {"low_stock_inventory_count": low_stock_count}


def maintenance_context(request):
    try:
        urgent_count = MaintenanceRequest.objects.filter(
            status__in=["open", "in_progress"],
            priority="urgent",
        ).count()
    except Exception:
        urgent_count = 0
    return {"urgent_maintenance_count": urgent_count}


def staff_role_context(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {
            "user_role": "",
            "is_owner": False,
            "is_operator": False,
            "is_cleaner": False,
            "can_manage_business": False,
            "can_manage_system": False,
            "cleaning_notification_count": 0,
        }

    try:
        cleaning_count = Room.objects.filter(status="Cleaning", cleaning_status="Needs Cleaning").count()
    except Exception:
        cleaning_count = 0

    return {
        "user_role": primary_role(user),
        "is_owner": is_owner(user),
        "is_operator": is_operator(user),
        "is_cleaner": is_cleaner(user),
        "can_manage_business": can_manage_business(user),
        "can_manage_system": can_manage_system(user),
        "cleaning_notification_count": cleaning_count,
    }
