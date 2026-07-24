from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('tenants', '0009_control_operation_notifications')]
    operations = [
        migrations.AddField(
            model_name='controlactivationoutbox', name='requested_at',
            field=models.DateTimeField(auto_now=True),
        ),
    ]
