import asyncio
import logging
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone

import asyncpg
import edge_tts
from gtts import gTTS
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.error import Forbidden, RetryAfter
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ======================= SETTINGS (edit here) =======================
TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_ID = 2094678066
CHANNEL = "@Confessions_org"
CHANNEL_URL = "https://t.me/Confessions_org"

MAX_CHARS = 1000               # max characters per message
COOLDOWN_SECONDS = 5           # wait time between a user's requests
BASE_DAILY_LIMIT = 20          # free voice messages per user per day
REFERRAL_BONUS = 5             # extra daily messages per successful invite
MAX_REFERRAL_BONUS = 50        # cap on the total bonus
SUB_CACHE_SECONDS = 300        # remember "subscribed" for 5 minutes
MAX_PARALLEL_CONVERSIONS = 5   # conversions running at the same time
ALERT_AFTER_FAILURES = 3       # failures in a row before alerting you
ALERT_COOLDOWN_SECONDS = 1800  # at most one alert per type every 30 minutes
DEFAULT_LANGUAGE = "en-US"
DEFAULT_GENDER = "female"

# To add a language later, add one more block here.
LANGUAGES = {
    "en-US": {"label": "🇺🇸 English (US)", "female": "en-US-JennyNeural", "male": "en-US-GuyNeural", "gtts": ("en", "com")},
    "en-GB": {"label": "🇬🇧 English (UK)", "female": "en-GB-SoniaNeural", "male": "en-GB-RyanNeural", "gtts": ("en", "co.uk")},
    "en-NG": {"label": "🇳🇬 English (Nigeria)", "female": "en-NG-EzinneNeural", "male": "en-NG-AbeoNeural", "gtts": ("en", "com.ng")},
    "fr-FR": {"label": "🇫🇷 French", "female": "fr-FR-DeniseNeural", "male": "fr-FR-HenriNeural", "gtts": ("fr", "fr")},
    "es-ES": {"label": "🇪🇸 Spanish", "female": "es-ES-ElviraNeural", "male": "es-ES-AlvaroNeural", "gtts": ("es", "es")},
    "de-DE": {"label": "🇩🇪 German", "female": "de-DE-KatjaNeural", "male": "de-DE-ConradNeural", "gtts": ("de", "de")},
    "pt-BR": {"label": "🇧🇷 Portuguese", "female": "pt-BR-FranciscaNeural", "male": "pt-BR-AntonioNeural", "gtts": ("pt", "com.br")},
    "it-IT": {"label": "🇮🇹 Italian", "female": "it-IT-ElsaNeural", "male": "it-IT-DiegoNeural", "gtts": ("it", "it")},
    "ar-SA": {"label": "🇸🇦 Arabic", "female": "ar-SA-ZariyahNeural", "male": "ar-SA-HamedNeural", "gtts": ("ar", "com")},
    "hi-IN": {"label": "🇮🇳 Hindi", "female": "hi-IN-SwaraNeural", "male": "hi-IN-MadhurNeural", "gtts": ("hi", "co.in")},
}

WELCOME_SUBSCRIBE = (
    "👋 Welcome to the Text-to-Voice Bot!\n\n"
    "I turn your text into natural-sounding voice messages. "
    "To start using the bot, please subscribe to our channel first. "
    "It only takes a moment.\n\n"
    "Tap the button below to join, then press “I've joined” to continue. 🙏"
)

HELP_TEXT = (
    "👋 Welcome! Send me any text and I'll turn it into a voice message.\n\n"
    "/voice - choose a female or male voice\n"
    "/language - choose a language or accent\n"
    "/invite - invite friends and earn extra daily voice messages\n\n"
    f"Maximum {MAX_CHARS} characters per message."
)

# ======================= INTERNAL STATE =======================
pool = None
convert_slots = None
sub_cache = {}      # user_id -> time until which "subscribed" is trusted
last_request = {}   # user_id -> time of last request (for cooldown)
state = {"edge_streak": 0, "total_streak": 0, "last_alert": {}}


