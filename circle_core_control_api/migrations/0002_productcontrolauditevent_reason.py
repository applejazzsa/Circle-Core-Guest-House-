from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("circle_core_control_api", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="productcontrolauditevent",
            name="reason",
            field=models.TextField(blank=True),
        ),
    ]
