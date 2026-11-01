import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import edge_tts
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = 2094678066
CHANNEL = "@Confessions_org"
CHANNEL_URL = "https://t.me/Confessions_org"
DB_PATH = os.environ.get("DB_PATH", "bot.db")

# Add more voices here later
VOICES = {
    "female": "en-US-JennyNeural",
    "male": "en-US-GuyNeural",
}
DEFAULT_VOICE = "female"
MAX_CHARS = 1000

# ---------------- Database ----------------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute(
    """CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        voice TEXT NOT NULL DEFAULT 'female'
    )"""
)
db.execute(
    """CREATE TABLE IF NOT EXISTS conversions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        ts TEXT NOT NULL,
        voice TEXT NOT NULL,
        chars INTEGER NOT NULL
    )"""
)
db.commit()


def now():
    return datetime.now(timezone.utc)


def track_user(user_id):
    ts = now().isoformat()
    db.execute(
        "INSERT OR IGNORE INTO users (user_id, first_seen, last_seen) VALUES (?, ?, ?)",
        (user_id, ts, ts),
    )
    db.execute("UPDATE users SET last_seen = ? WHERE user_id = ?", (ts, user_id))
    db.commit()


def get_voice(user_id):
    row = db.execute(
        "SELECT voice FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    return row[0] if row and row[0] in VOICES else DEFAULT_VOICE


def set_voice(user_id, voice):
    db.execute("UPDATE users SET voice = ? WHERE user_id = ?", (voice, user_id))
    db.commit()


def log_conversion(user_id, voice, chars):
    db.execute(
        "INSERT INTO conversions (user_id, ts, voice, chars) VALUES (?, ?, ?, ?)",
        (user_id, now().isoformat(), voice, chars),
    )
    db.commit()


def count(query, params=()):
    return db.execute(query, params).fetchone()[0]


# ---------------- Subscription check ----------------
async def is_member(bot, user_id):
    """True/False. Raises an error if the check itself fails."""
    member = await bot.get_chat_member(CHANNEL, user_id)
    if member.status in ("member", "administrator", "creator"):
        return True
    if member.status == "restricted" and getattr(member, "is_member", False):
        return True
    return False


def join_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Join the channel", url=CHANNEL_URL)],
            [InlineKeyboardButton("✅ I've joined", callback_data="check_sub")],
        ]
    )


async def recheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        ok = await is_member(context.bot, query.from_user.id)
    except Exception:
        logger.exception("Subscription check failed")
        await query.answer("Couldn't verify right now. Try again shortly.", show_alert=True)
        return
    if ok:
        await query.answer()
        await query.edit_message_text(
            "Thanks for joining! ✅ Now send me any text and I'll turn it into a voice message."
        )
    else:
        await query.answer("You haven't joined the channel yet.", show_alert=True)


# ---------------- Handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user.id)
    await update.message.reply_text(
        "Send me any text and I'll turn it into a voice message.\n"
        "Use /voice to choose a female or male voice.\n\n"
        f"You must be subscribed to {CHANNEL_URL} to use the bot."
    )


async def voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user.id)
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Female", callback_data="female"),
            InlineKeyboardButton("Male", callback_data="male"),
        ]]
    )
    await update.message.reply_text("Choose a voice:", reply_markup=keyboard)


async def choose_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    track_user(query.from_user.id)
    if query.data in VOICES:
        set_voice(query.from_user.id, query.data)
        await query.edit_message_text(f"Voice set to {query.data}.")


async def speak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    track_user(user_id)

    # Subscription gate (admin skips it)
    if user_id != ADMIN_ID:
        try:
            ok = await is_member(context.bot, user_id)
        except Exception:
            logger.exception("Subscription check failed")
            await update.message.reply_text(
                "I couldn't verify your subscription right now. Please try again in a moment."
            )
            return
        if not ok:
            await update.message.reply_text(
                "To use this bot, you need to subscribe to our channel first. "
                "Join it, then tap “I've joined”.",
                reply_markup=join_keyboard(),
            )
            return

    text = update.message.text
    if len(text) > MAX_CHARS:
        await update.message.reply_text(f"Text is too long. Max {MAX_CHARS} characters.")
        return

    choice = get_voice(user_id)
    await context.bot.send_chat_action(update.effective_chat.id, "record_voice")

    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            path = f.name
        await edge_tts.Communicate(text, VOICES[choice]).save(path)
        with open(path, "rb") as audio:
            await update.message.reply_voice(audio)
        log_conversion(user_id, choice, len(text))
    except Exception:
        logger.exception("Conversion failed")
        await update.message.reply_text("Sorry, something went wrong. Try again.")
    finally:
        if path and os.path.exists(path):
            os.remove(path)


async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin only, private chat only. Everyone else is ignored silently.
    if update.effective_user.id != ADMIN_ID or update.effective_chat.type != "private":
        return

    today = now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    week = (now() - timedelta(days=7)).isoformat()

    total_users = count("SELECT COUNT(*) FROM users")
    new_today = count("SELECT COUNT(*) FROM users WHERE first_seen >= ?", (today,))
    new_week = count("SELECT COUNT(*) FROM users WHERE first_seen >= ?", (week,))
    active_today = count("SELECT COUNT(*) FROM users WHERE last_seen >= ?", (today,))
    active_week = count("SELECT COUNT(*) FROM users WHERE last_seen >= ?", (week,))

    total_voices = count("SELECT COUNT(*) FROM conversions")
    voices_today = count("SELECT COUNT(*) FROM conversions WHERE ts >= ?", (today,))
    voices_week = count("SELECT COUNT(*) FROM conversions WHERE ts >= ?", (week,))
    users_converted = count("SELECT COUNT(DISTINCT user_id) FROM conversions")
    total_chars = count("SELECT COALESCE(SUM(chars), 0) FROM conversions")
    female = count("SELECT COUNT(*) FROM conversions WHERE voice = 'female'")
    male = count("SELECT COUNT(*) FROM conversions WHERE voice = 'male'")

    await update.message.reply_text(
        "📊 ADMIN DASHBOARD (times in UTC)\n\n"
        "👥 Users\n"
        f"Total: {total_users}\n"
        f"New today: {new_today}\n"
        f"New last 7 days: {new_week}\n"
        f"Active today: {active_today}\n"
        f"Active last 7 days: {active_week}\n"
        f"Used the bot at least once: {users_converted}\n\n"
        "🎙 Voice messages\n"
        f"Total: {total_voices}\n"
        f"Today: {voices_today}\n"
        f"Last 7 days: {voices_week}\n"
        f"Characters converted: {total_chars}\n\n"
        "🗣 Voice usage\n"
        f"Female: {female}\n"
        f"Male: {male}"
    )


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("voice", voice))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(CallbackQueryHandler(recheck, pattern="^check_sub$"))
    app.add_handler(CallbackQueryHandler(choose_voice, pattern="^(female|male)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, speak))
    app.run_polling()


if __name__ == "__main__":
    main()
