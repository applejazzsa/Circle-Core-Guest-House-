import datetime

from django.utils import timezone


GRACE_DAYS = 5
TRIAL_DAYS = 30


def trial_end(anchor=None):
    anchor = anchor or timezone.now()
    return anchor + datetime.timedelta(days=TRIAL_DAYS)


def first_day_of_next_month(value):
    if isinstance(value, datetime.datetime):
        value = timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    year = value.year + (1 if value.month == 12 else 0)
    month = 1 if value.month == 12 else value.month + 1
    return datetime.date(year, month, 1)


def _anniversary_next_year(value):
    if isinstance(value, datetime.datetime):
        value = timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    try:
        return value.replace(year=value.year + 1)
    except ValueError:
        return value.replace(year=value.year + 1, day=28)


def start_of_day(value):
    return timezone.make_aware(datetime.datetime.combine(value, datetime.time.min), timezone.get_current_timezone())


def next_renewal_datetime(billing_cycle, anchor=None):
    anchor = anchor or timezone.now()
    if billing_cycle == "annual":
        return start_of_day(_anniversary_next_year(anchor))
    return start_of_day(first_day_of_next_month(anchor))


def apply_paid_renewal(subscription, paid_at=None):
    paid_at = paid_at or timezone.now()
    subscription.status = "active"
    subscription.trial_ends_at = None
    subscription.expires_at = next_renewal_datetime(subscription.billing_cycle, paid_at)
    subscription.next_billing_date = subscription.expires_at.date()
    return subscription


def grace_ends_at(subscription):
    return subscription.control_grace_ends_at or (subscription.expires_at + datetime.timedelta(days=GRACE_DAYS))
