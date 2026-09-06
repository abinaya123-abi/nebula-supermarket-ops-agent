"""
Local command-line interface to test the agent WITHOUT Telegram.
Same agent brain (claude_agent.run_turn), same DB, same tools --
just typed in your terminal instead of a Telegram chat.

NOTE: The assignment requires a live Telegram bot as the actual
interface for submission/grading. This script is only for your own
local testing/development while you sort out Telegram access.

Usage:
    python cli_chat.py
Type '/new' to reset the conversation (preferences still persist).
Type 'exit' or Ctrl+C to quit.
"""
import os
from dotenv import load_dotenv
load_dotenv()

from models import init_db, get_session_factory
from gemini_agent import run_turn

engine = init_db(os.environ.get("DB_PATH", "kirana.db"))
SessionLocal = get_session_factory(engine)

SHOP_INFO = {
    "name": os.environ.get("SHOP_NAME", "Kirana Store"),
    "gstin": os.environ.get("SHOP_GSTIN", ""),
    "address": os.environ.get("SHOP_ADDRESS", ""),
}


def main():
    session = SessionLocal()
    conversation = []
    print(f"--- {SHOP_INFO['name']} Ops Agent (CLI mode) ---")
    print("Type '/new' to reset conversation, 'exit' to quit.\n")

    while True:
        try:
            user_text = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBye!")
            break

        if not user_text:
            continue
        if user_text.lower() == "exit":
            break
        if user_text == "/new":
            conversation = []
            print("Agent: Started a fresh chat. Standing preferences are still remembered.\n")
            continue

        reply_text, file_paths, conversation = run_turn(session, conversation, user_text, SHOP_INFO)
        print(f"Agent: {reply_text}\n")
        for path in file_paths:
            print(f"[Generated file: {path}]\n")

    session.close()


if __name__ == "__main__":
    main()
