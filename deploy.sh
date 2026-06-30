#!/usr/bin/env bash
# Запускается НА СЕРВЕРЕ. Подтягивает свежий код из GitHub и перезапускает бота.
set -e
cd "$(dirname "$0")"

echo "==> git pull"
git pull --ff-only

echo "==> обновляю зависимости"
.venv/bin/pip install -q -r requirements.txt

echo "==> перезапускаю сервис"
sudo systemctl restart turtle-bot

echo "==> статус"
sleep 1
systemctl --no-pager --lines=0 status turtle-bot | head -4
echo "Готово."
