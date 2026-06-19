from datetime import timedelta

from django.contrib.auth.backends import ModelBackend
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.db import connection, transaction
from django.utils import timezone


_DUMMY_PIN_HASH = make_password("000000")


class CaseInsensitiveModelBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        UserModel = get_user_model()
        if username is None:
            return None
        try:
            user = UserModel.objects.get(username__iexact=username)
        except UserModel.DoesNotExist:
            email_matches = UserModel.objects.filter(email__iexact=username)
            user = email_matches.first() if email_matches.count() == 1 else None
            if user is None:
                UserModel().set_password(password)
                return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None


class PhonePinBackend(ModelBackend):
    max_failed_attempts = 5
    lock_duration = timedelta(minutes=15)

    def authenticate(self, request, phone_number=None, pin=None, **kwargs):
        if not phone_number or pin is None or connection.schema_name == "public":
            return None

        from .models import StaffProfile

        normalized_phone = StaffProfile.normalize_phone(phone_number)
        now = timezone.now()
        with transaction.atomic():
            profile = (
                StaffProfile.objects.select_for_update()
                .select_related("user")
                .filter(phone_number=normalized_phone, user__is_active=True)
                .first()
            )
            if profile is None:
                check_password(str(pin), _DUMMY_PIN_HASH)
                return None

            if not profile.pin_enabled or not profile.pin_hash:
                check_password(str(pin), profile.pin_hash or _DUMMY_PIN_HASH)
                return None

            if profile.pin_locked_until and profile.pin_locked_until > now:
                check_password(str(pin), _DUMMY_PIN_HASH)
                return None

            if profile.pin_locked_until and profile.pin_locked_until <= now:
                profile.pin_failed_attempts = 0
                profile.pin_locked_until = None

            if profile.check_pin(pin):
                profile.pin_failed_attempts = 0
                profile.pin_locked_until = None
                profile.save(update_fields=["pin_failed_attempts", "pin_locked_until", "updated_at"])
                return profile.user if self.user_can_authenticate(profile.user) else None

            profile.pin_failed_attempts = min(
                self.max_failed_attempts,
                profile.pin_failed_attempts + 1,
            )
            if profile.pin_failed_attempts >= self.max_failed_attempts:
                profile.pin_locked_until = now + self.lock_duration
            profile.save(update_fields=["pin_failed_attempts", "pin_locked_until", "updated_at"])
        return None
