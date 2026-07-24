"""
Engagement tracking signals.

Fires on login, room creation, guest creation, and booking creation.
Increments the singleton TrialEngagement record for the current tenant schema.
Guards against the public schema and swallows all exceptions to remain non-fatal.
"""

from django.contrib.auth.signals import user_logged_in
from django.db.models.signals import post_save
from django.dispatch import receiver


def _increment(field):
    """Atomically increment one field on the TrialEngagement singleton."""
    from django.db import connection
    if connection.schema_name == "public":
        return
    try:
        from django.db.models import F
        from django.utils import timezone
        from core.models import TrialEngagement

        now = timezone.now()
        updated = TrialEngagement.objects.filter(pk=1).update(
            **{field: F(field) + 1},
            last_activity_at=now,
        )
        if updated == 0:
            # Row doesn't exist yet — create it
            TrialEngagement.objects.get_or_create(pk=1)
            TrialEngagement.objects.filter(pk=1).update(
                **{field: F(field) + 1},
                last_activity_at=now,
            )
    except Exception:
        pass


@receiver(user_logged_in)
def track_login(sender, request, user, **kwargs):
    from django.db import connection
    if connection.schema_name == "public":
        return
    try:
        from django.db.models import F
        from django.utils import timezone
        from core.models import ControlUserSecurity, TrialEngagement

        now = timezone.now()
        security, _ = ControlUserSecurity.objects.get_or_create(user=user)
        security.last_successful_login = now
        security.failed_login_count = 0
        security.save(update_fields=['last_successful_login', 'failed_login_count', 'updated_at'])
        updated = TrialEngagement.objects.filter(pk=1).update(
            login_count=F("login_count") + 1,
            last_login_at=now,
            last_activity_at=now,
        )
        if updated == 0:
            TrialEngagement.objects.get_or_create(pk=1)
            TrialEngagement.objects.filter(pk=1).update(
                login_count=F("login_count") + 1,
                last_login_at=now,
                last_activity_at=now,
            )
    except Exception:
        pass


@receiver(post_save, sender="auth.User")
def ensure_staff_profile(sender, instance, created, **kwargs):
    from django.db import connection
    if connection.schema_name == "public":
        return
    try:
        from core.models import StaffProfile
        role = "Owner" if instance.is_superuser else "Viewer"
        StaffProfile.objects.get_or_create(user=instance, defaults={"role": role})
    except Exception:
        # Auth tables are created before core migrations on a new tenant.
        pass


@receiver(post_save, sender="core.Room")
def track_room_created(sender, instance, created, **kwargs):
    if created:
        _increment("rooms_added")


@receiver(post_save, sender="core.Guest")
def track_guest_created(sender, instance, created, **kwargs):
    if created:
        _increment("guests_added")


@receiver(post_save, sender="core.Booking")
def track_booking_created(sender, instance, created, **kwargs):
    if created:
        _increment("bookings_added")
