import uuid
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('core', '0035_vehicle_guest_profiles')]
    operations = [
        migrations.AddField(model_name='subscription', name='control_grace_ends_at', field=models.DateTimeField(blank=True, null=True)),
        migrations.CreateModel(
            name='ControlManualPayment',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12)), ('currency', models.CharField(max_length=3)),
                ('payment_date', models.DateField()), ('payment_method_category', models.CharField(max_length=40)),
                ('internal_reference', models.CharField(max_length=100, unique=True)), ('invoice_reference', models.CharField(blank=True, max_length=100)),
                ('coverage_start', models.DateField(blank=True, null=True)), ('coverage_end', models.DateField(blank=True, null=True)),
                ('notes', models.TextField(blank=True)), ('evidence_metadata', models.JSONField(blank=True, default=dict)),
                ('activate_after_payment', models.BooleanField(default=False)), ('next_billing_date', models.DateField(blank=True, null=True)),
                ('status', models.CharField(choices=[('recorded', 'Recorded'), ('pending_verification', 'Pending verification'), ('verified', 'Verified'), ('rejected', 'Rejected'), ('reversed', 'Reversed')], default='pending_verification', max_length=30)),
                ('recorded_by', models.CharField(max_length=200)), ('operation_id', models.UUIDField(unique=True)), ('created_at', models.DateTimeField(auto_now_add=True)),
                ('subscription', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='control_manual_payments', to='core.subscription')),
            ],
            options={'ordering': ['-created_at']},
        ),
    ]
