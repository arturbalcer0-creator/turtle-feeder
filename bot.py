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
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
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
        _add_column(conn, "snooze_hours", 2)
        # Пауза/снуз: до этого момента напоминания молчат (ISO datetime или NULL).
        _add_column(conn, "mute_until", None, "TEXT")
        conn.commit()


def _add_column(conn, col, default, coltype="INTEGER"):
    """Добавить колонку в state, если её ещё нет, и заполнить значением по умолчанию."""
    existing = [r[1] for r in conn.execute("PRAGMA table_info(state)").fetchall()]
    if col not in existing:
        conn.execute(f"ALTER TABLE state ADD COLUMN {col} {coltype}")
        if default is not None:
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


def record_feeding(user_id, user_name, when=None):
    """Записать кормление и назначить следующее через feed_interval_days дней.

    when — момент кормления (datetime с TZ); по умолчанию сейчас.
    Позволяет задать «дату последнего кормления» задним числом.
    """
    now = when or datetime.now(TZ)
    interval = get_state()["feed_interval_days"]
    next_date = (now.date() + timedelta(days=interval)).isoformat()
    with db() as conn:
        conn.execute(
            "INSERT INTO feedings (user_id, user_name, fed_at) VALUES (?, ?, ?)",
            (user_id, user_name, now.isoformat()),
        )
        conn.execute(
            "UPDATE state SET next_feed_date = ?, last_reminder_at = NULL, mute_until = NULL"
            " WHERE id = 1",
            (next_date,),
        )
        conn.commit()
    return next_date


def undo_last_feeding():
    """Удалить последнее кормление и пересчитать срок по предыдущему. None — если пусто."""
    with db() as conn:
        row = conn.execute("SELECT id FROM feedings ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM feedings WHERE id = ?", (row["id"],))
        prev = conn.execute("SELECT fed_at FROM feedings ORDER BY id DESC LIMIT 1").fetchone()
        interval = conn.execute("SELECT feed_interval_days FROM state WHERE id = 1").fetchone()[0]
        if prev:
            prev_date = datetime.fromisoformat(prev["fed_at"]).astimezone(TZ).date()
            next_date = (prev_date + timedelta(days=interval)).isoformat()
        else:
            next_date = datetime.now(TZ).date().isoformat()  # истории нет — считаем, что пора
        conn.execute(
            "UPDATE state SET next_feed_date = ?, last_reminder_at = NULL, mute_until = NULL"
            " WHERE id = 1",
            (next_date,),
        )
        conn.commit()
    return next_date


def last_feeding():
    with db() as conn:
        return conn.execute(
            "SELECT user_name, fed_at FROM feedings ORDER BY id DESC LIMIT 1"
        ).fetchone()


# --- Клавиатуры и точки входа ---
# Метки кнопок нижней клавиатуры (вариант C). Их же текст ловим в обработчиках.
BTN_FEED = "🐢 Покормил(а)"
BTN_STATUS = "📊 Статус"
BTN_HISTORY = "📜 История"
BTN_SETTINGS = "⚙️ Настройки"

# Нижняя клавиатура — постоянная, всегда под рукой.
MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_FEED)],
        [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_HISTORY)],
        [KeyboardButton(text=BTN_SETTINGS)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# Одна кнопка «Покормил(а)» — вешается на ответы со статусом.
FEED_BUTTON = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text=BTN_FEED, callback_data="fed")]]
)

# Кнопка отмены — показывается под подтверждением кормления.
UNDO_BUTTON = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="↩️ Отменить", callback_data="undo")]]
)

def reminder_kb():
    """Клавиатура напоминания: покормить + отложить на настраиваемый срок."""
    hours = get_state()["snooze_hours"]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_FEED, callback_data="fed")],
            [InlineKeyboardButton(text=f"😴 Позже ({hours} ч)", callback_data="snooze")],
        ]
    )

PANEL_TEXT = "🐢 Панель кормления Саши\nВыберите действие (это сообщение можно закрепить):"


