from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0022_communicationlog_dailycloselock'),
    ]

    operations = [
        migrations.AddField(
            model_name='subscription',
            name='payfast_token',
            field=models.CharField(blank=True, max_length=200),
        ),
    ]
