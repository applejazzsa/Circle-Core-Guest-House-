"""
Create or update a Command Center admin user.

Usage:
    python manage.py create_command_user --username admin --email admin@circlecore.co.za --password <password>
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create or update a Command Center admin user."

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--email", required=True)
        parser.add_argument("--password", required=True)

    def handle(self, *args, **options):
        from command.models import CommandUser

        username = options["username"]
        email = options["email"].lower()
        password = options["password"]

        user, created = CommandUser.objects.get_or_create(
            username=username,
            defaults={"email": email},
        )
        user.email = email
        user.set_password(password)
        user.is_active = True
        user.save()

        verb = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{verb} command user: {username} <{email}>"))