def panel_kb():
    """Inline-панель «пульт» (вариант B) — её можно закрепить в чате."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_FEED, callback_data="fed")],
            [
                InlineKeyboardButton(text=BTN_STATUS, callback_data="m:status"),
                InlineKeyboardButton(text=BTN_HISTORY, callback_data="m:history"),
            ],
            [
                InlineKeyboardButton(text=BTN_SETTINGS, callback_data="m:settings"),
                InlineKeyboardButton(text="📈 Статистика", callback_data="m:stats"),
            ],
        ]
    )


def settings_kb():
    """Редактор настроек кнопками − / + (текущие значения на кнопках)."""
    s = get_state()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➖", callback_data="s:interval:-1"),
                InlineKeyboardButton(text=f"🍽 Кормить: {s['feed_interval_days']} дн", callback_data="noop"),
                InlineKeyboardButton(text="➕", callback_data="s:interval:1"),
            ],
            [
                InlineKeyboardButton(text="➖", callback_data="s:period:-1"),
                InlineKeyboardButton(text=f"🔔 Напоминать: {s['remind_every_hours']} ч", callback_data="noop"),
                InlineKeyboardButton(text="➕", callback_data="s:period:1"),
            ],
            [
                InlineKeyboardButton(text="➖", callback_data="s:snooze:-1"),
                InlineKeyboardButton(text=f"😴 Позже: {s['snooze_hours']} ч", callback_data="noop"),
                InlineKeyboardButton(text="➕", callback_data="s:snooze:1"),
            ],
            [InlineKeyboardButton(text="📅 Задать дату кормления", callback_data="d:pick")],
            [InlineKeyboardButton(text="⏸ Пауза (отпуск)", callback_data="p:menu")],
            [InlineKeyboardButton(text="✖️ Закрыть", callback_data="s:close")],
        ]
    )


def pause_kb():
    """Меню паузы: быстрые варианты и снятие."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="3 дня", callback_data="p:days:3"),
                InlineKeyboardButton(text="1 неделя", callback_data="p:days:7"),
                InlineKeyboardButton(text="2 недели", callback_data="p:days:14"),
            ],
            [InlineKeyboardButton(text="▶️ Снять паузу", callback_data="p:off")],
            [InlineKeyboardButton(text="✖️ Закрыть", callback_data="p:close")],
        ]
    )


def picker_text(iso):
    d = date.fromisoformat(iso)
    interval = get_state()["feed_interval_days"]
    nxt = d + timedelta(days=interval)
    return (
        "📅 Когда Сашу покормили последний раз?\n"
        f"Выбрано: {d.strftime('%d.%m.%Y')}\n"
        f"Следующее кормление станет: {nxt.strftime('%d.%m.%Y')}\n\n"
        "Листайте стрелками и нажмите «Подтвердить»."
    )


def picker_kb(iso):
    """Выбор даты стрелками ±1 день и ±1 неделя, без ввода текста."""
    d = date.fromisoformat(iso)
    today = datetime.now(TZ).date()
    prev7 = (d - timedelta(days=7)).isoformat()
    prev1 = (d - timedelta(days=1)).isoformat()
    next1 = min(today, d + timedelta(days=1)).isoformat()
    next7 = min(today, d + timedelta(days=7)).isoformat()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏪", callback_data=f"d:nav:{prev7}"),
                InlineKeyboardButton(text="◀️", callback_data=f"d:nav:{prev1}"),
                InlineKeyboardButton(text=d.strftime("%d.%m"), callback_data="noop"),
                InlineKeyboardButton(text="▶️", callback_data=f"d:nav:{next1}"),
                InlineKeyboardButton(text="⏩", callback_data=f"d:nav:{next7}"),
            ],
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"d:ok:{iso}"),
                InlineKeyboardButton(text="✖️ Отмена", callback_data="d:cancel"),
            ],
        ]
    )


def fmt_dt(iso_str):
    return datetime.fromisoformat(iso_str).astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def in_reminder_window(now):
    return REMIND_START_HOUR <= now.hour < REMIND_END_HOUR


