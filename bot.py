"""CodePro Telegram bot - free API key distribution.

Uses the same key store as backend.py (data/keys.json).
Runs in the MAIN thread (PTB requirement); see run.py.
"""
import os, time
from dotenv import load_dotenv
load_dotenv()

os.environ.pop("no_proxy", None)
os.environ.pop("NO_PROXY", None)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          ContextTypes)

import backend as B

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN required in env")


def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Get API Key", callback_data="getkey"),
         InlineKeyboardButton("📋 My Key", callback_data="mykey")],
        [InlineKeyboardButton("🤖 Models", callback_data="models"),
         InlineKeyboardButton("❓ Help", callback_data="help")],
    ])


WELCOME = (
    "🚀 Welcome to CodePro!\n\n"
    "Free AI API, powered by Vidhyantra.\n\n"
    "• Models: codepro (coding), sol / terra / ultra / quick / chatgpt56 / code (chat)\n"
    "• OpenAI-compatible: works with Cline, CodeGPT & more\n"
    "• Base URL: https://codepro-ai.onrender.com/v1\n\n"
    "Tap 🔑 Get API Key to start — it's free!"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, reply_markup=main_kb())


def _get_or_create_key(user_id: int):
    keys = B.load_keys()
    for k, rec in keys.items():
        if rec.get("tg_id") == user_id:
            return k, False
    k = B.new_api_key()
    keys[k] = {"created": int(time.time()), "credits": 100000,
               "tg_id": user_id}
    B.save_keys(keys)
    return k, True


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    d = q.data
    if d == "getkey" or d == "mykey":
        key, is_new = _get_or_create_key(uid)
        label = "🎉 New key generated!" if is_new else "📋 Your API key:"
        await q.edit_message_text(
            f"{label}\n\n{key}\n\n"
            f"Use as Authorization: Bearer <key>\n"
            f"Base URL: https://codepro-ai.onrender.com/v1\n\n"
            "⚠️ Keep it private!",
            reply_markup=main_kb())
    elif d == "models":
        await q.edit_message_text(
            "🤖 Available models:\n\n"
            "• codepro — coding & agentic tasks\n"
            "• sol / terra / ultra / quick / chatgpt56 — chat\n"
            "• code — coding via Get API\n\n"
            "OpenAI-compatible via /v1/chat/completions.",
            reply_markup=main_kb())
    elif d == "help":
        await q.edit_message_text(
            "❓ CodePro Help\n\n"
            "🔑 Get API Key — generate your free key\n"
            "📋 My Key — view your existing key\n"
            "🤖 Models — see available models\n\n"
            "Use the key in Cline / CodeGPT / any OpenAI client:\n"
            "https://codepro-ai.onrender.com/v1",
            reply_markup=main_kb())


async def getkey_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key, is_new = _get_or_create_key(update.effective_user.id)
    label = "🎉 New key generated!" if is_new else "📋 Your API key:"
    await update.message.reply_text(
        f"{label}\n\n{key}\n\nBase URL: "
        "https://codepro-ai.onrender.com/v1\n⚠️ Keep it private!")


def main():
    # Fresh event loop per run (self-healing pattern)
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    app = (Application.builder().token(BOT_TOKEN)
           .read_timeout(30).write_timeout(30).connect_timeout(30)
           .build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("getkey", getkey_cmd))
    app.add_handler(CallbackQueryHandler(on_button))

    print("CodePro bot polling...", flush=True)
    app.run_polling(bootstrap_retries=-1, close_loop=False,
                    stop_signals=[])


if __name__ == "__main__":
    main()
