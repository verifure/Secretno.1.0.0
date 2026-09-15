"""
Whisper Bot — эфемерные сообщения в Telegram.

Один файл, без пакетов и относительных импортов — специально, чтобы структура
репозитория никогда не могла сломать деплой (`python main.py` работает всегда,
из любой директории).

Идея: @botname Привет @username — во всплывающем результате inline-режима
появляется кнопка "Отправить шёпот". После отправки в чат приходит карточка с
кнопкой "Прочитать 🔓". Текст видит только тот, кто нажал кнопку — Telegram
показывает его во всплывающем окне (answerCallbackQuery(show_alert=True)),
которое видно только нажавшему. Бота не нужно добавлять в чат — inline-режим
работает из любого чата, группы или канала.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("whisper-bot")


# ---------------------------------------------------------------------------
# Конфигурация — читается из переменных окружения (Railway Variables)
# ---------------------------------------------------------------------------


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip())
    except ValueError:
        return default


TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. Получите токен у @BotFather "
        "и добавьте переменную BOT_TOKEN в Railway -> Variables."
    )

REDIS_URL = os.getenv("REDIS_URL") or os.getenv("REDIS_PRIVATE_URL") or None
WHISPER_TTL = _int("WHISPER_TTL", 7 * 24 * 3600)          # время жизни шёпота, сек
BURN_AFTER_READING = _bool("BURN_AFTER_READING", False)    # удалять после прочтения
NOTIFY_SENDER = _bool("NOTIFY_SENDER", True)                # писать автору о прочтении
MAX_TEXT_LEN = _int("MAX_TEXT_LEN", 200)                    # лимит всплывающего окна Telegram

_public_url = (
    os.getenv("PUBLIC_URL")
    or os.getenv("WEBHOOK_URL")
    or (f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}" if os.getenv("RAILWAY_PUBLIC_DOMAIN") else None)
)
PUBLIC_URL = _public_url.rstrip("/") if _public_url else None
USE_WEBHOOK = _bool("USE_WEBHOOK", False) and bool(PUBLIC_URL)
PORT = _int("PORT", 8080)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or TOKEN.split(":")[-1][:32]


# ---------------------------------------------------------------------------
# Хранилище: память процесса по умолчанию, Redis если задан REDIS_URL
# ---------------------------------------------------------------------------


@dataclass
class Whisper:
    sender_id: int
    sender_name: str
    sender_username: Optional[str]
    target: str  # username получателя, нижний регистр, без @
    text: str
    created_at: float

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def from_json(raw: str) -> "Whisper":
        return Whisper(**json.loads(raw))


class MemoryStore:
    """Словарь с TTL. Данные теряются при рестарте контейнера."""

    def __init__(self, max_items: int = 50_000) -> None:
        self._data: dict[str, tuple[float, Whisper]] = {}
        self._max_items = max_items

    def _purge(self) -> None:
        now = time.time()
        for k in [k for k, (exp, _) in self._data.items() if exp <= now]:
            self._data.pop(k, None)
        if len(self._data) > self._max_items:
            overflow = len(self._data) - self._max_items
            for k, _ in sorted(self._data.items(), key=lambda kv: kv[1][0])[:overflow]:
                self._data.pop(k, None)

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def put(self, key: str, whisper: Whisper, ttl: int) -> None:
        self._purge()
        self._data[key] = (time.time() + ttl, whisper)

    async def get(self, key: str) -> Optional[Whisper]:
        item = self._data.get(key)
        if not item:
            return None
        expires_at, whisper = item
        if expires_at <= time.time():
            self._data.pop(key, None)
            return None
        return whisper

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)


class RedisStore:
    """Хранилище в Redis — переживает рестарты и редеплои."""

    def __init__(self, url: str, prefix: str = "whisper:") -> None:
        self._url = url
        self._prefix = prefix
        self._redis = None

    async def start(self) -> None:
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(self._url, decode_responses=True)
        await self._redis.ping()

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()

    async def put(self, key: str, whisper: Whisper, ttl: int) -> None:
        await self._redis.set(self._prefix + key, whisper.to_json(), ex=ttl)

    async def get(self, key: str) -> Optional[Whisper]:
        raw = await self._redis.get(self._prefix + key)
        return Whisper.from_json(raw) if raw else None

    async def delete(self, key: str) -> None:
        await self._redis.delete(self._prefix + key)


store = RedisStore(REDIS_URL) if REDIS_URL else MemoryStore()


# ---------------------------------------------------------------------------
# Разбор запроса: получатель + текст
# ---------------------------------------------------------------------------

# Telegram username: латиница, цифры, подчёркивание, длина 5-32.
MENTION_RE = re.compile(r"@([A-Za-z0-9_]{5,32})\b")

CB_PREFIX = "w:"

HELP_TEXT = (
    "🤫 <b>Шёпот — эфемерные сообщения</b>\n\n"
    "Меня <b>не нужно добавлять в чат</b>. Просто напишите в поле ввода "
    "любого чата:\n\n"
    "<code>@{username} Привет @verifure</code>\n\n"
    "Сверху появится кнопка «Отправить шёпот». Нажмите — в чат уйдёт сообщение "
    "с кнопкой «Прочитать 🔓», и текст увидит <b>только</b> указанный "
    "получатель. Остальные получат отказ.\n\n"
    "<b>Правила:</b>\n"
    "• получатель — <b>последний</b> @username в запросе;\n"
    "• текст — всё остальное;\n"
    "• длина текста — до {limit} символов (лимит всплывающего окна Telegram);\n"
    "• у получателя должен быть публичный @username;\n"
    "• автор всегда может перечитать свой шёпот."
)


def parse_query(query: str) -> tuple[Optional[str], str]:
    """Возвращает (получатель без @, текст шёпота)."""
    query = (query or "").strip()
    matches = list(MENTION_RE.finditer(query))
    if not matches:
        return None, query
    last = matches[-1]
    target = last.group(1)
    text = (query[: last.start()] + " " + query[last.end():]).strip()
    text = re.sub(r"\s+", " ", text)
    return target.lower(), text


def make_key(user_id: int, target: str, text: str) -> str:
    """Короткий детерминированный ключ: одинаковый запрос не плодит записи."""
    raw = f"{user_id}|{target}|{text}".encode("utf-8")
    return hashlib.blake2s(raw, digest_size=8).hexdigest()  # 16 символов


def article(result_id: str, title: str, description: str, message: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=result_id,
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(
            message, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        ),
    )


# ---------------------------------------------------------------------------
# Хендлеры
# ---------------------------------------------------------------------------


async def on_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    iq = update.inline_query
    user = iq.from_user
    target, text = parse_query(iq.query)
    me = context.bot.username
    hint_button = InlineQueryResultsButton(text="Как пользоваться?", start_parameter="help")

    results: list[InlineQueryResultArticle] = []

    if not iq.query.strip():
        results.append(
            article(
                "empty",
                "Напишите текст и @username получателя",
                "Например: Привет @verifure",
                f"🤫 Отправляйте тайные сообщения через @{me}",
            )
        )
    elif target is None:
        results.append(
            article(
                "no-target",
                "Укажите получателя через @username",
                f"Например: {iq.query.strip()} @verifure",
                f"🤫 Отправляйте тайные сообщения через @{me}",
            )
        )
    elif not text:
        results.append(
            article(
                "no-text",
                "Добавьте текст шёпота",
                f"Например: Привет @{target}",
                f"🤫 Отправляйте тайные сообщения через @{me}",
            )
        )
    elif len(text) > MAX_TEXT_LEN:
        results.append(
            article(
                "too-long",
                f"Слишком длинно: {len(text)} из {MAX_TEXT_LEN}",
                "Telegram показывает во всплывающем окне не более 200 символов",
                f"🤫 Отправляйте тайные сообщения через @{me}",
            )
        )
    else:
        key = make_key(user.id, target, text)
        await store.put(
            key,
            Whisper(
                sender_id=user.id,
                sender_name=user.full_name,
                sender_username=(user.username or None),
                target=target,
                text=text,
                created_at=time.time(),
            ),
            ttl=WHISPER_TTL,
        )

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Прочитать 🔓", callback_data=f"{CB_PREFIX}{key}")]]
        )
        body = (
            f"🤫 <b>Шёпот для @{html.escape(target)}</b>\n"
            f"<i>Сообщение видно только получателю.</i>"
        )
        results.append(
            InlineQueryResultArticle(
                id=key,
                title=f"Отправить шёпот для @{target}",
                description=text,
                input_message_content=InputTextMessageContent(
                    body, parse_mode=ParseMode.HTML, disable_web_page_preview=True
                ),
                reply_markup=keyboard,
            )
        )

    # cache_time=0 + is_personal=True — чтобы Telegram не кэшировал чужие результаты.
    await iq.answer(results, cache_time=0, is_personal=True, button=hint_button)


async def on_read(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cq = update.callback_query
    key = cq.data[len(CB_PREFIX):]
    whisper = await store.get(key)

    if whisper is None:
        await cq.answer("🕳 Шёпот истёк или уже сожжён.", show_alert=True)
        return

    user = cq.from_user
    username = (user.username or "").lower()
    is_target = bool(username) and username == whisper.target
    is_sender = user.id == whisper.sender_id

    if not (is_target or is_sender):
        await cq.answer("🤐 Это не тебе. Тут ничего интересного.", show_alert=True)
        return

    if is_sender and not is_target:
        await cq.answer(f"Ваш шёпот для @{whisper.target}:\n\n{whisper.text}", show_alert=True)
        return

    await cq.answer(whisper.text, show_alert=True)

    if NOTIFY_SENDER and whisper.sender_id != user.id:
        try:
            await context.bot.send_message(
                chat_id=whisper.sender_id,
                text=f"👀 @{html.escape(whisper.target)} прочитал(а) ваш шёпот.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass  # автор не начинал диалог с ботом — молча игнорируем

    if BURN_AFTER_READING:
        await store.delete(key)
        if cq.inline_message_id:
            try:
                await context.bot.edit_message_text(
                    inline_message_id=cq.inline_message_id,
                    text=f"🔥 <b>Шёпот для @{html.escape(whisper.target)} прочитан и сожжён.</b>",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError as exc:
                log.debug("Не удалось отредактировать inline-сообщение: %s", exc)


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    me = context.bot.username
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Попробовать в чате →", switch_inline_query="Привет ")]]
    )
    await update.effective_message.reply_text(
        HELP_TEXT.format(username=me, limit=MAX_TEXT_LEN),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Ошибка при обработке апдейта", exc_info=context.error)


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------


async def _post_init(app: Application) -> None:
    await store.start()
    from telegram import BotCommand

    await app.bot.set_my_commands(
        [
            BotCommand("start", "Как пользоваться ботом"),
            BotCommand("help", "Как пользоваться ботом"),
        ]
    )
    me = await app.bot.get_me()
    log.info("Бот @%s запущен. Inline-режим должен быть включён в @BotFather.", me.username)


async def _post_shutdown(app: Application) -> None:
    await store.close()


def build_application() -> Application:
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .concurrent_updates(True)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], on_start))
    app.add_handler(InlineQueryHandler(on_inline_query))
    app.add_handler(CallbackQueryHandler(on_read, pattern=rf"^{CB_PREFIX}"))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    app = build_application()

    log.info(
        "Хранилище: %s | TTL: %s c | burn: %s",
        "Redis" if REDIS_URL else "память процесса",
        WHISPER_TTL,
        BURN_AFTER_READING,
    )

    allowed = ["inline_query", "callback_query", "message"]

    if USE_WEBHOOK:
        url = f"{PUBLIC_URL}/telegram/{WEBHOOK_SECRET}"
        log.info("Режим webhook: %s (порт %s)", url, PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=f"telegram/{WEBHOOK_SECRET}",
            webhook_url=url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=allowed,
            drop_pending_updates=True,
        )
    else:
        log.info("Режим long polling")
        app.run_polling(allowed_updates=allowed, drop_pending_updates=True)


if __name__ == "__main__":
    main()