# --- Тексты (используются командами, кнопками и inline-панелью) ---
def status_text():
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
        days_late = (today - nd).days
        if days_late > 0:
            lines.append(f"⚠️ Просрочено на {days_late} дн. — пора кормить!")
        elif days_late == 0:
            lines.append("Пора кормить сегодня 🐢")
        else:
            lines.append(f"Следующее кормление — {nd.strftime('%d.%m.%Y')} (через {-days_late} дн.)")
    mute = state["mute_until"]
    if mute and datetime.now(TZ) < datetime.fromisoformat(mute):
        lines.append(f"⏸ На паузе до {fmt_dt(mute)}")
    return "\n".join(lines)


def stats_text():
    cutoff = (datetime.now(TZ) - timedelta(days=30)).isoformat()
    with db() as conn:
        rows = conn.execute(
            "SELECT fed_at FROM feedings WHERE fed_at >= ? ORDER BY id DESC", (cutoff,)
        ).fetchall()
    if len(rows) < 2:
        return "📈 Пока мало данных за месяц (нужно ≥2 кормлений)."
    times = [datetime.fromisoformat(r["fed_at"]) for r in rows]  # от новых к старым
    diffs = [(times[i] - times[i + 1]).total_seconds() / 86400 for i in range(len(times) - 1)]
    avg = sum(diffs) / len(diffs)
    interval = get_state()["feed_interval_days"]
    verdict = "в графике 👍" if avg <= interval + 0.5 else "реже, чем нужно ⚠️"
    return (
        f"📈 За последние 30 дней — {len(rows)} кормлений.\n"
        f"Средний интервал: {avg:.1f} дн. (цель {interval}).\n"
        f"Итог: {verdict}"
    )


def pause_text():
    state = get_state()
    mute = state["mute_until"]
    now = datetime.now(TZ)
    if mute and now < datetime.fromisoformat(mute):
        status = f"Сейчас на паузе до {fmt_dt(mute)}."
    else:
        status = "Сейчас пауза не активна."
    return (
        "⏸ Пауза напоминаний (например, на отпуск).\n"
        f"{status}\n\n"
        "Выберите срок паузы. Вернувшись, задайте дату последнего кормления "
        "в настройках, чтобы поправить отсчёт."
    )


def history_text():
    with db() as conn:
        rows = conn.execute(
            "SELECT user_name, fed_at FROM feedings ORDER BY id DESC LIMIT 7"
        ).fetchall()
    if not rows:
        return "Истории пока нет."
    lines = ["Последние кормления:"]
    for r in rows:
        lines.append(f"• {fmt_dt(r['fed_at'])} — {r['user_name']}")
    return "\n".join(lines)


def settings_text():
    return (
        "⚙️ Настройки\n\n"
        "Меняйте значения кнопками − / + ниже.\n"
        f"Напоминания приходят в окне {REMIND_START_HOUR}:00–{REMIND_END_HOUR}:00."
    )


async def do_feed(user_id, user_name, answer):
    next_date = record_feeding(user_id, user_name)
    next_human = datetime.fromisoformat(next_date).strftime("%d.%m.%Y")
    await answer(
        f"Готово! {user_name} покормил(а) Сашу 🐢\n"
        f"Следующее кормление — {next_human}.",
        reply_markup=UNDO_BUTTON,
    )


async def do_undo(answer):
    next_date = undo_last_feeding()
    if next_date is None:
        await answer("Отменять нечего — история кормлений пуста.")
        return
    last = last_feeding()
    tail = (
        f"Теперь последнее: {last['user_name']} — {fmt_dt(last['fed_at'])}."
        if last
        else "История теперь пуста — считаю, что пора кормить."
    )
    await answer(
        "↩️ Последнее кормление отменено.\n"
        f"Следующее — {date.fromisoformat(next_date).strftime('%d.%m.%Y')}.\n{tail}"
    )


