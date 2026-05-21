from django.core.management.base import BaseCommand
from core.models import POSCategory, POSItem


class Command(BaseCommand):
    help = 'Seed default POS categories and items'

    def handle(self, *args, **options):
        created_count = 0

        categories_data = [
            {'name': 'Beverages', 'icon': '🥤', 'color': '#3b82f6', 'sort_order': 1},
            {'name': 'Snacks', 'icon': '🍕', 'color': '#f59e0b', 'sort_order': 2},
            {'name': 'Alcohol', 'icon': '🍺', 'color': '#8b5cf6', 'sort_order': 3},
            {'name': 'Toiletries', 'icon': '🧴', 'color': '#06b6d4', 'sort_order': 4},
            {'name': 'Room Extras', 'icon': '🛁', 'color': '#22c55e', 'sort_order': 5},
        ]

        categories = {}
        for cat_data in categories_data:
            cat, created = POSCategory.objects.get_or_create(
                name=cat_data['name'],
                defaults=cat_data,
            )
            categories[cat.name] = cat
            if created:
                self.stdout.write(f"  Created category: {cat.name}")

        items_data = [
            # Beverages
            {'name': 'Coca-Cola', 'category': 'Beverages', 'price': 25, 'emoji': '🥤', 'is_quick_item': True, 'sort_order': 1},
            {'name': 'Still Water', 'category': 'Beverages', 'price': 15, 'emoji': '💧', 'is_quick_item': True, 'sort_order': 2},
            {'name': 'Coffee', 'category': 'Beverages', 'price': 30, 'emoji': '☕', 'is_quick_item': True, 'sort_order': 3},
            {'name': 'Tea', 'category': 'Beverages', 'price': 20, 'emoji': '🍵', 'is_quick_item': True, 'sort_order': 4},
            {'name': 'Orange Juice', 'category': 'Beverages', 'price': 30, 'emoji': '🍊', 'is_quick_item': True, 'sort_order': 5},
            {'name': 'Sparkling Water', 'category': 'Beverages', 'price': 20, 'emoji': '🫧', 'is_quick_item': False, 'sort_order': 6},
            {'name': 'Energy Drink', 'category': 'Beverages', 'price': 40, 'emoji': '⚡', 'is_quick_item': False, 'sort_order': 7},
            # Snacks
            {'name': 'Chips', 'category': 'Snacks', 'price': 20, 'emoji': '🍟', 'is_quick_item': True, 'sort_order': 1},
            {'name': 'Chocolate', 'category': 'Snacks', 'price': 25, 'emoji': '🍫', 'is_quick_item': True, 'sort_order': 2},
            {'name': 'Biscuits', 'category': 'Snacks', 'price': 18, 'emoji': '🍪', 'is_quick_item': True, 'sort_order': 3},
            {'name': 'Muffin', 'category': 'Snacks', 'price': 30, 'emoji': '🧁', 'is_quick_item': False, 'sort_order': 4},
            {'name': 'Sandwich', 'category': 'Snacks', 'price': 45, 'emoji': '🥪', 'is_quick_item': False, 'sort_order': 5},
            # Alcohol
            {'name': 'Beer', 'category': 'Alcohol', 'price': 45, 'emoji': '🍺', 'is_quick_item': True, 'sort_order': 1},
            {'name': 'Wine (Glass)', 'category': 'Alcohol', 'price': 65, 'emoji': '🍷', 'is_quick_item': True, 'sort_order': 2},
            {'name': 'Cider', 'category': 'Alcohol', 'price': 40, 'emoji': '🍻', 'is_quick_item': True, 'sort_order': 3},
            {'name': 'Whiskey Shot', 'category': 'Alcohol', 'price': 55, 'emoji': '🥃', 'is_quick_item': False, 'sort_order': 4},
            # Toiletries
            {'name': 'Toothbrush', 'category': 'Toiletries', 'price': 35, 'emoji': '🪥', 'is_quick_item': True, 'sort_order': 1},
            {'name': 'Toothpaste', 'category': 'Toiletries', 'price': 40, 'emoji': '🧴', 'is_quick_item': True, 'sort_order': 2},
            {'name': 'Shampoo', 'category': 'Toiletries', 'price': 45, 'emoji': '🧴', 'is_quick_item': False, 'sort_order': 3},
            {'name': 'Soap', 'category': 'Toiletries', 'price': 25, 'emoji': '🧼', 'is_quick_item': True, 'sort_order': 4},
            # Room Extras
            {'name': 'Extra Towel', 'category': 'Room Extras', 'price': 20, 'emoji': '🛁', 'is_quick_item': True, 'sort_order': 1},
            {'name': 'Parking (Day)', 'category': 'Room Extras', 'price': 50, 'emoji': '🚗', 'is_quick_item': True, 'sort_order': 2},
            {'name': 'Late Checkout', 'category': 'Room Extras', 'price': 150, 'emoji': '🔑', 'is_quick_item': True, 'sort_order': 3},
            {'name': 'Laundry', 'category': 'Room Extras', 'price': 80, 'emoji': '👕', 'is_quick_item': False, 'sort_order': 4},
        ]

        for item_data in items_data:
            cat_name = item_data.pop('category')
            cat = categories.get(cat_name)
            item, created = POSItem.objects.get_or_create(
                name=item_data['name'],
                defaults={**item_data, 'category': cat},
            )
            if created:
                created_count += 1
                self.stdout.write(f"  Created item: {item.name} (R{item.price})")

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. {created_count} POS items created."
        ))
