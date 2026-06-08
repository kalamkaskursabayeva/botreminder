
import os
import re
import sqlite3
import datetime
from datetime import time as dtime
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

TZ = ZoneInfo("Asia/Almaty")
DB_PATH = "overseer.db"
TOKEN = os.environ.get("BOT_TOKEN", "8688991463:AAGcaub3H-iqplkGufXlZ5f79s_OnCUuwLM")

# ───────────────────────── База данных ─────────────────────────
CONN = None


def init_db():
    global CONN
    CONN = sqlite3.connect(DB_PATH, check_same_thread=False)
    CONN.row_factory = sqlite3.Row
    CONN.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id  INTEGER PRIMARY KEY,
            name     TEXT,
            username TEXT,
            is_admin INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS tasks(
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER,
            what      TEXT,
            when_str  TEXT,
            where_str TEXT,
            date      TEXT,
            progress  INTEGER DEFAULT 0,
            responded INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS subtasks(
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            label   TEXT,
            done    INTEGER DEFAULT 0);
    """)
    CONN.commit()


def today():
    return datetime.datetime.now(TZ).strftime("%Y-%m-%d")


def is_admin(uid):
    r = CONN.execute("SELECT is_admin FROM users WHERE user_id=?", (uid,)).fetchone()
    return bool(r and r["is_admin"])


def any_admin():
    return CONN.execute("SELECT 1 FROM users WHERE is_admin=1 LIMIT 1").fetchone() is not None


def admin_ids():
    return [r["user_id"] for r in
            CONN.execute("SELECT user_id FROM users WHERE is_admin=1").fetchall()]


def recompute(task_id):
    """Прогресс задачи = доля выполненных подзадач."""
    subs = CONN.execute("SELECT done FROM subtasks WHERE task_id=?", (task_id,)).fetchall()
    if subs:
        p = round(sum(s["done"] for s in subs) / len(subs) * 100)
        CONN.execute("UPDATE tasks SET progress=? WHERE id=?", (p, task_id))
        CONN.commit()
        return p
    return None


# ───────────────────────── Карточка задачи ─────────────────────────
def task_card_text(t):
    return (f"📋 {t['what']}\n"
            f"🕒 {t['when_str']}    📍 {t['where_str']}\n"
            f"Прогресс: {t['progress']}%")


def task_keyboard(task_id):
    subs = CONN.execute(
        "SELECT id, label, done FROM subtasks WHERE task_id=? ORDER BY id",
        (task_id,)).fetchall()
    rows = []
    for s in subs:
        mark = "✅" if s["done"] else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {s['label']}",
                                          callback_data=f"sub:{task_id}:{s['id']}")])
    rows.append([
        InlineKeyboardButton("25%", callback_data=f"set:{task_id}:25"),
        InlineKeyboardButton("50%", callback_data=f"set:{task_id}:50"),
        InlineKeyboardButton("75%", callback_data=f"set:{task_id}:75"),
        InlineKeyboardButton("Готово", callback_data=f"set:{task_id}:100"),
    ])
    return InlineKeyboardMarkup(rows)


def apply_percent(task_id, pct):
    """Выставить процент: если есть подзадачи — отметить нужное количество."""
    subs = CONN.execute("SELECT id FROM subtasks WHERE task_id=? ORDER BY id",
                         (task_id,)).fetchall()
    if subs:
        n = round(pct / 100 * len(subs))
        for i, s in enumerate(subs):
            CONN.execute("UPDATE subtasks SET done=? WHERE id=?",
                         (1 if i < n else 0, s["id"]))
        CONN.commit()
        return recompute(task_id)
    CONN.execute("UPDATE tasks SET progress=? WHERE id=?", (pct, task_id))
    CONN.commit()
    return pct


# ───────────────────────── Статусы и сводка ─────────────────────────
def expected_now():
    h = datetime.datetime.now(TZ).hour
    if h < 13:
        return 0
    if h < 19:
        return 50
    return 90


def status_of(t):
    if t["progress"] >= 100:
        return "✅ готово"
    if not t["responded"]:
        return "🔕 молчит"
    if t["progress"] < expected_now():
        return "⚠️ отстаёт"
    return "🟢 в графике"


def build_report():
    rows = CONN.execute(
        "SELECT t.*, u.name AS name FROM tasks t "
        "JOIN users u ON u.user_id=t.user_id WHERE t.date=? ORDER BY u.name",
        (today(),)).fetchall()
    if not rows:
        return "Сегодня задач нет."
    counts = {"🟢": 0, "⚠️": 0, "🔕": 0, "✅": 0}
    lines = ["📊 Сводка за день:\n"]
    for t in rows:
        s = status_of(t)
        counts[s.split()[0]] += 1
        lines.append(f"• {t['name']}: {t['what']} — {t['progress']}%  {s}")
    lines.append(f"\nВ графике: {counts['🟢']} · Отстают: {counts['⚠️']} · "
                 f"Молчат: {counts['🔕']} · Готово: {counts['✅']}")
    return "\n".join(lines)


# ───────────────────────── Команды ─────────────────────────
ADMIN_HELP = (
    "Вы — руководитель. Команды:\n\n"
    "📝 Поставить задачу:\n"
    "/task @username | что | когда | где | подзадача1; подзадача2\n\n"
    "Пример:\n"
    "/task @aigul | Обзвонить лиды | к 14:00 | удалённо | список лидов; 10 звонков; занести результаты\n\n"
    "👥 /people — список сотрудников (кто нажал /start)\n"
    "📊 /report — сводка прямо сейчас\n\n"
    "Чек-ины приходят сотрудникам в 10:00, 13:00 и 19:00. "
    "В 19:00 вам придёт сводка."
)

EMP_HELLO = (
    "Привет! 👋 Я буду присылать вам задачи на день и спрашивать, "
    "как продвигается. Отмечать прогресс можно кнопками под задачей "
    "или просто ответить текстом — например «почти готово» или «осталось 30%»."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    row = CONN.execute("SELECT is_admin FROM users WHERE user_id=?", (u.id,)).fetchone()
    admin = row["is_admin"] if row else 0
    if not admin and not any_admin():
        admin = 1  # первый стартовавший = руководитель
    CONN.execute(
        "INSERT OR REPLACE INTO users(user_id, name, username, is_admin) VALUES(?,?,?,?)",
        (u.id, u.full_name, u.username or "", admin))
    CONN.commit()
    await update.message.reply_text(ADMIN_HELP if admin else EMP_HELLO)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        ADMIN_HELP if is_admin(update.effective_user.id) else EMP_HELLO)


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")


async def people_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = CONN.execute(
        "SELECT name, username, user_id, is_admin FROM users "
        "ORDER BY is_admin DESC, name").fetchall()
    if not rows:
        return await update.message.reply_text("Пока никто не нажал /start.")
    lines = []
    for r in rows:
        tag = "@" + r["username"] if r["username"] else str(r["user_id"])
        role = " (руководитель)" if r["is_admin"] else ""
        lines.append(f"• {r['name']} — {tag}{role}")
    await update.message.reply_text("Зарегистрированы:\n" + "\n".join(lines))


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(build_report())


async def add_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Эта команда только для руководителя.")
    raw = update.message.text.partition(" ")[2]
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 4:
        return await update.message.reply_text(
            "Формат:\n/task @username | что | когда | где | подзадача1; подзадача2\n\n"
            "Список сотрудников: /people")
    who, what, when_str, where_str = parts[0], parts[1], parts[2], parts[3]
    subs = [s.strip() for s in parts[4].split(";") if s.strip()] if len(parts) >= 5 else []

    if who.startswith("@"):
        emp = CONN.execute("SELECT user_id, name FROM users WHERE username=?",
                           (who[1:],)).fetchone()
    elif who.lstrip("-").isdigit():
        emp = CONN.execute("SELECT user_id, name FROM users WHERE user_id=?",
                           (int(who),)).fetchone()
    else:
        emp = CONN.execute("SELECT user_id, name FROM users WHERE name=?",
                           (who,)).fetchone()
    if not emp:
        return await update.message.reply_text(
            "Не нашёл такого сотрудника. Он должен сначала нажать /start. Список: /people")

    cur = CONN.execute(
        "INSERT INTO tasks(user_id, what, when_str, where_str, date) VALUES(?,?,?,?,?)",
        (emp["user_id"], what, when_str, where_str, today()))
    tid = cur.lastrowid
    for s in subs:
        CONN.execute("INSERT INTO subtasks(task_id, label) VALUES(?,?)", (tid, s))
    CONN.commit()

    await update.message.reply_text(f"✅ Задача добавлена для {emp['name']}: {what}")
    t = CONN.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    try:
        await context.bot.send_message(
            emp["user_id"], "📋 Новая задача:\n\n" + task_card_text(t),
            reply_markup=task_keyboard(tid))
    except Exception:
        await update.message.reply_text(
            "⚠️ Не смог написать сотруднику — пусть сначала нажмёт /start у бота.")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    kind, tid, val = q.data.split(":")
    tid = int(tid)
    CONN.execute("UPDATE tasks SET responded=1 WHERE id=?", (tid,))
    CONN.commit()

    if kind == "sub":
        cur = CONN.execute("SELECT done FROM subtasks WHERE id=?", (int(val),)).fetchone()
        CONN.execute("UPDATE subtasks SET done=? WHERE id=?",
                     (0 if cur["done"] else 1, int(val)))
        CONN.commit()
        recompute(tid)
    elif kind == "set":
        apply_percent(tid, int(val))

    t = CONN.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    try:
        await q.edit_message_text(task_card_text(t), reply_markup=task_keyboard(tid))
    except Exception:
        pass


def parse_progress(text):
    low = text.lower()
    if re.search(r"готов|сделал|закончил|всё\b|все\b|done", low):
        return 100
    if "почти" in low:
        return 90
    if "половин" in low:
        return 50
    if re.search(r"начал|приступил", low):
        return 20
    m = re.search(r"(\d{1,3})", text)
    if m:
        return min(100, int(m.group(1)))
    return None


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if is_admin(u.id):
        return  # у руководителя — команды, свободный текст игнорируем
    t = CONN.execute(
        "SELECT * FROM tasks WHERE user_id=? AND date=? AND progress<100 "
        "ORDER BY id DESC LIMIT 1", (u.id, today())).fetchone()
    if not t:
        return await update.message.reply_text("Сейчас активных задач нет. Спасибо!")
    CONN.execute("UPDATE tasks SET responded=1 WHERE id=?", (t["id"],))
    CONN.commit()

    v = parse_progress(update.message.text)
    if v is None:
        return await update.message.reply_text(
            "Принял. Сколько примерно готово? Ответьте числом или нажмите кнопки в задаче.")
    p = apply_percent(t["id"], v)
    msg = ("Отлично, отметил задачу выполненной. Спасибо! ✅" if p >= 100
           else f"Принято — {p}%. Напомню ближе к сроку.")
    await update.message.reply_text(msg)


# ───────────────────────── Задачи по расписанию ─────────────────────────
async def send_checkin(context, phase):
    head = {
        "morning": "Доброе утро! Задача на сегодня:",
        "midday":  "Как продвигается задача?",
        "evening": "Финальная проверка по задаче на сегодня:",
    }[phase]
    rows = CONN.execute("SELECT * FROM tasks WHERE date=? AND progress<100",
                        (today(),)).fetchall()
    for t in rows:
        try:
            await context.bot.send_message(
                t["user_id"], head + "\n\n" + task_card_text(t),
                reply_markup=task_keyboard(t["id"]))
        except Exception:
            pass


async def alert_admins(context):
    rows = CONN.execute(
        "SELECT t.*, u.name AS name FROM tasks t "
        "JOIN users u ON u.user_id=t.user_id WHERE t.date=?", (today(),)).fetchall()
    problems = [f"• {t['name']}: {t['what']} — {status_of(t)}"
                for t in rows
                if t["progress"] < 100 and (not t["responded"] or t["progress"] < expected_now())]
    if not problems:
        return
    text = "⚠️ Требуют внимания:\n" + "\n".join(problems)
    for a in admin_ids():
        try:
            await context.bot.send_message(a, text)
        except Exception:
            pass


async def job_morning(context: ContextTypes.DEFAULT_TYPE):
    await send_checkin(context, "morning")


async def job_midday(context: ContextTypes.DEFAULT_TYPE):
    await send_checkin(context, "midday")
    await alert_admins(context)


async def job_evening(context: ContextTypes.DEFAULT_TYPE):
    await send_checkin(context, "evening")
    report = build_report()
    for a in admin_ids():
        try:
            await context.bot.send_message(a, report)
        except Exception:
            pass


# ───────────────────────── Запуск ─────────────────────────
def main():
    if not TOKEN:
        raise SystemExit("Установите переменную окружения BOT_TOKEN (токен от @BotFather).")
    init_db()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("people", people_cmd))
    app.add_handler(CommandHandler("task", add_task))
    app.add_handler(CommandHandler(["report", "tasks"], report_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    jq = app.job_queue
    jq.run_daily(job_morning, time=dtime(10, 0, tzinfo=TZ))
    jq.run_daily(job_midday,  time=dtime(13, 0, tzinfo=TZ))
    jq.run_daily(job_evening, time=dtime(19, 0, tzinfo=TZ))

    print("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
