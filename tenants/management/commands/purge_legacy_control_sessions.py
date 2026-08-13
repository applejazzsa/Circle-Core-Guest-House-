from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone


class Command(BaseCommand):
    help = (
        'Identify or delete active authenticated sessions created before tenant-schema '
        'tagging. Run once in staging after deployment; dry-run is the default.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--confirm-environment', required=True)
        parser.add_argument('--execute', action='store_true')
        parser.add_argument('--confirm-purge', default='')

    def handle(self, *args, **options):
        environment = getattr(settings, 'ENVIRONMENT', '').strip().lower()
        if options['confirm_environment'].strip().lower() != environment:
            raise CommandError('The explicitly confirmed environment does not match this deployment.')
        if environment not in {'staging', 'test'}:
            raise CommandError('Legacy control-session cleanup is restricted to staging.')

        legacy_keys = []
        undecodable = 0
        for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator():
            try:
                data = session.get_decoded()
            except Exception:
                undecodable += 1
                continue
            if data.get('_auth_user_id') and not data.get('_circle_core_tenant_schema'):
                legacy_keys.append(session.session_key)

        if options['execute']:
            phrase = f'PURGE LEGACY SESSIONS {environment}'
            if options['confirm_purge'] != phrase:
                raise CommandError(f'Execution requires --confirm-purge "{phrase}".')
            deleted, _ = Session.objects.filter(session_key__in=legacy_keys).delete()
            self.stdout.write(self.style.SUCCESS(
                f'Deleted {deleted} active legacy authenticated session(s); undecodable={undecodable}.'
            ))
        else:
            self.stdout.write(
                f'Dry run: {len(legacy_keys)} active legacy authenticated session(s) would be deleted; '
                f'undecodable={undecodable}.'
            )
