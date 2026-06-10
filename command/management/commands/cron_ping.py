"""
cron_ping — called by the cron container after each scheduled job.
Updates a single CronHealth row so the Command Center can show
whether the cron service is alive and when it last ran.

Usage:
    python manage.py cron_ping --job trial_reminders --status ok --message "3 emails sent"
    python manage.py cron_ping --job backup --status error --message "disk full"
"""
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Record a cron job heartbeat in the CronHealth table."

    def add_arguments(self, parser):
        parser.add_argument("--job", required=True, help="Job name (e.g. trial_reminders, backup)")
        parser.add_argument("--status", default="ok", choices=["ok", "error", "warning"])
        parser.add_argument("--message", default="", help="Short result message")

    def handle(self, *args, **options):
        from command.models import CronHealth
        CronHealth.objects.update_or_create(
            job_name=options["job"],
            defaults={
                "last_run_at": timezone.now(),
                "last_status": options["status"],
                "last_message": options["message"][:500],
            },
        )
        self.stdout.write(f"[cron_ping] {options['job']} → {options['status']}")