def utc_now():
    return datetime.now(timezone.utc)


def day_start():
    return utc_now().replace(hour=0, minute=0, second=0, microsecond=0)


# ======================= DATABASE =======================
async def init_db():
    global pool
    dsn = DATABASE_URL.split("?")[0]  # remove extra options, SSL is set below
    pool = await asyncpg.create_pool(
        dsn,
        ssl="require",
        min_size=1,
        max_size=5,
        command_timeout=30,
        max_inactive_connection_lifetime=30,
        statement_cache_size=0,
    )
    statements = [
        """CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
            gender TEXT NOT NULL DEFAULT 'female',
            language TEXT NOT NULL DEFAULT 'en-US',
            banned BOOLEAN NOT NULL DEFAULT FALSE,
            blocked BOOLEAN NOT NULL DEFAULT FALSE,
            referred_by BIGINT,
            referral_rewarded BOOLEAN NOT NULL DEFAULT FALSE
        )""",
        """CREATE TABLE IF NOT EXISTS conversions (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            ts TIMESTAMPTZ NOT NULL DEFAULT now(),
            gender TEXT NOT NULL,
            language TEXT NOT NULL,
            chars INTEGER NOT NULL,
            engine TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS errors (
            id BIGSERIAL PRIMARY KEY,
            ts TIMESTAMPTZ NOT NULL DEFAULT now(),
            user_id BIGINT,
            message TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_conv_user_ts ON conversions (user_id, ts)",
        "CREATE INDEX IF NOT EXISTS idx_conv_ts ON conversions (ts)",
        "CREATE INDEX IF NOT EXISTS idx_users_referred ON users (referred_by)",
    ]
    for statement in statements:
        await pool.execute(statement)


async def register(update, referrer=None):
    """Save or refresh the user. Returns their record, or None if banned."""
    user_id = update.effective_user.id
    row = await pool.fetchrow(
        """INSERT INTO users (user_id, referred_by) VALUES ($1, $2)
           ON CONFLICT (user_id) DO UPDATE SET last_seen = now(), blocked = FALSE
           RETURNING banned, gender, language, (xmax = 0) AS is_new""",
        user_id,
        referrer,
    )
    if row["banned"] and user_id != ADMIN_ID:
        return None
    return row


async def usage(user_id):
    row = await pool.fetchrow(
        """SELECT
             (SELECT COUNT(*) FROM conversions WHERE user_id = $1 AND ts >= $2) AS used,
             (SELECT COUNT(*) FROM users WHERE referred_by = $1 AND referral_rewarded) AS refs""",
        user_id,
        day_start(),
    )
    bonus = min(row["refs"] * REFERRAL_BONUS, MAX_REFERRAL_BONUS)
    return row["used"], BASE_DAILY_LIMIT + bonus, row["refs"]


async def log_error(user_id, message):
    try:
        await pool.execute(
            "INSERT INTO errors (user_id, message) VALUES ($1, $2)",
            user_id,
            str(message)[:300],
        )
    except Exception:
        logger.exception("Could not log error")


# ======================= ALERTS =======================
async def maybe_alert(bot, key, text):
    now = time.monotonic()
    if now - state["last_alert"].get(key, -1e9) < ALERT_COOLDOWN_SECONDS:
        return
    state["last_alert"][key] = now
    try:
        await bot.send_message(ADMIN_ID, text)
    except Exception:
        logger.exception("Could not alert admin")


async def on_error(update, context):
    logger.error("Unhandled error", exc_info=context.error)
    await maybe_alert(context.bot, "unhandled", f"⚠️ Unexpected bot error: {context.error!r}"[:500])


# ======================= SUBSCRIPTION CHECK =======================
async def check_subscribed(bot, user_id):
    """True/False. Raises an error if the check itself fails."""
    if user_id == ADMIN_ID:
        return True
    expires = sub_cache.get(user_id)
    if expires and expires > time.monotonic():
        return True
    member = await bot.get_chat_member(CHANNEL, user_id)
    ok = member.status in ("member", "administrator", "creator") or (
        member.status == "restricted" and getattr(member, "is_member", False)
    )
    if ok:
        if len(sub_cache) > 20000:
            sub_cache.clear()
        sub_cache[user_id] = time.monotonic() + SUB_CACHE_SECONDS
    else:
        sub_cache.pop(user_id, None)
    return ok


def join_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Join our channel", url=CHANNEL_URL)],
            [InlineKeyboardButton("✅ I've joined", callback_data="check_sub")],
        ]
    )


async def ensure_subscribed(update, context):
    """Returns True if the user may continue. Otherwise sends the subscribe message."""
    try:
        ok = await check_subscribed(context.bot, update.effective_user.id)
    except Exception:
        logger.exception("Subscription check failed")
        await maybe_alert(
            context.bot,
            "subcheck",
            "⚠️ I can't verify channel subscriptions. Make sure I'm an admin of the channel.",
        )
        await update.message.reply_text(
            "I couldn't verify your subscription right now. Please try again in a moment."
        )
        return False
    if not ok:
        await update.message.reply_text(WELCOME_SUBSCRIBE, reply_markup=join_keyboard())
    return ok


async def recheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        ok = await check_subscribed(context.bot, query.from_user.id)
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


# ======================= TEXT TO SPEECH =======================
async def synthesize(bot, user_id, text, language, gender, path):
    """Creates the audio file. Tries edge-tts twice, then falls back to gTTS."""
    lang = LANGUAGES.get(language, LANGUAGES[DEFAULT_LANGUAGE])
    voice_name = lang[gender] if gender in ("female", "male") else lang["female"]

    edge_error = None
    for _ in range(2):
        try:
            await asyncio.wait_for(edge_tts.Communicate(text, voice_name).save(path), timeout=30)
            state["edge_streak"] = 0
            state["total_streak"] = 0
            return "edge"
        except Exception as e:
            edge_error = e
            await asyncio.sleep(1)

    state["edge_streak"] += 1
    await log_error(user_id, f"edge-tts: {edge_error!r}")
    if state["edge_streak"] >= ALERT_AFTER_FAILURES:
        await maybe_alert(
            bot,
            "edge",
            "⚠️ The main voice engine (edge-tts) has failed several times in a row. "
            "Using the backup engine (single voice, no male/female choice).",
        )

    try:
        code, tld = lang["gtts"]
        await asyncio.to_thread(lambda: gTTS(text, lang=code, tld=tld).save(path))
        state["total_streak"] = 0
        return "gtts"
    except Exception as e:
        state["total_streak"] += 1
        await log_error(user_id, f"gTTS: {e!r}")
        if state["total_streak"] >= ALERT_AFTER_FAILURES:
            await maybe_alert(
                bot,
                "total",
                "🚨 Both voice engines are failing. Users are not getting voice messages.",
            )
        raise


async def reward_referrer(bot, user_id):
    referrer = await pool.fetchval(
        """UPDATE users SET referral_rewarded = TRUE
           WHERE user_id = $1 AND referred_by IS NOT NULL AND NOT referral_rewarded
           RETURNING referred_by""",
        user_id,
    )
    if referrer:
        try:
            await bot.send_message(
                referrer,
                "🎉 A friend you invited just used the bot! Your daily limit has increased.",
            )
        except Exception:
            pass


async def speak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    rec = await register(update)
    if rec is None:
        return
    if not await ensure_subscribed(update, context):
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    if user_id != ADMIN_ID:
        now = time.monotonic()
        if now - last_request.get(user_id, -1e9) < COOLDOWN_SECONDS:
            await update.message.reply_text("⏳ Please wait a few seconds before sending another message.")
            return
        if len(last_request) > 20000:
            last_request.clear()
        last_request[user_id] = now

    if len(text) > MAX_CHARS:
        await update.message.reply_text(f"Text is too long. Maximum is {MAX_CHARS} characters.")
        return

    if user_id != ADMIN_ID:
        used, limit, _ = await usage(user_id)
        if used >= limit:
            await update.message.reply_text(
                f"You've reached today's limit of {limit} voice messages. "
                "It resets at midnight UTC.\n\n"
                "🎁 Use /invite to earn more daily voice messages."
            )
            return

    await context.bot.send_chat_action(update.effective_chat.id, "record_voice")

    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            path = f.name
        async with convert_slots:
            engine = await synthesize(
                context.bot, user_id, text, rec["language"], rec["gender"], path
            )
        with open(path, "rb") as audio:
            await update.message.reply_voice(audio)
        await pool.execute(
            "INSERT INTO conversions (user_id, gender, language, chars, engine) VALUES ($1, $2, $3, $4, $5)",
            user_id,
            rec["gender"],
            rec["language"],
            len(text),
            engine,
        )
        await reward_referrer(context.bot, user_id)
    except Exception:
        logger.exception("Conversion failed")
        await update.message.reply_text("Sorry, something went wrong. Please try again.")
    finally:
        if path and os.path.exists(path):
            os.remove(path)


# ======================= USER COMMANDS =======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    referrer = None
    if context.args and context.args[0].startswith("ref_"):
        try:
            candidate = int(context.args[0][4:])
        except ValueError:
            candidate = None
        if candidate and candidate != user_id:
            if await pool.fetchval("SELECT 1 FROM users WHERE user_id = $1", candidate):
                referrer = candidate

    rec = await register(update, referrer)
    if rec is None:
        return
    if not await ensure_subscribed(update, context):
        return
    await update.message.reply_text(HELP_TEXT)


def mark(label, selected):
    return f"✅ {label}" if selected else label


async def voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rec = await register(update)
    if rec is None:
        return
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(mark("Female", rec["gender"] == "female"), callback_data="g:female"),
            InlineKeyboardButton(mark("Male", rec["gender"] == "male"), callback_data="g:male"),
        ]]
    )
    await update.message.reply_text("Choose a voice:", reply_markup=keyboard)


async def choose_gender(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    gender = query.data.split(":")[1]
    if gender not in ("female", "male"):
        return
    rec = await register(update)
    if rec is None:
        return
    await pool.execute("UPDATE users SET gender = $2 WHERE user_id = $1", query.from_user.id, gender)
    await query.edit_message_text(f"Voice set to {gender}. ✅")


async def language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rec = await register(update)
    if rec is None:
        return
    buttons = [
        InlineKeyboardButton(mark(info["label"], code == rec["language"]), callback_data=f"l:{code}")
        for code, info in LANGUAGES.items()
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    await update.message.reply_text("Choose a language or accent:", reply_markup=InlineKeyboardMarkup(rows))


async def choose_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    code = query.data.split(":", 1)[1]
    if code not in LANGUAGES:
        return
    rec = await register(update)
    if rec is None:
        return
    await pool.execute("UPDATE users SET language = $2 WHERE user_id = $1", query.from_user.id, code)
    await query.edit_message_text(f"Language set to {LANGUAGES[code]['label']}. ✅")


async def invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rec = await register(update)
    if rec is None:
        return
    user_id = update.effective_user.id
    used, limit, refs = await usage(user_id)
    link = f"https://t.me/{context.bot.username}?start=ref_{user_id}"
    await update.message.reply_text(
        "🎁 Invite friends and earn more daily voice messages!\n\n"
        f"Your link:\n{link}\n\n"
        f"For every friend who joins and uses the bot, your daily limit goes up by "
        f"{REFERRAL_BONUS} (up to +{MAX_REFERRAL_BONUS}).\n\n"
        f"Friends who joined: {refs}\n"
        f"Today: {used}/{limit} voice messages used"
    )


# ======================= ADMIN COMMANDS =======================
def is_admin_private(update):
    return update.effective_user.id == ADMIN_ID and update.effective_chat.type == "private"


async def dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_private(update):
        return
    today = day_start()
    week = utc_now() - timedelta(days=7)

    s = await pool.fetchrow(
        """SELECT
          (SELECT COUNT(*) FROM users) AS total_users,
          (SELECT COUNT(*) FROM users WHERE first_seen >= $1) AS new_today,
          (SELECT COUNT(*) FROM users WHERE first_seen >= $2) AS new_week,
          (SELECT COUNT(*) FROM users WHERE last_seen >= $1) AS active_today,
          (SELECT COUNT(*) FROM users WHERE last_seen >= $2) AS active_week,
          (SELECT COUNT(*) FROM users WHERE banned) AS banned,
          (SELECT COUNT(*) FROM users WHERE blocked) AS blocked,
          (SELECT COUNT(*) FROM users WHERE referred_by IS NOT NULL) AS referred,
          (SELECT COUNT(*) FROM conversions) AS total_conv,
          (SELECT COUNT(*) FROM conversions WHERE ts >= $1) AS conv_today,
          (SELECT COUNT(*) FROM conversions WHERE ts >= $2) AS conv_week,
          (SELECT COUNT(DISTINCT user_id) FROM conversions) AS converters,
          (SELECT COALESCE(SUM(chars), 0) FROM conversions) AS chars,
          (SELECT COUNT(*) FROM conversions WHERE engine = 'gtts' AND ts >= $1) AS fallback_today,
          (SELECT COUNT(*) FROM errors WHERE ts >= $1) AS errors_today""",
        today,
        week,
    )
    langs = await pool.fetch(
        "SELECT language, COUNT(*) AS n FROM conversions GROUP BY language ORDER BY n DESC LIMIT 5"
    )
    genders = {
        r["gender"]: r["n"]
        for r in await pool.fetch("SELECT gender, COUNT(*) AS n FROM conversions GROUP BY gender")
    }

    lang_lines = "\n".join(
        f"{LANGUAGES.get(r['language'], {}).get('label', r['language'])}: {r['n']}" for r in langs
    ) or "No data yet"
    pct = round(100 * s["converters"] / s["total_users"]) if s["total_users"] else 0

    await update.message.reply_text(
        "📊 ADMIN DASHBOARD (times in UTC)\n\n"
        "👥 Users\n"
        f"Total: {s['total_users']}\n"
        f"New today: {s['new_today']}\n"
        f"New last 7 days: {s['new_week']}\n"
        f"Active today: {s['active_today']}\n"
        f"Active last 7 days: {s['active_week']}\n"
        f"Used the bot at least once: {s['converters']} ({pct}%)\n"
        f"Came from referrals: {s['referred']}\n"
        f"Banned: {s['banned']}\n"
        f"Blocked the bot: {s['blocked']}\n\n"
        "🎙 Voice messages\n"
        f"Total: {s['total_conv']}\n"
        f"Today: {s['conv_today']}\n"
        f"Last 7 days: {s['conv_week']}\n"
        f"Characters converted: {s['chars']}\n\n"
        "🗣 Voice usage\n"
        f"Female: {genders.get('female', 0)}\n"
        f"Male: {genders.get('male', 0)}\n\n"
        f"🌍 Top languages\n{lang_lines}\n\n"
        "🛠 Health today\n"
        f"Errors: {s['errors_today']}\n"
        f"Backup engine used: {s['fallback_today']}"
    )


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_private(update):
        return
    msg = update.message
    parts = msg.text.split(None, 1)
    text = parts[1].strip() if len(parts) > 1 else ""
    if not text and msg.reply_to_message:
        text = (msg.reply_to_message.text or msg.reply_to_message.caption or "").strip()
    if not text:
        await msg.reply_text(
            "Usage: /broadcast your message here\n"
            "(or reply to a text message with /broadcast)"
        )
        return
    n = await pool.fetchval("SELECT COUNT(*) FROM users WHERE NOT banned AND NOT blocked")
    context.bot_data["pending_broadcast"] = text
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Send", callback_data="bc:send"),
            InlineKeyboardButton("❌ Cancel", callback_data="bc:cancel"),
        ]]
    )
    await msg.reply_text(
        f"📣 Broadcast preview:\n\n{text}\n\nSend this to {n} users?",
        reply_markup=keyboard,
    )


async def broadcast_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    action = query.data.split(":")[1]
    text = context.bot_data.pop("pending_broadcast", None)
    if action == "cancel":
        await query.edit_message_text("Broadcast cancelled.")
        return
    if not text:
        await query.edit_message_text("Nothing to send (it expired). Run /broadcast again.")
        return

    await query.edit_message_text("📤 Sending... I'll message you when it's done.")
    rows = await pool.fetch("SELECT user_id FROM users WHERE NOT banned AND NOT blocked")
    sent = failed = blocked = 0
    for row in rows:
        uid = row["user_id"]
        try:
            await context.bot.send_message(uid, text)
            sent += 1
        except RetryAfter as e:
            delay = e.retry_after.total_seconds() if hasattr(e.retry_after, "total_seconds") else e.retry_after
            await asyncio.sleep(delay + 1)
            try:
                await context.bot.send_message(uid, text)
                sent += 1
            except Exception:
                failed += 1
        except Forbidden:
            blocked += 1
            await pool.execute("UPDATE users SET blocked = TRUE WHERE user_id = $1", uid)
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await context.bot.send_message(
        ADMIN_ID,
        f"✅ Broadcast finished.\nDelivered: {sent}\nBlocked the bot: {blocked}\nFailed: {failed}",
    )


async def set_ban(update, context, banned):
    if not is_admin_private(update):
        return
    word = "ban" if banned else "unban"
    try:
        target = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text(f"Usage: /{word} USER_ID")
        return
    if target == ADMIN_ID:
        await update.message.reply_text("You can't do that to the admin account.")
        return
    status = await pool.execute("UPDATE users SET banned = $2 WHERE user_id = $1", target, banned)
    if status.endswith(" 0"):
        await update.message.reply_text("User not found. They must have used the bot at least once.")
    else:
        await update.message.reply_text(f"User {target} {'banned' if banned else 'unbanned'}. ✅")


async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_ban(update, context, True)


async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_ban(update, context, False)


# ======================= STARTUP =======================
async def post_init(app):
    global convert_slots
    await init_db()
    convert_slots = asyncio.Semaphore(MAX_PARALLEL_CONVERSIONS)
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Start the bot"),
            BotCommand("voice", "Choose female or male voice"),
            BotCommand("language", "Choose language or accent"),
            BotCommand("invite", "Invite friends, earn more messages"),
        ]
    )


async def post_shutdown(app):
    if pool:
        await pool.close()


def main():
    defaults = Defaults(link_preview_options=LinkPreviewOptions(is_disabled=True))
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .defaults(defaults)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("voice", voice))
    app.add_handler(CommandHandler("language", language))
    app.add_handler(CommandHandler("invite", invite))
    app.add_handler(CommandHandler("dashboard", dashboard))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CallbackQueryHandler(recheck, pattern="^check_sub$"))
    app.add_handler(CallbackQueryHandler(choose_gender, pattern="^g:"))
    app.add_handler(CallbackQueryHandler(choose_language, pattern="^l:"))
    app.add_handler(CallbackQueryHandler(broadcast_button, pattern="^bc:"))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, speak)
    )
    app.add_error_handler(on_error)
    app.run_polling()


if __name__ == "__main__":
    main()
