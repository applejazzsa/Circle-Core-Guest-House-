from django.shortcuts import redirect

from .roles import is_cleaner, is_operator, is_owner


class SubscriptionMiddleware:
    EXEMPT_PATHS = [
        "/login/",
        "/logout/",
        "/subscription/",
        "/trial/",
        "/static/",
        "/media/",
        "/api/",
        "/admin/",
    ]

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.user.is_authenticated:
            return self.get_response(request)

        if any(request.path.startswith(path) for path in self.EXEMPT_PATHS):
            return self.get_response(request)

        from .models import Subscription

        subscription = Subscription.objects.select_related("plan").first()
        if not subscription:
            return redirect("/subscription/setup/")

        if not subscription.is_active:
            return redirect("/subscription/expired/")

        if request.path.startswith("/expenses/") and not subscription.has_feature("expenses"):
            return redirect("/subscription/upgrade/?feature=expenses")

        if request.path.startswith("/inventory/") and not subscription.has_feature("inventory"):
            return redirect("/subscription/upgrade/?feature=inventory")

        if request.path.startswith("/maintenance/") and not subscription.has_feature("maintenance_requests"):
            return redirect("/subscription/upgrade/?feature=maintenance_requests")

        if request.path.startswith("/pos/") and not subscription.has_feature("pos_integration"):
            return redirect("/subscription/upgrade/?feature=pos_integration")

        request.subscription = subscription
        return self.get_response(request)


class RoleAccessMiddleware:
    EXEMPT_PATHS = [
        "/login/",
        "/logout/",
        "/static/",
        "/media/",
        "/api/",
        "/admin/",
        "/subscription/expired/",
        "/subscription/upgrade/",
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
        if not request.user.is_authenticated:
            return self.get_response(request)

        if any(request.path.startswith(path) for path in self.EXEMPT_PATHS):
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
