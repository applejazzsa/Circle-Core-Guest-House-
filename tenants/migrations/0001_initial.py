import django.db.models.deletion
import django_tenants.postgresql_backend.base
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name='GuestHouseTenant',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('schema_name', models.CharField(
                    db_index=True,
                    max_length=63,
                    unique=True,
                    validators=[django_tenants.postgresql_backend.base._check_schema_name],
                )),
                ('name', models.CharField(max_length=200)),
                ('owner_name', models.CharField(max_length=200)),
                ('owner_email', models.EmailField(unique=True)),
                ('owner_phone', models.CharField(blank=True, max_length=20)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'verbose_name': 'Guest House',
                'verbose_name_plural': 'Guest Houses',
            },
        ),
        migrations.CreateModel(
            name='Domain',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('domain', models.CharField(db_index=True, max_length=253, unique=True)),
                ('is_primary', models.BooleanField(db_index=True, default=True)),
                ('tenant', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='domains',
                    to='tenants.guesthousetenant',
                )),
            ],
        ),
    ]
