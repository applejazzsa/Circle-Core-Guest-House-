#!/bin/sh
set -eu

SETTINGS="--settings=config.settings_production"

echo "Cron service started."
echo "Backups fire daily at 02:00 SAST (00:00 UTC)."
echo "Trial reminders fire daily at 08:00 SAST (06:00 UTC)."

ping_job() {
    JOB=$1
    STATUS=$2
    MSG=$3
    python manage.py cron_ping --job "$JOB" --status "$STATUS" --message "$MSG" $SETTINGS \
        || echo "[$(date -u)] cron_ping failed (non-fatal)"
}

while true; do
    CURRENT=$(date -u +%H:%M)

    if [ "$CURRENT" = "00:00" ]; then
        echo "[$(date -u)] Running backup_local..."
        if python manage.py backup_local --output /app/backups $SETTINGS; then
            ping_job "backup" "ok" "Backup completed at $(date -u)"
        else
            ping_job "backup" "error" "backup_local failed at $(date -u)"
            echo "[$(date -u)] backup_local failed - check logs."
        fi
        sleep 90

    elif [ "$CURRENT" = "06:00" ]; then
        echo "[$(date -u)] Running send_trial_reminders..."
        if python manage.py send_trial_reminders $SETTINGS; then
            ping_job "trial_reminders" "ok" "Reminders sent at $(date -u)"
        else
            ping_job "trial_reminders" "error" "send_trial_reminders failed at $(date -u)"
            echo "[$(date -u)] send_trial_reminders failed - check logs."
        fi
        sleep 90

    else
        sleep 60
    fi
done
