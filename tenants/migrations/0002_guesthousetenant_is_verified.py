from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='guesthousetenant',
            name='is_verified',
            field=models.BooleanField(default=False),
        ),
    ]
