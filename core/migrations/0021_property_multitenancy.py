from django.db import migrations, models
import django.db.models.deletion


def create_default_property(apps, schema_editor):
    Property = apps.get_model("core", "Property")
    GuestHouseSettings = apps.get_model("core", "GuestHouseSettings")
    settings = GuestHouseSettings.objects.filter(pk=1).first()
    name = settings.guest_house_name if settings else "My Guest House"
    Property.objects.get_or_create(pk=1, defaults={"name": name})


def assign_existing_records(apps, schema_editor):
    Property = apps.get_model("core", "Property")
    prop = Property.objects.first()
    if not prop:
        return
    for model_name in ["Room", "Expense", "InventoryItem", "POSCategory", "POSItem", "POSSale", "POSShift"]:
        Model = apps.get_model("core", model_name)
        Model.objects.filter(prop__isnull=True).update(prop=prop)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0020_bookingrefund"),
    ]

    operations = [
        migrations.CreateModel(
            name="Property",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=200)),
                ("address", models.TextField(blank=True)),
                ("phone", models.CharField(blank=True, max_length=50)),
                ("email", models.EmailField(blank=True, max_length=254)),
                ("description", models.TextField(blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("sort_order", models.IntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"verbose_name_plural": "Properties", "ordering": ["sort_order", "name"]},
        ),
        migrations.RunPython(create_default_property, migrations.RunPython.noop),
        migrations.AddField(
            model_name="room",
            name="prop",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="rooms", to="core.property"),
        ),
        migrations.AddField(
            model_name="expense",
            name="prop",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="expenses", to="core.property"),
        ),
        migrations.AddField(
            model_name="inventoryitem",
            name="prop",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="inventory_items", to="core.property"),
        ),
        migrations.AddField(
            model_name="poscategory",
            name="prop",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="pos_categories", to="core.property"),
        ),
        migrations.AddField(
            model_name="positem",
            name="prop",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="pos_items", to="core.property"),
        ),
        migrations.AddField(
            model_name="possale",
            name="prop",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="pos_sales", to="core.property"),
        ),
        migrations.AddField(
            model_name="posshift",
            name="prop",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="pos_shifts", to="core.property"),
        ),
        migrations.RunPython(assign_existing_records, migrations.RunPython.noop),
    ]
