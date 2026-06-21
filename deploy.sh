#!/bin/bash
# Circle Core Guest House — one-command deploy
# Usage (on the VPS): bash deploy.sh

set -e

COMPOSE="docker-compose.production.yml"
WEB_SERVICE="guesthouse-web"
NGINX_CONTAINER="circlecore-nginx"

echo ""
echo "=== Circle Core: Deploying ==="
echo ""

echo "[1/5] Pulling latest code..."
git pull origin main

echo ""
echo "[2/5] Rebuilding web image (no cache)..."
sudo docker compose -f $COMPOSE build $WEB_SERVICE

echo ""
echo "[3/5] Starting containers..."
sudo docker compose -f $COMPOSE up -d

echo ""
echo "[4/5] Running migrations & collecting static files..."
sleep 5  # give gunicorn a moment to start
sudo docker exec $WEB_SERVICE python manage.py migrate --noinput
sudo docker exec $WEB_SERVICE python manage.py collectstatic --noinput --clear

echo ""
echo "[5/5] Restarting nginx..."
sudo docker restart $NGINX_CONTAINER

echo ""
echo "=== Deploy complete! ==="
echo ""
sudo docker compose -f $COMPOSE ps
