from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('core', '0037_control_user_security')]
    operations = [
        migrations.AddField(model_name='controlusersecurity', name='last_successful_login', field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='controlusersecurity', name='last_failed_login', field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='controlusersecurity', name='failed_login_count', field=models.PositiveIntegerField(default=0)),
    ]
