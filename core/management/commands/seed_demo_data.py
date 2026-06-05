"""
seed_demo_data — Auto-populates a tenant schema with realistic demo content.

Called automatically during trial registration so new accounts never start empty.
The dashboard feels alive from minute one.

Usage:
    python manage.py seed_demo_data --schema <schema_name>
"""

from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django_tenants.utils import schema_context

from tenants.models import GuestHouseTenant


class Command(BaseCommand):
    help = 'Seed a tenant schema with demo rooms, guests, bookings, and revenue data.'

    def add_arguments(self, parser):
        parser.add_argument('--schema', required=True, help='Tenant schema name.')
        parser.add_argument('--force', action='store_true',
                            help='Re-seed even if data already exists.')

    def handle(self, *args, **options):
        schema_name = options['schema']
        force = options['force']

        if not GuestHouseTenant.objects.filter(schema_name=schema_name).exists():
            raise CommandError(f'No tenant with schema "{schema_name}".')

        with schema_context(schema_name):
            self._seed(schema_name, force)

    def _seed(self, schema_name, force):
        from core.models import (Booking, Expense, Guest, GuestHouseSettings,
                                 Payment, Property, Room)

        # ── Guard ──────────────────────────────────────────────────────────
        if not force and Room.objects.exists():
            self.stdout.write(f'[{schema_name}] Demo data already present — skipping. Use --force to re-seed.')
            return

        if force and Room.objects.exists():
            self.stdout.write(f'[{schema_name}] --force: removing existing demo data before re-seeding...')
            Payment.objects.all().delete()
            Booking.objects.all().delete()
            Expense.objects.all().delete()
            Guest.objects.all().delete()
            Room.objects.all().delete()
            Property.objects.all().delete()

        today = timezone.localdate()

        # ── Settings ───────────────────────────────────────────────────────
        GuestHouseSettings.objects.filter(pk=1).update(
            phone='012 345 6789',
            email='info@myguesthouse.co.za',
            address='12 Hospitality Street\nPretoria\n0001\nSouth Africa',
            banking_details=(
                'Bank: First National Bank\n'
                'Account Name: My Guest House\n'
                'Account Number: 62812345678\n'
                'Branch Code: 250655\n'
                'Reference: Your booking reference'
            ),
            check_in_time='14:00:00',
            check_out_time='10:00:00',
            invoice_notes='Thank you for choosing us. We look forward to hosting you again.',
            receipt_notes='Payment received. Thank you for your business.',
        )

        # ── Property ───────────────────────────────────────────────────────
        prop, _ = Property.objects.get_or_create(
            name='Main Property',
            defaults={
                'address': '12 Hospitality Street, Pretoria',
                'is_active': True,
                'sort_order': 1,
            },
        )

        # ── Rooms ──────────────────────────────────────────────────────────
        rooms_data = [
            {
                'name': 'Room 101', 'room_type': 'Double',
                'price_per_night': Decimal('650.00'), 'max_guests': 2,
                'description': 'Comfortable double room with en-suite bathroom, TV and Wi-Fi.',
            },
            {
                'name': 'Room 102', 'room_type': 'Single',
                'price_per_night': Decimal('450.00'), 'max_guests': 1,
                'description': 'Cosy single room with shower, TV and desk.',
            },
            {
                'name': 'Room 201', 'room_type': 'Twin',
                'price_per_night': Decimal('750.00'), 'max_guests': 2,
                'description': 'Spacious twin room ideal for colleagues or friends.',
            },
            {
                'name': 'Family Suite', 'room_type': 'Family',
                'price_per_night': Decimal('1200.00'), 'max_guests': 4,
                'description': 'Full family suite with lounge area, two bedrooms and kitchenette.',
            },
            {
                'name': 'Deluxe Suite', 'room_type': 'Suite',
                'price_per_night': Decimal('1500.00'), 'max_guests': 2,
                'description': 'Premium suite with king bed, jacuzzi, private balcony and minibar.',
            },
        ]

        rooms = []
        for rd in rooms_data:
            room, _ = Room.objects.get_or_create(
                name=rd['name'],
                defaults={**rd, 'prop': prop, 'status': 'Available', 'cleaning_status': 'Clean'},
            )
            rooms.append(room)

        r101, r102, r201, family, deluxe = rooms

        # ── Guests ─────────────────────────────────────────────────────────
        guests_data = [
            {'first_name': 'Thabo', 'last_name': 'Molefe',
             'phone': '082 123 4567', 'email': 'thabo.molefe@gmail.com',
             'id_passport_number': '8801015800086'},
            {'first_name': 'Zanele', 'last_name': 'Dlamini',
             'phone': '073 987 6543', 'email': 'zanele.d@outlook.com',
             'id_passport_number': '9203220432081'},
            {'first_name': 'Mark', 'last_name': 'Williams',
             'phone': '061 456 7890', 'email': 'mark.williams@icloud.com'},
            {'first_name': 'Fatima', 'last_name': 'Khan',
             'phone': '082 111 2233', 'email': 'fatima.khan@gmail.com'},
            {'first_name': 'Sipho', 'last_name': 'Ndlovu',
             'phone': '076 333 4455', 'email': 'sipho.ndlovu@yahoo.com'},
            {'first_name': 'Sarah', 'last_name': 'Botha',
             'phone': '083 444 5566', 'email': 'sarah.botha@gmail.com'},
            {'first_name': 'Lungelo', 'last_name': 'Zulu',
             'phone': '071 555 6677', 'email': 'lungelo.z@webmail.co.za'},
            {'first_name': 'Priya', 'last_name': 'Naidoo',
             'phone': '064 777 8899', 'email': 'priya.naidoo@gmail.com'},
        ]

        guests = []
        for gd in guests_data:
            g, _ = Guest.objects.get_or_create(
                first_name=gd['first_name'], last_name=gd['last_name'],
                defaults=gd,
            )
            guests.append(g)

        thabo, zanele, mark, fatima, sipho, sarah, lungelo, priya = guests

        # ── Bookings (relative to today) ───────────────────────────────────
        def make_booking(guest, room, checkin_offset, nights, status,
                         source='Walk-in', deposit_pct=0.5, paid_full=False,
                         ref_suffix=''):
            checkin = today + timedelta(days=checkin_offset)
            checkout = checkin + timedelta(days=nights)
            rate = room.price_per_night
            total = rate * nights
            deposit = (total * Decimal(str(deposit_pct))).quantize(Decimal('0.01'))

            if paid_full:
                deposit_paid = total
                balance = Decimal('0.00')
            elif deposit_pct > 0:
                deposit_paid = deposit
                balance = total - deposit
            else:
                deposit_paid = Decimal('0.00')
                balance = total

            ref = f'REF{today.year}{today.month:02d}{ref_suffix}'

            b, created = Booking.objects.get_or_create(
                booking_reference=ref,
                defaults={
                    'guest': guest,
                    'room': room,
                    'check_in_date': checkin,
                    'check_out_date': checkout,
                    'num_guests': 2,
                    'rate_per_night': rate,
                    'total_amount': total,
                    'deposit_required': deposit,
                    'deposit_paid': deposit_paid,
                    'balance_due': balance,
                    'booking_source': source,
                    'status': status,
                },
            )

            if created and deposit_paid > 0:
                Payment.objects.create(
                    booking=b,
                    amount=deposit_paid,
                    payment_date=checkin - timedelta(days=1) if checkin > today else today,
                    payment_method='EFT',
                    payment_type='Payment',
                    reference=f'DEP-{ref}',
                    notes='Deposit payment',
                )

            if created and paid_full and balance > 0:
                Payment.objects.create(
                    booking=b,
                    amount=balance,
                    payment_date=checkin,
                    payment_method='Cash',
                    payment_type='Payment',
                    reference=f'FULL-{ref}',
                    notes='Balance on check-in',
                )

            return b

        # Currently checked in — been here 2 days, leaves in 2 days
        b1 = make_booking(thabo, r101, -2, 4, 'Checked In', source='Phone Call',
                          deposit_pct=0.5, ref_suffix='001')
        r101.status = 'Occupied'
        r101.save(update_fields=['status'])
        if b1.check_in_time is None:
            b1.check_in_time = timezone.now() - timedelta(days=2)
            b1.save(update_fields=['check_in_time'])

        # Arriving today — confirmed
        make_booking(zanele, r201, 0, 3, 'Confirmed', source='WhatsApp',
                     deposit_pct=0.5, ref_suffix='002')

        # Arriving in 3 days — confirmed
        make_booking(mark, r102, 3, 2, 'Confirmed', source='Booking.com',
                     deposit_pct=1.0, ref_suffix='003')

        # Checked out yesterday — room needs cleaning
        b4 = make_booking(fatima, deluxe, -3, 2, 'Checked Out', source='Website',
                          paid_full=True, ref_suffix='004')
        deluxe.status = 'Cleaning'
        deluxe.cleaning_status = 'Needs Cleaning'
        deluxe.save(update_fields=['status', 'cleaning_status'])
        if b4.check_in_time is None:
            b4.check_in_time = timezone.now() - timedelta(days=3)
            b4.check_out_time = timezone.now() - timedelta(days=1)
            b4.save(update_fields=['check_in_time', 'check_out_time'])

        # Pending future booking
        make_booking(sipho, family, 7, 5, 'Pending', source='Referral',
                     deposit_pct=0, ref_suffix='005')

        # Historical bookings — last month (for revenue)
        lm = -30
        make_booking(sarah, r101, lm, 3, 'Checked Out', paid_full=True, ref_suffix='006')
        make_booking(lungelo, deluxe, lm - 5, 2, 'Checked Out', paid_full=True, ref_suffix='007')
        make_booking(priya, family, lm - 2, 4, 'Checked Out', paid_full=True,
                     source='Airbnb', ref_suffix='008')
        make_booking(mark, r201, lm - 10, 2, 'Checked Out', paid_full=True,
                     source='Phone Call', ref_suffix='009')

        # Month before that
        lm2 = -60
        make_booking(thabo, family, lm2, 3, 'Checked Out', paid_full=True, ref_suffix='010')
        make_booking(zanele, deluxe, lm2 - 3, 2, 'Checked Out', paid_full=True, ref_suffix='011')
        make_booking(fatima, r101, lm2 - 7, 5, 'Checked Out', paid_full=True, ref_suffix='012')

        # ── Expenses ───────────────────────────────────────────────────────
        expenses = [
            (today - timedelta(days=2), 'Cleaning', 'Laundry service', 380, 'Fresh Laundry Co'),
            (today - timedelta(days=5), 'Utilities', 'Electricity bill', 1240, 'City Power'),
            (today - timedelta(days=8), 'Supplies', 'Toiletries & amenities', 850, 'Makro'),
            (today - timedelta(days=15), 'Maintenance', 'Plumbing repair Room 102', 650, 'A&B Plumbers'),
            (today - timedelta(days=20), 'Staff', 'Cleaner wages', 2800, 'Nomsa Khumalo'),
            (today - timedelta(days=32), 'Utilities', 'Water & rates', 890, 'Municipality'),
            (today - timedelta(days=35), 'Marketing', 'Social media ads', 500, 'Meta Ads'),
            (today - timedelta(days=40), 'Cleaning', 'Deep clean - all rooms', 1200, 'Pro Clean Services'),
            (today - timedelta(days=42), 'Staff', 'Cleaner wages', 2800, 'Nomsa Khumalo'),
            (today - timedelta(days=55), 'Supplies', 'Bed linen replacement', 3200, 'Hospitality Supplies SA'),
            (today - timedelta(days=60), 'Utilities', 'Electricity bill', 1180, 'City Power'),
            (today - timedelta(days=62), 'Staff', 'Cleaner wages', 2800, 'Nomsa Khumalo'),
        ]

        for exp_date, cat, desc, amt, paid_to in expenses:
            Expense.objects.get_or_create(
                date=exp_date,
                description=desc,
                defaults={
                    'prop': prop,
                    'category': cat,
                    'amount': Decimal(str(amt)),
                    'paid_to': paid_to,
                    'payment_method': 'EFT',
                },
            )

        # Reset engagement so demo rooms/guests/bookings don't inflate health score
        try:
            from core.models import TrialEngagement
            TrialEngagement.objects.filter(pk=1).update(
                rooms_added=0,
                guests_added=0,
                bookings_added=0,
                reports_viewed=0,
            )
        except Exception:
            pass

        self.stdout.write(self.style.SUCCESS(
            f'[{schema_name}] Demo data seeded: {len(rooms)} rooms, {len(guests)} guests, '
            f'12 bookings, {len(expenses)} expenses.'
        ))
