from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('tenants', '0010_activation_request_timestamp')]
    operations = [
        migrations.CreateModel(
            name='ControlDeliveryWorkerHeartbeat',
            fields=[
                ('name', models.CharField(default='control-delivery', max_length=40, primary_key=True, serialize=False)),
                ('last_seen_at', models.DateTimeField()),
                ('last_success_at', models.DateTimeField(blank=True, null=True)),
                ('last_error_code', models.CharField(blank=True, max_length=80)),
            ],
            options={'verbose_name': 'Control delivery worker heartbeat'},
        ),
    ]