# --- Команды (вариант A: попадают в меню Telegram) ---
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
        f"Кормим раз в {state['feed_interval_days']} дн. Когда подойдёт срок, буду писать "
        f"в этот чат (с {REMIND_START_HOUR}:00 до {REMIND_END_HOUR}:00), "
        "пока кто-нибудь не нажмёт «Покормил(а)».\n\n"
        "Кнопки снизу — для быстрых действий. Ниже — панель, которую удобно закрепить.",
        reply_markup=MAIN_KB,
    )
    await message.answer(PANEL_TEXT, reply_markup=panel_kb())


@dp.message(Command("menu"))
async def cmd_menu(message: Message):
    await message.answer(PANEL_TEXT, reply_markup=panel_kb())


@dp.message(Command("feed"))
async def cmd_feed(message: Message):
    user = message.from_user
    await do_feed(user.id, user.full_name, message.answer)


@dp.message(Command("status"))
async def cmd_status(message: Message):
    await message.answer(status_text(), reply_markup=FEED_BUTTON)


@dp.message(Command("history"))
async def cmd_history(message: Message):
    await message.answer(history_text())


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    await message.answer(stats_text())


@dp.message(Command("undo"))
async def cmd_undo(message: Message):
    await do_undo(message.answer)


@dp.message(Command("settings"))
async def cmd_settings(message: Message):
    await message.answer(settings_text(), reply_markup=settings_kb())


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


# --- Нижняя клавиатура (вариант C): ловим текст кнопок ---
@dp.message(F.text == BTN_FEED)
async def kb_feed(message: Message):
    await do_feed(message.from_user.id, message.from_user.full_name, message.answer)


@dp.message(F.text == BTN_STATUS)
async def kb_status(message: Message):
    await message.answer(status_text(), reply_markup=FEED_BUTTON)


@dp.message(F.text == BTN_HISTORY)
async def kb_history(message: Message):
    await message.answer(history_text())


@dp.message(F.text == BTN_SETTINGS)
async def kb_settings(message: Message):
    await message.answer(settings_text(), reply_markup=settings_kb())


# --- Inline-кнопки (вариант B: панель и напоминания) ---
@dp.callback_query(F.data == "fed")
async def cb_fed(callback: CallbackQuery):
    user = callback.from_user
    await do_feed(user.id, user.full_name, callback.message.answer)
    await callback.answer("Записал!")


@dp.callback_query(F.data == "m:status")
async def cb_m_status(callback: CallbackQuery):
    await callback.message.answer(status_text(), reply_markup=FEED_BUTTON)
    await callback.answer()


@dp.callback_query(F.data == "m:history")
async def cb_m_history(callback: CallbackQuery):
    await callback.message.answer(history_text())
    await callback.answer()


@dp.callback_query(F.data == "m:settings")
async def cb_m_settings(callback: CallbackQuery):
    await callback.message.answer(settings_text(), reply_markup=settings_kb())
    await callback.answer()


@dp.callback_query(F.data == "m:stats")
async def cb_m_stats(callback: CallbackQuery):
    await callback.message.answer(stats_text())
    await callback.answer()


@dp.callback_query(F.data == "undo")
async def cb_undo(callback: CallbackQuery):
    await do_undo(callback.message.answer)
    await callback.answer("Отменено")


@dp.callback_query(F.data == "snooze")
async def cb_snooze(callback: CallbackQuery):
    hours = get_state()["snooze_hours"]
    until = datetime.now(TZ) + timedelta(hours=hours)
    set_state(mute_until=until.isoformat())
    await callback.message.edit_text(f"😴 Отложено на {hours} ч. Напомню позже.")
    await callback.answer(f"Напомню через {hours} ч")


@dp.callback_query(F.data.startswith("p:"))
async def cb_pause(callback: CallbackQuery):
    action = callback.data.split(":")[1]
    if action == "menu":
        await callback.message.answer(pause_text(), reply_markup=pause_kb())
    elif action == "close":
        await callback.message.delete()
    elif action == "off":
        set_state(mute_until=None)
        await callback.message.edit_text("▶️ Пауза снята. Напоминания снова активны.")
    elif action == "days":
        days = int(callback.data.split(":")[2])
        until = datetime.now(TZ) + timedelta(days=days)
        set_state(mute_until=until.isoformat())
        await callback.message.edit_text(f"⏸ Пауза до {fmt_dt(until.isoformat())}.")
    await callback.answer()


