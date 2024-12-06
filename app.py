import asyncio
import os
import logging
import sqlite3
from contextlib import closing
import aiosqlite
from functools import wraps

import telegram
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters, CommandHandler
import openai

# Настройки
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ALLOWED_CHAT_IDS = os.environ.get("ALLOWED_CHAT_IDS", "")
BOT_USERNAME = None

openai.api_key = OPENAI_API_KEY

# Логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Инициализация БД
DB_FILE = "data/conversation.db"

async def init_db():
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            message_id INTEGER,
            reply_to_message_id INTEGER,
            role TEXT,
            content TEXT
        )
        """)
        await conn.commit()

async def save_message(chat_id, message_id, reply_to_message_id, role, content):
    async with aiosqlite.connect(DB_FILE) as conn:
        await conn.execute(
            "INSERT INTO messages (chat_id, message_id, reply_to_message_id, role, content) VALUES (?, ?, ?, ?, ?)",
            (chat_id, message_id, reply_to_message_id, role, content)
        )
        await conn.commit()

async def get_conversation_chain(chat_id, start_message_id):
    """Получаем всю цепочку сообщений (ветку) начиная с переданного message_id до корня."""
    async with aiosqlite.connect(DB_FILE) as conn:
        conn.row_factory = aiosqlite.Row

        async def get_parent_mid(mid):
            async with conn.execute(
                "SELECT reply_to_message_id FROM messages WHERE message_id=? AND chat_id=?", 
                (mid, chat_id)
            ) as cursor:
                row = await cursor.fetchone()
            return row["reply_to_message_id"] if row else None

        # Найдём всю цепочку от текущего сообщения до корня
        chain = [start_message_id]
        current = start_message_id
        while True:
            parent = await get_parent_mid(current)
            if parent is None or parent == 0:
                break
            chain.append(parent)
            current = parent

        placeholders = ",".join("?" * len(chain))
        async with conn.execute(
            f"SELECT * FROM messages WHERE chat_id=? AND message_id IN ({placeholders})",
            [chat_id] + chain
        ) as cursor:
            rows = await cursor.fetchall()

        msg_map = {r["message_id"]: r for r in rows}
        chain = chain[::-1]
        
        return [{"role": msg_map[mid]["role"], "content": msg_map[mid]["content"]} 
                for mid in chain]

async def handle_message(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_USERNAME
    if not BOT_USERNAME:
        bot_info = await context.bot.getMe()
        if not bot_info:
            raise Exception("Не удалось получить информацию о боте")
        BOT_USERNAME = bot_info.username

    # Обрабатываем все сообщения: и групповые, и нет. Но основная логика для группового.
    message = update.effective_message

    # Определяем роль: если сообщение от бота - assistant, иначе user
    role = "assistant" if message.from_user and message.from_user.is_bot else "user"
    content = message.text or message.caption or ""
    reply_to_message_id = message.reply_to_message.message_id if message.reply_to_message else None

    # Сохраняем ВСЕ сообщения в БД
    await save_message(
        chat_id=message.chat_id,
        message_id=message.message_id,
        reply_to_message_id=reply_to_message_id,
        role=role,
        content=content
    )

    # Проверяем, нужно ли отправлять запрос к модели:
    # 1. Если сообщение упоминает бота (@username)
    # 2. Или если сообщение является ответом на сообщение бота
    mention_bot = f"@{BOT_USERNAME.lower()}" in content.lower()
    replying_to_bot = (message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.username == BOT_USERNAME)

    if mention_bot or replying_to_bot or message.chat.type == "private":
        # Отправляем "печатает..."
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

        if replying_to_bot:
            # Получаем всю ветку сообщений для контекста
            conversation = await get_conversation_chain(
                chat_id=message.chat_id,
                start_message_id=message.reply_to_message.message_id
            )
            # Добавляем текущее сообщение пользователя в конец
            # Текущее сообщение - всегда user (т.к. обращаются к боту)
            # Если по каким-то причинам это бот, игнорируем (но такого не случится обычно)
            if role == "user":
                # Заменим контент, убрав упоминание бота, если оно есть
                user_message = content.replace(BOT_USERNAME, "").strip()
                conversation.append({"role": "user", "content": user_message})
        else:
            # Если просто упомянули бота, но не ответили на его сообщение, начинаем новую ветку
            user_message = content.replace(BOT_USERNAME, "").strip()
            conversation = [{"role": "user", "content": user_message}]

        # Запрос к OpenAI
        try:
            response = openai.chat.completions.create(
                model="gpt-4o",
                messages=conversation
            )
            answer = response.choices[0].message.content.strip()
        except Exception as e:
            logging.error(e)
            answer = "Произошла ошибка при запросе к модели."

        sent_message = await message.reply_text(answer)
        # Сохраняем ответ бота
        await save_message(
            chat_id=sent_message.chat_id,
            message_id=sent_message.message_id,
            reply_to_message_id=message.message_id,
            role="assistant",
            content=answer
        )

async def start_command(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот запущен. Пишите запросы, упоминая меня в групповом чате, или отвечайте на мои сообщения.")

def check_allowed_chat():
    def decorator(func):
        @wraps(func)
        async def wrapper(update: telegram.Update, context: ContextTypes.DEFAULT_TYPE):
            if not ALLOWED_CHAT_IDS:  # If empty string, allow all
                return await func(update, context)
            allowed_chat_ids = [int(chat_id) for chat_id in ALLOWED_CHAT_IDS.split(",") if chat_id]
            if update.effective_chat.id not in allowed_chat_ids:
                logging.info(f"Chat {update.effective_chat.id} is not allowed")
                return
            return await func(update, context)
        return wrapper
    return decorator


if __name__ == "__main__":
    try:
        bot = ApplicationBuilder().token(BOT_TOKEN).build()
        bot.add_handler(CommandHandler("start", check_allowed_chat()(start_command)))
        bot.add_handler(MessageHandler(filters.ALL, check_allowed_chat()(handle_message)))
        bot.run_polling()
    except KeyboardInterrupt:
        logging.info("Bot stopped by user")
    except Exception as e:
        logging.error(f"Bot stopped due to error: {e}")
