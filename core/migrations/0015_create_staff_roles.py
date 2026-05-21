from django.db import migrations


def create_staff_roles(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("auth", "User")
    for role in ["Owner", "Operator", "Cleaner"]:
        Group.objects.get_or_create(name=role)

    owner_group = Group.objects.get(name="Owner")
    for user in User.objects.filter(is_superuser=True):
        user.groups.add(owner_group)


def remove_staff_roles(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name__in=["Owner", "Operator", "Cleaner"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0014_room_booked_status"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(create_staff_roles, remove_staff_roles),
    ]
