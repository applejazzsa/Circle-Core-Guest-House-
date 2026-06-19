from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


ROLES = ["Owner", "Manager", "Reception", "Cleaner", "Viewer", "Operator"]


def create_roles_and_profiles(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("auth", "User")
    StaffProfile = apps.get_model("core", "StaffProfile")

    groups = {name: Group.objects.get_or_create(name=name)[0] for name in ROLES}
    for user in User.objects.all().prefetch_related("groups"):
        role = "Owner" if user.is_superuser else next(
            (group.name for group in user.groups.all() if group.name in ROLES),
            "Viewer",
        )
        StaffProfile.objects.get_or_create(user=user, defaults={"role": role})
        if user.is_superuser:
            user.groups.add(groups["Owner"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0028_remove_pos"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="StaffProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("phone_number", models.CharField(blank=True, max_length=20, null=True, unique=True)),
                ("pin_hash", models.CharField(blank=True, max_length=128)),
                ("pin_enabled", models.BooleanField(default=False)),
                ("pin_failed_attempts", models.PositiveSmallIntegerField(default=0)),
                ("pin_locked_until", models.DateTimeField(blank=True, null=True)),
                ("role", models.CharField(choices=[("Owner", "Owner / Admin"), ("Manager", "Manager"), ("Reception", "Reception"), ("Cleaner", "Cleaner"), ("Viewer", "Viewer"), ("Operator", "Operator (legacy)")], default="Viewer", max_length=20)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="staff_profile", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["user__username"]},
        ),
        migrations.RunPython(create_roles_and_profiles, migrations.RunPython.noop),
    ]
