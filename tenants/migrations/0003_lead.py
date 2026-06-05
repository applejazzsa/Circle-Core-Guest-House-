import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0002_guesthousetenant_is_verified'),
    ]

    operations = [
        migrations.CreateModel(
            name='Lead',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('full_name', models.CharField(max_length=200)),
                ('business_name', models.CharField(max_length=200)),
                ('email', models.EmailField()),
                ('phone', models.CharField(max_length=50)),
                ('num_rooms', models.PositiveIntegerField(blank=True, null=True)),
                ('notes', models.TextField(blank=True)),
                ('status', models.CharField(
                    choices=[
                        ('new', 'New Lead'), ('contacted', 'Contacted'),
                        ('demo_scheduled', 'Demo Scheduled'), ('demo_completed', 'Demo Completed'),
                        ('trial_started', 'Trial Started'), ('converted', 'Converted'),
                        ('lost', 'Lost'),
                    ],
                    default='new', max_length=20,
                )),
                ('source', models.CharField(blank=True, default='website', max_length=50)),
                ('notes_internal', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('contacted_at', models.DateTimeField(blank=True, null=True)),
                ('tenant', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='leads',
                    to='tenants.guesthousetenant',
                )),
            ],
            options={'ordering': ['-created_at'], 'verbose_name': 'Lead', 'verbose_name_plural': 'Leads'},
        ),
    ]
