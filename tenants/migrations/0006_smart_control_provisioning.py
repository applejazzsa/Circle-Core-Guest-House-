import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('tenants', '0005_guesthousetenant_notes_internal')]

    operations = [
        migrations.AddField(
            model_name='guesthousetenant',
            name='smart_control_reference',
            field=models.UUIDField(blank=True, editable=False, null=True, unique=True),
        ),
        migrations.CreateModel(
            name='ControlActivationOutbox',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('user_id', models.PositiveBigIntegerField()),
                ('recipient', models.EmailField(max_length=254)),
                ('state', models.CharField(choices=[('pending', 'Pending'), ('sent', 'Sent'), ('failed', 'Failed')], db_index=True, default='pending', max_length=20)),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('last_error_code', models.CharField(blank=True, max_length=80)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='control_activation_messages', to='tenants.guesthousetenant')),
            ],
            options={
                'ordering': ['created_at'],
                'constraints': [models.UniqueConstraint(fields=('tenant', 'user_id'), name='unique_guest_control_activation')],
            },
        ),
    ]
