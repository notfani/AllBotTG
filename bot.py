"""
Telegram-бот: @all тегает участников чата.
Вызывать могут только владелец группы и администраторы.

Бот запоминает людей отдельно по каждому чату:
- кто написал сообщение;
- кто зашёл (chat_member и служебное new_chat_members).
Вышедших и кикнутых удаляет из базы.

Уже сидевших в группе до добавления бота Telegram не отдаёт.
"""

from __future__ import annotations

import html
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

import telebot
from dotenv import load_dotenv
from telebot.apihelper import ApiTelegramException

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = Path(os.getenv("DB_PATH", "users.db"))
MENTIONS_PER_MESSAGE = int(os.getenv("MENTIONS_PER_MESSAGE", "25"))
CHUNK_DELAY = float(os.getenv("CHUNK_DELAY", "0.4"))

ALL_RE = re.compile(r"(^|\s)@all\b", re.IGNORECASE)
IN_CHAT = {"creator", "administrator", "member", "restricted"}
GONE = {"left", "kicked"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("all-bot")

if not BOT_TOKEN:
    raise SystemExit("Не задан BOT_TOKEN. Скопируйте .env.example в .env и впишите токен.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
BOT_ID: int | None = None


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS members (
                chat_id    INTEGER NOT NULL,
                user_id    INTEGER NOT NULL,
                first_name TEXT    NOT NULL,
                last_name  TEXT,
                username   TEXT,
                is_bot     INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_members_chat ON members(chat_id)"
        )


def display_name(first_name: str, last_name: str | None, username: str | None) -> str:
    name = (first_name or "").strip()
    if last_name:
        name = f"{name} {last_name}".strip()
    if not name:
        name = f"@{username}" if username else "user"
    return name


def save_member(chat_id: int, user: telebot.types.User) -> None:
    if user is None or user.id == BOT_ID:
        return
    with db() as conn:
        conn.execute(
            """
            INSERT INTO members (chat_id, user_id, first_name, last_name, username, is_bot, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                first_name = excluded.first_name,
                last_name  = excluded.last_name,
                username   = excluded.username,
                is_bot     = excluded.is_bot,
                updated_at = excluded.updated_at
            """,
            (
                chat_id,
                user.id,
                user.first_name or "",
                user.last_name,
                user.username,
                1 if user.is_bot else 0,
                int(time.time()),
            ),
        )


def forget_member(chat_id: int, user_id: int) -> None:
    with db() as conn:
        conn.execute(
            "DELETE FROM members WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )


def remember_from_message(message: telebot.types.Message) -> None:
    if message.from_user is None or message.chat.type not in ("group", "supergroup"):
        return
    save_member(message.chat.id, message.from_user)


def list_members(chat_id: int, exclude_ids: set[int]) -> list[sqlite3.Row]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT user_id, first_name, last_name, username
            FROM members
            WHERE chat_id = ? AND is_bot = 0
            ORDER BY updated_at DESC
            """,
            (chat_id,),
        ).fetchall()
    return [row for row in rows if row["user_id"] not in exclude_ids]


def is_admin_or_owner(chat_id: int, user_id: int) -> bool:
    try:
        member = bot.get_chat_member(chat_id, user_id)
    except ApiTelegramException as exc:
        log.warning("get_chat_member failed: %s", exc)
        return False
    return member.status in ("administrator", "creator")


def mention(user_id: int, first_name: str, last_name: str | None, username: str | None) -> str:
    name = html.escape(display_name(first_name, last_name, username))
    return f'<a href="tg://user?id={user_id}">{name}</a>'


def chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def send_mentions(chat_id: int, caller_name: str, members: list[sqlite3.Row]) -> None:
    total = len(members)
    pages = max(1, (total + MENTIONS_PER_MESSAGE - 1) // MENTIONS_PER_MESSAGE)
    bot.send_message(
        chat_id,
        f"📣 {html.escape(caller_name)} вызывает всех ({total}):",
    )

    page = 0
    for batch in chunks(members, MENTIONS_PER_MESSAGE):
        page += 1
        parts: list[list[str]] = [[]]
        acc_len = 0
        for row in batch:
            piece = mention(row["user_id"], row["first_name"], row["last_name"], row["username"])
            if parts[-1] and acc_len + len(piece) + 1 > 3800:
                parts.append([piece])
                acc_len = len(piece)
            else:
                parts[-1].append(piece)
                acc_len += len(piece) + 1
        for part in parts:
            body = " ".join(part)
            bot.send_message(
                chat_id,
                f"<i>{page}/{pages}</i>\n{body}",
                disable_web_page_preview=True,
            )
            time.sleep(CHUNK_DELAY)


@bot.message_handler(commands=["start", "help"])
def on_help(message: telebot.types.Message) -> None:
    if message.chat.type == "private":
        bot.reply_to(
            message,
            "Добавьте меня в группу и выдайте права администратора.\n\n"
            "В группе админ или владелец пишет <code>@all</code> — "
            "я отмечу тех, кто писал или зашёл после моего добавления.\n\n"
            "Обычные участники вызвать всех не могут.\n"
            "В @BotFather выключите Privacy Mode, иначе я не увижу обычные сообщения.",
        )
        return
    remember_from_message(message)
    bot.reply_to(
        message,
        "Админ или владелец может написать <code>@all</code>, чтобы отметить "
        "запомненных участников этого чата.\n"
        "Команда <code>/count</code> — сколько людей я уже запомнил.",
    )


@bot.message_handler(commands=["count"])
def on_count(message: telebot.types.Message) -> None:
    if message.chat.type not in ("group", "supergroup"):
        return
    remember_from_message(message)
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM members WHERE chat_id = ? AND is_bot = 0",
            (message.chat.id,),
        ).fetchone()
    bot.reply_to(message, f"Запомнил участников: <b>{row['n']}</b>")


@bot.message_handler(commands=["all"])
def on_all_command(message: telebot.types.Message) -> None:
    handle_all(message)


@bot.message_handler(
    func=lambda m: bool(m.text) and bool(ALL_RE.search(m.text)),
    content_types=["text"],
)
def on_all_text(message: telebot.types.Message) -> None:
    handle_all(message)


def handle_all(message: telebot.types.Message) -> None:
    if message.chat.type not in ("group", "supergroup") or message.from_user is None:
        return
    remember_from_message(message)

    if not is_admin_or_owner(message.chat.id, message.from_user.id):
        bot.reply_to(
            message,
            "Тегать всех могут только владелец группы и администраторы.",
        )
        return

    members = list_members(
        message.chat.id,
        exclude_ids={message.from_user.id, BOT_ID or 0},
    )
    if not members:
        bot.reply_to(
            message,
            "Пока некого звать. Я вижу только тех, кто писал или зашёл после моего добавления.",
        )
        return

    caller = display_name(
        message.from_user.first_name,
        message.from_user.last_name,
        message.from_user.username,
    )
    try:
        send_mentions(message.chat.id, caller, members)
    except ApiTelegramException as exc:
        log.exception("Не удалось отправить упоминания")
        bot.reply_to(
            message,
            f"Не получилось отправить упоминания: <code>{html.escape(str(exc))}</code>",
        )


@bot.message_handler(content_types=["new_chat_members"])
def on_service_join(message: telebot.types.Message) -> None:
    if message.chat.type not in ("group", "supergroup"):
        return
    for user in message.new_chat_members or []:
        if user.is_bot and user.id == BOT_ID:
            log.info("Бота добавили в чат %s", message.chat.id)
            continue
        save_member(message.chat.id, user)
        log.info("Вход (service) chat=%s user=%s", message.chat.id, user.id)
    if message.from_user:
        save_member(message.chat.id, message.from_user)


@bot.message_handler(content_types=["left_chat_member"])
def on_service_leave(message: telebot.types.Message) -> None:
    user = message.left_chat_member
    if user is None:
        return
    forget_member(message.chat.id, user.id)
    log.info("Выход (service) chat=%s user=%s", message.chat.id, user.id)


@bot.message_handler(
    content_types=["text", "photo", "video", "document", "sticker", "voice", "audio", "animation"]
)
def on_any(message: telebot.types.Message) -> None:
    remember_from_message(message)


@bot.chat_member_handler()
def on_chat_member(update: telebot.types.ChatMemberUpdated) -> None:
    if update.chat.type not in ("group", "supergroup"):
        return
    user = update.new_chat_member.user
    status = update.new_chat_member.status
    if status in IN_CHAT:
        save_member(update.chat.id, user)
        log.info("В чате chat=%s user=%s status=%s", update.chat.id, user.id, status)
    elif status in GONE:
        forget_member(update.chat.id, user.id)
        log.info("Покинул chat=%s user=%s status=%s", update.chat.id, user.id, status)


@bot.my_chat_member_handler()
def on_my_status(update: telebot.types.ChatMemberUpdated) -> None:
    log.info(
        "Статус бота в чате %s: %s -> %s",
        update.chat.id,
        update.old_chat_member.status,
        update.new_chat_member.status,
    )


def main() -> None:
    global BOT_ID
    init_db()
    me = bot.get_me()
    BOT_ID = me.id
    log.info("Бот запущен: @%s, база %s", me.username, DB_PATH)
    bot.infinity_polling(
        skip_pending=True,
        allowed_updates=["message", "chat_member", "my_chat_member"],
    )


if __name__ == "__main__":
    main()
