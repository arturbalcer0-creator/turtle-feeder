"""Telegram-бот, напоминающий кормить черепаху Сашу раз в N дней.

Логика:
- Кормим раз в FEED_INTERVAL_DAYS дней.
- В день, когда пора кормить, бот шлёт напоминание в общий чат и повторяет
  его каждый REMIND_EVERY_MINUTES минут, но только внутри дневного окна
  [REMIND_START_HOUR, REMIND_END_HOUR), пока кто-то не нажмёт кнопку «Покормил(а)».
- После подтверждения следующее кормление назначается на +FEED_INTERVAL_DAYS дней,
  напоминания прекращаются. Ведётся история кормлений (кто и когда).
"""

import asyncio
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# --- Конфигурация (через переменные окружения) ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = os.environ.get("DB_PATH", "turtle.db")
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Moscow"))
FEED_INTERVAL_DAYS = int(os.environ.get("FEED_INTERVAL_DAYS", "3"))
REMIND_START_HOUR = int(os.environ.get("REMIND_START_HOUR", "10"))
REMIND_END_HOUR = int(os.environ.get("REMIND_END_HOUR", "22"))
REMIND_EVERY_MINUTES = int(os.environ.get("REMIND_EVERY_MINUTES", "60"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("turtle")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# --- База данных ---
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                chat_id INTEGER,
                next_feed_date TEXT,
                last_reminder_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                user_name TEXT,
                fed_at TEXT NOT NULL
            )
            """
        )
        conn.execute("INSERT OR IGNORE INTO state (id) VALUES (1)")
        # Настройки, редактируемые из чата (хранятся в БД, а не в .env).
        # env-значения используются только как начальные при первом запуске.
        _add_column(conn, "feed_interval_days", FEED_INTERVAL_DAYS)
        _add_column(conn, "remind_every_hours", max(1, round(REMIND_EVERY_MINUTES / 60)))
        conn.commit()


def _add_column(conn, col, default):
    """Добавить колонку в state, если её ещё нет, и заполнить значением по умолчанию."""
    existing = [r[1] for r in conn.execute("PRAGMA table_info(state)").fetchall()]
    if col not in existing:
        conn.execute(f"ALTER TABLE state ADD COLUMN {col} INTEGER")
        conn.execute(f"UPDATE state SET {col} = ? WHERE {col} IS NULL", (default,))


def get_state():
    with db() as conn:
        return conn.execute("SELECT * FROM state WHERE id = 1").fetchone()


def set_state(**fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE state SET {cols} WHERE id = 1", tuple(fields.values()))
        conn.commit()


def record_feeding(user_id, user_name):
    """Записать кормление и назначить следующее на +FEED_INTERVAL_DAYS дней."""
    now = datetime.now(TZ)
    interval = get_state()["feed_interval_days"]
    next_date = (now.date() + timedelta(days=interval)).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT INTO feedings (user_id, user_name, fed_at) VALUES (?, ?, ?)",
            (user_id, user_name, now.isoformat()),
        )
        conn.execute(
            "UPDATE state SET next_feed_date = ?, last_reminder_at = NULL WHERE id = 1",
            (next_date,),
        )
        conn.commit()
    return next_date


def last_feeding():
    with db() as conn:
        return conn.execute(
            "SELECT user_name, fed_at FROM feedings ORDER BY id DESC LIMIT 1"
        ).fetchone()


# --- Вспомогательное ---
FEED_BUTTON = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="🐢 Покормил(а)", callback_data="fed")]
    ]
)


def fmt_dt(iso_str):
    return datetime.fromisoformat(iso_str).astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def in_reminder_window(now):
    return REMIND_START_HOUR <= now.hour < REMIND_END_HOUR


# --- Обработчики команд ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    state = get_state()
    # Запоминаем чат, куда слать напоминания (последний, где вызвали /start).
    fields = {"chat_id": message.chat.id}
    if not state["next_feed_date"]:
        # Первый запуск — считаем, что кормить пора сегодня, чтобы задать ритм.
        fields["next_feed_date"] = datetime.now(TZ).date().isoformat()
    set_state(**fields)
    await message.answer(
        "Привет! Я напоминаю кормить черепаху Сашу 🐢\n\n"
        f"Кормим раз в {FEED_INTERVAL_DAYS} дня. Когда подойдёт срок, я буду писать "
        f"в этот чат каждый час (с {REMIND_START_HOUR}:00 до {REMIND_END_HOUR}:00), "
        "пока кто-нибудь не нажмёт кнопку «Покормил(а)».\n\n"
        "Команды:\n"
        "/feed — отметить, что покормили\n"
        "/status — когда следующее кормление\n"
        "/history — последние 7 кормлений\n"
        "/settings — текущие настройки\n"
        "/interval <дни> — раз в сколько дней кормить\n"
        "/period <часы> — как часто повторять напоминание",
        reply_markup=FEED_BUTTON,
    )


async def do_feed(user_id, user_name, answer):
    next_date = record_feeding(user_id, user_name)
    next_human = datetime.fromisoformat(next_date).strftime("%d.%m.%Y")
    await answer(
        f"Готово! {user_name} покормил(а) Сашу 🐢\n"
        f"Следующее кормление — {next_human}."
    )


@dp.message(Command("feed"))
async def cmd_feed(message: Message):
    user = message.from_user
    await do_feed(user.id, user.full_name, message.answer)


@dp.callback_query(F.data == "fed")
async def cb_fed(callback: CallbackQuery):
    user = callback.from_user
    await do_feed(user.id, user.full_name, callback.message.answer)
    await callback.answer("Записал!")


@dp.message(Command("status"))
async def cmd_status(message: Message):
    state = get_state()
    last = last_feeding()
    lines = []
    if last:
        lines.append(f"Последний раз кормил(а): {last['user_name']} — {fmt_dt(last['fed_at'])}")
    else:
        lines.append("Кормлений ещё не было.")
    if state["next_feed_date"]:
        nd = date.fromisoformat(state["next_feed_date"])
        today = datetime.now(TZ).date()
        if today >= nd:
            lines.append("Пора кормить! 🐢")
        else:
            days = (nd - today).days
            lines.append(f"Следующее кормление — {nd.strftime('%d.%m.%Y')} (через {days} дн.)")
    await message.answer("\n".join(lines), reply_markup=FEED_BUTTON)


@dp.message(Command("history"))
async def cmd_history(message: Message):
    with db() as conn:
        rows = conn.execute(
            "SELECT user_name, fed_at FROM feedings ORDER BY id DESC LIMIT 7"
        ).fetchall()
    if not rows:
        await message.answer("Истории пока нет.")
        return
    lines = ["Последние кормления:"]
    for r in rows:
        lines.append(f"• {fmt_dt(r['fed_at'])} — {r['user_name']}")
    await message.answer("\n".join(lines))


@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    s = get_state()
    await message.answer(
        "Текущие настройки:\n"
        f"• Кормим раз в {s['feed_interval_days']} дн.\n"
        f"• Напоминаем каждые {s['remind_every_hours']} ч "
        f"(в окне {REMIND_START_HOUR}:00–{REMIND_END_HOUR}:00)\n\n"
        "Изменить:\n"
        "/interval <дни> — частота кормления\n"
        "/period <часы> — период напоминаний"
    )


@dp.message(Command("interval"))
async def cmd_interval(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg:
        await message.answer(
            f"Сейчас кормим раз в {get_state()['feed_interval_days']} дн.\n"
            "Изменить: /interval <число дней>, например /interval 3"
        )
        return
    if not arg.isdigit() or int(arg) < 1:
        await message.answer("Нужно целое число дней ≥ 1. Пример: /interval 3")
        return
    set_state(feed_interval_days=int(arg))
    await message.answer(
        f"Готово: кормим раз в {int(arg)} дн. "
        "Применится к следующему кормлению (нажмите «Покормил(а)», чтобы пересчитать срок)."
    )


@dp.message(Command("period"))
async def cmd_period(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg:
        await message.answer(
            f"Сейчас напоминаем каждые {get_state()['remind_every_hours']} ч.\n"
            "Изменить: /period <число часов>, например /period 3"
        )
        return
    if not arg.isdigit() or int(arg) < 1:
        await message.answer("Нужно целое число часов ≥ 1. Пример: /period 3")
        return
    set_state(remind_every_hours=int(arg))
    await message.answer(f"Готово: напоминания каждые {int(arg)} ч.")


# --- Фоновый цикл напоминаний ---
async def reminder_loop():
    while True:
        try:
            await maybe_remind()
        except Exception:
            log.exception("Ошибка в цикле напоминаний")
        await asyncio.sleep(60)


async def maybe_remind():
    state = get_state()
    if not state["chat_id"] or not state["next_feed_date"]:
        return

    now = datetime.now(TZ)
    if not in_reminder_window(now):
        return

    next_date = date.fromisoformat(state["next_feed_date"])
    if now.date() < next_date:
        return  # ещё не пора

    last = state["last_reminder_at"]
    if last:
        last_dt = datetime.fromisoformat(last)
        if now - last_dt < timedelta(hours=state["remind_every_hours"]):
            return  # рано для следующего напоминания

    await bot.send_message(
        state["chat_id"],
        "🐢 Пора покормить Сашу! Нажмите кнопку, когда покормите.",
        reply_markup=FEED_BUTTON,
    )
    set_state(last_reminder_at=now.isoformat())
    log.info("Отправлено напоминание в чат %s", state["chat_id"])


# --- Точка входа ---
async def main():
    init_db()
    asyncio.create_task(reminder_loop())
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
