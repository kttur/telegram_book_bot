"""Keep callback loading visible, then report long operations in a reply."""

import asyncio
import json
import logging
import math
import os
from urllib.parse import parse_qs, unquote, urlsplit

from telegram.error import TelegramError

from catalogs import CatalogUnavailableError


logger = logging.getLogger(__name__)


def status_delay_from_env():
    delay = float(os.getenv("BOT_STATUS_DELAY", "8"))
    if not math.isfinite(delay) or not 0 < delay <= 10:
        raise ValueError("BOT_STATUS_DELAY must be between 0 (exclusive) and 10 seconds")
    return delay


def describe_action(action, query=None):
    if action is None:
        return "Открытие раздела"
    value = action.value or ""
    if action.action_type in {"search_books", "search_authors"}:
        kind = "книг" if action.action_type == "search_books" else "авторов"
        return f"Поиск {kind}: «{unquote(value)}»"
    if action.action_type == "download":
        return f"Загрузка книги: «{value or 'book'}»"
    if action.action_type == "suggest_similar_books":
        return f"Рекомендации для книги: «{json.loads(value).get('book_name', '')}»"
    label = getattr(action, "label", None)
    if action.action_type == "page":
        params = parse_qs(urlsplit(action.url).query)
        if not label and "searchTerm" in params:
            label = f"Поиск: «{params['searchTerm'][0].strip(chr(34))}»"
        return f"{label or 'Просмотр каталога'} · страница {int(value) + 1}"
    if not label and query and query.message and query.message.reply_markup:
        for row in query.message.reply_markup.inline_keyboard:
            for button in row:
                if str(button.callback_data) == str(query.data):
                    label = button.text
    return f"Открытие: «{label or 'раздел каталога'}»"


class OperationProgress:
    def __init__(self, update, bot, label, *, delay=8, edit_interval=1):
        self.update = update
        self.bot = bot
        # Leave room for the status within Telegram's message length limit.
        self.label = label[:1000]
        self.delay = delay
        self.edit_interval = edit_interval
        self.message = None
        self._status = "Обработка запроса…"
        self._last_text = None
        self._finished = asyncio.Event()
        self._changed = asyncio.Event()
        self._ack_lock = asyncio.Lock()
        self._acknowledged = False
        self._closed = False

    async def __aenter__(self):
        self._loop = asyncio.get_running_loop()
        self._monitor = asyncio.create_task(self._watch())
        return self

    async def _acknowledge(self):
        query = self.update.callback_query
        if not query:
            return
        async with self._ack_lock:
            if self._acknowledged:
                return
            self._acknowledged = True
            try:
                await query.answer()
            except TelegramError:
                # An expired callback must not interrupt the actual operation.
                logger.warning("Could not acknowledge callback", exc_info=True)

    def set_stage(self, text):
        if not self._closed:
            self._status = text
            self._changed.set()

    def on_catalog_progress(self, progress):
        # Catalog requests run in a worker thread; Telegram calls stay on the loop.
        server = (f"сервером {progress.server_number} из {progress.server_count}"
                  if progress.server_number is not None else "внешним сервером")
        tor = " через Tor" if progress.via_tor else ""
        text = f"Соединение с {server}{tor}…\nЭтап: {progress.stage}"
        self._loop.call_soon_threadsafe(self.set_stage, text)

    async def run(self, function, *args, **kwargs):
        return await asyncio.to_thread(function, *args, on_progress=self.on_catalog_progress, **kwargs)

    async def _publish(self, status, *, create=True):
        text = f"{self.label}\n{status}"
        if text == self._last_text:
            return
        try:
            if self.message:
                await self.bot.edit_message_text(
                    chat_id=self.update.effective_chat.id,
                    message_id=self.message.message_id, text=text, parse_mode=None,
                )
            elif create:
                source = self.update.effective_message
                self.message = await self.bot.send_message(
                    chat_id=self.update.effective_chat.id, text=text, parse_mode=None,
                    reply_to_message_id=source.message_id if source else None,
                    allow_sending_without_reply=True,
                )
            self._last_text = text
        except TelegramError:
            logger.warning("Could not publish operation status", exc_info=True)

    async def _watch(self):
        try:
            await asyncio.wait_for(self._finished.wait(), timeout=self.delay)
            return
        except asyncio.TimeoutError:
            pass
        await self._acknowledge()
        if self._finished.is_set():
            return
        self._changed.clear()
        await self._publish(self._status)
        while not self._finished.is_set():
            await self._changed.wait()
            self._changed.clear()
            # Coalesce rapid cookie/redirect/mirror transitions to avoid flooding.
            try:
                await asyncio.wait_for(self._finished.wait(), timeout=self.edit_interval)
                return
            except asyncio.TimeoutError:
                pass
            await self._publish(self._status)

    async def __aexit__(self, exc_type, exc, traceback):
        self._closed = True
        self._finished.set()
        self._changed.set()
        await self._monitor
        await self._acknowledge()
        if isinstance(exc, CatalogUnavailableError):
            await self._publish("Каталоги сейчас недоступны. Попробуйте ещё раз немного позже.")
            return True
        if isinstance(exc, asyncio.CancelledError):
            final_status = "Операция отменена."
        elif exc is not None:
            final_status = "Не удалось выполнить операцию. Попробуйте ещё раз."
        else:
            final_status = "Готово."
        await self._publish(final_status, create=exc is not None)
        return False
