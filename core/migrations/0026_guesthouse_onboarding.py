from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0025_trialengagement'),
    ]

    operations = [
        migrations.AddField(
            model_name='guesthousesettings',
            name='onboarding_complete',
            field=models.BooleanField(default=False),
        ),
    ]
