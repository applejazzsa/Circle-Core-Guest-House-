from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0024_alter_payment_payment_method_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='TrialEngagement',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('login_count', models.PositiveIntegerField(default=0)),
                ('rooms_added', models.PositiveIntegerField(default=0)),
                ('guests_added', models.PositiveIntegerField(default=0)),
                ('bookings_added', models.PositiveIntegerField(default=0)),
                ('reports_viewed', models.PositiveIntegerField(default=0)),
                ('last_login_at', models.DateTimeField(blank=True, null=True)),
                ('last_activity_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Trial Engagement',
            },
        ),
    ]
