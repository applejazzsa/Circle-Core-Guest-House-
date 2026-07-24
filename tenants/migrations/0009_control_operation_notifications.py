import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('tenants', '0008_product_access_entitlement')]
    operations = [migrations.CreateModel(name='ControlOperationNotification', fields=[
        ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
        ('operation_id', models.UUIDField(unique=True)), ('action', models.CharField(max_length=80)),
        ('recipient', models.EmailField(blank=True, max_length=254)), ('behavior', models.CharField(max_length=30)),
        ('state', models.CharField(choices=[('queued', 'Queued'), ('suppressed', 'Suppressed'), ('sent', 'Sent'), ('failed', 'Failed')], max_length=20)),
        ('safe_payload', models.JSONField(blank=True, default=dict)), ('attempts', models.PositiveSmallIntegerField(default=0)),
        ('last_error_code', models.CharField(blank=True, max_length=80)), ('created_at', models.DateTimeField(auto_now_add=True)),
        ('sent_at', models.DateTimeField(blank=True, null=True)),
        ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='control_operation_notifications', to='tenants.guesthousetenant')),
    ])]
