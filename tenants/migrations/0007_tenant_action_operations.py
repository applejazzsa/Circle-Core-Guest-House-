from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('tenants', '0006_smart_control_provisioning')]
    operations = [
        migrations.AddField(model_name='guesthousetenant', name='archived_at', field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name='guesthousetenant', name='control_previous_subscription_status', field=models.CharField(blank=True, max_length=20)),
        migrations.AddField(model_name='controlactivationoutbox', name='kind', field=models.CharField(choices=[('activation', 'Activation'), ('administrator_invitation', 'Administrator invitation'), ('password_reset', 'Password reset')], default='activation', max_length=40)),
        migrations.RemoveConstraint(model_name='controlactivationoutbox', name='unique_guest_control_activation'),
        migrations.AddConstraint(model_name='controlactivationoutbox', constraint=models.UniqueConstraint(fields=('tenant', 'user_id', 'kind'), name='unique_guest_control_activation_kind')),
    ]
