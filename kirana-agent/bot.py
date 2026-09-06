"""
Telegram entrypoint. Polling-based (simplest to deploy).

- Per-chat conversation history lives IN MEMORY, cleared on /new.
- Preferences live in the DB, so they survive /new and process restarts.
- Telegram redelivers updates on network hiccups -- we dedupe on update_id
  in the DB (ProcessedUpdate) before ever touching the agent, which is the
  outermost idempotency guard on top of the tool-level idempotency_keys.
"""
import os
import logging
from dotenv import load_dotenv
load_dotenv(override=True)

from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from models import init_db, get_session_factory, ProcessedUpdate
from gemini_agent import run_turn
import tools
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kirana-bot")

engine = init_db(os.environ.get("DB_PATH", "kirana.db"))
SessionLocal = get_session_factory(engine)

SHOP_INFO = {
    "name": os.environ.get("SHOP_NAME", "Kirana Store"),
    "gstin": os.environ.get("SHOP_GSTIN", ""),
    "address": os.environ.get("SHOP_ADDRESS", ""),
}

# chat_id -> conversation list (Claude message format)
CONVERSATIONS = {}


def _already_processed(session, update_id: int) -> bool:
    if session.get(ProcessedUpdate, update_id):
        return True
    session.add(ProcessedUpdate(update_id=update_id))
    session.commit()
    return False


async def handle_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    CONVERSATIONS[update.effective_chat.id] = []
    await update.message.reply_text(
        "Started a fresh chat. Your standing preferences (payment default, shop info, "
        "preferred brands) are still remembered — only this conversation was cleared."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = SessionLocal()
    try:
        if _already_processed(session, update.update_id):
            log.info("Skipping duplicate update_id=%s", update.update_id)
            return

        chat_id = update.effective_chat.id
        conversation = CONVERSATIONS.setdefault(chat_id, [])
        user_text = update.message.text

        await update.message.chat.send_action("typing")

        reply_text, file_paths, conversation = run_turn(session, conversation, user_text, SHOP_INFO)
        CONVERSATIONS[chat_id] = conversation

        if reply_text:
            await update.message.reply_text(reply_text)
        for path in file_paths:
            with open(path, "rb") as f:
                await update.message.reply_document(document=f)
    except Exception as e:
        log.exception("Error handling message")
        await update.message.reply_text(f"Something went wrong on my end: {e}")
    finally:
        session.close()


def main():
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("new", handle_new))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