@dp.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data.startswith("s:"))
async def cb_settings_change(callback: CallbackQuery):
    if callback.data == "s:close":
        await callback.message.delete()
        await callback.answer()
        return
    _, field, delta = callback.data.split(":")
    delta = int(delta)
    s = get_state()
    if field == "interval":
        set_state(feed_interval_days=min(30, max(1, s["feed_interval_days"] + delta)))
    elif field == "period":
        set_state(remind_every_hours=min(24, max(1, s["remind_every_hours"] + delta)))
    else:
        set_state(snooze_hours=min(12, max(1, s["snooze_hours"] + delta)))
    try:
        await callback.message.edit_reply_markup(reply_markup=settings_kb())
    except Exception:
        pass  # значение упёрлось в границу — клавиатура не изменилась, это ок
    await callback.answer()


@dp.callback_query(F.data.startswith("d:"))
async def cb_date(callback: CallbackQuery):
    parts = callback.data.split(":")
    action = parts[1]
    if action == "cancel":
        await callback.message.delete()
        await callback.answer()
        return
    if action == "pick":
        today = datetime.now(TZ).date().isoformat()
        await callback.message.answer(picker_text(today), reply_markup=picker_kb(today))
        await callback.answer()
        return
    iso = parts[2]
    if action == "nav":
        try:
            await callback.message.edit_text(picker_text(iso), reply_markup=picker_kb(iso))
        except Exception:
            pass  # дошли до сегодня — дата не изменилась, это ок
        await callback.answer()
        return
    if action == "ok":
        d = date.fromisoformat(iso)
        when = datetime(d.year, d.month, d.day, 12, 0, tzinfo=TZ)
        record_feeding(callback.from_user.id, callback.from_user.full_name, when=when)
        nxt = date.fromisoformat(get_state()["next_feed_date"])
        await callback.message.edit_text(
            f"✅ Отмечено кормление {d.strftime('%d.%m.%Y')}.\n"
            f"Следующее кормление — {nxt.strftime('%d.%m.%Y')}."
        )
        await callback.answer("Готово")


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

    mute = state["mute_until"]
    if mute and now < datetime.fromisoformat(mute):
        return  # пауза или отложено

    if not in_reminder_window(now):
        return

    next_date = date.fromisoformat(state["next_feed_date"])
    days_late = (now.date() - next_date).days
    if days_late < 0:
        return  # ещё не пора

    last = state["last_reminder_at"]
    if last:
        last_dt = datetime.fromisoformat(last)
        if now - last_dt < timedelta(hours=state["remind_every_hours"]):
            return  # рано для следующего напоминания

    if days_late == 0:
        text = "🐢 Пора покормить Сашу! Нажмите кнопку, когда покормите."
    elif days_late == 1:
        text = "⚠️ Сашу пора было покормить ещё вчера! Уже на 1 день дольше срока."
    else:
        text = f"‼️ Сашу не кормили — уже на {days_late} дн. дольше срока! Он голодает 🐢"

    await bot.send_message(state["chat_id"], text, reply_markup=reminder_kb())
    set_state(last_reminder_at=now.isoformat())
    log.info("Отправлено напоминание в чат %s (просрочка %s дн.)", state["chat_id"], days_late)


# --- Точка входа ---
async def set_commands():
    await bot.set_my_commands(
        [
            BotCommand(command="feed", description="🐢 Отметить кормление"),
            BotCommand(command="status", description="📊 Когда следующее кормление"),
            BotCommand(command="history", description="📜 Последние кормления"),
            BotCommand(command="stats", description="📈 Статистика за месяц"),
            BotCommand(command="undo", description="↩️ Отменить последнее кормление"),
            BotCommand(command="settings", description="⚙️ Настройки"),
            BotCommand(command="menu", description="📋 Панель с кнопками"),
        ]
    )


async def main():
    init_db()
    await set_commands()
    asyncio.create_task(reminder_loop())
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
