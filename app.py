from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from rag_engine import RagEngine

load_dotenv()

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


@dataclass
class Exchange:
    question: str
    answer: str


class BotState:
    def __init__(self) -> None:
        self.history: dict[int, deque[Exchange]] = defaultdict(lambda: deque(maxlen=3))


state = BotState()


def get_config() -> dict[str, str | int]:
    return {
        "token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "knowledge_dir": os.getenv("KNOWLEDGE_DIR", "knowledge_base"),
        "db_path": os.getenv("DB_PATH", "rag_store.db"),
        "embedding_model": os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2"),
        "ollama_host": os.getenv("OLLAMA_HOST", "http://localhost:11434"),
        "ollama_model": os.getenv("OLLAMA_MODEL", "llama3.2:3b"),
        "top_k": int(os.getenv("TOP_K", "4")),
        "max_context_chars": int(os.getenv("MAX_CONTEXT_CHARS", "3500")),
    }


CONFIG = get_config()
RAG = RagEngine(
    knowledge_dir=str(CONFIG["knowledge_dir"]),
    db_path=str(CONFIG["db_path"]),
    embedding_model=str(CONFIG["embedding_model"]),
    ollama_host=str(CONFIG["ollama_host"]),
    ollama_model=str(CONFIG["ollama_model"]),
    top_k=int(CONFIG["top_k"]),
    max_context_chars=int(CONFIG["max_context_chars"]),
)


def split_message(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    cursor = 0
    while cursor < len(text):
        chunks.append(text[cursor : cursor + limit])
        cursor += limit
    return chunks


async def safe_reply(update: Update, text: str) -> None:
    if not update.message:
        return

    for part in split_message(text):
        await update.message.reply_text(part)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "Mini-RAG Telegram Bot\n\n"
        "Commands:\n"
        "/ask <question> - Ask from indexed knowledge docs\n"
        "/help - Show bot usage\n"
        "/image - Not enabled in this build (Option A only)\n"
        "/summarize - Summarize your last bot answer\n\n"
        "Tip: You can also send a plain text message, and I treat it as /ask."
    )
    await safe_reply(update, help_text)


async def image_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_reply(
        update,
        "This bot is configured for Option A (Mini-RAG) only. Use /ask for text questions.",
    )


async def image_upload_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_reply(
        update,
        "Image description is disabled in this submission. This is an Option A Mini-RAG bot.",
    )


async def ask_and_reply(update: Update, question: str) -> None:
    if not update.message:
        return

    user = update.effective_user
    user_id = user.id if user else 0

    start = time.perf_counter()
    answer, chunks = await asyncio.to_thread(RAG.answer, question)
    elapsed = time.perf_counter() - start

    state.history[user_id].append(Exchange(question=question, answer=answer))

    source_names = sorted({item.source for item in chunks})
    source_line = f"\n\nSources: {', '.join(source_names)}" if source_names else ""
    footer = f"\n\nResponse time: {elapsed:.2f}s"

    await safe_reply(update, answer + source_line + footer)


async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = " ".join(context.args).strip()
    if not question:
        await safe_reply(update, "Usage: /ask <your question>")
        return

    await ask_and_reply(update, question)


async def summarize_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user = update.effective_user
    user_id = user.id if user else 0
    history = state.history.get(user_id)

    if not history:
        await safe_reply(update, "No previous answer found. Use /ask first.")
        return

    latest_answer = history[-1].answer
    summary = await asyncio.to_thread(RAG.summarize_text, latest_answer)
    await safe_reply(update, f"Summary:\n{summary}")


async def text_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if not text:
        return

    await ask_and_reply(update, text)


async def on_startup(app: Application) -> None:
    logger.info("Indexing knowledge base...")
    indexed_files, indexed_chunks = await asyncio.to_thread(RAG.index_knowledge_base)
    logger.info("Index completed. files=%s chunks=%s", indexed_files, indexed_chunks)


def main() -> None:
    token = str(CONFIG["token"])
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Create .env from .env.example")

    asyncio.set_event_loop(asyncio.new_event_loop())

    application = Application.builder().token(token).post_init(on_startup).build()

    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("ask", ask_command))
    application.add_handler(CommandHandler("image", image_command))
    application.add_handler(CommandHandler("summarize", summarize_command))
    application.add_handler(MessageHandler(filters.PHOTO, image_upload_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_query_handler))

    logger.info("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
