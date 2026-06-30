# 🐢 Бот-напоминалка кормить черепаху Сашу

Напоминает в общий чат покормить черепаху раз в 3 дня. В день кормления пишет
каждый час (в окне 10:00–22:00), пока кто-нибудь не нажмёт кнопку **«Покормил(а)»**.
Ведёт историю: кто и когда кормил.

## Команды

- `/start` — зарегистрировать чат и получить инструкцию
- `/feed` — отметить, что покормили (то же делает кнопка)
- `/status` — когда следующее кормление и кто кормил последним
- `/history` — последние 10 кормлений

## Как это работает

- Покормили → следующее кормление через `FEED_INTERVAL_DAYS` дней (по умолчанию 3).
- Когда наступает день кормления, бот шлёт напоминание и повторяет каждый час
  внутри дневного окна, пока кто-то не подтвердит. Ночью молчит.
- После подтверждения отсчёт сбрасывается на +3 дня.

Настройки — через переменные окружения (см. `.env.example`).

## Шаг 1. Создать бота

1. В Telegram напишите [@BotFather](https://t.me/BotFather) → `/newbot`, получите **токен**.
2. Чтобы бот видел команды в группах, у @BotFather: `/setprivacy` → выберите бота → **Disable**.
3. Добавьте бота в ваш общий групповой чат.

## Шаг 2. Запуск на VM (Cloud.ru)

```bash
# на VM (Ubuntu/Debian)
sudo apt update && sudo apt install -y python3-venv git
sudo mkdir -p /opt/turtle-feeder && sudo chown $USER /opt/turtle-feeder
cd /opt/turtle-feeder

# скопируйте сюда файлы проекта (bot.py, requirements.txt и т.д.)

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env          # вставьте BOT_TOKEN, при желании поправьте TZ и часы
```

Проверьте, что запускается:

```bash
set -a; source .env; set +a
.venv/bin/python bot.py
```

В групповом чате отправьте `/start@имя_бота` — бот должен ответить. Останавливаем `Ctrl+C`.

## Шаг 3. Автозапуск через systemd

```bash
sudo cp turtle-bot.service /etc/systemd/system/turtle-bot.service
# проверьте пути/пользователя внутри файла (WorkingDirectory, ExecStart, User при необходимости)
sudo systemctl daemon-reload
sudo systemctl enable --now turtle-bot
sudo systemctl status turtle-bot      # должно быть active (running)
journalctl -u turtle-bot -f           # смотреть логи
```

Готово. Бот переживает перезагрузку VM и сам перезапускается при сбоях.

## Заметки

- База — один файл `turtle.db` (SQLite) рядом с ботом. Бэкап = скопировать этот файл.
- Напоминания приходят в тот чат, где последний раз вызвали `/start`.
- Поменять интервал/окно/частоту — отредактируйте `.env` и `sudo systemctl restart turtle-bot`.
