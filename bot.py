import os
import tempfile

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

TOKEN = os.environ["BOT_TOKEN"]

# Add more voices here later
VOICES = {
    "female": "en-US-JennyNeural",
    "male": "en-US-GuyNeural",
}
DEFAULT_VOICE = "female"
MAX_CHARS = 1000


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me any text and I'll turn it into a voice message.\n"
        "Use /voice to choose a female or male voice."
    )


async def voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    if query.data in VOICES:
        context.user_data["voice"] = query.data
        await query.edit_message_text(f"Voice set to {query.data}.")


async def speak(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if len(text) > MAX_CHARS:
        await update.message.reply_text(
            f"Text is too long. Max {MAX_CHARS} characters."
        )
        return

    choice = context.user_data.get("voice", DEFAULT_VOICE)
    await context.bot.send_chat_action(update.effective_chat.id, "record_voice")

    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            path = f.name
        await edge_tts.Communicate(text, VOICES[choice]).save(path)
        with open(path, "rb") as audio:
            await update.message.reply_voice(audio)
    except Exception:
        await update.message.reply_text("Sorry, something went wrong. Try again.")
    finally:
        if path and os.path.exists(path):
            os.remove(path)


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("voice", voice))
    app.add_handler(CallbackQueryHandler(choose_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, speak))
    app.run_polling()


if __name__ == "__main__":
    main()
