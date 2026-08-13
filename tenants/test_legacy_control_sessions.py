from io import StringIO

from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings


@override_settings(ENVIRONMENT='staging')
class LegacyControlSessionCleanupTests(TestCase):
    def _session(self, *, tagged):
        session = SessionStore()
        session['_auth_user_id'] = '7'
        session['_auth_user_backend'] = 'django.contrib.auth.backends.ModelBackend'
        if tagged:
            session['_circle_core_tenant_schema'] = 'tenant_known'
        session.save()
        return session.session_key

    def test_dry_run_preserves_sessions_and_execute_removes_only_untagged(self):
        legacy = self._session(tagged=False)
        current = self._session(tagged=True)
        output = StringIO()
        call_command('purge_legacy_control_sessions', confirm_environment='staging', stdout=output)
        self.assertIn('1 active legacy', output.getvalue())
        self.assertTrue(Session.objects.filter(session_key=legacy).exists())

        call_command(
            'purge_legacy_control_sessions', confirm_environment='staging', execute=True,
            confirm_purge='PURGE LEGACY SESSIONS staging', stdout=StringIO(),
        )
        self.assertFalse(Session.objects.filter(session_key=legacy).exists())
        self.assertTrue(Session.objects.filter(session_key=current).exists())

    def test_wrong_confirmation_is_rejected(self):
        self._session(tagged=False)
        with self.assertRaises(CommandError):
            call_command(
                'purge_legacy_control_sessions', confirm_environment='staging', execute=True,
                confirm_purge='wrong', stdout=StringIO(),
            )
