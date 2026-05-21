from getpass import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create a superuser owner account for Circle Core Guest House."

    def handle(self, *args, **options):
        username = input("Username: ").strip()
        email = input("Email: ").strip()
        password = getpass("Password: ")

        if not username:
            raise CommandError("Username is required.")
        if not password:
            raise CommandError("Password is required.")

        User = get_user_model()
        if User.objects.filter(username=username).exists():
            raise CommandError(f'User "{username}" already exists.')

        user = User.objects.create_superuser(username=username, email=email, password=password)
        owner_group, _ = Group.objects.get_or_create(name="Owner")
        user.groups.add(owner_group)
        self.stdout.write(self.style.SUCCESS(f'Owner account "{username}" created.'))
