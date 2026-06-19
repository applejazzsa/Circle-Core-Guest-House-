import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("core", "0029_staffprofile_phone_pin_login"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(name="OfflineDevice", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("client_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
            ("label", models.CharField(max_length=120)), ("is_active", models.BooleanField(default=False)),
            ("lease_expires_at", models.DateTimeField(blank=True, null=True)), ("created_at", models.DateTimeField(auto_now_add=True)),
            ("last_seen_at", models.DateTimeField(blank=True, null=True)), ("revoked_at", models.DateTimeField(blank=True, null=True)),
            ("approved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="approved_offline_devices", to=settings.AUTH_USER_MODEL)),
            ("prop", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="offline_devices", to="core.property")),
            ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="offline_devices", to=settings.AUTH_USER_MODEL)),
        ], options={"ordering": ["-created_at"]}),
        migrations.CreateModel(name="OfflineOperation", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("operation_id", models.UUIDField()), ("operation_type", models.CharField(max_length=40)),
            ("payload", models.JSONField(default=dict)), ("payload_hash", models.CharField(max_length=64)),
            ("status", models.CharField(choices=[("applied", "Applied"), ("conflict", "Conflict"), ("rejected", "Rejected")], max_length=12)),
            ("result", models.JSONField(blank=True, default=dict)), ("error", models.CharField(blank=True, max_length=255)),
            ("client_created_at", models.DateTimeField()), ("processed_at", models.DateTimeField(auto_now_add=True)),
            ("device", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="operations", to="core.offlinedevice")),
        ], options={"ordering": ["-processed_at"]}),
        migrations.CreateModel(name="OfflineConflict", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("reason", models.CharField(max_length=255)), ("server_state", models.JSONField(blank=True, default=dict)),
            ("is_resolved", models.BooleanField(default=False)), ("resolution", models.CharField(blank=True, max_length=30)),
            ("resolved_at", models.DateTimeField(blank=True, null=True)),
            ("operation", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="conflict", to="core.offlineoperation")),
            ("resolved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
        ]),
        migrations.AddConstraint(model_name="offlineoperation", constraint=models.UniqueConstraint(fields=("device", "operation_id"), name="unique_offline_device_operation")),
    ]
