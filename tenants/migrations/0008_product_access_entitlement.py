from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('tenants', '0007_tenant_action_operations')]
    operations = [migrations.AddField(model_name='guesthousetenant', name='product_access_enabled', field=models.BooleanField(default=True))]
