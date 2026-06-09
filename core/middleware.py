from django.conf import settings
from django.db import connection
from django.shortcuts import redirect, render
from django.utils import timezone

from .roles import is_cleaner, is_operator, is_owner
from .subscriptions import grace_ends_at

# Paths that are always accessible regardless of subscription state
_ALWAYS_EXEMPT = [
    "/login/",
    "/logout/",
    "/static/",
    "/media/",
    "/admin/",
    "/command/",
]

# Paths exempt from subscription checks but not from read-only blocking
_SUBSCRIPTION_EXEMPT = [
    "/subscription/",
    "/trial/",
    "/api/",
    "/payfast/",
]


def _admin_path():
    return "/" + getattr(settings, "ADMIN_URL", "admin/").lstrip("/")


class SubscriptionMiddleware:

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if connection.schema_name == 'public':
            return self.get_response(request)

        if not request.user.is_authenticated:
            return self.get_response(request)

        if any(request.path.startswith(p) for p in [*_ALWAYS_EXEMPT, _admin_path()]):
            return self.get_response(request)

        if any(request.path.startswith(p) for p in _SUBSCRIPTION_EXEMPT):
            return self.get_response(request)

        from .models import Subscription

        subscription = Subscription.objects.select_related("plan").first()
        if not subscription:
            return redirect("/subscription/setup/")

        now = timezone.now()
        grace_until = grace_ends_at(subscription)

        # Determine subscription state
        if subscription.status == "suspended":
            state = "suspended"
        elif subscription.status in ("active", "trial") and now < subscription.expires_at:
            state = "active"
        elif subscription.status == "cancelled":
            state = "cancelled"
        elif now < grace_until:
            state = "grace"
        else:
            state = "read_only"

        request.subscription = subscription
        request.subscription_state = state

        if state == "suspended":
            return render(request, "subscription/cancelled.html",
                          {"subscription": subscription, "suspended": True}, status=402)

        if state == "cancelled":
            return render(request, "subscription/cancelled.html",
                          {"subscription": subscription}, status=402)

        if state == "grace":
            request.grace_days_left = max(0, int((grace_until - now).total_seconds() / 86400))

        if state == "read_only":
            # Block all write operations — allow GETs so data is viewable
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                return render(request, "subscription/read_only.html",
                              {"subscription": subscription}, status=403)

        # Feature gating (only when fully active or in grace)
        if state in ("active", "grace", "trial"):
            if request.path.startswith("/expenses/") and not subscription.has_feature("expenses"):
                return redirect("/subscription/upgrade/?feature=expenses")
            if request.path.startswith("/inventory/") and not subscription.has_feature("inventory"):
                return redirect("/subscription/upgrade/?feature=inventory")
            if request.path.startswith("/maintenance/") and not subscription.has_feature("maintenance_requests"):
                return redirect("/subscription/upgrade/?feature=maintenance_requests")
            if request.path.startswith("/pos/") and not subscription.has_feature("pos_integration"):
                return redirect("/subscription/upgrade/?feature=pos_integration")

        return self.get_response(request)


class RoleAccessMiddleware:
    EXEMPT_PATHS = [
        "/login/",
        "/logout/",
        "/static/",
        "/media/",
        "/api/",
        "/admin/",
        "/subscription/",
        "/payfast/",
        "/command/",
    ]

    CLEANER_ALLOWED_PREFIXES = [
        "/cleaning/",
        "/housekeeping/",
        "/maintenance/",
    ]

    CLEANER_BLOCKED_ROOM_SUFFIXES = [
        "/add/",
        "/edit/",
        "/delete/",
    ]
    CLEANER_BLOCKED_MAINTENANCE_PARTS = [
        "/add/",
        "/edit/",
    ]

    OWNER_ONLY_PREFIXES = [
        "/settings/",
        "/subscription/plans/",
        "/subscription/setup/",
        "/subscription/status/",
        "/staff/",
    ]

    OPERATOR_BLOCKED_PREFIXES = [
        "/security/",
        "/daily-close/",
        "/export/",
    ]

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if connection.schema_name == 'public':
            return self.get_response(request)

        if not request.user.is_authenticated:
            return self.get_response(request)

        if any(request.path.startswith(path) for path in [*self.EXEMPT_PATHS, _admin_path()]):
            return self.get_response(request)

        if is_cleaner(request.user):
            if request.path == "/":
                return redirect("/cleaning/")
            if not any(request.path.startswith(path) for path in self.CLEANER_ALLOWED_PREFIXES):
                return redirect("/cleaning/")
            if request.path.startswith("/rooms/") and any(
                request.path.endswith(suffix) for suffix in self.CLEANER_BLOCKED_ROOM_SUFFIXES
            ):
                return redirect("/cleaning/")
            if request.path.startswith("/maintenance/") and any(
                part in request.path for part in self.CLEANER_BLOCKED_MAINTENANCE_PARTS
            ):
                return redirect("/maintenance/")

        if not is_cleaner(request.user) and is_operator(request.user):
            if any(request.path.startswith(path) for path in self.OPERATOR_BLOCKED_PREFIXES):
                return redirect("/")

        if any(request.path.startswith(path) for path in self.OWNER_ONLY_PREFIXES) and not is_owner(request.user):
            return redirect("/")

        return self.get_response(request)
