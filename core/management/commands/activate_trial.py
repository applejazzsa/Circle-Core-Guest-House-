from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Deprecated. Trials are started automatically on registration.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING(
            'activate_trial is no longer used.\n'
            'A 30-day trial subscription is created automatically when a guest house\n'
            'registers at https://circlecore.co.za/register/\n\n'
            'To extend a trial use: python manage.py extend_subscription --schema <name> 30'
        ))
