from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('core', '0036_tenant_action_operations'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.CreateModel(
            name='ControlUserSecurity',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('force_password_reset', models.BooleanField(default=False)), ('password_hash_at_force', models.CharField(blank=True, max_length=128)),
                ('forced_at', models.DateTimeField(blank=True, null=True)), ('locked_at', models.DateTimeField(blank=True, null=True)),
                ('lock_reason', models.CharField(blank=True, max_length=200)), ('unlocked_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='control_security', to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
