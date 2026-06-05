#!/bin/sh
set -eu

echo "Cron service started."
echo "Backups fire daily at 02:00 SAST (00:00 UTC)."
echo "Trial reminders fire daily at 08:00 SAST (06:00 UTC)."

while true; do
    CURRENT=$(date -u +%H:%M)
    if [ "$CURRENT" = "00:00" ]; then
        echo "[$(date -u)] Running backup_local..."
        python manage.py backup_local \
            --output /app/backups \
            --settings=config.settings_production \
            || echo "[$(date -u)] backup_local failed - check logs."
        sleep 90
    elif [ "$CURRENT" = "06:00" ]; then
        echo "[$(date -u)] Running send_trial_reminders..."
        python manage.py send_trial_reminders \
            --settings=config.settings_production \
            || echo "[$(date -u)] send_trial_reminders failed - check logs."
        sleep 90
    else
        sleep 60
    fi
done
